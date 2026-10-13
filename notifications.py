"""Notification settings and delivery shared by Pages, Actions and the CLI."""
import base64
import copy
import hashlib
import hmac
import json
import logging
import os
import re
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

import config
from account_store import now
from push import email_settings, send_email

CHANNEL_LABELS = {'email':'邮件', 'pushplus':'PushPlus', 'wxpusher':'WxPusher',
                  'telegram':'Telegram', 'serverchan':'Server酱', 'webhook':'Webhook'}
SECRET_FIELDS = {'pushplus':('token',), 'wxpusher':('spt',),
                 'telegram':('bot_token','chat_id'), 'serverchan':('send_key',),
                 'webhook':('url','secret','headers')}
WEBHOOK_FORMATS = ('json', 'wecom', 'feishu', 'dingtalk')


def selected_methods(value=None):
    value = (os.getenv('PUSH_METHOD') or config.PUSH_METHOD or '') if value is None else value
    return set(re.split(r'[,;\s]+', str(value).strip().lower())) - {''}


def notification_settings(method=None):
    selected = selected_methods(method)
    return {
        'pushplus': {'enabled':'pushplus' in selected, 'token':os.getenv('PUSHPLUS_TOKEN') or config.PUSHPLUS_TOKEN or ''},
        'wxpusher': {'enabled':'wxpusher' in selected, 'spt':os.getenv('WXPUSHER_SPT') or config.WXPUSHER_SPT or ''},
        'telegram': {'enabled':'telegram' in selected, 'bot_token':os.getenv('TELEGRAM_BOT_TOKEN') or config.TELEGRAM_BOT_TOKEN or '',
                     'chat_id':os.getenv('TELEGRAM_CHAT_ID') or config.TELEGRAM_CHAT_ID or ''},
        'serverchan': {'enabled':'serverchan' in selected, 'send_key':os.getenv('SERVERCHAN_SPT') or config.SERVERCHAN_SPT or ''},
        'webhook': {'enabled':'webhook' in selected, 'format':(os.getenv('WEBHOOK_TYPE') or 'wecom').strip().lower(),
                    'url':os.getenv('WEBHOOK_URL') or '', 'secret':os.getenv('WEBHOOK_SECRET') or '',
                    'headers':os.getenv('WEBHOOK_HEADERS') or {}},
    }


def webhook_headers(value):
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else {}
        except (TypeError, ValueError):
            raise ValueError('Webhook 请求头必须为 JSON 对象') from None
    if not isinstance(value, dict) or len(value) > 20:
        raise ValueError('Webhook 请求头必须为 JSON 对象，最多 20 项')
    for name, text in value.items():
        if not isinstance(name, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            raise ValueError('Webhook 请求头名称无效')
        if name.lower() in ('host','content-length','connection','transfer-encoding'):
            raise ValueError('Webhook 请求头不能覆盖连接或长度信息')
        if not isinstance(text, str) or len(text) > 4096 or '\r' in text or '\n' in text:
            raise ValueError('Webhook 请求头值必须为不含换行的字符串')
    return dict(value)


def validate_webhook_url(url):
    try:
        parsed = urlsplit(url)
        port = parsed.port
        valid = parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password and not parsed.fragment
        valid = valid and (port is None or 1 <= port <= 65535)
    except ValueError:
        valid = False
    if not valid or len(url) > 4096 or any(c.isspace() for c in url):
        raise ValueError('请填写有效的 HTTPS Webhook 地址，不含用户名、密码或片段')
    return url


def prepare_notification(channel, values, current=None):
    if not isinstance(channel, str) or channel not in SECRET_FIELDS or not isinstance(values, dict):
        raise ValueError('通知渠道或配置无效')
    settings = {**notification_settings()[channel], **copy.deepcopy(current or {})}
    if 'enabled' in values:
        settings['enabled'] = values['enabled']
    if not isinstance(settings['enabled'], bool):
        raise ValueError('通知开关必须为 true 或 false')
    for field in SECRET_FIELDS[channel]:
        if field not in values:
            continue
        value = values[field]
        # A blank field preserves an already saved credential, as with SMTP passwords.
        if isinstance(value, str):
            value = value.strip()
        if value:
            settings[field] = value
    if channel == 'webhook':
        settings['format'] = values.get('format', settings['format'])
        if settings['format'] not in WEBHOOK_FORMATS:
            raise ValueError('Webhook 类型无效')
        for field in ('secret','headers'):
            if values.get('clear_' + field) is True:
                settings[field] = {} if field == 'headers' else ''
        settings['headers'] = webhook_headers(settings.get('headers', {}))
    for field in SECRET_FIELDS[channel]:
        if field != 'headers' and (not isinstance(settings[field], str) or len(settings[field]) > 8192):
            raise ValueError('通知配置字段无效或过长')
    if settings['enabled']:
        required = ('url',) if channel == 'webhook' else SECRET_FIELDS[channel]
        if any(not settings[field] for field in required):
            raise ValueError('请完整填写 ' + CHANNEL_LABELS[channel] + ' 的通知配置')
        if channel == 'webhook':
            validate_webhook_url(settings['url'])
        elif channel == 'telegram':
            if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', settings['bot_token']):
                raise ValueError('Telegram Bot Token 格式无效')
        elif channel == 'serverchan' and not re.fullmatch(r'[A-Za-z0-9_-]+', settings['send_key']):
            raise ValueError('Server酱 SendKey 格式无效')
    return {key: settings[key] for key in notification_settings()[channel]}


def public_notifications(settings):
    public = {}
    for channel, defaults in notification_settings().items():
        value = {**defaults, **settings.get(channel, {})}
        public[channel] = {'enabled':bool(value.get('enabled')),
                           **{'has_' + field:bool(value.get(field)) for field in SECRET_FIELDS[channel]}}
        if channel == 'webhook':
            public[channel]['format'] = value.get('format', 'json')
    return public


def notification_title(is_success):
    return '微信读书 · ' + ('任务完成' if is_success else '任务失败')


def webhook_request(settings, content, is_success=True, context=None, test=False):
    url = validate_webhook_url(settings['url'])
    kind = settings.get('format', 'json')
    title = '微信读书 · 通知测试' if test else notification_title(is_success)
    text = title + '\n' + content
    if kind == 'json':
        data = {k:v for k,v in (context or {}).items() if k in
                ('account_id','account_name','run_id','status','completed','target')}
        payload = {'event':'wxread.test' if test else 'wxread.reading.' + ('completed' if is_success else 'failed'),
                   'title':title, 'content':content, 'success':is_success, 'timestamp':now(), 'data':data}
    elif kind == 'feishu':
        payload = {'msg_type':'text', 'content':{'text':text}}
        if settings.get('secret'):
            timestamp = str(int(time.time()))
            key = (timestamp + '\n' + settings['secret']).encode()
            payload.update(timestamp=timestamp, sign=base64.b64encode(hmac.new(key, b'', hashlib.sha256).digest()).decode())
    elif kind in ('wecom','dingtalk'):
        if kind == 'wecom':
            text = text.encode('utf-8')[:2048].decode('utf-8', errors='ignore')
        payload = {'msgtype':'text', 'text':{'content':text}}
        if kind == 'dingtalk' and settings.get('secret'):
            timestamp = str(int(time.time()*1000))
            secret = settings['secret'].encode()
            signature = base64.b64encode(hmac.new(secret, (timestamp+'\n'+settings['secret']).encode(), hashlib.sha256).digest()).decode()
            parsed = urlsplit(url)
            query = [(k,v) for k,v in parse_qsl(parsed.query, keep_blank_values=True) if k not in ('timestamp','sign')]
            url = urlunsplit(parsed._replace(query=urlencode(query + [('timestamp',timestamp),('sign',signature)])))
    else:
        raise ValueError('Webhook 类型无效')
    return url, payload, webhook_headers(settings.get('headers', {}))


def _post(url, payload, headers=None, proxies=None):
    try:
        response = requests.post(url, json=payload, headers={'Content-Type':'application/json', **(headers or {})},
                                 timeout=(5, 15), allow_redirects=False, proxies=proxies)
        if not 200 <= response.status_code < 300:
            raise RuntimeError('通知服务返回 HTTP ' + str(response.status_code))
        return response
    except requests.RequestException:
        raise RuntimeError('无法连接通知服务，请检查配置和网络') from None


def _result(response):
    try:
        value = response.json()
    except ValueError:
        raise RuntimeError('通知服务返回了无效响应') from None
    if not isinstance(value, dict):
        raise RuntimeError('通知服务返回了无效响应')
    return value


def send_channel(channel, settings, content, is_success=True, context=None, test=False):
    settings = prepare_notification(channel, {**settings, 'enabled':True})
    title = '微信读书 · 通知测试' if test else notification_title(is_success)
    if channel == 'webhook':
        url, payload, headers = webhook_request(settings, content, is_success, context, test=test)
        response = _post(url, payload, headers)
        if settings['format'] == 'json':
            return True
        result = _result(response)
        code = result.get('code', result.get('StatusCode')) if settings['format']=='feishu' else result.get('errcode')
        accepted = type(code) is int and code == 0
    elif channel == 'pushplus':
        result = _result(_post('https://www.pushplus.plus/send', {'token':settings['token'],'title':title,'content':content,'template':'txt'}))
        accepted = result.get('code') == 200
    elif channel == 'wxpusher':
        result = _result(_post('https://wxpusher.zjiecode.com/api/send/message/simple-push',
                               {'spt':settings['spt'],'content':content,'summary':title,'contentType':1}))
        accepted = result.get('code') == 1000
        for item in result.get('data', []) if isinstance(result.get('data'), list) else []:
            if item.get('code') != 1000:
                accepted = False
    elif channel == 'telegram':
        url = 'https://api.telegram.org/bot' + settings['bot_token'] + '/sendMessage'
        proxies = {k:v for k,v in {'http':os.getenv('http_proxy'),'https':os.getenv('https_proxy')}.items() if v}
        payload = {'chat_id':settings['chat_id'],'text':(title+'\n'+content)[:4096]}
        try:
            response = _post(url, payload, proxies=proxies or None)
        except RuntimeError:
            if not proxies:
                raise
            response = _post(url, payload, proxies={'http':None,'https':None})
        result = _result(response)
        accepted = result.get('ok') is True
    else:
        key = settings['send_key']
        match = re.match(r'^sctp(\d+)t', key)
        url = 'https://' + match.group(1) + '.push.ft07.com/send/' + key + '.send' if match else 'https://sctapi.ftqq.com/' + key + '.send'
        result = _result(_post(url, {'title':title,'desp':content}))
        accepted = result.get('code') == 0
    if not accepted:
        # Provider response text can echo tokens, URLs or content; never surface it.
        raise RuntimeError(CHANNEL_LABELS[channel] + ' 拒绝了通知请求，请检查密钥、接收对象和服务设置')
    return True


def send_notifications(content, email_config=None, channels=None, is_success=True, recipient=None, context=None):
    email_config = email_settings() if email_config is None else email_config
    channels = notification_settings() if channels is None else channels
    result = {'succeeded':[], 'failed':[]}
    targets = [('email', email_config)] + [(channel, channels.get(channel, {})) for channel in SECRET_FIELDS]
    for channel, settings in targets:
        if not settings.get('enabled'):
            continue
        try:
            if channel == 'email':
                send_email(content, settings, is_success=is_success, recipient=recipient)
            else:
                send_channel(channel, settings, content, is_success=is_success, context=context)
            result['succeeded'].append(channel)
        except Exception:
            result['failed'].append(channel)
            logging.error('%s 通知未发送成功，请检查该渠道配置或网络', CHANNEL_LABELS[channel])
    return result
