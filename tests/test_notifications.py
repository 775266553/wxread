import copy
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import requests

from account_store import AccountStore
from jobs import JobManager
from notifications import (notification_settings, prepare_notification, public_notifications,
                           send_channel, send_notifications, webhook_request)
from push import email_settings, push


class NotificationCase(unittest.TestCase):
    def test_legacy_secrets_and_multiple_methods_remain_supported(self):
        with patch.dict(os.environ, {'PUSH_METHOD':'email,pushplus,webhook','PUSHPLUS_TOKEN':'LEGACY-TOKEN',
                                     'WEBHOOK_URL':'https://hooks.example.com/secret','WEBHOOK_TYPE':'feishu'}):
            settings = notification_settings()
            self.assertTrue(settings['pushplus']['enabled'])
            self.assertTrue(settings['webhook']['enabled'])
            self.assertTrue(email_settings()['enabled'])
            self.assertFalse(settings['telegram']['enabled'])
            self.assertEqual(settings['pushplus']['token'], 'LEGACY-TOKEN')
            self.assertEqual(settings['webhook']['format'], 'feishu')

    def test_blank_credentials_are_preserved_and_can_be_cleared(self):
        current = {'enabled':True,'format':'feishu','url':'https://hooks.example.com/PRIVATE-URL',
                   'secret':'PRIVATE-SIGNATURE','headers':{'Authorization':'Bearer PRIVATE-HEADER'}}
        value = prepare_notification('webhook', {'url':'','secret':'','headers':''}, current)
        self.assertEqual(value, current)
        self.assertIsNot(value['headers'], current['headers'])
        cleared = prepare_notification('webhook', {'clear_secret':True,'clear_headers':True}, current)
        self.assertEqual(cleared['secret'], '')
        self.assertEqual(cleared['headers'], {})
        self.assertEqual(current['secret'], 'PRIVATE-SIGNATURE')
        public = public_notifications({'webhook':current})
        self.assertTrue(public['webhook']['has_url'])
        for value in ['PRIVATE-URL','PRIVATE-SIGNATURE','PRIVATE-HEADER']:
            self.assertNotIn(value, json.dumps(public))

    def test_invalid_webhook_and_headers_are_rejected(self):
        for url in ['http://example.com','https://user:password@example.com','https://example.com/#fragment',
                    'https://example.com:bad','file:///tmp/file','https://example.com/\nheader']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                prepare_notification('webhook', {'enabled':True,'url':url})
        for headers in ['invalid', '[]', '{"Host":"other.example"}', '{"X-Token":"line1\\nline2"}']:
            with self.subTest(headers=headers), self.assertRaises(ValueError):
                prepare_notification('webhook', {'headers':headers})
        with self.assertRaises(ValueError):
            prepare_notification('webhook', {'enabled':'false'})

    def test_robot_payloads_and_signatures_use_their_platform_protocol(self):
        base = {'url':'https://hooks.example.com/send?access_token=URL-TOKEN','secret':'SEC_test_signing_key','headers':{}}
        with patch('notifications.time.time', return_value=1700000000):
            url, payload, _ = webhook_request({**base,'format':'feishu'}, '阅读结果')
            self.assertEqual(url, base['url'])
            self.assertEqual(payload['msg_type'], 'text')
            self.assertEqual(payload['timestamp'], '1700000000')
            self.assertEqual(payload['sign'], 'tk3jgbMwojLLZqtyPDNlnzta8XuESb5NJfQUaFQo47M=')
            url, payload, _ = webhook_request({**base,'format':'dingtalk'}, '阅读结果')
            query = parse_qs(urlsplit(url).query)
            self.assertEqual(query['access_token'], ['URL-TOKEN'])
            self.assertEqual(query['timestamp'], ['1700000000000'])
            self.assertEqual(query['sign'], ['EM5u8jWpOyzte4iF95VrFdaEC2QpEO++pdkzJB0FN08='])
            self.assertEqual(payload['msgtype'], 'text')
            _, payload, _ = webhook_request({**base,'format':'wecom'}, '中文'*1500)
            self.assertLessEqual(len(payload['text']['content'].encode()), 2048)
            self.assertNotIn('sign', payload)
        _, payload, headers = webhook_request({**base,'format':'json','headers':{'X-Test':'abc'}}, '阅读结果',
                                              context={'account_id':'a','completed':4,'cookies':'PRIVATE-COOKIE'})
        self.assertEqual(payload['event'], 'wxread.reading.completed')
        self.assertEqual(payload['data'], {'account_id':'a','completed':4})
        self.assertNotIn('PRIVATE-COOKIE', str(payload))
        self.assertEqual(headers, {'X-Test':'abc'})
        _, test_payload, _ = webhook_request({**base,'format':'json'}, '测试', test=True)
        self.assertEqual(test_payload['event'], 'wxread.test')

    def test_successful_http_status_does_not_hide_provider_rejection(self):
        cases = [('pushplus',{'token':'abc'},{'code':401}),
                 ('wxpusher',{'spt':'SPT_abc'},{'code':1000,'data':[{'code':1001}]}),
                 ('telegram',{'bot_token':'123:abc','chat_id':'456'},{'ok':False}),
                 ('serverchan',{'send_key':'SCT_abc'},{'code':400}),
                 ('webhook',{'url':'https://hooks.example.com','format':'wecom'},{'errcode':93000}),
                 ('webhook',{'url':'https://hooks.example.com','format':'feishu'},{'code':19021}),
                 ('webhook',{'url':'https://hooks.example.com','format':'dingtalk'},{'errcode':310000})]
        for channel, settings, reply in cases:
            with self.subTest(channel=channel,reply=reply), patch('notifications.requests.post') as post:
                post.return_value.status_code = 200
                post.return_value.json.return_value = reply
                with self.assertRaises(RuntimeError):
                    send_channel(channel, settings, '测试消息')

    def test_old_channels_use_supported_payloads_and_validate_success(self):
        cases = [('pushplus',{'token':'token'},{'code':200}),
                 ('wxpusher',{'spt':'SPT_abc'},{'code':1000,'data':[{'code':1000}]}),
                 ('telegram',{'bot_token':'123:abc','chat_id':'456'},{'ok':True}),
                 ('serverchan',{'send_key':'SCTabc'},{'code':0}),
                 ('webhook',{'url':'https://hooks.example.com','format':'feishu'},{'code':0})]
        for channel, settings, reply in cases:
            with self.subTest(channel=channel), patch('notifications.requests.post') as post:
                post.return_value.status_code = 200
                post.return_value.json.return_value = reply
                self.assertTrue(send_channel(channel, settings, '包含中文和换行\n第二行'))
                self.assertFalse(post.call_args.kwargs['allow_redirects'])
                self.assertEqual(post.call_args.kwargs['timeout'], (5,15))
                if channel=='wxpusher':
                    self.assertTrue(post.call_args.args[0].endswith('/simple-push'))
                    self.assertEqual(post.call_args.kwargs['json']['spt'], 'SPT_abc')

    def test_all_enabled_channels_are_attempted_despite_an_email_or_webhook_failure(self):
        channels = {channel:{'enabled':True} for channel in notification_settings()}
        def deliver(channel, *_args, **_kwargs):
            if channel=='webhook':
                raise RuntimeError('PRIVATE-ENDPOINT-ERROR')
            return True
        with patch('notifications.send_email', side_effect=RuntimeError('PRIVATE-SMTP-ERROR')) as email, \
             patch('notifications.send_channel', side_effect=deliver) as channel, self.assertLogs(level='ERROR') as logs:
            result = send_notifications('结果', email_config={'enabled':True}, channels=channels, recipient='account@example.com')
        self.assertEqual(result['succeeded'], ['pushplus','wxpusher','telegram','serverchan'])
        self.assertEqual(result['failed'], ['email','webhook'])
        self.assertEqual(channel.call_count, 5)
        self.assertEqual(email.call_args.kwargs['recipient'], 'account@example.com')
        self.assertNotIn('PRIVATE-', '\n'.join(logs.output))

    def test_network_error_does_not_expose_url_or_tokens(self):
        with patch('notifications.requests.post', side_effect=requests.ConnectionError('https://example.com/PRIVATE-TOKEN')):
            with self.assertRaises(RuntimeError) as error:
                send_channel('webhook', {'url':'https://example.com/PRIVATE-TOKEN','format':'json'}, 'test')
        self.assertNotIn('PRIVATE-TOKEN', str(error.exception))

    def test_legacy_push_adapter_supports_multiple_channels(self):
        with patch('notifications.send_notifications', return_value={'succeeded':['email','pushplus'],'failed':[]}) as send:
            self.assertTrue(push('结果', 'email,pushplus'))
        self.assertTrue(send.call_args.kwargs['email_config']['enabled'])
        self.assertTrue(send.call_args.kwargs['channels']['pushplus']['enabled'])

    def test_reading_records_channel_failures_without_changing_success(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AccountStore(directory)
            account = store.bind({'wr_vid':'1','wr_skey':'test'}, {}, name='测试', read_num=2, email='account@example.com')
            client = Mock()
            client.run.return_value = 2
            jobs = JobManager(store, client_factory=Mock(return_value=client))
            run_id = store.new_run(store.account(account['id'], private=True))
            try:
                with patch('jobs.send_notifications', return_value={'succeeded':['email'],'failed':['webhook']}) as send:
                    jobs._execute(store.account(account['id'], private=True), run_id, threading.Event())
                run = store.runs()[0]
                self.assertEqual(run['status'], 'success')
                self.assertIn('通知未送达：Webhook', run['message'])
                self.assertEqual(send.call_args.kwargs['recipient'], 'account@example.com')
                self.assertEqual(send.call_args.kwargs['context']['completed'], 2)
            finally:
                jobs.shutdown()


if __name__=='__main__':
    unittest.main()
