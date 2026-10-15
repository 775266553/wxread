import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from account_store import now
from push import email_settings
from notifications import CHANNEL_LABELS, notification_settings, send_notifications
from reader import AuthenticationError, Cancelled, ReadClient


def schedule_defaults():
    return {'enabled': True, 'cron': os.getenv('CRON_SCHEDULE') or '0 1 * * *',
            'timezone': os.getenv('TZ') or 'Asia/Shanghai'}


def schedule_trigger(settings):
    if not isinstance(settings.get('enabled'), bool):
        raise ValueError('定时开关必须为 true 或 false')
    return CronTrigger.from_crontab(str(settings['cron']), timezone=str(settings['timezone']))


class JobManager:
    def __init__(self, store, client_factory=ReadClient):
        self.store = store
        self.client_factory = client_factory
        self.lock = threading.RLock()
        self.active = {}
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='reading')
        self.scheduler = BackgroundScheduler()

    def start_scheduler(self):
        self.apply_schedule(self.store.settings('schedule', schedule_defaults()))
        self.scheduler.start()

    def apply_schedule(self, settings):
        trigger = schedule_trigger(settings)
        if settings['enabled']:
            self.scheduler.add_job(self.run_all, trigger, id='daily-reading', replace_existing=True,
                                   max_instances=1, coalesce=True, misfire_grace_time=300)
        elif self.scheduler.get_job('daily-reading'):
            self.scheduler.remove_job('daily-reading')

    def next_run(self):
        job = self.scheduler.get_job('daily-reading')
        date = getattr(job, 'next_run_time', None)
        return date.isoformat() if date else None

    def run_one(self, account_id):
        with self.lock:
            if account_id in self.active:
                raise ValueError('这个账户已有任务在运行或排队')
            account = self.store.account(account_id, private=True)
            if account['status'] == 'needs_login':
                raise ValueError('请先重新扫码绑定这个账户')
            cancel = threading.Event()
            run_id = self.store.new_run(account)
            self.active[account_id] = {'cancel': cancel, 'run_id': run_id}
            self.executor.submit(self._execute, account, run_id, cancel)
            return run_id

    def run_all(self):
        started, skipped = [], []
        for account in self.store.accounts():
            if not account['enabled']:
                continue
            try:
                started.append(self.run_one(account['id']))
            except (ValueError, KeyError) as exc:
                skipped.append({'account_id': account['id'], 'message': str(exc)})
        return {'started': started, 'skipped': skipped}

    def _execute(self, account, run_id, cancel):
        completed = 0

        def progress(count, message):
            nonlocal completed
            completed = count
            self.store.update_run(run_id, completed=count, message=message)

        status, message = 'failed', '任务执行失败'
        try:
            if cancel.is_set():
                raise Cancelled('任务已停止')
            # A queued account may have refreshed credentials before its turn.
            # Read them just before creating its own isolated HTTP session.
            account = self.store.account(account['id'], private=True)
            self.store.update_run(run_id, status='running', message='正在准备阅读')
            client = self.client_factory(account, on_cookies=lambda cookies: self.store.save_cookies(account['id'], cookies))
            completed = client.run(account['read_num'], progress, cancel)
            status, message = 'success', f'任务完成，记录时长 {completed * 0.5:g} 分钟'
            self.store.set_status(account['id'], 'ready')
        except Cancelled:
            status, message = 'cancelled', '任务已停止'
        except AuthenticationError as exc:
            message = str(exc)
            self.store.set_status(account['id'], 'needs_login')
        except Exception as exc:
            message = str(exc) if type(exc) is RuntimeError else '任务执行失败，请检查网络与账户配置'
            logging.error('账户 %s 阅读任务失败（%s）', account['name'], type(exc).__name__)
        finally:
            self.store.update_run(run_id, status=status, completed=completed, message=message, finished_at=now())
            with self.lock:
                self.active.pop(account['id'], None)
        if status in ('success', 'failed'):
            content = f"账户：{account['name']}\n结果：{message}\n完成次数：{completed}/{account['read_num']}\n时间：{now()}"
            result = send_notifications(content, email_config=self.store.settings('email', email_settings()),
                                        channels=self.store.settings('notifications', notification_settings()),
                                        is_success=status == 'success', recipient=account['email'] or None,
                                        context={'account_id':account['id'], 'account_name':account['name'],
                                                 'run_id':run_id, 'status':status, 'completed':completed, 'target':account['read_num']})
            if result['failed']:
                failed = '、'.join(CHANNEL_LABELS[channel] for channel in result['failed'])
                self.store.update_run(run_id, message=message + '；通知未送达：' + failed)

    def cancel(self, account_id):
        with self.lock:
            entry = self.active.get(account_id)
            if not entry:
                raise ValueError('这个账户没有正在执行的任务')
            entry['cancel'].set()

    def is_active(self, account_id):
        with self.lock:
            return account_id in self.active

    def shutdown(self, wait=True):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        with self.lock:
            for entry in self.active.values():
                entry['cancel'].set()
        self.executor.shutdown(wait=wait)
