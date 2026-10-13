"""Run every enabled account once (local CLI / GitHub Actions)."""
import logging
import sys

from account_store import AccountStore
from jobs import JobManager


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    store = AccountStore()
    store.seed_from_environment()
    if not store.accounts():
        logging.error('没有已绑定账户。请运行 python web.py 扫码绑定，或配置 WXREAD_ACCOUNTS_JSON / WXREAD_CURL_BASH。')
        return 1
    manager = JobManager(store)
    result = manager.run_all()
    manager.executor.shutdown(wait=True)
    run_ids = set(result['started'])
    runs = [run for run in store.runs(limit=10000) if run['id'] in run_ids]
    for run in runs:
        logging.info('%s：%s', run['account_name'], run['message'])
    return 1 if any(run['status'] != 'success' for run in runs) or result['skipped'] else 0


if __name__ == '__main__':
    sys.exit(main())
