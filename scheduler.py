"""Optional headless scheduler. Use web.py when you want the dashboard and QR login."""
import logging
import signal
import threading

from account_store import AccountStore
from jobs import JobManager


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    store = AccountStore()
    store.seed_from_environment()
    store.recover_runs()
    jobs = JobManager(store)
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    try:
        jobs.start_scheduler()
        logging.info('定时调度已启动。下次执行：%s', jobs.next_run())
        stopped.wait()
    finally:
        jobs.shutdown()
