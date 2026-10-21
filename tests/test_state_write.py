"""Windows sharing conflicts on the actual checkpoint save path."""
import ctypes
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'codex_recency'))
import watcher


class StateWriteTests(unittest.TestCase):
    def test_unrelated_permission_error_is_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'state.json'
            with patch.object(watcher.os, 'replace', side_effect=PermissionError('denied')), \
                 patch.object(watcher.time, 'sleep') as sleep:
                with self.assertRaises(PermissionError):
                    watcher.save(target, {'sequence': 1})
                sleep.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'Windows file sharing semantics')
    def test_reader_without_delete_share_releases_during_save(self):
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                       ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'state.json'
            watcher.save(target, {'sequence': 0})
            # An ordinary reader permits reading/writing, but not rename/delete.
            handle = kernel.CreateFileW(str(target), 0x80000000, 1 | 2, None, 3, 0x80, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            release = threading.Timer(.15, lambda: kernel.CloseHandle(handle))
            release.start()
            try:
                watcher.save(target, {'sequence': 1})
            finally:
                release.join()
            self.assertEqual(json.loads(target.read_text(encoding='utf-8')), {'sequence': 1})

    @unittest.skipUnless(os.name == 'nt', 'Windows file sharing semantics')
    def test_persistent_access_denial_is_bounded_and_preserves_old_json(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'state.json'
            watcher.save(target, {'sequence': 0})
            error = PermissionError('persistent access denied')
            error.winerror = 5
            with patch.object(watcher.os, 'replace', side_effect=error) as replace, \
                 patch.object(watcher.time, 'sleep') as sleep:
                with self.assertRaises(PermissionError):
                    watcher.save(target, {'sequence': 1})
                self.assertLessEqual(replace.call_count, 8)
                self.assertLessEqual(sum(call.args[0] for call in sleep.call_args_list), 1)
            self.assertEqual(json.loads(target.read_text(encoding='utf-8')), {'sequence': 0})


if __name__ == '__main__':
    unittest.main()
