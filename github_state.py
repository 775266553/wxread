"""Encrypted durable state for the GitHub Pages + Actions deployment."""
import base64
import copy
import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from apscheduler.triggers.cron import CronTrigger
from nacl.public import PrivateKey, PublicKey, SealedBox
from nacl.secret import SecretBox

from account_store import AccountStore, now, validate_count, validate_email
from push import email_settings

STATE_BRANCH = 'wxread-state'
STATE_FILE = 'state.enc.json'


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()


def validate_id(value):
    if not re.fullmatch(r'[a-f0-9]{32}', str(value)):
        raise ValueError('会话或账户标识无效')
    return value


def seal(value, public_key):
    key = PublicKey(base64.b64decode(public_key, validate=True))
    return base64.b64encode(SealedBox(key).encrypt(encode(value))).decode()


def unseal(value, private_key):
    return json.loads(SealedBox(private_key).decrypt(base64.b64decode(value, validate=True)))


class GitHubAPI:
    def __init__(self, repository=None, token=None):
        self.repository = repository or os.environ['GITHUB_REPOSITORY']
        if not re.fullmatch(r'[\w.-]+/[\w.-]+', self.repository):
            raise ValueError('仓库地址无效')
        self.token = token or os.environ['GH_TOKEN']

    def request(self, method, path, value=None, missing=False):
        try:
            response = requests.request(method, 'https://api.github.com/repos/' + self.repository + path,
                headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
                         'X-GitHub-Api-Version': '2026-03-10'}, json=value, timeout=(10, 30), allow_redirects=False)
        except requests.RequestException:
            raise RuntimeError('无法连接 GitHub API') from None
        if missing and response.status_code == 404:
            return None
        if response.status_code == 409:
            raise ConflictError()
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f'GitHub API 请求失败（HTTP {response.status_code}）')
        return response.json() if response.content else None

    def get_file(self, path):
        result = self.request('GET', f'/contents/{path}?ref={STATE_BRANCH}', missing=True)
        if result is None:
            return None, None
        return json.loads(base64.b64decode(result['content'])), result['sha']

    def put_file(self, path, value, sha=None):
        body = {'branch': STATE_BRANCH, 'message': 'Update encrypted wxread state',
                'content': base64.b64encode(encode(value)).decode()}
        if sha:
            body['sha'] = sha
        return self.request('PUT', '/contents/' + path, body)


class ConflictError(RuntimeError):
    pass


class GitHubState:
    def __init__(self, api=None, private_key=None):
        self.api = api or GitHubAPI()
        key = private_key or os.getenv('WXREAD_CONTROL_KEY', '')
        if not key:
            raise ValueError('请先在 Pages 页面连接仓库并初始化加密密钥')
        self.private_key = PrivateKey(base64.b64decode(key, validate=True))
        self.box = SecretBox(hashlib.sha256(bytes(self.private_key) + b'wxread-state-v1').digest())

    def defaults(self):
        return {'version': 1, 'accounts': [], 'runs': [], 'clients': [],
                'schedule': {'enabled': True, 'cron': '7 17 * * *', 'timezone': 'UTC'},
                'email': email_settings()}

    def read(self):
        metadata, _ = self.api.get_file('control.json')
        if not metadata or metadata.get('public_key') != base64.b64encode(bytes(self.private_key.public_key)).decode():
            raise ValueError('仓库公钥与 WXREAD_CONTROL_KEY 不匹配，请检查初始化配置')
        value, sha = self.api.get_file(STATE_FILE)
        if value is None:
            state = self.defaults()
            if os.getenv('WXREAD_ACCOUNTS_JSON') or os.getenv('WXREAD_CURL_BASH'):
                import tempfile
                with tempfile.TemporaryDirectory() as directory:
                    legacy = AccountStore(directory)
                    legacy.seed_from_environment()
                    state['accounts'] = [legacy.account(a['id'], private=True) for a in legacy.accounts()]
                    for account in state['accounts']:
                        account['enabled'] = bool(account['enabled'])
            return state, None
        return json.loads(self.box.decrypt(base64.b64decode(value['ciphertext'], validate=True))), sha

    def mutate(self, function, publish=True):
        for attempt in range(5):
            state, sha = self.read()
            function(state)
            state['updated_at'] = now()
            encrypted = {'version': 1, 'ciphertext': base64.b64encode(self.box.encrypt(encode(state))).decode()}
            try:
                self.api.put_file(STATE_FILE, encrypted, sha)
            except ConflictError:
                if attempt == 4:
                    raise RuntimeError('仓库状态同时更新，请稍后重试') from None
                time.sleep(1 + attempt)
                continue
            if publish:
                self.publish(state)
            return state
        raise RuntimeError('状态保存失败')

    def respond(self, session_id, public_key, value):
        validate_id(session_id)
        path = f'responses/{session_id}.json'
        for attempt in range(5):
            _, sha = self.api.get_file(path)
            try:
                self.api.put_file(path, {'version': 1, 'ciphertext': seal(value, public_key)}, sha)
                return
            except ConflictError:
                if attempt == 4:
                    raise RuntimeError('状态同步冲突，请刷新页面') from None
                time.sleep(1)

    def publish(self, state):
        snapshot = overview(state)
        for client in state.get('clients', [])[-4:]:
            if datetime.fromisoformat(client['expires_at']) > datetime.now(timezone.utc):
                self.respond(client['id'], client['public_key'], {'status': 'snapshot', 'overview': snapshot})


def workflow_cron():
    from pathlib import Path
    path = Path(__file__).parent / '.github/workflows/deploy.yml'
    match = re.search(r"^\s*- cron:\s*['\"]([^'\"]+)['\"]", path.read_text(), re.M)
    return match.group(1) if match else '7 17 * * *'


def overview(state):
    schedule = {**state['schedule'], 'cron': workflow_cron(), 'timezone': 'UTC'}
    trigger = CronTrigger.from_crontab(schedule['cron'], timezone='UTC')
    next_run = trigger.get_next_fire_time(None, datetime.now(timezone.utc)) if schedule['enabled'] else None
    email = copy.deepcopy(state['email'])
    email['has_password'] = bool(email.pop('password', ''))
    accounts = [{k: a[k] for k in ('id','name','read_num','email','enabled','status','created_at','updated_at')}
                for a in state['accounts']]
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
    days = {(today - timedelta(days=6-i)).isoformat(): {'date': (today-timedelta(days=6-i)).isoformat(),
             'minutes': 0, 'success': 0, 'running': 0} for i in range(7)}
    for run in state['runs']:
        key = datetime.fromisoformat(run['started_at']).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
        if key in days:
            days[key]['minutes'] += run['completed'] * 0.5
            days[key]['success'] += run['status'] == 'success'
            days[key]['running'] += run['status'] == 'running'
    return {'accounts': accounts, 'runs': sorted(state['runs'], key=lambda r:r['started_at'], reverse=True)[:100],
            'schedule': schedule, 'next_run': next_run.isoformat() if next_run else None, 'email': email,
            'statistics': {'days': list(days.values()), 'today': days[today.isoformat()]}}


def bind_account(state, private_account, options):
    cookies = private_account['cookies']
    if not cookies.get('wr_vid') or not cookies.get('wr_skey'):
        raise ValueError('登录信息不完整，请重新绑定')
    target_id = options.get('account_id')
    existing = next((a for a in state['accounts'] if a['cookies']['wr_vid'] == cookies['wr_vid']), None)
    if target_id and (not existing or existing['id'] != target_id):
        raise ValueError('重新绑定时请扫描原账户；新账户请使用添加账户')
    if existing:
        import uuid
        existing.update(cookies=copy.deepcopy(cookies), headers=copy.deepcopy(private_account['headers']),
                        status='ready', updated_at=now(), session_version=uuid.uuid4().hex)
        return existing['id']
    import uuid
    account = {'id': uuid.uuid4().hex, 'name': str(options.get('name') or private_account.get('name') or '微信读书账户')[:80],
               'cookies': copy.deepcopy(cookies), 'headers': copy.deepcopy(private_account['headers']),
               'read_num': validate_count(options.get('read_num', 40)), 'email': validate_email(options.get('email', '')),
               'enabled': True, 'status': 'ready', 'created_at': now(), 'updated_at': now(),
               'session_version': uuid.uuid4().hex}
    state['accounts'].append(account)
    return account['id']
