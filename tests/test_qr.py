import base64
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, Mock, patch

from account_store import AccountStore
from qr_login import QRLoginManager, QR_READY_SCRIPT


class QRCase(unittest.TestCase):
    def test_expired_svg_refreshes_in_same_browser_and_unchanged_qr_is_not_recaptured(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = QRLoginManager(AccountStore(directory))
            cancel = Mock(is_set=Mock(return_value=False), wait=Mock(return_value=False))
            manager.sessions['test'] = {'status': 'starting', 'qr': None, 'cancel': cancel}
            iteration = -1
            page, context = MagicMock(), MagicMock()
            wrapper, image, modal, overlay = MagicMock(), MagicMock(), MagicMock(), MagicMock()
            page.locator.side_effect = lambda selector: {
                '.wr_login_modal_qr_wrapper': Mock(first=wrapper),
                '.wr_login_modal_wrapper': modal,
                '.wr_login_modal_qr_overlay': Mock(first=overlay),
            }[selector]
            wrapper.locator.return_value.first = image
            wrapper.is_visible.return_value = True
            image.is_visible.return_value = True
            overlay.is_visible.side_effect = lambda: iteration == 2
            modal.is_visible.side_effect = lambda: iteration < 3
            page.evaluate.side_effect = lambda script: True if script == QR_READY_SCRIPT else 'test-agent'
            image.evaluate.side_effect = lambda _: '<path old/>' if iteration < 2 else '<path new/>'
            wrapper.screenshot.side_effect = lambda **_: b'old-png' if iteration < 2 else b'new-png'

            def cookies(_):
                nonlocal iteration
                iteration += 1
                return [] if iteration < 3 else [
                    {'name': 'wr_vid', 'value': 'test-account'},
                    {'name': 'wr_skey', 'value': 'test-cookie'},
                ]

            context.cookies.side_effect = cookies
            context.new_page.return_value = page
            playwright = MagicMock()
            browser = playwright.chromium.launch.return_value.__enter__.return_value
            browser.new_context.return_value = context
            factory = MagicMock()
            factory.return_value.__enter__.return_value = playwright
            snapshots = []
            original_update = manager.update

            def update(session, **values):
                snapshots.append(values)
                original_update(session, **values)

            with patch('playwright.sync_api.sync_playwright', factory), patch.object(manager, 'update', update):
                manager._run('test', {})

            waiting = [s for s in snapshots if s['status'] == 'waiting']
            self.assertEqual([s['qr_version'] for s in waiting], [1, 2])
            self.assertEqual([s['qr'] for s in waiting], [
                'data:image/png;base64,' + base64.b64encode(b'old-png').decode(),
                'data:image/png;base64,' + base64.b64encode(b'new-png').decode(),
            ])
            self.assertIn({'status': 'refreshing', 'message': '正在刷新二维码…', 'qr': None}, snapshots)
            self.assertEqual(manager.state('test')['status'], 'success')
            self.assertEqual(len(manager.store.accounts()), 1)
            overlay.click.assert_called_once()
            playwright.chromium.launch.assert_called_once()
            self.assertEqual(wrapper.screenshot.call_count, 2)
            page.wait_for_function.assert_not_called()

    def test_refreshing_session_counts_towards_active_limit(self):
        manager = QRLoginManager(Mock())
        import time
        manager.sessions = {str(i): {'created': time.time(), 'status': 'refreshing', 'cancel': threading.Event()}
                            for i in range(2)}
        with self.assertRaises(ValueError):
            manager.start()

    def check_refresh_recovery(self, manual=False, during_capture=False, cancel_during_capture=False):
        with tempfile.TemporaryDirectory() as directory:
            manager = QRLoginManager(AccountStore(directory))
            cancel = Mock(is_set=Mock(return_value=False))
            manager.sessions['test'] = {'status': 'starting', 'qr': None, 'cancel': cancel,
                                        'refresh': threading.Event(), 'refresh_id': None}
            page, context, wrapper, image, modal, overlay = [MagicMock() for _ in range(6)]
            page.locator.side_effect = lambda selector: {
                '.wr_login_modal_qr_wrapper': Mock(first=wrapper),
                '.wr_login_modal_wrapper': modal,
                '.wr_login_modal_qr_overlay': Mock(first=overlay),
            }[selector]
            wrapper.locator.return_value.first = image
            wrapper.is_visible.return_value = image.is_visible.return_value = True
            iteration = -1
            navigated = lambda: page.goto.call_count > 1
            overlay.is_visible.side_effect = lambda: not manual and iteration >= 1 and not navigated()
            # Simulate a visible expiry overlay whose click does nothing.
            overlay.click.return_value = None
            image.evaluate.side_effect = lambda _: 'new-svg' if navigated() else 'old-svg'
            captured = []

            def screenshot(**_):
                if not captured and during_capture:
                    manager.refresh('test', 'manual-request')
                if not captured and cancel_during_capture:
                    manager.cancel('test')
                    cancel.is_set.return_value = True
                captured.append(navigated())
                return b'new-png' if navigated() else b'old-png'

            wrapper.screenshot.side_effect = screenshot
            modal.is_visible.side_effect = lambda: iteration < 6
            page.evaluate.side_effect = lambda script: True if script == QR_READY_SCRIPT else 'test-agent'

            def cookies(_):
                nonlocal iteration
                iteration += 1
                return [] if iteration < 6 else [{'name': 'wr_vid', 'value': 'third'},
                                                  {'name': 'wr_skey', 'value': 'third-fixture'}]

            def wait(_):
                if manual and not during_capture and iteration == 0:
                    manager.refresh('test', 'manual-request')
                return False

            context.cookies.side_effect = cookies
            cancel.wait.side_effect = wait
            context.new_page.return_value = page
            playwright, factory = MagicMock(), MagicMock()
            browser = playwright.chromium.launch.return_value.__enter__.return_value
            browser.new_context.return_value = context
            factory.return_value.__enter__.return_value = playwright
            updates = []
            original = manager.update

            def update(session, **values):
                updates.append(values)
                original(session, **values)
                if values['status'] == 'waiting' and manager.state(session).get('refresh_id') == 'manual-request':
                    self.assertEqual(values['qr'], 'data:image/png;base64,' + base64.b64encode(b'new-png').decode(),
                                     'only a newly captured QR may acknowledge a refresh request')

            with patch('playwright.sync_api.sync_playwright', factory), \
                 patch('qr_login.time.monotonic', side_effect=lambda: max(0, iteration) * 5), \
                 patch.object(manager, 'update', update):
                manager._run('test', {})
            if cancel_during_capture:
                self.assertEqual(manager.state('test')['status'], 'cancelled')
                self.assertEqual(manager.store.accounts(), [])
                self.assertFalse(any(item['status'] == 'waiting' for item in updates), 'a cancelled capture must never be published')
                return
            self.assertEqual(manager.state('test')['status'], 'success')
            self.assertEqual([item['qr_version'] for item in updates if item['status'] == 'waiting'], [1] if during_capture else [1, 2])
            self.assertEqual(wrapper.screenshot.call_count, 2, 'do not recapture an old code after expiry')
            self.assertEqual(page.goto.call_count, 2, 'recover inside the existing isolated context')
            browser.new_context.assert_called_once()
            playwright.chromium.launch.assert_called_once()
            if manual:
                self.assertEqual(manager.state('test')['refresh_id'], 'manual-request')
            else:
                overlay.click.assert_called_once()

    def test_stuck_expiry_overlay_reopens_login_without_starting_another_browser(self):
        self.check_refresh_recovery()

    def test_manual_refresh_invalidates_old_image_and_acknowledges_request(self):
        self.check_refresh_recovery(manual=True)

    def test_manual_refresh_arriving_during_capture_cannot_acknowledge_the_old_qr(self):
        self.check_refresh_recovery(manual=True, during_capture=True)

    def test_cancel_arriving_during_capture_cannot_reopen_the_closed_session(self):
        self.check_refresh_recovery(cancel_during_capture=True)

    def test_cancelled_windows_do_not_block_a_third_binding(self):
        manager = QRLoginManager(Mock())
        import time
        manager.sessions = {str(i): {'created': time.time(), 'status': 'waiting', 'cancel': threading.Event()}
                            for i in range(2)}
        manager.cancel('0')
        manager.cancel('1')
        with patch('qr_login.threading.Thread') as thread:
            manager.start()
        thread.return_value.start.assert_called_once()


if __name__ == '__main__':
    unittest.main()
