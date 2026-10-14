#!/usr/bin/env python3
"""Ombre Brain 钩子的本地中继（只用标准库）。由 ob_hook.py 按需拉起，不用手动运行。

为什么要它：每个钩子都是新起的进程，直连远端 OB 时每次都要重新 TCP + TLS 握手，
一个钩子就要好几个往返。中继常驻在本机，跟 OB 保持一条长连接：
- 要等结果的（开窗、发问）：钩子 → 本机中继（不到 1ms）→ 热连接上 OB（一个往返）；
- 不用等的（每轮存回复、关窗补存）：中继收下就回 202，自己在后台慢慢发。
  中继是独立进程，Claude Code 退出时不会被一起杀掉（后台 async 钩子会）。

安全：只听 127.0.0.1；每次启动生成随机密钥，写进 0600 的状态文件，不带密钥的请求一律 403。
闲置 30 分钟自动退出。地址或 token 变了，钩子会拉起新的中继，旧的闲置后自己退出。

状态文件：~/.claude/hooks/.ob_relay.json（可用 OB_RELAY_STATE 改）。
"""

import hashlib
import http.client
import json
import os
import queue
import secrets
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:  # 断线不丢：重试用完的请求写进 ob_hook 的待发目录，连上后补发（同目录的 ob_hook.py 提供）
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from ob_hook import spool_count, spool_flush, spool_put
except Exception:  # 单独拷走的旧中继：退回原来的只报错
    spool_put = None

IDLE_SECONDS = 30 * 60
UPSTREAM_TIMEOUT = 30
_PATHS = frozenset({"session-start", "prompt", "stop", "session-end"})


def state_path() -> Path:
    return Path(os.environ.get("OB_RELAY_STATE") or Path.home() / ".claude" / "hooks" / ".ob_relay.json")


def fingerprint(url: str, token: str) -> str:
    return hashlib.sha256(f"{url}\x1f{token}".encode()).hexdigest()[:16]


def _proxy_for(scheme: str, host: str):
    """系统 / 环境代理（含 macOS 网络设置）；被 no_proxy 排除的主机返回 None。"""
    import urllib.request

    try:
        if urllib.request.proxy_bypass(host):
            return None
        url = urllib.request.getproxies().get(scheme) or urllib.request.getproxies().get("https")
    except Exception:
        return None
    if not url:
        return None
    parsed = urllib.parse.urlsplit(url if "://" in url else f"http://{url}")
    if not parsed.hostname:
        return None
    return parsed.hostname, parsed.port or 8080


class Upstream:
    """到 OB 的一条长连接；断了就重连一次。多个请求排队用同一条连接。"""

    def __init__(self, url: str, token: str) -> None:
        parsed = urllib.parse.urlsplit(url)
        self.https = parsed.scheme == "https"
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or (443 if self.https else 80)
        self.prefix = parsed.path.rstrip("/")
        self.token = token
        self.lock = threading.Lock()
        self.conn = None

    def _connect(self):
        """远端 OB 时尊重系统 / 环境代理（国内连远端常要代理）：经代理 CONNECT 建隧道，长连接照样保持。
        本机地址不走代理。http.client 自己不认代理，所以这里显式处理。"""
        cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
        local = self.host in ("127.0.0.1", "localhost", "::1")
        proxy = None if local else _proxy_for("https" if self.https else "http", self.host)
        if proxy:
            conn = cls(proxy[0], proxy[1], timeout=UPSTREAM_TIMEOUT)
            conn.set_tunnel(self.host, self.port)
            return conn
        return cls(self.host, self.port, timeout=UPSTREAM_TIMEOUT)

    def post(self, path: str, body: bytes) -> tuple[int, bytes]:
        headers = {"Content-Type": "application/json", "X-Ombre-Hook-Token": self.token,
                   "Connection": "keep-alive"}
        with self.lock:
            for attempt in range(2):
                if self.conn is None:
                    self.conn = self._connect()
                try:
                    self.conn.request("POST", f"{self.prefix}/autorecall/{path}", body=body, headers=headers)
                    response = self.conn.getresponse()
                    data = response.read()
                    if response.getheader("connection", "").lower() == "close":
                        self.conn.close()
                        self.conn = None
                    return response.status, data
                except (OSError, http.client.HTTPException):
                    try:
                        self.conn.close()
                    except Exception:
                        pass
                    self.conn = None
                    if attempt:
                        raise
        raise OSError("unreachable")


def _report(where: str, message: str) -> None:
    """和 ob_hook.report_error 同一个待补报文件：下一次钩子连上 OB 时报给面板。"""
    try:
        directory = state_path().parent
        pending_path = directory / ".ob_hook_errors.json"
        try:
            pending = json.loads(pending_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pending = []
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        pending = (pending + [{"at": now, "where": f"relay:{where}", "message": message[:300]}])[-20:]
        pending_path.write_text(json.dumps(pending, ensure_ascii=False), encoding="utf-8")
        with open(directory / "ob_hook.log", "a", encoding="utf-8") as handle:
            handle.write(f"{now} [relay:{where}] {message}\n")
    except Exception:
        pass


def serve(url: str, token: str) -> None:
    secret = secrets.token_hex(16)
    upstream = Upstream(url, token)
    backlog: "queue.Queue[tuple[str, bytes]]" = queue.Queue(maxsize=1000)
    last_seen = [time.monotonic()]

    def send(path: str, body: bytes) -> int:
        try:
            return upstream.post(path, body)[0]
        except Exception:
            return 0

    def flush() -> None:
        if spool_put is not None and spool_count():
            spool_flush(send)

    def worker() -> None:
        while True:
            try:
                path, body = backlog.get(timeout=60)
            except queue.Empty:
                flush()  # 闲着的时候也试一下：OB 回来了就把暂存的补发掉
                continue
            last = ""
            for delay in (0, 2, 10):
                time.sleep(delay)
                try:
                    status, _ = upstream.post(path, body)
                    if status < 500 and status != 429:
                        flush()
                        break
                    last = f"OB 返回 {status}"
                except Exception as exc:
                    last = f"{type(exc).__name__}: {exc}"
            else:
                if spool_put is not None:
                    spool_put(path, body)
                    _report(path, f"中继后台发送三次都失败，已暂存，连上后补发：{last}")
                else:
                    _report(path, f"中继后台发送三次都失败：{last}")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            last_seen[0] = time.monotonic()
            if self.headers.get("X-Relay-Secret") != secret:
                return self._reply(403, b"{}")
            parsed = urllib.parse.urlsplit(self.path)
            name = parsed.path.strip("/")
            if name not in _PATHS:
                return self._reply(404, b"{}")
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if "nowait=1" in parsed.query:
                try:
                    backlog.put_nowait((name, body))
                except queue.Full:
                    return self._reply(503, b"{}")
                return self._reply(202, b"{}")
            try:
                status, data = upstream.post(name, body)
            except Exception:
                return self._reply(502, b"{}")
            return self._reply(status, data)

        def _reply(self, status: int, data: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump({"port": server.server_address[1], "secret": secret, "pid": os.getpid(),
                   "fp": fingerprint(url, token)}, handle)
    os.replace(temp, path)

    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    while time.monotonic() - last_seen[0] < IDLE_SECONDS or not backlog.empty():
        time.sleep(30)
    server.shutdown()
    try:
        current = json.loads(path.read_text())
        if current.get("pid") == os.getpid():
            path.unlink()
    except (OSError, ValueError):
        pass


if __name__ == "__main__":
    url = os.environ.get("OB_URL", "").rstrip("/")
    token = os.environ.get("OB_HOOK_TOKEN", "")
    if url and token:
        try:
            serve(url, token)
        except Exception:
            sys.exit(0)
