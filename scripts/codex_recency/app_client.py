"""Call the installed, unmodified codex-app-tools MCP server over stdio."""
import json
from collections import deque
import os
import queue
import subprocess
import threading
import time


class AppClient:
    def __init__(self, server):
        self.thread_id = os.environ["CODEX_THREAD_ID"]
        self.next_id = 0
        self.queue = queue.Queue()
        self.errors = deque(maxlen=20)
        self.process = subprocess.Popen(
            [os.environ["CODEX_MCP_NODE_PATH"], str(server)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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

    def call(self, method, params, timeout=25):
        self.next_id += 1
        request_id = self.next_id
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            item = self.queue.get(timeout=max(.01, deadline - time.monotonic()))
            if item.get("eof"):
                raise RuntimeError("App MCP transport closed: " + "\n".join(self.errors))
            if item.get("id") == request_id:
                if "error" in item:
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
        if self.process.poll() is None:
            self.process.stdin.close()
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
