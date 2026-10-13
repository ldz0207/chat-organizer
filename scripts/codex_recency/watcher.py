"""Opt-in, version-bound local Codex sidebar recency worker (standard library)."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import time

from app_client import AppClient
from native_rpc import Rpc


class WorkerBusy(RuntimeError):
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
        })
        # Discard previews, paths, titles and other unused response fields.
        data.extend({"id": t["id"], "section": {"id": t["section"]["id"]},
                     "recencyAt": t.get("recencyAt")} for t in response["data"]
                    if (t.get("section") or {}).get("id") in groups)
        cursor = response.get("nextCursor")
        if not cursor:
            return data


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


def move(app, row, groups):
    group = groups[row["section"]["id"]]
    frontend = group["sidebar_section_id"]
    key = "codex:thread:local:" + row["id"]
    section = validate_groups(app.tool("list_threads", {"limit": 1}), groups)[frontend]
    keys = section["itemKeys"]
    receipt = {"thread_id": row["id"], "section_id": frontend, "recency_at": row["recencyAt"]}
    if key not in keys:
        return dict(receipt, result="skipped-not-current-member")
    if keys[0] == key:
        return dict(receipt, result="already-first")
    app.tool("move_thread_to_sidebar_section", {
        "threadId": row["id"], "hostId": "local", "source": "codex", "sectionId": frontend,
    })
    updated = validate_groups(app.tool("list_threads", {"limit": 1}), groups)[frontend]
    if set(updated["itemKeys"]) != set(keys) or len(updated["itemKeys"]) != len(keys):
        raise RuntimeError("Group membership changed during move; paused")
    if not updated["itemKeys"] or updated["itemKeys"][0] != key:
        raise RuntimeError("Native UI readback did not confirm promotion; paused")
    if [k for k in updated["itemKeys"] if k != key] != [k for k in keys if k != key]:
        raise RuntimeError("Other member relative order changed; paused")
    return dict(receipt, result="moved", before_index=keys.index(key), after_index=0,
                member_set_unchanged=True, time=time.time())


def app_cycle(factory, groups, changes):
    # Node/app-tools is alive only for a bounded health check or changed batch.
    with factory() as app:
        validate_groups(app.tool("list_threads", {"limit": 1}), groups)
        for row in changes:
            # Commit each successful receipt before a later item can fail.
            yield move(app, row, groups)


def make_progress(config):
    settings = config.get("progress")
    if not settings:
        return None
    sys.path.insert(0, settings["module_dir"])
    from task_progress import Reporter
    return Reporter(settings["task_id"], settings["title"], state=settings["state_dir"],
                    auto_start=False, port=settings["port"])


def run(config, rpc_factory=Rpc, app_factory=None):
    state = Path(config["state_dir"])
    state.mkdir(parents=True, exist_ok=True)
    groups = {g["native_section_id"]: g for g in config["groups"]}
    interval, health = config.get("interval_seconds", 3), config.get("app_health_seconds", 60)
    if os.environ.get("CODEX_VERSION") != config["backend_version"]:
        raise RuntimeError("Untested Codex version; capability revalidation required")
    factory = app_factory or (lambda: AppClient(config["app_tools_server"]))
    checkpoint, baseline, stop = (state / name for name in ("watcher.json", "last-recency.json", "stop-request.json"))
    previous = json.loads(checkpoint.read_text(encoding="utf-8-sig")) if checkpoint.exists() else {}
    seen = json.loads(baseline.read_text(encoding="utf-8-sig")) if baseline.exists() else {}
    first, last_scan = not bool(seen), previous.get("updated_at", time.time())
    pid, started, scans, moves, latest = os.getpid(), time.time(), 0, previous.get("moves", 0), []
    progress, progress_error = None, None
    try:
        progress = make_progress(config)
    except Exception as error:
        progress_error = str(error)
    try:
        with worker_guard(state / "worker.lock"), rpc_factory(os.environ["CODEX_CLI_PATH"]) as rpc:
            next_health, next_progress = 0, 0
            while not stop.exists():
                cycle_start = time.monotonic()
                current = rows(rpc, groups)
                scans += 1
                changes = select_changes(current, seen, initial=first, since=last_scan)
                first, last_scan = False, time.time()
                if changes or time.monotonic() >= next_health:
                    for receipt in app_cycle(factory, groups, changes):
                        latest = (latest + [receipt])[-6:]
                        if receipt["result"] == "moved":
                            moves += 1
                            with (state / "moves.jsonl").open("a", encoding="utf-8") as log:
                                log.write(json.dumps(receipt, ensure_ascii=True) + "\n")
                    next_health = time.monotonic() + health
                next_seen = {t["id"]: t.get("recencyAt") for t in current}
                if next_seen != seen:
                    save(baseline, next_seen)
                seen = next_seen
                save(checkpoint, {
                    "pid": pid, "status": "running", "started_at": started, "updated_at": time.time(),
                    "scans": scans, "moves": moves, "interval_seconds": interval, "app_health_seconds": health,
                    "last_cycle_seconds": time.monotonic() - cycle_start, "latest": latest,
                    "backend": config["backend_version"], "caller_thread_id": os.environ["CODEX_THREAD_ID"],
                    "groups": [g["name"] for g in groups.values()], "progress_error": progress_error,
                })
                if progress and (changes or time.monotonic() >= next_progress):
                    try:
                        progress.update("监听本机聊天活动", moves, None, "仅活动变化时启动侧栏接口并同组上移", unit="次",
                                        restart_reason="启动或恢复本机排序worker" if scans == 1 else None)
                    except Exception as error:
                        progress_error = str(error)
                    next_progress = time.monotonic() + 15
                time.sleep(max(.2, interval - (time.monotonic() - cycle_start)))
        if progress:
            progress.stop("paused", "用户请求或维护停机")
        save(checkpoint, {"pid": pid, "status": "stopped", "updated_at": time.time(), "scans": scans, "moves": moves})
    except WorkerBusy:
        raise
    except Exception as error:
        save(checkpoint, {"pid": pid, "status": "failed", "updated_at": time.time(), "scans": scans,
                          "moves": moves, "error": str(error), "latest": latest})
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
