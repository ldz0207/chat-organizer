"""Synthetic tests: activity semantics, native readback and lazy process lifetime."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "codex_recency"))
import watcher

GROUPS = {"native-a": {"native_section_id": "native-a", "sidebar_section_id": "sidebar-a", "name": "Example"}}


def row(identifier="one", stamp=10, group="native-a"):
    return {"id": identifier, "recencyAt": stamp, "section": {"id": group}}


class FakeApp:
    def __init__(self, keys=None, changed=None):
        self.keys = keys or ["codex:thread:local:other", "codex:thread:local:one"]
        self.changed = changed
        self.writes = 0

    def tool(self, name, arguments):
        if name == "move_thread_to_sidebar_section":
            self.writes += 1
            key = "codex:thread:local:" + arguments["threadId"]
            self.keys = [key] + [k for k in self.keys if k != key]
            if self.changed:
                self.keys = self.changed
            return {}
        return {"sections": [{"sectionId": "sidebar-a", "name": "Example", "itemKeys": self.keys[:]}]}


class ActivityTests(unittest.TestCase):
    def test_initial_only_newest_per_group(self):
        result = watcher.select_changes([row("old", 1), row("new", 2), row("second", 3, "b")], {}, initial=True)
        self.assertEqual([r["id"] for r in result], ["new", "second"])

    def test_unchanged_and_clock_regression_do_not_move(self):
        self.assertEqual(watcher.select_changes([row(stamp=10)], {"one": 10}), [])
        self.assertEqual(watcher.select_changes([row(stamp=9)], {"one": 10}), [])

    def test_later_user_activity_moves(self):
        self.assertEqual(watcher.select_changes([row(stamp=11)], {"one": 10}), [row(stamp=11)])

    def test_batch_oldest_first_so_latest_finishes_top(self):
        result = watcher.select_changes([row("new", 12), row("old", 11)], {"new": 10, "old": 10})
        self.assertEqual([r["id"] for r in result], ["old", "new"])

    def test_old_manually_grouped_member_is_not_new_activity(self):
        self.assertEqual(watcher.select_changes([row(stamp=8)], {}, since=10), [])

    def test_fresh_new_member_is_activity(self):
        self.assertEqual(watcher.select_changes([row(stamp=11)], {}, since=10), [row(stamp=11)])

    def test_missing_recency_is_not_activity(self):
        self.assertEqual(watcher.select_changes([row(stamp=None)], {}, initial=True), [])

    def test_response_discards_content_and_handles_pagination(self):
        calls = []
        class Rpc:
            def call(self, method, arguments):
                calls.append((method, arguments))
                item = dict(row(), preview="PRIVATE SYNTHETIC TEXT", cwd="PRIVATE PATH", name="PRIVATE TITLE")
                return {"data": [item], "nextCursor": "next" if len(calls) == 1 else None}
        result = watcher.rows(Rpc(), GROUPS)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][1]["cursor"], "next")
        self.assertEqual(set(result[0]), {"id", "section", "recencyAt"})
        self.assertNotIn("PRIVATE", json.dumps(result))


class MoveTests(unittest.TestCase):
    def test_same_group_move_preserves_other_member_order(self):
        app = FakeApp()
        receipt = watcher.move(app, row(), GROUPS)
        self.assertEqual(receipt["result"], "moved")
        self.assertEqual(receipt["before_index"], 1)
        self.assertEqual(app.writes, 1)

    def test_already_first_no_write(self):
        app = FakeApp(["codex:thread:local:one", "codex:thread:local:other"])
        self.assertEqual(watcher.move(app, row(), GROUPS)["result"], "already-first")
        self.assertEqual(app.writes, 0)

    def test_no_cross_group_move_when_membership_changed(self):
        app = FakeApp(["codex:thread:local:other"])
        self.assertEqual(watcher.move(app, row(), GROUPS)["result"], "skipped-not-current-member")
        self.assertEqual(app.writes, 0)

    def test_group_rename_stops(self):
        snapshot = {"sections": [{"sectionId": "sidebar-a", "name": "Renamed", "itemKeys": []}]}
        with self.assertRaisesRegex(RuntimeError, "identity changed"):
            watcher.validate_groups(snapshot, GROUPS)

    def test_membership_drift_stops(self):
        with self.assertRaisesRegex(RuntimeError, "membership changed"):
            watcher.move(FakeApp(changed=["codex:thread:local:one"]), row(), GROUPS)

    def test_other_order_drift_stops(self):
        app = FakeApp(["codex:thread:local:a", "codex:thread:local:b", "codex:thread:local:one"],
                      ["codex:thread:local:one", "codex:thread:local:b", "codex:thread:local:a"])
        with self.assertRaisesRegex(RuntimeError, "relative order changed"):
            watcher.move(app, row(), GROUPS)

    def test_readback_did_not_promote_stops(self):
        with self.assertRaisesRegex(RuntimeError, "did not confirm"):
            watcher.move(FakeApp(changed=["codex:thread:local:other", "codex:thread:local:one"]), row(), GROUPS)


class LifecycleTests(unittest.TestCase):
    def test_successful_receipt_is_available_before_later_batch_failure(self):
        class App(FakeApp):
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def tool(self, name, arguments):
                if name == "move_thread_to_sidebar_section" and arguments["threadId"] == "other":
                    raise RuntimeError("synthetic denied")
                return super().tool(name, arguments)
        receipts = watcher.app_cycle(App, GROUPS, [row("one", 11), row("other", 12)])
        self.assertEqual(next(receipts)["result"], "moved")
        with self.assertRaisesRegex(RuntimeError, "synthetic denied"):
            next(receipts)

    def test_app_client_not_alive_during_idle_scans_or_sleep(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            watcher.save(state / "last-recency.json", {"one": 10})
            watcher.save(state / "watcher.json", {"moves": 0, "updated_at": 10})
            config = {"backend_version": "test", "state_dir": temporary, "groups": list(GROUPS.values()),
                      "interval_seconds": 3, "app_health_seconds": 60}
            opened, closed, scans, active = [], [], [], []
            class Rpc:
                def __init__(self, binary): pass
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def call(self, method, arguments):
                    scans.append(1)
                    return {"data": [row(stamp=11 if len(scans) >= 3 else 10)], "nextCursor": None}
            class App(FakeApp):
                def __enter__(self): opened.append(1); active.append(1); return self
                def __exit__(self, *args): closed.append(1); active.pop()
            def sleep(seconds):
                self.assertFalse(active, "App-tools must exit before polling sleep")
                if len(scans) == 4:
                    (state / "stop-request.json").write_text("{}")
            with patch.dict(os.environ, {"CODEX_VERSION": "test", "CODEX_THREAD_ID": "synthetic-caller", "CODEX_CLI_PATH": "synthetic-cli"}), patch.object(watcher.time, "sleep", sleep):
                watcher.run(config, Rpc, App)
            self.assertEqual(len(opened), 2)  # startup health, then changed batch
            self.assertEqual(opened, closed)
            self.assertEqual(len(scans), 4)
            receipt = json.loads((state / "moves.jsonl").read_text())
            self.assertEqual(receipt["recency_at"], 11)
            self.assertEqual(json.loads((state / "watcher.json").read_text())["moves"], 1)

    def test_duplicate_os_lock_does_not_overwrite_existing_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            checkpoint = state / "watcher.json"
            watcher.save(checkpoint, {"status": "running", "moves": 3})
            before = checkpoint.read_bytes()
            config = {"backend_version": "test", "state_dir": temporary, "groups": list(GROUPS.values())}
            with watcher.worker_guard(state / "worker.lock"), patch.dict(os.environ, {"CODEX_VERSION": "test"}):
                with self.assertRaises(watcher.WorkerBusy):
                    watcher.run(config)
            self.assertEqual(checkpoint.read_bytes(), before)

    def test_backend_version_change_prevents_transport_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"CODEX_VERSION": "new"}):
                with self.assertRaisesRegex(RuntimeError, "Untested Codex version"):
                    watcher.run({"backend_version": "old", "state_dir": temporary, "groups": list(GROUPS.values())})


if __name__ == "__main__":
    unittest.main()
