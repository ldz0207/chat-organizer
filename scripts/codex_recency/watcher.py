"""Opt-in, version-bound local Codex sidebar recency worker (standard library)."""
import argparse
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import sys
import time

from app_client import AppClient
from native_rpc import Rpc
from interface_errors import InterfaceUnavailable


class WorkerBusy(RuntimeError):
    pass


class StopRequested(RuntimeError):
    pass


def save(path, value):
    temporary = path.with_name(path.name + ".{}.tmp".format(os.getpid()))
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def load_config(path):
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if config.get("schema_version") != 1 or not config.get("backend_version"):
        raise ValueError("A version-bound configuration is required")
    groups = config.get("groups", [])
    if not groups or any(not all(g.get(k) for k in ("native_section_id", "sidebar_section_id", "name")) for g in groups):
        raise ValueError("Configure the explicitly approved existing groups")
    for key in ("native_section_id", "sidebar_section_id"):
        if len({g[key] for g in groups}) != len(groups):
            raise ValueError("Duplicate group identity")
    for key in ("app_tools_server", "state_dir"):
        if not Path(config[key]).is_absolute():
            raise ValueError("{} must be an absolute device-local path".format(key))
    interval = config.get("interval_seconds", 3)
    health = config.get("app_health_seconds", 60)
    if not 1 <= interval <= 60 or not interval <= health <= 300:
        raise ValueError("Polling/health intervals out of range")
    if not 30 <= config.get("app_request_timeout_seconds", 90) <= 120:
        raise ValueError("App request timeout must be between 30 and 120 seconds")
    return config


@contextmanager
def worker_guard(path):
    """One OS lock for this state directory; not a lock on other app clients."""
    with path.open("a+b") as handle:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise WorkerBusy("Another recency worker holds the lock") from error
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise WorkerBusy("Another recency worker holds the lock") from error
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def rows(rpc, groups):
    data, cursor = [], None
    while True:
        response = rpc.call("thread/list", {
            "limit": 200, "cursor": cursor, "sortKey": "recency_at",
            "sortDirection": "desc", "useStateDbOnly": True,
            "modelProviders": [], "archived": False,
        })
        # Discard previews, paths, titles and other unused response fields.
        data.extend({"id": t["id"], "section": {"id": t["section"]["id"]},
                     "recencyAt": t.get("recencyAt")} for t in response["data"]
                    if (t.get("section") or {}).get("id") in groups)
        cursor = response.get("nextCursor")
        if not cursor:
            return data


def archived_ids(rpc):
    identifiers, cursor = set(), None
    while True:
        response = rpc.call("thread/list", {
            "limit": 200, "cursor": cursor, "sortKey": "updated_at",
            "useStateDbOnly": True, "modelProviders": [], "archived": True,
        })
        identifiers.update(t["id"] for t in response["data"])
        cursor = response.get("nextCursor")
        if not cursor:
            return identifiers


def select_changes(current, previous, initial=False, since=None):
    changes = []
    if initial:
        newest = {}
        for row in current:
            section, stamp = row["section"]["id"], row.get("recencyAt")
            if stamp is not None and (section not in newest or stamp > newest[section]["recencyAt"]):
                newest[section] = row
        return sorted(newest.values(), key=lambda row: (row["recencyAt"], row["id"]))
    for row in current:
        stamp, old = row.get("recencyAt"), previous.get(row["id"])
        if stamp is not None and ((old is not None and stamp > old) or
                                  (old is None and since is not None and stamp >= int(since))):
            changes.append(row)
    return sorted(changes, key=lambda row: (row["recencyAt"], row["id"]))


def validate_groups(snapshot, groups):
    sections = {s["sectionId"]: s for s in snapshot["sections"]}
    for group in groups.values():
        section = sections.get(group["sidebar_section_id"])
        if section is None or section["name"] != group["name"]:
            raise RuntimeError("Approved group identity changed; paused")
    return sections


def confirm_order(keys, updated_keys, key):
    if set(updated_keys) != set(keys) or len(updated_keys) != len(keys):
        raise RuntimeError("Group membership changed during move; paused")
    if not updated_keys or updated_keys[0] != key:
        raise RuntimeError("Native UI readback did not confirm promotion; paused")
    if [k for k in updated_keys if k != key] != [k for k in keys if k != key]:
        raise RuntimeError("Other member relative order changed; paused")


def reconcile_move(app, groups, pending_path, known_archived):
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    group = groups.get(pending["row"]["section"]["id"])
    if group is None or group["sidebar_section_id"] != pending["section_id"]:
        raise RuntimeError("Approved group identity changed for pending move; paused")
    section = validate_groups(app.tool("list_threads", {"limit": 1}), groups)[pending["section_id"]]
    archived_keys = {"codex:thread:local:" + identifier for identifier in known_archived}
    keys = [k for k in pending["before_keys"] if k not in archived_keys]
    updated_keys = [k for k in section["itemKeys"] if k not in archived_keys]
    key = "codex:thread:local:" + pending["row"]["id"]
    if key in archived_keys:
        raise RuntimeError("Pending move target archived before readback; paused")
    confirm_order(keys, updated_keys, key)
    pending_path.unlink()
    return dict(thread_id=pending["row"]["id"], section_id=pending["section_id"],
                recency_at=pending["row"]["recencyAt"], result="moved-reconciled",
                before_index=keys.index(key), after_index=0, active_member_set_unchanged=True,
                time=time.time())


def move(app, row, groups, known_archived=(), pending_path=None):
    group = groups[row["section"]["id"]]
    frontend = group["sidebar_section_id"]
    key = "codex:thread:local:" + row["id"]
    section = validate_groups(app.tool("list_threads", {"limit": 1}), groups)[frontend]
    archived_keys = {"codex:thread:local:" + identifier for identifier in known_archived}
    raw_keys = section["itemKeys"]
    keys = [key for key in raw_keys if key not in archived_keys]
    receipt = {"thread_id": row["id"], "section_id": frontend, "recency_at": row["recencyAt"]}
    if key in archived_keys:
        return dict(receipt, result="skipped-archived")
    if key not in keys:
        return dict(receipt, result="skipped-not-current-member")
    if keys[0] == key:
        return dict(receipt, result="already-first")
    if pending_path:
        save(pending_path, {"row": row, "section_id": frontend, "before_keys": keys})
    app.tool("move_thread_to_sidebar_section", {
        "threadId": row["id"], "hostId": "local", "source": "codex", "sectionId": frontend,
    })
    updated = validate_groups(app.tool("list_threads", {"limit": 1}), groups)[frontend]
    updated_keys = [key for key in updated["itemKeys"] if key not in archived_keys]
    confirm_order(keys, updated_keys, key)
    if pending_path:
        pending_path.unlink()
    return dict(receipt, result="moved", before_index=keys.index(key), after_index=0,
                member_set_unchanged=set(raw_keys) == set(updated["itemKeys"]),
                active_member_set_unchanged=True, time=time.time())


def app_cycle(factory, groups, changes, known_archived=(), pending_path=None):
    # Node/app-tools is alive only for a bounded health check or changed batch.
    with factory() as app:
        # AppClient initialization already reads native tools/list. Idle health
        # does not need a global conversation listing (including other hosts).
        if not changes and not (pending_path and pending_path.exists()):
            return
        validate_groups(app.tool("list_threads", {"limit": 1}), groups)
        if pending_path and pending_path.exists():
            yield reconcile_move(app, groups, pending_path, known_archived)
        for row in changes:
            # Commit each successful receipt before a later item can fail.
            yield move(app, row, groups, known_archived, pending_path)


def make_progress(config):
    settings = config.get("progress")
    if not settings:
        return None
    sys.path.insert(0, settings["module_dir"])
    from task_progress import Reporter
    return Reporter(settings["task_id"], settings["title"], state=settings["state_dir"],
                    auto_start=False, port=settings["port"])


def wait_for_reconnect(seconds, stop, heartbeat):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if stop.exists():
            raise StopRequested()
        heartbeat()
        time.sleep(min(.5, max(0, deadline - time.monotonic())))


def run(config, rpc_factory=Rpc, app_factory=None):
    state = Path(config["state_dir"])
    state.mkdir(parents=True, exist_ok=True)
    groups = {g["native_section_id"]: g for g in config["groups"]}
    interval, health = config.get("interval_seconds", 3), config.get("app_health_seconds", 60)
    if os.environ.get("CODEX_VERSION") != config["backend_version"]:
        raise RuntimeError("Untested Codex version; capability revalidation required")
    checkpoint, baseline, stop = (state / name for name in ("watcher.json", "last-recency.json", "stop-request.json"))
    pending_path = state / "pending-move.json"
    previous = json.loads(checkpoint.read_text(encoding="utf-8-sig")) if checkpoint.exists() else {}
    seen = json.loads(baseline.read_text(encoding="utf-8-sig")) if baseline.exists() else {}
    first, last_scan = not bool(seen), previous.get("updated_at", time.time())
    pid, started, scans, moves, latest = os.getpid(), time.time(), 0, previous.get("moves", 0), []
    progress, progress_error = None, None
    try:
        progress = make_progress(config)
    except Exception as error:
        progress_error = str(error)
    next_health, next_progress, last_heartbeat = 0, 0, 0
    progress_started, failures, activity_gate, gate_time = False, 0, None, None
    progress_status = None

    def publish(status="running", operation=None, error=None, force=True):
        nonlocal next_progress, last_heartbeat, progress_started, progress_error, progress_status
        now = time.monotonic()
        if not force and now - last_heartbeat < 3:
            return
        save(checkpoint, {
            "pid": pid, "status": status, "started_at": started, "updated_at": time.time(),
            "scans": scans, "moves": moves, "interval_seconds": interval, "app_health_seconds": health,
            "last_cycle_seconds": now - cycle_start, "latest": latest,
            "backend": config["backend_version"], "caller_thread_id": os.environ["CODEX_THREAD_ID"],
            "groups": [g["name"] for g in groups.values()], "progress_error": progress_error,
            "operation": operation, "error": str(error) if error else None,
            "reconnect_failures": failures,
        })
        last_heartbeat = now
        if progress and (status != progress_status or now >= next_progress):
            try:
                stage = "等待新聊天触发重连" if status == "waiting-activity" else "监听本机聊天活动"
                progress.update(stage, moves, None, operation or "仅活动变化时启动侧栏接口并同组上移", unit="次",
                                restart_reason="启动或恢复本机排序worker" if not progress_started else None)
                progress_started = True
                progress_status = status
            except Exception as progress_exception:
                progress_error = str(progress_exception)
            next_progress = now + 15

    def on_wait(operation):
        if stop.exists():
            raise StopRequested()
        publish("checking-interface", operation, force=False)

    factory = app_factory or (lambda: AppClient(config["app_tools_server"],
                                                config.get("app_request_timeout_seconds", 90), on_wait))
    try:
        with worker_guard(state / "worker.lock"), ExitStack() as rpc_stack:
            rpc = None
            while not stop.exists():
                cycle_start = time.monotonic()
                current = []
                try:
                    if rpc is None:
                        transport = (Rpc(os.environ["CODEX_CLI_PATH"], on_wait) if rpc_factory is Rpc
                                     else rpc_factory(os.environ["CODEX_CLI_PATH"]))
                        rpc = rpc_stack.enter_context(transport)
                    current = rows(rpc, groups)
                    scans += 1
                    if activity_gate is not None:
                        if not select_changes(current, activity_gate, since=gate_time):
                            publish("waiting-activity", "连续三次接口失败；等待新的真实聊天活动", force=False)
                            time.sleep(interval)
                            continue
                        activity_gate, failures = None, 0
                    changes = select_changes(current, seen, initial=first, since=last_scan)
                    if changes or pending_path.exists() or time.monotonic() >= next_health:
                        known_archived = archived_ids(rpc)
                        for receipt in app_cycle(factory, groups, changes, known_archived, pending_path):
                            latest = (latest + [receipt])[-6:]
                            if receipt["result"] in ("moved", "moved-reconciled"):
                                moves += 1
                                with (state / "moves.jsonl").open("a", encoding="utf-8") as log:
                                    log.write(json.dumps(receipt, ensure_ascii=True) + "\n")
                            seen[receipt["thread_id"]] = receipt["recency_at"]
                            save(baseline, seen)
                        next_health = time.monotonic() + health
                    next_seen = {t["id"]: t.get("recencyAt") for t in current}
                    if next_seen != seen:
                        save(baseline, next_seen)
                    seen = next_seen
                    first, last_scan, failures = False, time.time(), 0
                    publish()
                except InterfaceUnavailable as error:
                    failures += 1
                    with (state / "interface-errors.jsonl").open("a", encoding="utf-8") as log:
                        log.write(json.dumps({"time": time.time(), "source": error.source,
                                              "operation": error.operation, "error_type": type(error).__name__,
                                              "error": str(error), "attempt": failures}) + "\n")
                    if error.source == "metadata":
                        rpc_stack.close()
                        rpc = None
                    if failures >= 3:
                        if error.source != "app":
                            raise
                        activity_gate = {t["id"]: t.get("recencyAt") for t in current}
                        gate_time = time.time()
                        publish("waiting-activity", error.operation, error)
                        continue
                    publish("reconnecting", error.operation, error)
                    wait_for_reconnect((5, 15)[failures - 1], stop,
                                       lambda: publish("reconnecting", error.operation, error, force=False))
                    continue
                time.sleep(max(.2, interval - (time.monotonic() - cycle_start)))
        if progress:
            progress.stop("paused", "用户请求或维护停机")
        save(checkpoint, {"pid": pid, "status": "stopped", "updated_at": time.time(), "scans": scans, "moves": moves})
    except StopRequested:
        if progress:
            progress.stop("paused", "用户请求或维护停机")
        save(checkpoint, {"pid": pid, "status": "stopped", "updated_at": time.time(), "scans": scans, "moves": moves})
    except WorkerBusy:
        raise
    except Exception as error:
        save(checkpoint, {"pid": pid, "status": "failed", "updated_at": time.time(), "scans": scans,
                          "moves": moves, "error": str(error), "error_type": type(error).__name__,
                          "failure_kind": "transport" if isinstance(error, InterfaceUnavailable) else "safety",
                          "latest": latest})
        if progress:
            try:
                progress.stop("failed", str(error))
            except Exception:
                pass
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run", action="store_true", help="Run only after explicit user authorization")
    args = parser.parse_args()
    config = load_config(args.config)
    if not args.run:
        print(json.dumps({"status": "NOT_EXECUTED", "configured_groups": len(config["groups"]),
                          "requires": "Native capability check and explicit user authorization"}))
        return
    for name in ("CODEX_APP_TOOLS_PIPE_PATH", "CODEX_THREAD_ID", "CODEX_MCP_NODE_PATH", "CODEX_CLI_PATH"):
        if not os.environ.get(name):
            raise RuntimeError("Missing genuine executor environment: " + name)
    if (Path(config["state_dir"]) / "stop-request.json").exists():
        print("Recency worker is stopped by user request")
        return
    try:
        run(config)
    except WorkerBusy as error:
        print(str(error) + "; no second writer started")


if __name__ == "__main__":
    main()
