import copy
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import requests

from account_store import AccountStore
from config import convert, data as template
from jobs import JobManager
from push import send_email
from reader import AuthenticationError, ReadClient
from web import create_app


class BaseStoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AccountStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def bind(self, vid='1', name='我的账户'):
        return self.store.bind({'wr_vid': vid, 'wr_skey': 'SECRET-COOKIE-' + vid}, {'user-agent': 'test'}, name, 2)


class StoreCase(BaseStoreCase):
    def test_curl_parses_quotes_and_cookie_values_without_executing(self):
        h, c = convert('curl "https://weread.qq.com/web/book/read" \\\n -H "Cookie: wr_vid=1;wr_skey=abc=xyz" -H \'User-Agent: Example\'')
        self.assertEqual(c, {'wr_vid': '1', 'wr_skey': 'abc=xyz'})
        self.assertEqual(h, {'user-agent': 'Example'})

    def test_accounts_are_separate_encrypted_and_survive_restart(self):
        a, b = self.bind('1'), self.bind('2', '家人')
        self.store.save_cookies(a['id'], {'wr_vid': '1', 'wr_skey': 'NEW-SECRET'})
        reopened = AccountStore(self.tmp.name)
        self.assertEqual(reopened.account(a['id'], True)['cookies']['wr_skey'], 'NEW-SECRET')
        self.assertEqual(reopened.account(b['id'], True)['cookies']['wr_skey'], 'SECRET-COOKIE-2')
        self.assertNotIn('credentials', reopened.accounts()[0])
        raw = self.store.path.read_bytes()
        self.assertNotIn(b'SECRET-COOKIE', raw)
        self.assertNotIn(b'NEW-SECRET', raw)

    def test_rebind_preserves_settings_and_rejects_wrong_identity(self):
        a = self.bind()
        self.store.update(a['id'], {'name': '自定义', 'read_num': 10, 'email': 'reader@example.com', 'enabled': False})
        again = self.store.bind({'wr_vid':'1', 'wr_skey':'new'}, account_id=a['id'])
        self.assertEqual(again['id'], a['id'])
        self.assertEqual((again['name'], again['read_num'], again['email'], again['enabled']), ('自定义',10,'reader@example.com',False))
        with self.assertRaises(ValueError):
            self.store.bind({'wr_vid':'2','wr_skey':'new'}, account_id=a['id'])
        self.assertEqual(len(self.store.accounts()), 1)

    def test_duplicate_jobs_cannot_start_even_from_different_store(self):
        account = self.bind()
        run = self.store.new_run(account)
        with self.assertRaises(ValueError):
            AccountStore(self.tmp.name).new_run(account)
        self.store.update_run(run, status='success')
        self.store.new_run(account)

    def test_statistics_use_all_runs_and_calendar_timezone(self):
        account = self.bind()
        for _ in range(110):
            run = self.store.new_run(account)
            self.store.update_run(run, status='success', completed=2)
        self.assertEqual(len(self.store.runs()), 100)
        self.assertEqual(self.store.statistics()['today']['minutes'], 110)
        self.assertEqual(self.store.statistics()['today']['success'], 110)
        self.assertEqual(len(self.store.statistics()['days']), 7)

    def test_environment_is_seeded_once(self):
        a = self.bind()
        with patch.dict(os.environ, {'WXREAD_ACCOUNTS_JSON':json.dumps([{'cookies':{'wr_vid':'1','wr_skey':'old'}}])}):
            self.store.seed_from_environment()
        self.assertEqual(self.store.account(a['id'],True)['cookies']['wr_skey'],'SECRET-COOKIE-1')


class ReaderCase(BaseStoreCase):
    def response(self, payload, cookies=None):
        response = Mock(ok=True, status_code=200)
        response.json.return_value = payload
        response.cookies = requests.cookies.cookiejar_from_dict(cookies or {})
        return response

    def test_reader_does_not_mutate_other_accounts_or_template(self):
        a, b = self.bind('1'), self.bind('2')
        original = copy.deepcopy(template)
        session = Mock()
        session.post.side_effect = [self.response({}, {'wr_skey':'refreshed'}), self.response({'succ':1,'synckey':42})]
        client = ReadClient(self.store.account(a['id'],True), lambda c:self.store.save_cookies(a['id'],c), session)
        cancel=Mock();cancel.is_set.return_value=False;cancel.wait.return_value=False
        self.assertEqual(client.run(1,cancel=cancel),1)
        self.assertEqual(session.post.call_args.kwargs['cookies']['wr_skey'],'refreshed')
        self.assertEqual(self.store.account(b['id'],True)['cookies']['wr_skey'],'SECRET-COOKIE-2')
        self.assertEqual(template, original)
        self.assertEqual(self.store.account(a['id'],True)['cookies']['wr_skey'],'refreshed')
        session.close.assert_called_once()

    def test_error_retries_are_bounded(self):
        a=self.bind();session=Mock()
        session.post.side_effect=lambda *args,**kw:self.response({'succ':0,'errCode':123})
        cancel=Mock();cancel.is_set.return_value=False;cancel.wait.return_value=False
        with self.assertRaisesRegex(RuntimeError,'连续失败'):
            ReadClient(self.store.account(a['id'],True),session=session).run(1,cancel=cancel)
        self.assertEqual(sum(call.args[0].endswith('/book/read') for call in session.post.call_args_list),4)

    def test_one_account_failure_does_not_block_other_accounts(self):
        a,b=self.bind('1'),self.bind('2')
        class Client:
            def __init__(self,account,**kwargs):self.account=account
            def run(self,count,progress,cancel):
                if self.account['vid']=='1':raise AuthenticationError('重新扫码')
                progress(count,'完成')
                return count
        jobs=JobManager(self.store,Client)
        result=jobs.run_all();jobs.executor.shutdown(wait=True)
        self.assertEqual(len(result['started']),2)
        statuses={r['account_id']:r['status'] for r in self.store.runs()}
        self.assertEqual(statuses,{a['id']:'failed',b['id']:'success'})
        self.assertEqual(self.store.account(a['id'])['status'],'needs_login')
        self.assertEqual(self.store.account(b['id'])['status'],'ready')

    def test_running_job_can_be_cancelled_and_not_duplicated(self):
        a=self.bind();started=threading.Event()
        from reader import Cancelled
        class Client:
            def __init__(self,*args,**kwargs):pass
            def run(self,count,progress,cancel):
                started.set();cancel.wait(5);raise Cancelled()
        jobs=JobManager(self.store,Client)
        jobs.run_one(a['id']);self.assertTrue(started.wait(2))
        with self.assertRaises(ValueError):jobs.run_one(a['id'])
        jobs.cancel(a['id']);jobs.executor.shutdown(wait=True)
        self.assertEqual(self.store.runs()[0]['status'],'cancelled')


class WebCase(BaseStoreCase):
    def setUp(self):
        super().setUp()
        with patch.dict(os.environ, {'ADMIN_TOKEN':'management-secret'}):
            self.app = create_app(self.store)
        self.client=self.app.test_client()
        self.headers={'X-WXREAD-Request':'1'}

    def tearDown(self):
        self.app.extensions['jobs'].shutdown()
        super().tearDown()

    def login(self):
        response=self.client.post('/api/auth',json={'token':'management-secret'},headers=self.headers)
        self.assertEqual(response.status_code,200)

    def test_authentication_and_csrf_are_required(self):
        self.assertEqual(self.client.get('/api/overview').status_code,401)
        self.assertEqual(self.client.post('/api/auth',json={'token':'management-secret'}).status_code,403)
        self.assertEqual(self.client.post('/api/auth',json={'token':'wrong'},headers=self.headers).status_code,401)
        self.login()
        self.assertEqual(self.client.get('/api/overview').status_code,200)
        self.assertEqual(self.client.post('/api/run-all',json={},headers={**self.headers,'Origin':'https://attacker.example'}).status_code,403)

    def test_import_update_and_validation(self):
        self.login()
        response=self.client.post('/api/accounts/import',json={'curl':"curl https://weread.qq.com -b 'wr_vid=1; wr_skey=secret'",'name':'账户一'},headers=self.headers)
        self.assertEqual(response.status_code,200)
        account=response.get_json()
        for value in (0,-1,2.5,True,2881):
            response=self.client.patch('/api/accounts/'+account['id'],json={'read_num':value},headers=self.headers)
            self.assertEqual(response.status_code,400)
        response=self.client.patch('/api/accounts/'+account['id'],json={'read_num':4,'email':'one@example.com'},headers=self.headers)
        self.assertEqual(response.get_json()['read_num'],4)
        self.assertNotIn('secret',self.client.get('/api/overview').get_data(as_text=True))
        self.assertEqual(self.client.patch('/api/accounts/missing',json={},headers=self.headers).status_code,404)

    def test_email_secret_is_not_exposed_and_blank_keeps_password(self):
        self.login()
        settings={'enabled':True,'host':'smtp.example.com','port':465,'security':'ssl','username':'me@example.com','password':'mail-secret','sender':'me@example.com','recipient':'reader@example.com'}
        self.assertEqual(self.client.put('/api/settings/email',json=settings,headers=self.headers).status_code,200)
        settings['password']=''
        self.client.put('/api/settings/email',json=settings,headers=self.headers)
        self.assertEqual(self.store.settings('email')['password'],'mail-secret')
        result=self.client.get('/api/overview').get_json()['email']
        self.assertTrue(result['has_password']);self.assertNotIn('password',result)
        with patch('web.send_email',return_value=True) as send:
            self.assertEqual(self.client.post('/api/settings/email/test',json=settings,headers=self.headers).status_code,200)
            self.assertEqual(send.call_args.args[1]['password'],'mail-secret')

    def test_invalid_schedule_does_not_replace_saved_settings(self):
        self.login()
        good={'enabled':False,'cron':'30 8 * * *','timezone':'Asia/Shanghai'}
        self.assertEqual(self.client.put('/api/settings/schedule',json=good,headers=self.headers).status_code,200)
        response=self.client.put('/api/settings/schedule',json={**good,'cron':'bad'},headers=self.headers)
        self.assertEqual(response.status_code,400)
        self.assertEqual(self.store.settings('schedule'),good)

    def test_local_mode_rejects_remote_or_rebound_hosts(self):
        with patch.dict(os.environ,{'ADMIN_TOKEN':''}):app=create_app(self.store)
        try:
            c=app.test_client()
            self.assertEqual(c.get('/api/overview',base_url='http://attacker.example').status_code,403)
            self.assertEqual(c.get('/api/overview',environ_base={'REMOTE_ADDR':'192.0.2.1'}).status_code,403)
        finally:app.extensions['jobs'].shutdown()


class EmailCase(unittest.TestCase):
    def test_tls_and_account_recipient_override(self):
        settings={'enabled':True,'host':'smtp.example.com','port':587,'security':'starttls','username':'sender@example.com','password':'secret','sender':'sender@example.com','recipient':'default@example.com'}
        with patch('push.smtplib.SMTP') as cls:
            smtp=cls.return_value.__enter__.return_value
            smtp.send_message.return_value={}
            self.assertTrue(send_email('账户阅读完成',settings,recipient='account@example.com'))
            smtp.starttls.assert_called_once()
            smtp.login.assert_called_once_with('sender@example.com','secret')
            self.assertEqual(smtp.send_message.call_args.args[0]['To'],'account@example.com')
            self.assertEqual(settings['recipient'],'default@example.com')


if __name__=='__main__':unittest.main()
