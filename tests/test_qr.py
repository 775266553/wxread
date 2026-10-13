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
            self.assertIn({'status': 'refreshing', 'message': '二维码已失效，正在自动更新…', 'qr': None}, snapshots)
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


if __name__ == '__main__':
    unittest.main()
