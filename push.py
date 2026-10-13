import json
import logging
import os
import re
import smtplib
import ssl
from email.message import EmailMessage

from account_store import validate_email

import requests

from config import (
    PUSHPLUS_TOKEN,
    SERVERCHAN_SPT,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    WXPUSHER_SPT,
    PUSH_METHOD,
)

logger = logging.getLogger(__name__)


def email_settings():
    return {
        'enabled': 'email' in re.split(r'[,;\s]+', str(os.getenv('PUSH_METHOD') or PUSH_METHOD or '').lower()),
        'host': os.getenv('SMTP_HOST', ''),
        'port': os.getenv('SMTP_PORT') or '465',
        'security': os.getenv('SMTP_SECURITY') or 'ssl',
        'username': os.getenv('SMTP_USER', ''),
        'password': os.getenv('SMTP_PASSWORD', ''),
        'sender': os.getenv('SMTP_FROM', '') or os.getenv('SMTP_USER', ''),
        'recipient': os.getenv('EMAIL_TO', ''),
    }


def validate_email_settings(settings, require_recipient=True):
    settings = dict(settings)
    if not isinstance(settings.get('enabled', False), bool):
        raise ValueError('邮件开关必须为 true 或 false')
    for field in ('host', 'username', 'sender', 'recipient'):
        settings[field] = str(settings.get(field, '')).strip()
    settings['password'] = str(settings.get('password', ''))
    if not settings['host'] or any(c.isspace() for c in settings['host']) or '/' in settings['host']:
        raise ValueError('请填写 SMTP 服务器域名')
    try:
        settings['port'] = int(settings['port'])
    except (KeyError, TypeError, ValueError):
        raise ValueError('SMTP 端口必须为 1–65535') from None
    if not 1 <= settings['port'] <= 65535:
        raise ValueError('SMTP 端口必须为 1–65535')
    if settings.get('security') not in ('ssl', 'starttls'):
        raise ValueError('请选择 SSL 或 STARTTLS 加密方式')
    if bool(settings['username']) != bool(settings['password']):
        raise ValueError('SMTP 用户名与授权码需要一起填写')
    if not validate_email(settings['sender']):
        raise ValueError('请填写发件邮箱')
    recipients = [validate_email(address) for address in settings['recipient'].split(',') if address.strip()]
    if require_recipient and not recipients:
        raise ValueError('请填写收件邮箱')
    settings['recipient'] = ','.join(recipients)
    return settings


def send_email(content, settings=None, is_success=True, recipient=None, subject=None):
    settings = dict(settings or email_settings())
    if recipient:
        settings['recipient'] = recipient
    settings = validate_email_settings(settings)
    message = EmailMessage()
    message['Subject'] = subject or f"微信读书 · {'任务完成' if is_success else '任务失败'}"
    message['From'] = settings['sender']
    message['To'] = settings['recipient']
    message.set_content(content)
    context = ssl.create_default_context()
    if settings['security'] == 'ssl':
        connection = smtplib.SMTP_SSL(settings['host'], settings['port'], timeout=30, context=context)
    else:
        connection = smtplib.SMTP(settings['host'], settings['port'], timeout=30)
    with connection as smtp:
        if settings['security'] == 'starttls':
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
        if settings['username']:
            smtp.login(settings['username'], settings['password'])
        refused = smtp.send_message(message)
        if refused:
            raise RuntimeError('部分收件地址被 SMTP 服务器拒绝')
    return True


class PushNotification:
    """Compatibility adapter for callers of the original push methods."""
    def _send(self, channel, fields, content, is_success=True):
        from notifications import send_channel
        try:
            return send_channel(channel, fields, content, is_success=is_success)
        except Exception:
            logger.error('通知请求未成功，请检查该渠道配置和网络')
            return False

    def push_pushplus(self, content, token, is_success):
        return self._send('pushplus', {'token':token}, content, is_success)

    def push_telegram(self, content, bot_token, chat_id):
        return self._send('telegram', {'bot_token':bot_token, 'chat_id':chat_id}, content)

    def push_wxpusher(self, content, spt):
        return self._send('wxpusher', {'spt':spt}, content)

    def push_serverChan(self, content, spt, is_success):
        return self._send('serverchan', {'send_key':spt}, content, is_success)


def push(content, method, is_success=True):
    from notifications import notification_settings, selected_methods, send_notifications
    methods = selected_methods(method or '')
    settings = email_settings()
    settings['enabled'] = 'email' in methods
    result = send_notifications(content, email_config=settings, channels=notification_settings(method or ''),
                                is_success=is_success)
    return bool(result['succeeded']) and not result['failed']
