"""Call the installed, unmodified codex-app-tools MCP server over stdio."""
import json
from collections import deque
import os
import queue
import subprocess
import threading
import time
from interface_errors import InterfaceUnavailable, RequestTimedOut


class AppClient:
    def __init__(self, server, request_timeout=90, on_wait=None):
        self.thread_id = os.environ["CODEX_THREAD_ID"]
        self.request_timeout, self.on_wait = request_timeout, on_wait
        self.next_id = 0
        self.queue = queue.Queue()
        self.errors = deque(maxlen=20)
        self.process = subprocess.Popen(
            [os.environ["CODEX_MCP_NODE_PATH"], str(server)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
        )
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._stderr, daemon=True).start()
        try:
            self.call("initialize", {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "approved-section-recency", "version": "1.1.0"},
            })
            self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            names = {t["name"] for t in self.call("tools/list", {})["tools"]}
            if not {"list_threads", "move_thread_to_sidebar_section"} <= names:
                raise RuntimeError("Required native sidebar tools unavailable")
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

    def call(self, method, params, timeout=None):
        timeout = self.request_timeout if timeout is None else timeout
        operation = method + (":" + params["name"] if method == "tools/call" else "")
        self.next_id += 1
        request_id = self.next_id
        try:
            self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        except OSError as error:
            if isinstance(error, PermissionError) or (not isinstance(error, (BrokenPipeError, ConnectionError))
                                                     and self.process.poll() is None):
                raise
            raise InterfaceUnavailable("app", operation, "transport disconnected") from error
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RequestTimedOut("app", operation, timeout)
            try:
                item = self.queue.get(timeout=min(1, remaining))
            except queue.Empty:
                if self.on_wait:
                    self.on_wait(operation)
                continue
            if item.get("eof"):
                raise InterfaceUnavailable("app", operation, "transport closed: " + "\n".join(self.errors))
            if item.get("id") == request_id:
                if "error" in item:
                    if item["error"].get("code") == -32001:
                        raise RequestTimedOut("app", operation, timeout)
                    raise RuntimeError(str(item["error"]))
                return item["result"]

    def tool(self, name, arguments):
        result = self.call("tools/call", {
            "name": name, "arguments": arguments,
            "_meta": {"openai/threadId": self.thread_id},
        })
        if result.get("isError"):
            raise RuntimeError(str(result.get("content")))
        texts = [c["text"] for c in result.get("content", []) if c.get("type") == "text"]
        if not texts:
            raise RuntimeError("Native tool returned no readable result")
        return json.loads(texts[0])

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
