import unittest
from unittest.mock import Mock, patch

import requests

from github_state import ConflictError, GitHubAPI


def response(status, value=None):
    result = Mock(status_code=status, content=b'json', headers={})
    result.json.return_value = value
    return result


class GitHubAPICase(unittest.TestCase):
    def setUp(self):
        self.api = GitHubAPI('test/repo', 'PRIVATE-TOKEN')

    @patch('github_state.time.sleep')
    @patch('github_state.requests.request')
    def test_read_recovers_from_network_and_server_failures(self, request, sleep):
        request.side_effect = [requests.Timeout('PRIVATE-URL'), response(503), response(200, {'ok': True})]
        self.assertEqual(self.api.request('GET', '/contents/state.enc.json'), {'ok': True})
        self.assertEqual(request.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    @patch('github_state.time.sleep')
    @patch('github_state.requests.request')
    def test_exhausted_reads_report_safe_operation_and_http_code(self, request, sleep):
        request.return_value = response(502, {'message': 'PRIVATE-RESPONSE'})
        with self.assertRaises(RuntimeError) as raised:
            self.api.request('GET', '/contents/state.enc.json')
        self.assertIn('GET', str(raised.exception))
        self.assertIn('502', str(raised.exception))
        self.assertIn('账户状态', str(raised.exception))
        self.assertNotIn('PRIVATE', str(raised.exception))
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(request.call_count, 3)

    @patch('github_state.requests.request')
    def test_authentication_failure_is_not_retried(self, request):
        request.return_value = response(401)
        with self.assertRaises(RuntimeError) as raised:
            self.api.request('GET', '/contents/state.enc.json')
        self.assertFalse(getattr(raised.exception, 'retryable', True))
        self.assertEqual(request.call_count, 1)

    @patch('github_state.requests.request')
    def test_missing_file_is_distinct_from_a_failed_read(self, request):
        request.return_value = response(404)
        self.assertEqual(self.api.get_file('requests/test.json'), (None, None))
        self.assertEqual(request.call_count, 1)

    @patch('github_state.requests.request')
    def test_accepted_write_with_lost_response_is_reconciled_without_another_write(self, request):
        value = {'ciphertext': 'fixture'}
        request.side_effect = requests.Timeout('PRIVATE-URL')
        with patch.object(self.api, 'get_file', return_value=(value, 'new-sha')) as read:
            self.api.put_file('state.enc.json', value, 'old-sha')
        self.assertEqual(request.call_count, 1, 'do not repeat an already committed update')
        read.assert_called_once_with('state.enc.json')

    @patch('github_state.time.sleep')
    @patch('github_state.requests.request')
    def test_uncommitted_write_retries_only_when_the_version_is_unchanged(self, request, sleep):
        request.side_effect = [response(503), response(201, {'ok': True})]
        with patch.object(self.api, 'get_file', return_value=({'ciphertext': 'previous'}, 'old-sha')):
            self.api.put_file('state.enc.json', {'ciphertext': 'new'}, 'old-sha')
        self.assertEqual(request.call_count, 2)

    @patch('github_state.requests.request')
    def test_retry_never_overwrites_another_committed_update(self, request):
        request.side_effect = requests.Timeout()
        with patch.object(self.api, 'get_file', return_value=({'ciphertext': 'concurrent'}, 'other-sha')):
            with self.assertRaises(ConflictError):
                self.api.put_file('state.enc.json', {'ciphertext': 'new'}, 'old-sha')
        self.assertEqual(request.call_count, 1)

    @patch('github_state.requests.request')
    def test_conflict_after_an_accepted_write_is_success(self, request):
        value = {'ciphertext': 'fixture'}
        request.return_value = response(409)
        with patch.object(self.api, 'get_file', return_value=(value, 'new-sha')):
            self.api.put_file('state.enc.json', value, 'old-sha')

    @patch('github_state.requests.request')
    def test_arbitrary_mutations_are_not_automatically_repeated(self, request):
        request.side_effect = requests.Timeout()
        with self.assertRaises(RuntimeError):
            self.api.request('POST', '/actions/workflows/deploy.yml/dispatches', {})
        self.assertEqual(request.call_count, 1)

    @patch('github_state.time.sleep')
    @patch('github_state.requests.request')
    def test_repeated_write_failures_are_bounded(self, request, sleep):
        request.return_value = response(503)
        with patch.object(self.api, 'get_file', return_value=({'ciphertext': 'old'}, 'old-sha')):
            with self.assertRaises(RuntimeError):
                self.api.put_file('state.enc.json', {'ciphertext': 'new'}, 'old-sha')
        self.assertEqual(request.call_count, 3)

    @patch('github_state.time.sleep')
    @patch('github_state.requests.request')
    def test_rate_limit_wait_is_honored_without_a_long_blocking_retry(self, request, sleep):
        limited = response(429)
        limited.headers = {'retry-after': '120'}
        request.return_value = limited
        with self.assertRaises(RuntimeError) as raised:
            self.api.request('GET', '/contents/state.enc.json')
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.retry_after, 120)
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()
