"""Capture the real WeRead login QR in a fresh, isolated browser context."""
import base64
import hashlib
import logging
import os
import re
import threading
import time
import uuid
from urllib.parse import unquote


QR_SESSION_SECONDS = 360
QR_READY_SCRIPT = """() => {
    const node = document.querySelector('.wr_login_modal_qr_img');
    if (!node || node.classList.contains('wr_login_modal_qr_img_expired')) return false;
    if (node.tagName === 'IMG') return node.complete && node.naturalWidth > 0;
    if (node.tagName === 'CANVAS') return node.width > 0 && node.height > 0;
    return Boolean(node.querySelector('path, rect, canvas, img'));
}"""


class QRLoginManager:
    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        self.sessions = {}

    def start(self, options=None):
        options = dict(options or {})
        if options.get('account_id'):
            self.store.account(options['account_id'])
        with self.lock:
            self.sessions = {key: value for key, value in self.sessions.items() if time.time() - value['created'] < 900}
            if sum(s['status'] in ('starting', 'waiting', 'refreshing') for s in self.sessions.values()) >= 2:
                raise ValueError('已有两个扫码窗口，请先关闭或完成绑定')
            session_id = uuid.uuid4().hex
            self.sessions[session_id] = {'status': 'starting', 'message': '正在加载微信读书登录页…',
                                         'created': time.time(), 'qr': None, 'cancel': threading.Event()}
        threading.Thread(target=self._run, args=(session_id, options), daemon=True, name='qr-login').start()
        return session_id

    def update(self, session_id, **values):
        with self.lock:
            if session_id in self.sessions:
                self.sessions[session_id].update(values)

    def state(self, session_id):
        with self.lock:
            if session_id not in self.sessions:
                raise KeyError('扫码会话不存在或已过期')
            state = self.sessions[session_id]
            return {key: value for key, value in state.items() if key not in ('created', 'cancel')}

    def cancel(self, session_id):
        with self.lock:
            if session_id in self.sessions:
                self.sessions[session_id]['cancel'].set()

    def close(self):
        with self.lock:
            for state in self.sessions.values():
                state['cancel'].set()

    def _run(self, session_id, options):
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                launch = {'headless': True}
                if os.getenv('PLAYWRIGHT_CHROMIUM_EXECUTABLE'):
                    launch['executable_path'] = os.environ['PLAYWRIGHT_CHROMIUM_EXECUTABLE']
                elif os.getenv('PLAYWRIGHT_CHANNEL'):
                    launch['channel'] = os.environ['PLAYWRIGHT_CHANNEL']
                with playwright.chromium.launch(**launch) as browser:
                    context = browser.new_context(viewport={'width': 1200, 'height': 900}, locale='zh-CN')
                    # Do not let the official page use the desktop WeChat client's quick login.
                    # Every binding must start with a new QR and an explicit phone confirmation.
                    context.route(re.compile(r'^https?://localhost\.weixin\.qq\.com'), lambda route: route.abort())
                    page = context.new_page()
                    page.goto('https://weread.qq.com/', wait_until='domcontentloaded', timeout=45000)
                    qr = page.locator('.wr_login_modal_qr_wrapper').first
                    if not qr.is_visible():
                        page.get_by_text('登录', exact=True).first.click(timeout=15000)
                    modal = page.locator('.wr_login_modal_wrapper')
                    modal.wait_for(state='visible', timeout=30000)
                    qr.wait_for(state='visible', timeout=30000)
                    image = qr.locator('.wr_login_modal_qr_img').first
                    expired_overlay = page.locator('.wr_login_modal_qr_overlay').first
                    with self.lock:
                        cancel = self.sessions[session_id]['cancel']
                    deadline = time.monotonic() + QR_SESSION_SECONDS
                    fingerprint = None
                    version = 0
                    while time.monotonic() < deadline and not cancel.is_set():
                        cookies = {cookie['name']: cookie['value'] for cookie in context.cookies('https://weread.qq.com/')}
                        if cookies.get('wr_vid') and cookies.get('wr_skey') and not modal.is_visible():
                            name = options.get('name') or unquote(cookies.get('wr_name', '')) or '微信读书账户'
                            account = self.store.bind(cookies, {
                                'accept': 'application/json, text/plain, */*',
                                'content-type': 'application/json',
                                'origin': 'https://weread.qq.com', 'referer': 'https://weread.qq.com/',
                                'user-agent': page.evaluate('navigator.userAgent'),
                            }, name=name, read_num=options.get('read_num'), email=options.get('email', ''),
                                account_id=options.get('account_id'))
                            self.update(session_id, status='success', message='账户绑定成功', qr=None, account=account)
                            return
                        if expired_overlay.is_visible():
                            self.update(session_id, status='refreshing', message='二维码已失效，正在自动更新…', qr=None)
                            # Refresh inside the existing official login session; avoid another Actions startup.
                            expired_overlay.click(timeout=5000)
                            fingerprint = None
                        if qr.is_visible():
                            if image.is_visible() and page.evaluate(QR_READY_SCRIPT):
                                content = image.evaluate("node => node.tagName === 'CANVAS' ? node.toDataURL() : node.tagName === 'IMG' ? node.currentSrc : node.innerHTML")
                                current = hashlib.sha256(content.encode()).hexdigest()
                                if current != fingerprint:
                                    screenshot = base64.b64encode(qr.screenshot(timeout=5000)).decode()
                                    if not page.evaluate(QR_READY_SCRIPT):
                                        continue
                                    version += 1
                                    self.update(session_id, status='waiting',
                                                message='使用微信扫一扫，并在手机上确认登录；失效后会自动更新',
                                                qr='data:image/png;base64,' + screenshot, qr_version=version)
                                    fingerprint = current
                        if cancel.wait(1):
                            break
                    self.update(session_id, status='cancelled' if cancel.is_set() else 'expired',
                                message='扫码已关闭' if cancel.is_set() else '本次扫码窗口已结束，请重新生成二维码', qr=None)
        except ValueError as exc:
            self.update(session_id, status='failed', message=str(exc), qr=None)
        except Exception as exc:
            logging.warning('扫码登录加载失败（%s）', type(exc).__name__)
            self.update(session_id, status='failed', qr=None,
                        message='无法加载扫码登录。请检查浏览器安装和网络，或使用 cURL 导入账户。')
