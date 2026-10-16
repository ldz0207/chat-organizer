"""Native MCP tool-result failures must retain transport/safety semantics."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'codex_recency'))
from app_client import AppClient
from interface_errors import InterfaceUnavailable


class ToolErrorTests(unittest.TestCase):
    def invoke(self, content, name='list_threads'):
        client = AppClient.__new__(AppClient)
        client.thread_id = 'synthetic-caller'
        client.call = lambda method, params: {'isError': True, 'content': content}
        return client.tool(name, {'limit': 1})

    def test_native_wrapped_timeout_remains_reconnectable(self):
        with self.assertRaisesRegex(InterfaceUnavailable, 'app tools/call:list_threads'):
            self.invoke([{'type': 'text', 'text': 'Timed out waiting for MCP response to thread/list'}])

    def test_uncertain_move_timeout_is_reconnectable_for_readback(self):
        with self.assertRaises(InterfaceUnavailable):
            self.invoke([{'type': 'text', 'text': 'Timed out waiting for MCP response to thread/move'}],
                        'move_thread_to_sidebar_section')

    def test_permission_and_unknown_errors_keep_safety_stop(self):
        for message in ('permission denied', 'Approved group identity changed',
                        'Timed out waiting for MCP response to thread/list; permission denied',
                        'Timed out waiting for MCP response to thread/list\npermission denied',
                        'unknown native failure'):
            with self.subTest(message=message):
                with self.assertRaises(RuntimeError) as caught:
                    self.invoke([{'type': 'text', 'text': message}])
                self.assertNotIsInstance(caught.exception, InterfaceUnavailable)

    def test_mixed_error_content_is_not_assumed_transient(self):
        with self.assertRaises(RuntimeError) as caught:
            self.invoke([{'type': 'text', 'text': 'Timed out waiting for MCP response to thread/list'},
                         {'type': 'text', 'text': 'permission denied'}])
        self.assertNotIsInstance(caught.exception, InterfaceUnavailable)


if __name__ == '__main__':
    unittest.main()
