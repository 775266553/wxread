"""One independent WeRead client per account; importing this module never runs a task."""
import copy
import hashlib
import json
import logging
import random
import threading
import time
import urllib.parse

import requests

from config import book, chapter, data as default_data

KEY = '3c5c8717f3daf09iop3423zafeqoi'
READ_URL = 'https://weread.qq.com/web/book/read'
RENEW_URL = 'https://weread.qq.com/web/login/renewal'
FIX_SYNCKEY_URL = 'https://weread.qq.com/web/book/chapterInfos'
REQUEST_TIMEOUT = (10, 30)
AUTH_ERROR_CODES = {-2012, -2013}


class AuthenticationError(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


def encode_data(data):
    return '&'.join(f'{k}={urllib.parse.quote(str(data[k]), safe="")}' for k in sorted(data))


def cal_hash(value):
    a = b = 0x15051505
    length = len(value)
    index = length - 1
    while index > 0:
        a = 0x7fffffff & (a ^ ord(value[index]) << (length - index) % 30)
        b = 0x7fffffff & (b ^ ord(value[index - 1]) << index % 30)
        index -= 2
    return hex(a + b)[2:].lower()


class ReadClient:
    def __init__(self, account, on_cookies=None, session=None):
        self.headers = copy.deepcopy(account['headers'])
        self.cookies = copy.deepcopy(account['cookies'])
        self.data = copy.deepcopy(default_data)
        self.session = session or requests.Session()
        self.on_cookies = on_cookies or (lambda cookies: None)

    def post(self, url, payload):
        response = self.session.post(url, headers=self.headers, cookies=self.cookies,
                                     data=json.dumps(payload, separators=(',', ':')), timeout=REQUEST_TIMEOUT)
        new_cookies = response.cookies.get_dict()
        if new_cookies:
            self.cookies.update(new_cookies)
            self.on_cookies(self.cookies)
        # Avoid duplicate cookies with different domains in the requests session jar.
        self.session.cookies.clear()
        return response

    def refresh_cookie(self, strict=True):
        for payload in ({'rq': '%2Fweb%2Fbook%2Fread', 'ql': False},
                        {'rq': '%2Fweb%2Fbook%2Fread', 'ql': True},
                        {'rq': '%2Fweb%2Fbook%2Fread'}):
            try:
                response = self.post(RENEW_URL, payload)
                if response.ok and response.cookies.get('wr_skey'):
                    return True
            except requests.RequestException:
                logging.warning('Cookie 刷新请求失败')
        if strict:
            raise AuthenticationError('登录状态已失效，请重新扫码绑定')
        return False

    def run(self, read_num, progress=None, cancel=None):
        progress = progress or (lambda completed, message: None)
        cancel = cancel or threading.Event()
        completed = repairs = failures = 0
        last_time = int(time.time()) - 30
        try:
            if cancel.is_set():
                raise Cancelled('任务已停止')
            self.refresh_cookie(strict=False)
            while completed < read_num:
                if cancel.is_set():
                    raise Cancelled('任务已停止')
                payload = self.data
                payload.pop('s', None)
                payload['b'], payload['c'] = random.choice(book), random.choice(chapter)
                current = int(time.time())
                payload.update(ct=current, rt=current - last_time,
                               ts=current * 1000 + random.randint(0, 1000), rn=random.randint(0, 1000))
                payload['sg'] = hashlib.sha256(f"{payload['ts']}{payload['rn']}{KEY}".encode()).hexdigest()
                payload['s'] = cal_hash(encode_data(payload))
                try:
                    response = self.post(READ_URL, payload)
                    result = response.json()
                except requests.RequestException:
                    raise RuntimeError('阅读请求失败，请检查网络连接') from None
                except ValueError:
                    raise RuntimeError(f'阅读接口返回非 JSON 响应（HTTP {response.status_code}）') from None
                if not isinstance(result, dict):
                    raise RuntimeError('阅读接口返回了无法识别的数据')
                if response.ok and result.get('succ') and 'synckey' in result:
                    completed += 1
                    repairs = failures = 0
                    last_time = current
                    progress(completed, f'已完成 {completed * 0.5:g} 分钟')
                    if cancel.wait(30):
                        raise Cancelled('任务已停止')
                elif response.ok and result.get('succ'):
                    repairs += 1
                    if repairs > 3:
                        raise RuntimeError('连续 3 次未恢复 synckey，任务已终止')
                    progress(completed, f'正在修复阅读状态（{repairs}/3）')
                    self.post(FIX_SYNCKEY_URL, {'bookIds': ['3300060341']}).raise_for_status()
                    if cancel.wait(5):
                        raise Cancelled('任务已停止')
                else:
                    failures += 1
                    if failures > 3:
                        if result.get('errCode') in AUTH_ERROR_CODES:
                            raise AuthenticationError('登录验证失败，请重新扫码绑定')
                        raise RuntimeError(f"阅读接口连续失败（错误码 {result.get('errCode', response.status_code)}）")
                    self.refresh_cookie(strict=result.get('errCode') in AUTH_ERROR_CODES)
                    if cancel.wait(5):
                        raise Cancelled('任务已停止')
            return completed
        finally:
            self.session.close()
