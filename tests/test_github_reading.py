import base64
import itertools
import os
import unittest
from unittest.mock import patch

from nacl.public import PrivateKey

from github_state import GitHubAPIError, GitHubState, bind_account
from github_worker import reading
from tests.test_github import MemoryAPI


class GitHubReadingCase(unittest.TestCase):
    def setUp(self):
        key = PrivateKey.generate()
        self.repository = GitHubState(MemoryAPI(key), base64.b64encode(bytes(key)).decode())
        self.repository.mutate(lambda state: bind_account(state, {
            'cookies': {'wr_vid': 'fixture-account', 'wr_skey': 'PRIVATE-COOKIE'}, 'headers': {}},
            {'name': '账户', 'read_num': 2}))

    def run_worker(self, failed_sync, terminal='success'):
        class Jobs:
            closed = False
            def __init__(self, local):
                self.local = local
                self.remaining_ticks = 2
            def run_all(self):
                account = self.local.accounts()[0]
                self.run = self.local.new_run(account)
                return {'started': [self.run], 'skipped': []}
            @property
            def active(self):
                self.remaining_ticks -= 1
                return self.remaining_ticks >= 0
            def shutdown(self):
                Jobs.closed = True
                self.local.update_run(self.run, status=terminal, completed=2 if terminal == 'success' else 0)

        original = self.repository.mutate
        attempts = 0
        def mutate(function):
            nonlocal attempts
            attempts += 1
            if attempts in failed_sync:
                raise GitHubAPIError('PUT', '/contents/state.enc.json', status=503, retryable=True)
            return original(function)
        clock = itertools.count(0, 61)
        environment = {'WXREAD_CONTROL_KEY': 'fixture', 'GITHUB_EVENT_NAME': 'schedule', 'GITHUB_RUN_ID': '123', 'READ_ACCOUNT_ID': ''}
        with patch.dict(os.environ, environment), patch('github_worker.GitHubState', return_value=self.repository), \
             patch('github_worker.JobManager', Jobs), patch('github_worker.signal.signal'), \
             patch('github_worker.time.sleep'), patch('github_worker.time.monotonic', side_effect=lambda: next(clock)), \
             patch.object(self.repository, 'mutate', side_effect=mutate):
            try:
                result = reading()
            finally:
                self.assertTrue(Jobs.closed, 'always finish or cancel the local jobs before cleanup')
        return result, attempts

    def test_temporary_progress_sync_failure_does_not_abort_reading_or_lose_final_results(self):
        with self.assertLogs(level='INFO') as logs:
            result, attempts = self.run_worker({1, 2})
        self.assertEqual(result, 0)
        self.assertEqual(attempts, 4)
        state, _ = self.repository.read()
        self.assertEqual(state['runs'][0]['status'], 'success')
        self.assertEqual(state['runs'][0]['completed'], 2)
        self.assertEqual(state['runs'][0]['workflow_run_id'], '123')
        self.assertNotIn('PRIVATE-COOKIE', '\n'.join(logs.output))
        self.assertIn('最终进度已保存', logs.output[-1])

    def test_failure_to_save_final_results_still_fails_the_workflow(self):
        with self.assertLogs(level='INFO') as logs:
            with self.assertRaises(GitHubAPIError):
                self.run_worker({4})
        self.assertTrue(any('1 个成功' in message for message in logs.output))
        self.assertFalse(any('最终进度已保存' in message for message in logs.output))

    def test_account_failure_is_not_hidden_by_successful_state_sync(self):
        with self.assertLogs(level='INFO'):
            result, _ = self.run_worker(set(), terminal='failed')
        self.assertEqual(result, 1)
        state, _ = self.repository.read()
        self.assertEqual(state['runs'][0]['status'], 'failed')
