"""Read activity metadata through the installed Codex app-server, never its DB."""
import json
from collections import deque
import queue
import subprocess
import threading
import time
from interface_errors import InterfaceUnavailable, RequestTimedOut


class Rpc:
    def __init__(self, binary, on_wait=None):
        self.on_wait = on_wait
        self.next_id = 0
        self.queue = queue.Queue()
        self.errors = deque(maxlen=20)
        self.process = subprocess.Popen(
            [str(binary), "app-server", "--stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
            # Redirected pipes work without an attached Windows console.
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
        )
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._stderr, daemon=True).start()
        try:
            self.call("initialize", {
                "clientInfo": {"name": "section-recency-reader", "version": "1.1.0"},
                "capabilities": {"experimentalApi": True},
            })
            self.send({"method": "initialized", "params": {}})
        except Exception:
            self.close()
            raise

    def _read(self):
        for line in self.process.stdout:
            try:
                self.queue.put(json.loads(line))
            except ValueError:
                pass
        self.queue.put({"eof": True})

    def send(self, obj):
        self.process.stdin.write(json.dumps(obj, ensure_ascii=True) + "\n")
        self.process.stdin.flush()

    def _stderr(self):
        for line in self.process.stderr:
            self.errors.append(line.rstrip())

    def call(self, method, params, timeout=12):
        self.next_id += 1
        request_id = self.next_id
        try:
            self.send({"method": method, "id": request_id, "params": params})
        except OSError as error:
            if isinstance(error, PermissionError) or (not isinstance(error, (BrokenPipeError, ConnectionError))
                                                     and self.process.poll() is None):
                raise
            raise InterfaceUnavailable("metadata", method, "transport disconnected") from error
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RequestTimedOut("metadata", method, timeout)
            try:
                item = self.queue.get(timeout=min(1, remaining))
            except queue.Empty:
                if self.on_wait:
                    self.on_wait(method)
                continue
            if item.get("eof"):
                raise InterfaceUnavailable("metadata", method, "transport closed: " + "\n".join(self.errors))
            if item.get("id") == request_id:
                if "error" in item:
                    raise RuntimeError("{}: {}".format(method, item["error"]))
                return item.get("result")

    def close(self):
        try:
            self.process.stdin.close()
        except OSError:
            pass  # The owned pipe may already be invalid after child exit.
        if self.process.poll() is None:
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=3)
        self.process.stdout.close()
        self.process.stderr.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
