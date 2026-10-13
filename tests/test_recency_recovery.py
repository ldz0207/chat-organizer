"""Transport reconnect, retained activity and uncertain-write regression tests."""
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'codex_recency'))
import watcher
from app_client import AppClient
from native_rpc import Rpc
from interface_errors import InterfaceUnavailable, RequestTimedOut

GROUP = {'native-a': {'native_section_id': 'native-a', 'sidebar_section_id': 'sidebar-a', 'name': 'Example'}}
KEY = 'codex:thread:local:one'
OTHER = 'codex:thread:local:other'


class TransportTests(unittest.TestCase):
    def client(self, kind=AppClient):
        client = kind.__new__(kind)
        client.next_id = 0
        client.queue = queue.Queue()
        client.errors = []
        client.on_wait = None
        client.request_timeout = 90
        client.send = lambda message: None
        client.process = SimpleNamespace(poll=lambda: None)
        return client

    def test_windows_invalid_pipe_after_child_exit_is_transient(self):
        for kind in (AppClient, Rpc):
            with self.subTest(kind=kind.__name__):
                client = self.client(kind)
                client.process = SimpleNamespace(poll=lambda: 1)
                def send(message): raise OSError(22, 'Invalid argument')
                client.send = send
                with self.assertRaises(InterfaceUnavailable):
                    client.call('tools/list', {})

    def test_live_child_os_error_and_permission_denial_are_not_retried(self):
        for error in (OSError(22, 'Invalid argument'), PermissionError(13, 'Access denied')):
            client = self.client()
            def send(message): raise error
            client.send = send
            with self.assertRaises(OSError) as caught:
                client.call('tools/list', {})
            self.assertNotIsInstance(caught.exception, InterfaceUnavailable)

    def test_app_timeout_is_explicit_and_typed(self):
        client = self.client()
        with self.assertRaisesRegex(RequestTimedOut, 'app tools/call:list_threads.*0.001s'):
            client.call('tools/call', {'name': 'list_threads'}, timeout=.001)

    def test_metadata_timeout_is_explicit_and_typed(self):
        with self.assertRaisesRegex(RequestTimedOut, 'metadata thread/list'):
            self.client(Rpc).call('thread/list', {}, timeout=.001)

    def test_response_after_old_25_second_cutoff_is_accepted(self):
        client = self.client()
        callbacks = []
        client.on_wait = callbacks.append
        class Responses:
            calls = 0
            def get(self, timeout):
                self.calls += 1
                if self.calls == 1:
                    raise queue.Empty()
                return {'id': 1, 'result': {'ok': True}}
        client.queue = Responses()
        with patch('app_client.time.monotonic', side_effect=[0, 0, 28]):
            self.assertEqual(client.call('tools/call', {'name': 'list_threads'}), {'ok': True})
        self.assertEqual(callbacks, ['tools/call:list_threads'])

    def test_eof_is_transient_but_permission_error_is_not(self):
        client = self.client()
        client.queue.put({'eof': True})
        with self.assertRaises(InterfaceUnavailable):
            client.call('tools/list', {})
        client.queue.put({'id': 2, 'error': {'code': -32000, 'message': 'permission denied'}})
        with self.assertRaisesRegex(RuntimeError, 'permission denied') as caught:
            client.call('tools/list', {})
        self.assertNotIsInstance(caught.exception, InterfaceUnavailable)

    def test_mcp_request_timeout_is_transient(self):
        client = self.client()
        client.queue.put({'id': 1, 'error': {'code': -32001, 'message': 'timed out'}})
        with self.assertRaises(RequestTimedOut):
            client.call('tools/list', {})


class RecoveryTests(unittest.TestCase):
    def test_idle_health_only_initializes_transport_without_listing_conversations(self):
        events = []
        class App:
            def __enter__(self): events.append('opened'); return self
            def __exit__(self, *args): events.append('closed')
            def tool(self, name, arguments): raise AssertionError('Idle health must not list conversations')
        self.assertEqual(list(watcher.app_cycle(App, GROUP, [])), [])
        self.assertEqual(events, ['opened', 'closed'])

    def exercise(self, directory, failures=1, applied=None, permission=False, gate=False):
        state = Path(directory)
        watcher.save(state / 'last-recency.json', {'one': 10})
        watcher.save(state / 'watcher.json', {'moves': 0, 'updated_at': 10})
        config = {'backend_version': 'test', 'state_dir': directory, 'groups': list(GROUP.values()),
                  'interval_seconds': 3, 'app_health_seconds': 60}
        clock, scans, opened, writes, active = [100.0], [], [], [], []
        keys = [OTHER, KEY]
        class Metadata:
            def __init__(self, binary): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def call(self, method, arguments):
                if arguments.get('archived'):
                    return {'data': []}
                scans.append(1)
                stamp = 12 if gate and len(scans) >= 6 else 11
                return {'data': [{'id': 'one', 'recencyAt': stamp, 'section': {'id': 'native-a'}}]}
        class App:
            def __enter__(self):
                opened.append(len(scans)); active.append(True); return self
            def __exit__(self, *args): active.pop()
            def tool(self, name, arguments):
                if permission:
                    raise RuntimeError('permission denied')
                if applied is None and len(opened) <= failures:
                    raise RequestTimedOut('app', 'tools/call:list_threads', 90)
                if name == 'move_thread_to_sidebar_section':
                    writes.append(1)
                    if applied is not False:
                        keys[:] = [KEY, OTHER]
                    if applied is not None:
                        raise RequestTimedOut('app', 'tools/call:move_thread_to_sidebar_section', 90)
                    return {}
                return {'sections': [{'sectionId': 'sidebar-a', 'name': 'Example', 'itemKeys': keys[:]}]}
        def sleep(seconds):
            self.assertFalse(active, 'MCP process must close before reconnect wait')
            clock[0] += seconds
            if len(scans) >= (7 if gate else 3):
                (state / 'stop-request.json').write_text('{}')
        with patch.dict(os.environ, {'CODEX_VERSION': 'test', 'CODEX_THREAD_ID': 'synthetic', 'CODEX_CLI_PATH': 'synthetic'}), \
             patch.object(watcher.time, 'sleep', sleep), patch.object(watcher.time, 'monotonic', lambda: clock[0]), \
             patch.object(watcher.time, 'time', lambda: clock[0]):
            watcher.run(config, Metadata, App)
        return {'state': state, 'opened': opened, 'scans': scans, 'writes': writes}

    def test_timeout_reconnects_without_losing_activity_or_creating_writer(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.exercise(temporary)
            state = result['state']
            self.assertEqual(result['opened'][:2], [1, 2])
            self.assertEqual(len(result['writes']), 1)
            self.assertEqual(json.loads((state / 'last-recency.json').read_text()), {'one': 11})
            self.assertEqual(json.loads((state / 'watcher.json').read_text())['moves'], 1)
            error = json.loads((state / 'interface-errors.jsonl').read_text())
            self.assertEqual(error['error_type'], 'RequestTimedOut')

    def test_three_failures_wait_for_new_real_activity_then_recover(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.exercise(temporary, failures=3, gate=True)
            self.assertEqual(result['opened'][:4], [1, 2, 3, 6])
            self.assertEqual(len(result['writes']), 1)
            self.assertEqual(json.loads((result['state'] / 'last-recency.json').read_text()), {'one': 12})

    def test_applied_write_timeout_is_read_back_without_resending(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.exercise(temporary, applied=True)
            state = result['state']
            self.assertEqual(len(result['writes']), 1)
            receipt = json.loads((state / 'moves.jsonl').read_text())
            self.assertEqual(receipt['result'], 'moved-reconciled')
            self.assertFalse((state / 'pending-move.json').exists())
            self.assertEqual(json.loads((state / 'watcher.json').read_text())['moves'], 1)

    def test_unconfirmed_write_stops_instead_of_resending(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, 'readback did not confirm'):
                self.exercise(temporary, applied=False)
            state = Path(temporary)
            self.assertTrue((state / 'pending-move.json').exists())
            self.assertEqual(json.loads((state / 'watcher.json').read_text())['failure_kind'], 'safety')
            self.assertEqual(json.loads((state / 'last-recency.json').read_text()), {'one': 10})

    def test_permission_rejection_stops_without_automatic_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, 'permission denied'):
                self.exercise(temporary, permission=True)
            self.assertFalse((Path(temporary) / 'interface-errors.jsonl').exists())
            self.assertEqual(json.loads((Path(temporary) / 'watcher.json').read_text())['failure_kind'], 'safety')

    def test_manual_stop_interrupts_reconnect_wait(self):
        with tempfile.TemporaryDirectory() as temporary:
            stop = Path(temporary) / 'stop-request.json'
            def heartbeat(): stop.write_text('{}')
            with self.assertRaises(watcher.StopRequested), patch.object(watcher.time, 'sleep', lambda _: None):
                watcher.wait_for_reconnect(5, stop, heartbeat)


if __name__ == '__main__': unittest.main()
