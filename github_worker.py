"""Ephemeral Actions worker. Never prints decrypted state, Cookies or mail credentials."""
import copy
import json
import logging
import os
import signal
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

from account_store import AccountStore, now, validate_count, validate_email
from config import convert, headers as default_headers
from github_state import GitHubState, bind_account, overview, unseal, validate_id
from jobs import JobManager
from push import send_email, validate_email_settings
from qr_login import QRLoginManager


def find_account(state, account_id):
    validate_id(account_id)
    account = next((a for a in state['accounts'] if a['id'] == account_id), None)
    if not account:
        raise ValueError('账户不存在')
    return account


def email_config(state, options):
    settings = {**state['email'], **{k:options[k] for k in
                 ('enabled','host','port','security','username','sender','recipient') if k in options}}
    if options.get('password'):
        settings['password'] = str(options['password'])
    if not isinstance(settings.get('enabled'), bool):
        raise ValueError('邮件开关无效')
    return validate_email_settings(settings, require_recipient=False) if settings['enabled'] else settings


def control_operation(state, operation, options):
    if operation == 'inspect':
        return
    if operation == 'import':
        headers, cookies = convert(str(options.get('curl', '')))
        bind_account(state, {'headers': {**default_headers, **headers}, 'cookies': cookies}, options)
    elif operation == 'update':
        account = find_account(state, options['account_id'])
        name = str(options.get('name', account['name'])).strip()
        if not name or len(name) > 80:
            raise ValueError('账户名称应为 1–80 个字符')
        if not isinstance(options.get('enabled', account['enabled']), bool):
            raise ValueError('账户开关无效')
        account.update(name=name, read_num=validate_count(options.get('read_num', account['read_num'])),
                       email=validate_email(options.get('email', account['email'])),
                       enabled=options.get('enabled', account['enabled']), updated_at=now())
    elif operation == 'delete':
        find_account(state, options['account_id'])
        state['accounts'] = [a for a in state['accounts'] if a['id'] != options['account_id']]
    elif operation == 'schedule':
        if not isinstance(options.get('enabled'), bool):
            raise ValueError('计划开关无效')
        state['schedule']['enabled'] = options['enabled']
    elif operation == 'email':
        state['email'] = email_config(state, options)
    else:
        raise ValueError('操作无效')


def register_client(state, session_id, public_key):
    clients = [c for c in state.get('clients', []) if c['id'] != session_id and
               datetime.fromisoformat(c['expires_at']) > datetime.now(timezone.utc)]
    clients.append({'id': session_id, 'public_key': public_key,
                    'expires_at': (datetime.now(timezone.utc)+timedelta(hours=12)).isoformat()})
    state['clients'] = clients[-4:]


def qr_binding(repository, session_id, public_key, options):
    with tempfile.TemporaryDirectory() as directory:
        local = AccountStore(directory)
        initial, _ = repository.read()
        target = options.get('account_id')
        login_options = {k:options[k] for k in ('name','read_num','email') if k in options}
        if target:
            account = find_account(initial, target)
            imported = local.bind(account['cookies'], account['headers'], account['name'], account['read_num'], account['email'])
            login_options['account_id'] = imported['id']
        manager = QRLoginManager(local)
        login = manager.start(login_options)
        last_status = None
        try:
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                result = manager.state(login)
                if result['status'] == 'success':
                    account = local.account(result['account']['id'], private=True)
                    state = repository.mutate(lambda s: bind_account(s, account, options))
                    repository.respond(session_id, public_key, {'status':'success', 'message':'账户已绑定', 'overview':overview(state)})
                    return
                if result['status'] in ('failed','expired','cancelled'):
                    repository.respond(session_id, public_key, result)
                    return
                # One initial QR snapshot plus terminal status; do not create a commit every two seconds.
                if result['status'] != last_status and (result['status'] != 'waiting' or result.get('qr')):
                    repository.respond(session_id, public_key, {**result, 'expires_at':(datetime.now(timezone.utc)+timedelta(seconds=180)).isoformat()})
                    last_status = result['status']
                time.sleep(2)
            repository.respond(session_id, public_key, {'status':'expired','message':'扫码超时，请重新生成二维码'})
        finally:
            manager.close()


def control():
    repository = GitHubState()
    session_id = validate_id(os.environ['CONTROL_SESSION'])
    public_key = os.environ['CONTROL_PUBLIC_KEY']
    operation = os.environ['CONTROL_OPERATION']
    try:
        options = unseal(os.environ['CONTROL_PAYLOAD'], repository.private_key)
        if not isinstance(options, dict):
            raise ValueError('请求格式无效')
        client = options.pop('_client', None)
        if client:
            validate_id(client['id'])
            repository.mutate(lambda s: register_client(s, client['id'], client['public_key']))
        if operation == 'qr':
            qr_binding(repository, session_id, public_key, options)
        elif operation == 'test-email':
            state, _ = repository.read()
            send_email('来自 GitHub Actions 的微信读书测试邮件。', email_config(state, options), subject='微信读书 · 通知测试')
            repository.respond(session_id, public_key, {'status':'success','message':'测试邮件已发送'})
        else:
            state = repository.mutate(lambda s: control_operation(s, operation, options))
            repository.respond(session_id, public_key, {'status':'success','overview':overview(state)})
        return 0
    except Exception as exc:
        message = str(exc) if type(exc) in (ValueError, RuntimeError) else '操作失败，请检查 Actions 日志中的错误类型及配置'
        repository.respond(session_id, public_key, {'status':'failed','message':message})
        logging.error('控制操作失败（%s）', type(exc).__name__)
        return 1


def reading():
    if not os.getenv('WXREAD_CONTROL_KEY'):
        from main import main
        return main()
    repository = GitHubState()
    initial, _ = repository.read()
    if os.getenv('GITHUB_EVENT_NAME') == 'schedule' and not initial['schedule']['enabled']:
        return 0
    target = os.getenv('READ_ACCOUNT_ID')
    selected = [a for a in initial['accounts'] if a['id'] == target] if target else [a for a in initial['accounts'] if a['enabled']]
    if target and not selected:
        raise ValueError('指定账户不存在')
    if not selected:
        logging.info('没有参与本次阅读的账户')
        return 0
    with tempfile.TemporaryDirectory() as directory:
        local = AccountStore(directory)
        mapping = {}
        for account in selected:
            imported = local.bind(account['cookies'], account['headers'], account['name'], account['read_num'], account['email'])
            mapping[imported['id']] = account
            local.set_status(imported['id'], account['status'])
        local.set_settings('email', initial['email'])
        jobs = JobManager(local)
        result = jobs.run_all()
        workflow_id = os.getenv('GITHUB_RUN_ID', '')

        def persist(state):
            local_runs = local.runs(10000)
            for run in local_runs:
                run['account_id'] = mapping[run['account_id']]['id']
                run['workflow_run_id'] = workflow_id
            ids = {r['id'] for r in local_runs}
            state['runs'] = sorted([r for r in state['runs'] if r['id'] not in ids]+local_runs,
                                   key=lambda r:r['started_at'], reverse=True)[:1000]
            for local_id, original in mapping.items():
                current = next((a for a in state['accounts'] if a['id'] == original['id']), None)
                latest = local.account(local_id, private=True)
                # A fresh QR login takes precedence over renewal of the previous session.
                if current and current.get('session_version') == original.get('session_version'):
                    current.update(cookies=latest['cookies'], status=latest['status'], updated_at=now())

        def stop(*_):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, stop)
        try:
            repository.mutate(persist)
            last_publish = time.monotonic()
            while jobs.active:
                time.sleep(2)
                if time.monotonic()-last_publish >= 60:
                    repository.mutate(persist)
                    last_publish = time.monotonic()
            jobs.executor.shutdown(wait=True)
        except KeyboardInterrupt:
            jobs.shutdown()
        finally:
            repository.mutate(persist)
        runs = local.runs(10000)
        logging.info('本次任务：%d 个账户，%d 个成功', len(selected), sum(r['status']=='success' for r in runs))
        return 1 if result['skipped'] or any(r['status']!='success' for r in runs) else 0


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        sys.exit(control() if sys.argv[1:] == ['control'] else reading())
    except Exception as exc:
        logging.error('Actions 执行失败（%s）', type(exc).__name__)
        sys.exit(1)
