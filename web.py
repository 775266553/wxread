import hmac
import logging
import os
import secrets
import signal
from datetime import timedelta
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template, request, session
from werkzeug.exceptions import HTTPException
from waitress import serve

from account_store import AccountStore, validate_count, validate_email
from jobs import JobManager, schedule_defaults, schedule_trigger
from push import email_settings, send_email, validate_email_settings
from notifications import notification_settings, prepare_notification, public_notifications, send_channel
from qr_login import QRLoginManager


def create_app(store=None, jobs=None, logins=None):
    app = Flask(__name__)
    store = store or AccountStore()
    secret = store.settings('web-secret')
    if not secret:
        secret = secrets.token_hex(32)
        store.set_settings('web-secret', secret)
    app.config.update(SECRET_KEY=secret, MAX_CONTENT_LENGTH=128 * 1024,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict',
                      SESSION_COOKIE_SECURE=os.getenv('WEB_COOKIE_SECURE') == '1',
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=12))
    jobs = jobs or JobManager(store)
    logins = logins or QRLoginManager(store)
    app.extensions.update(store=store, jobs=jobs, logins=logins)
    admin_token = os.getenv('ADMIN_TOKEN', '')
    token_fingerprint = hmac.new(secret.encode(), admin_token.encode(), 'sha256').hexdigest()

    @app.before_request
    def protect():
        if request.path.startswith('/api/'):
            if not admin_token and request.remote_addr not in ('127.0.0.1', '::1'):
                return jsonify(error='远程访问需要设置 ADMIN_TOKEN'), 403
            if not admin_token and urlsplit(request.host_url).hostname not in ('localhost', '127.0.0.1', '::1'):
                return jsonify(error='本地模式只允许 localhost 访问'), 403
            if request.method not in ('GET', 'HEAD', 'OPTIONS'):
                origin = request.headers.get('Origin')
                if origin and urlsplit(origin).netloc != request.host:
                    return jsonify(error='请求来源无效'), 403
                if request.headers.get('X-WXREAD-Request') != '1':
                    return jsonify(error='请求缺少验证标记'), 403
            if admin_token and request.path not in ('/api/auth', '/api/session') and session.get('authenticated') != token_fingerprint:
                return jsonify(error='请先登录管理界面'), 401

    @app.after_request
    def response_headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    @app.errorhandler(Exception)
    def errors(exc):
        if isinstance(exc, KeyError):
            return jsonify(error=str(exc.args[0])), 404
        if isinstance(exc, ValueError):
            return jsonify(error=str(exc)), 400
        if isinstance(exc, HTTPException):
            return jsonify(error=exc.description), exc.code
        logging.error('Web 请求失败（%s）', type(exc).__name__)
        return jsonify(error='操作失败，请检查配置或稍后重试'), 500

    def body():
        value = request.get_json()
        if not isinstance(value, dict):
            raise ValueError('请求内容必须是 JSON 对象')
        return value

    @app.get('/')
    def index():
        return render_template('index.html')

    @app.get('/api/session')
    def session_state():
        return jsonify(authenticated=not admin_token or session.get('authenticated') == token_fingerprint,
                       requires_auth=bool(admin_token))

    @app.post('/api/auth')
    def auth():
        if admin_token and not hmac.compare_digest(str(body().get('token', '')), admin_token):
            return jsonify(error='管理口令不正确'), 401
        session.clear()
        session['authenticated'] = token_fingerprint
        session.permanent = True
        return jsonify(ok=True)

    @app.post('/api/logout')
    def logout():
        session.clear()
        return jsonify(ok=True)

    def public_email():
        settings = dict(store.settings('email', email_settings()))
        settings['has_password'] = bool(settings.pop('password', ''))
        return settings

    @app.get('/api/overview')
    def overview():
        schedule = store.settings('schedule', schedule_defaults())
        return jsonify(accounts=store.accounts(), runs=store.runs(),
                       schedule=schedule, next_run=jobs.next_run(), statistics=store.statistics(schedule['timezone']),
                       email=public_email(),
                       notifications=public_notifications(store.settings('notifications', notification_settings())))

    @app.patch('/api/accounts/<account_id>')
    def update_account(account_id):
        if jobs.is_active(account_id):
            raise ValueError('请先停止该账户的任务，再修改设置')
        return jsonify(store.update(account_id, body()))

    @app.delete('/api/accounts/<account_id>')
    def delete_account(account_id):
        if jobs.is_active(account_id):
            raise ValueError('请先停止该账户的任务，再移除账户')
        store.delete(account_id)
        return jsonify(ok=True)

    @app.post('/api/accounts/import')
    def import_account():
        values = body()
        return jsonify(store.import_curl(str(values.get('curl', '')), name=values.get('name', ''),
                                         read_num=values.get('read_num'), email=values.get('email', '')))

    @app.post('/api/accounts/<account_id>/run')
    def run_account(account_id):
        return jsonify(run_id=jobs.run_one(account_id)), 202

    @app.post('/api/accounts/<account_id>/stop')
    def stop_account(account_id):
        jobs.cancel(account_id)
        return jsonify(ok=True)

    @app.post('/api/run-all')
    def run_all():
        return jsonify(jobs.run_all()), 202

    @app.post('/api/login')
    def start_login():
        values = body()
        if values.get('account_id') and jobs.is_active(values['account_id']):
            raise ValueError('请先停止该账户的任务，再重新扫码')
        if values.get('read_num') is not None:
            values['read_num'] = validate_count(values['read_num'])
        values['email'] = validate_email(values.get('email', ''))
        return jsonify(session_id=logins.start(values)), 202

    @app.get('/api/login/<session_id>')
    def login_state(session_id):
        return jsonify(logins.state(session_id))

    @app.delete('/api/login/<session_id>')
    def cancel_login(session_id):
        logins.cancel(session_id)
        return jsonify(ok=True)

    @app.put('/api/settings/schedule')
    def update_schedule():
        values = body()
        settings = {key: values[key] for key in ('enabled', 'cron', 'timezone') if key in values}
        if len(settings) != 3:
            raise ValueError('请完整填写定时规则和时区')
        schedule_trigger(settings)
        jobs.apply_schedule(settings)
        store.set_settings('schedule', settings)
        return jsonify(ok=True)

    def prepared_email(values):
        settings = {**store.settings('email', email_settings()), **{
            key: values[key] for key in ('enabled', 'host', 'port', 'security', 'username', 'sender', 'recipient') if key in values}}
        if values.get('password'):
            settings['password'] = str(values['password'])
        if not isinstance(settings.get('enabled'), bool):
            raise ValueError('邮件开关必须为 true 或 false')
        if settings.get('enabled'):
            settings = validate_email_settings(settings, require_recipient=False)
        return settings

    @app.put('/api/settings/email')
    def update_email():
        store.set_settings('email', prepared_email(body()))
        return jsonify(ok=True)

    @app.post('/api/settings/email/test')
    def test_email():
        settings = prepared_email(body())
        try:
            send_email('这是一封来自微信读书控制台的测试邮件。\n收到此邮件代表 SMTP 通知通道可用。',
                       settings, subject='微信读书 · 通知测试')
        except ValueError:
            raise
        except Exception:
            return jsonify(error='测试邮件未发送成功，请检查 SMTP 服务器、授权码和收件邮箱'), 502
        return jsonify(ok=True)

    def prepared_notification(values):
        channel = values.get('channel')
        if channel not in notification_settings():
            raise ValueError('通知渠道无效')
        current = store.settings('notifications', notification_settings())
        settings = prepare_notification(channel, values.get('settings'), current.get(channel))
        return channel, current, settings

    @app.put('/api/settings/notifications')
    def update_notifications():
        channel, current, settings = prepared_notification(body())
        current[channel] = settings
        store.set_settings('notifications', current)
        return jsonify(ok=True)

    @app.post('/api/settings/notifications/test')
    def test_notification():
        channel, _, settings = prepared_notification(body())
        try:
            send_channel(channel, settings, '这是来自阅伴控制台的测试通知。', test=True)
        except ValueError:
            raise
        except Exception:
            return jsonify(error='测试请求未成功，请检查该渠道密钥、接收对象和网络'), 502
        return jsonify(ok=True, message='测试请求已接受，请检查接收端')

    return app


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    host = os.getenv('WEB_HOST') or '127.0.0.1'
    if host not in ('127.0.0.1', 'localhost', '::1') and not os.getenv('ADMIN_TOKEN'):
        raise SystemExit('对外监听需要设置 ADMIN_TOKEN 管理口令')
    store = AccountStore()
    store.seed_from_environment()
    store.recover_runs()
    app = create_app(store)
    manager, logins = app.extensions['jobs'], app.extensions['logins']
    manager.start_scheduler()
    def stop_server(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_server)
    try:
        serve(app, host=host, port=int(os.getenv('WEB_PORT') or 8080), threads=8)
    finally:
        logins.close()
        manager.shutdown()
