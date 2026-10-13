import json
import logging
import os
import random
import time
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
)

logger = logging.getLogger(__name__)


def email_settings():
    return {
        'enabled': str(os.getenv('PUSH_METHOD', '')).lower() == 'email',
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
    def __init__(self):
        self.pushplus_url = "https://www.pushplus.plus/send"
        self.telegram_url = "https://api.telegram.org/bot{}/sendMessage"
        self.server_chan_url = "https://sctapi.ftqq.com/{}.send"
        self.wxpusher_simple_url = "https://wxpusher.zjiecode.com/api/send/message/{}/{}"
        self.headers = {"Content-Type": "application/json"}
        self.proxies = {
            "http": os.getenv("http_proxy"),
            "https": os.getenv("https_proxy"),
        }

    def push_pushplus(self, content, token, is_success):
        attempts = 5
        title = f"微信阅读-{'成功' if is_success else '失败'}"
        for attempt in range(attempts):
            try:
                response = requests.post(
                    self.pushplus_url,
                    data=json.dumps({"token": token, "title": title,"content": content,}).encode("utf-8"),headers=self.headers,timeout=10,)
                response.raise_for_status()
                logger.info("PushPlus 响应: %s", response.text)
                return True
            except requests.exceptions.RequestException as exc:
                logger.error("PushPlus 推送失败: %s", exc)
                if attempt < attempts - 1:
                    sleep_time = random.randint(180, 360)
                    logger.info("%d 秒后重试...", sleep_time)
                    time.sleep(sleep_time)
        return False

    def push_telegram(self, content, bot_token, chat_id):
        url = self.telegram_url.format(bot_token)
        payload = {"chat_id": chat_id, "text": content}

        try:
            response = requests.post(url, json=payload, proxies=self.proxies, timeout=30)
            logger.info("Telegram 响应: %s", response.text)
            response.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Telegram 代理发送失败: %s", exc)
            try:
                response = requests.post(url, json=payload, timeout=30)
                response.raise_for_status()
                return True
            except Exception as inner_exc:
                logger.error("Telegram 发送失败: %s", inner_exc)
                return False

    def push_wxpusher(self, content, spt):
        attempts = 5
        url = self.wxpusher_simple_url.format(spt, content)

        for attempt in range(attempts):
            try:
                response = requests.get(url, timeout=10)
                response.raise_for_status()
                logger.info("WxPusher 响应: %s", response.text)
                return True
            except requests.exceptions.RequestException as exc:
                logger.error("WxPusher 推送失败: %s", exc)
                if attempt < attempts - 1:
                    sleep_time = random.randint(180, 360)
                    logger.info("%d 秒后重试...", sleep_time)
                    time.sleep(sleep_time)
        return False

    def push_serverChan(self, content, spt, is_success):
        attempts = 5
        url = self.server_chan_url.format(spt)

        title = f"微信阅读-{'成功' if is_success else '失败'}"

        for attempt in range(attempts):
            try:
                response = requests.post(
                    url,
                    data=json.dumps({"title": title, "desp": content}).encode("utf-8"),
                    headers=self.headers,
                    timeout=10,
                )
                response.raise_for_status()
                logger.info("ServerChan 响应: %s", response.text)
                return True
            except requests.exceptions.RequestException as exc:
                logger.error("ServerChan 推送失败: %s", exc)
                if attempt < attempts - 1:
                    sleep_time = random.randint(180, 360)
                    logger.info("%d 秒后重试...", sleep_time)
                    time.sleep(sleep_time)
        return False


def push(content, method, is_success = True):
    notifier = PushNotification()

    if method in (None, ""):
        logger.warning("未配置推送渠道，跳过推送。")
        return False

    method = str(method).lower()

    if method == 'email':
        try:
            return send_email(content, is_success=is_success)
        except Exception:
            logger.error('邮件发送失败，请检查 SMTP 配置、授权码和网络连接')
            return False

    if method == "pushplus":
        return notifier.push_pushplus(content, PUSHPLUS_TOKEN, is_success)
    if method == "telegram":
        return notifier.push_telegram(content, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    if method == "wxpusher":
        return notifier.push_wxpusher(content, WXPUSHER_SPT)
    if method == "serverchan":
        return notifier.push_serverChan(content, SERVERCHAN_SPT, is_success)

    logger.warning("无效的通知渠道 '%s'，已跳过推送。支持：email、pushplus、telegram、wxpusher、serverchan", method)
    return False
