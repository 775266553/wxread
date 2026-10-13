"""SQLite account/run storage. Credentials are encrypted and never returned by the API."""
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet

from config import READ_NUM, convert, headers as default_headers


def now():
    return datetime.now(timezone.utc).isoformat()


def validate_count(value):
    if isinstance(value, bool):
        raise ValueError('阅读次数必须是 1–2880 的整数')
    try:
        count = int(value)
    except (TypeError, ValueError):
        raise ValueError('阅读次数必须是 1–2880 的整数') from None
    if str(count) != str(value).strip() or not 1 <= count <= 2880:
        raise ValueError('阅读次数必须是 1–2880 的整数')
    return count


def validate_email(value):
    import re
    value = str(value or '').strip()
    if value and (len(value) > 254 or not re.fullmatch(r'[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+', value)):
        raise ValueError('请填写有效的邮箱地址')
    return value


class AccountStore:
    def __init__(self, directory=None):
        self.directory = Path(directory or os.getenv('WXREAD_DATA_DIR') or Path(__file__).parent / 'data')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        key = os.getenv('ACCOUNT_ENCRYPTION_KEY')
        if not key:
            key_path = self.directory / '.encryption-key'
            try:
                fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(Fernet.generate_key())
            except FileExistsError:
                pass
            key = key_path.read_bytes()
        self.cipher = Fernet(key)
        self.path = self.directory / 'wxread.sqlite3'
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY, vid TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
                    credentials TEXT NOT NULL, read_num INTEGER NOT NULL,
                    email TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'ready', created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, account_id TEXT NOT NULL, account_name TEXT NOT NULL,
                    status TEXT NOT NULL, completed INTEGER NOT NULL DEFAULT 0,
                    target INTEGER NOT NULL, message TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL, finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS runs_by_account ON runs(account_id, started_at);
                CREATE INDEX IF NOT EXISTS runs_by_start ON runs(started_at);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs(account_id)
                    WHERE status IN ('running','queued');
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def encrypt(self, value):
        return self.cipher.encrypt(json.dumps(value, ensure_ascii=False).encode()).decode()

    def decrypt(self, value):
        return json.loads(self.cipher.decrypt(value.encode()))

    @staticmethod
    def public(row):
        result = dict(row)
        result.pop('credentials', None)
        # Account identity is unnecessary on the public interface.
        result.pop('vid', None)
        result['enabled'] = bool(result['enabled'])
        return result

    def accounts(self):
        with self.lock, self.connect() as db:
            return [self.public(row) for row in db.execute('SELECT * FROM accounts ORDER BY created_at')]

    def account(self, account_id, private=False):
        with self.lock, self.connect() as db:
            row = db.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
        if row is None:
            raise KeyError('账户不存在')
        if not private:
            return self.public(row)
        result = dict(row)
        result.update(self.decrypt(result.pop('credentials')))
        return result

    def bind(self, cookies, headers=None, name='', read_num=None, email='', account_id=None):
        if not cookies.get('wr_vid') or not cookies.get('wr_skey'):
            raise ValueError('登录信息缺少 wr_vid 或 wr_skey，请重新扫码或导入完整 cURL')
        vid = str(cookies['wr_vid'])
        name = str(name).strip()[:80]
        email = validate_email(email)
        stamp = now()
        credentials = self.encrypt({'cookies': dict(cookies), 'headers': dict(headers or default_headers)})
        with self.lock, self.connect() as db:
            existing = db.execute('SELECT * FROM accounts WHERE vid=?', (vid,)).fetchone()
            if account_id:
                target = db.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
                if not target:
                    raise KeyError('账户不存在')
                if target['vid'] != vid:
                    raise ValueError('请使用原账户扫码；新账户请使用「添加账户」')
            if existing:
                if db.execute("SELECT 1 FROM runs WHERE account_id=? AND status IN ('running','queued')", (existing['id'],)).fetchone():
                    raise ValueError('请先停止该账户的任务，再重新绑定')
                db.execute('UPDATE accounts SET credentials=?,status=?,updated_at=? WHERE id=?',
                           (credentials, 'ready', stamp, existing['id']))
                account_id = existing['id']
            else:
                account_id = uuid.uuid4().hex
                db.execute('INSERT INTO accounts(id,vid,name,credentials,read_num,email,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                           (account_id, vid, name or '微信读书账户', credentials,
                            validate_count(read_num if read_num is not None else READ_NUM), email, stamp, stamp))
        return self.account(account_id)

    def import_curl(self, curl, **options):
        parsed_headers, cookies = convert(curl)
        return self.bind(cookies, {**default_headers, **parsed_headers}, **options)

    def update(self, account_id, values):
        allowed = {}
        if 'name' in values:
            name = str(values['name']).strip()
            if not name or len(name) > 80:
                raise ValueError('账户名称应为 1–80 个字符')
            allowed['name'] = name
        if 'read_num' in values:
            allowed['read_num'] = validate_count(values['read_num'])
        if 'email' in values:
            allowed['email'] = validate_email(values['email'])
        if 'enabled' in values:
            if not isinstance(values['enabled'], bool):
                raise ValueError('启用状态必须为 true 或 false')
            allowed['enabled'] = int(values['enabled'])
        self.account(account_id)
        if allowed:
            allowed['updated_at'] = now()
            with self.lock, self.connect() as db:
                if db.execute("SELECT 1 FROM runs WHERE account_id=? AND status IN ('running','queued')", (account_id,)).fetchone():
                    raise ValueError('请先停止该账户的任务，再修改设置')
                db.execute('UPDATE accounts SET ' + ','.join(f'{key}=?' for key in allowed) + ' WHERE id=?',
                           (*allowed.values(), account_id))
        return self.account(account_id)

    def save_cookies(self, account_id, cookies):
        with self.lock:
            account = self.account(account_id, private=True)
            credentials = self.encrypt({'cookies': dict(cookies), 'headers': account['headers']})
            with self.connect() as db:
                db.execute('UPDATE accounts SET credentials=?,updated_at=? WHERE id=?', (credentials, now(), account_id))

    def set_status(self, account_id, status):
        with self.lock, self.connect() as db:
            db.execute('UPDATE accounts SET status=?,updated_at=? WHERE id=?', (status, now(), account_id))

    def delete(self, account_id):
        self.account(account_id)
        with self.lock, self.connect() as db:
            if db.execute("SELECT 1 FROM runs WHERE account_id=? AND status IN ('running','queued')", (account_id,)).fetchone():
                raise ValueError('请先停止该账户的任务，再移除账户')
            db.execute('DELETE FROM accounts WHERE id=?', (account_id,))

    def new_run(self, account):
        run_id = uuid.uuid4().hex
        with self.lock, self.connect() as db:
            try:
                db.execute('INSERT INTO runs(id,account_id,account_name,status,target,started_at) VALUES(?,?,?,?,?,?)',
                           (run_id, account['id'], account['name'], 'queued', account['read_num'], now()))
            except sqlite3.IntegrityError:
                raise ValueError('这个账户已有任务在运行或排队') from None
        return run_id

    def update_run(self, run_id, **fields):
        allowed = {key: value for key, value in fields.items()
                   if key in {'status', 'completed', 'message', 'finished_at'}}
        if allowed:
            with self.lock, self.connect() as db:
                db.execute('UPDATE runs SET ' + ','.join(f'{key}=?' for key in allowed) + ' WHERE id=?',
                           (*allowed.values(), run_id))

    def runs(self, limit=100):
        with self.lock, self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM runs ORDER BY started_at DESC LIMIT ?', (limit,))]

    def statistics(self, timezone_name='Asia/Shanghai'):
        zone = ZoneInfo(timezone_name)
        today = datetime.now(zone).date()
        dates = [(today - timedelta(days=6 - i)).isoformat() for i in range(7)]
        days = {date: {'date': date, 'minutes': 0, 'success': 0, 'running': 0} for date in dates}
        cutoff = datetime.combine(today - timedelta(days=6), datetime.min.time(), zone).astimezone(timezone.utc).isoformat()
        with self.lock, self.connect() as db:
            rows = db.execute('SELECT started_at,status,completed FROM runs WHERE started_at>=?', (cutoff,)).fetchall()
        for row in rows:
            key = datetime.fromisoformat(row['started_at']).astimezone(zone).date().isoformat()
            if key in days:
                days[key]['minutes'] += row['completed'] * 0.5
                days[key]['success'] += row['status'] == 'success'
                days[key]['running'] += row['status'] == 'running'
        return {'days': list(days.values()), 'today': days[today.isoformat()]}

    def recover_runs(self):
        with self.lock, self.connect() as db:
            db.execute("UPDATE runs SET status='interrupted',message='服务重启，任务已中断',finished_at=? WHERE status IN ('running','queued')", (now(),))

    def settings(self, key, default=None):
        with self.lock, self.connect() as db:
            row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return self.decrypt(row['value']) if row else default

    def set_settings(self, key, value):
        with self.lock, self.connect() as db:
            db.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       (key, self.encrypt(value)))

    def seed_from_environment(self):
        # Import only on an empty database, so a restart never overwrites renewed cookies.
        if self.accounts():
            return
        accounts_json = os.getenv('WXREAD_ACCOUNTS_JSON')
        if accounts_json:
            accounts = json.loads(accounts_json)
            if not isinstance(accounts, list) or not accounts:
                raise ValueError('WXREAD_ACCOUNTS_JSON 必须是非空的账户数组')
            # Validate the full input before persisting any account.
            prepared = []
            for account in accounts:
                if not isinstance(account, dict):
                    raise ValueError('账户配置必须是对象')
                h, c = convert(account.get('curl', '')) if account.get('curl') else (default_headers, account.get('cookies', {}))
                if not c.get('wr_vid') or not c.get('wr_skey'):
                    raise ValueError('每个账户都需要 wr_vid 和 wr_skey')
                prepared.append((account, h, c, validate_count(account.get('read_num', READ_NUM)), validate_email(account.get('email', ''))))
            for account, h, c, count, email in prepared:
                imported = self.bind(c, {**default_headers, **h}, account.get('name', ''), count, email)
                if 'enabled' in account:
                    self.update(imported['id'], {'enabled': account['enabled']})
        elif os.getenv('WXREAD_CURL_BASH'):
            self.import_curl(os.environ['WXREAD_CURL_BASH'], name='默认账户')
