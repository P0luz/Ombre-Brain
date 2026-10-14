#!/usr/bin/env python3
"""Ombre Brain 记忆网关：一个 OpenAI 兼容的 /v1/chat/completions 代理（只用标准库，单文件）。

任何「能填 base_url 的」客户端——自制前端、Cherry Studio、ChatBox、LobeChat、RikkaHub、脚本——
把 base_url 从模型商换成这个网关，就有了和 Claude Code 钩子同样的自动召回：

    请求进来 → 新对话先取「上一段对话的接续 + 今日 dream」→ 用户这句存进 OB、唤醒词命中就带一条记忆
             → 拼进 system 发给真正的模型 → 回复原样转发（流式一边转一边拼全文）→ 结束后把回复存进 OB

    python3 ob_gateway.py --upstream https://api.deepseek.com/v1 --ob http://127.0.0.1:18001
    # 客户端：base_url = http://127.0.0.1:18080/v1，API key 照填模型商的 key（原样透传，不落盘）

上游写到版本段为止：OpenAI https://api.openai.com/v1、DeepSeek https://api.deepseek.com/v1、
Gemini https://generativelanguage.googleapis.com/v1beta/openai 。只有 chat/completions 会被加工，
其余路径（/models 等）原样转发。

环境变量 / 参数：
    OB_URL / --ob              OB 地址
    OB_HOOK_TOKEN / --token    OB 的 hook token（与服务端 OMBRE_HOOK_TOKEN / hooks.token 相同）
    OB_SCOPE / --scope         作用域：「上一段对话」只在同一作用域里找（默认 gateway）
    OB_GATEWAY_KEY / --key     网关自己的密钥。不监听本机地址时必须设：网关会把记忆注入回复，
                               谁能连上网关谁就能读到记忆。设了 key 时客户端用它当 API key，
                               模型商的 key 交给 OB_UPSTREAM_KEY / --upstream-key 由网关代填。

会话怎么认：OpenAI 接口是无状态的，网关按「作用域 + 第一条 system + 第一句用户发言」算会话 id；
请求头 X-OB-Session 可以直接指定。只有一句用户发言的请求视为新对话（会取接续），其余视为续聊。
工具调用轮（最后一条是 tool）不存不召回；只存文本，图片等非文本部分跳过。
OB 不通、超时、关着：照常转发，不带记忆，错误打到 stderr——记忆出问题不能卡住对话。
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HOP = {"connection", "keep-alive", "proxy-authorization", "proxy-connection", "te", "trailer",
        "transfer-encoding", "upgrade", "host", "content-length", "accept-encoding"}
_MAX_BODY = 32 * 1024 * 1024
_MAX_SESSIONS = 500
_RECALLS_KEPT = 3
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _log(message: str) -> None:
    print(f"[ob_gateway] {time.strftime('%H:%M:%S')} {message}", file=sys.stderr, flush=True)


def _is_local(host: str) -> bool:
    host = (host or "").strip("[]")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _open(request, timeout: float):
    """本机地址绕过系统代理（macOS 上 urllib 会把 127.0.0.1 也交给系统代理，实测 503）。"""
    if _is_local(urllib.request.urlparse(request.full_url).hostname or ""):
        return _DIRECT.open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)


def text_of(content) -> str:
    """OpenAI 消息 content 可能是字符串，也可能是 [{type:text,text}, {type:image_url,…}]；只取文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
        return "\n".join(x for x in parts if isinstance(x, str))
    return ""


def _sha(*parts: str) -> str:
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()


def session_id_for(scope: str, messages: list) -> str:
    system = next((text_of(m.get("content")) for m in messages if m.get("role") == "system"), "")
    first_user = next((text_of(m.get("content")) for m in messages if m.get("role") == "user"), "")
    return "gw-" + _sha(scope, system, first_user)[:24]


def inject(messages: list, extra: str) -> list:
    """把记忆拼进第一条 system（没有就在最前面加一条）。不放在对话中间：有些兼容接口不认中途的 system。"""
    if not extra:
        return messages
    out = [dict(m) for m in messages]
    for m in out:
        if m.get("role") == "system":
            base = m.get("content")
            if isinstance(base, list):
                m["content"] = list(base) + [{"type": "text", "text": extra}]
            else:
                m["content"] = f"{base or ''}\n\n{extra}".strip()
            return out
    return [{"role": "system", "content": extra}] + out


class OB:
    """OB 自动召回接口的极简客户端。任何失败都返回 {}。"""

    def __init__(self, url: str, token: str, scope: str) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.scope = scope

    def post(self, path: str, payload: dict, timeout: float) -> dict:
        if not self.token:
            return {}
        request = urllib.request.Request(
            f"{self.url}/autorecall/{path}",
            data=json.dumps({**payload, "cwd": self.scope}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Ombre-Hook-Token": self.token},
            method="POST",
        )
        try:
            with _open(request, timeout) as response:
                return json.loads(response.read().decode("utf-8") or "{}") or {}
        except (urllib.error.URLError, OSError, ValueError) as exc:
            _log(f"OB {path} 失败：{type(exc).__name__}: {exc}")
            return {}


class Gateway:
    def __init__(self, upstream: str, ob: OB, key: str = "", upstream_key: str = "",
                 recall_timeout: float = 6.0, start_timeout: float = 15.0) -> None:
        self.upstream = upstream.rstrip("/")
        self.ob = ob
        self.key = key
        self.upstream_key = upstream_key
        self.recall_timeout = recall_timeout
        self.start_timeout = start_timeout
        self._lock = threading.Lock()
        # 会话 id → {"start": 接续文本, "recalls": [最近几次召回]}。只在内存里，重启后续聊不再补接续。
        self._sessions: OrderedDict[str, dict] = OrderedDict()

    def _state(self, sid: str) -> dict:
        with self._lock:
            state = self._sessions.pop(sid, None) or {"start": "", "recalls": []}
            self._sessions[sid] = state
            while len(self._sessions) > _MAX_SESSIONS:
                self._sessions.popitem(last=False)
            return state

    def prepare(self, body: dict, session_header: str = "") -> tuple[dict, dict | None]:
        """返回 (改好的请求体, 本轮记录)。记录为 None 表示这轮不存（工具调用轮、没有用户发言）。"""
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages or not isinstance(messages[-1], dict):
            return body, None
        if messages[-1].get("role") != "user":
            return body, None
        sid = (session_header or "").strip()[:200] or session_id_for(self.ob.scope, messages)
        state = self._state(sid)
        user_turns = sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "user")
        if user_turns == 1 and not state.get("started"):
            state["start"] = self.ob.post("session-start", {"session_id": sid, "source": "startup"},
                                          self.start_timeout).get("context") or ""
            state["started"] = True
        text = text_of(messages[-1].get("content"))
        prompt_id = _sha(sid, str(user_turns), text)[:16]
        recalled = ""
        if text.strip():
            recalled = self.ob.post("prompt", {"session_id": sid, "prompt": text, "prompt_id": prompt_id},
                                    self.recall_timeout).get("context") or ""
        if recalled:
            state["recalls"] = (state["recalls"] + [recalled])[-_RECALLS_KEPT:]
        extra = "\n\n".join(x for x in [state["start"], *state["recalls"]] if x)
        out = dict(body)
        out["messages"] = inject(messages, extra)
        return out, {"session_id": sid, "prompt_id": prompt_id}

    def remember(self, turn: dict | None, reply: str) -> None:
        if not turn or not reply.strip():
            return
        key = f"a:{turn['prompt_id']}:{_sha(reply)[:8]}"
        payload = {"session_id": turn["session_id"],
                   "messages": [{"key": key, "role": "assistant", "text": reply}]}
        threading.Thread(target=self.ob.post, args=("stop", payload, 25.0), daemon=True).start()

    def upstream_url(self, path: str) -> str:
        if path.startswith("/v1/"):
            path = path[3:]
        return self.upstream + path


def _sse_delta(line: bytes) -> str | None:
    """解析一行 SSE；返回本行带来的文本增量，流结束返回 None。"""
    if not line.startswith(b"data:"):
        return ""
    data = line[5:].strip()
    if data == b"[DONE]":
        return None
    try:
        chunk = json.loads(data)
        choice = (chunk.get("choices") or [{}])[0]
        return text_of((choice.get("delta") or {}).get("content")) or ""
    except (ValueError, AttributeError, IndexError, TypeError):
        return ""


def make_handler(gw: Gateway):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "ob-gateway"

        def log_message(self, fmt, *args):  # 不把请求行（可能带 key）打到终端
            return

        def _send_json(self, status: int, payload: dict) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            if not gw.key:
                return True
            auth = self.headers.get("Authorization", "")
            return auth == f"Bearer {gw.key}" or self.headers.get("x-api-key", "") == gw.key

        def _upstream_headers(self) -> dict:
            headers = {k: v for k, v in self.headers.items() if k.lower() not in _HOP}
            headers["Accept-Encoding"] = "identity"
            if gw.upstream_key:  # 客户端带的是网关 key，换成模型商的
                headers = {k: v for k, v in headers.items() if k.lower() not in ("authorization", "x-api-key")}
                headers["Authorization"] = f"Bearer {gw.upstream_key}"
            return headers

        def do_GET(self):
            self._proxy(None)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > _MAX_BODY:
                return self._send_json(413, {"error": {"message": "request too large"}})
            self._proxy(self.rfile.read(length) if length else b"")

        def _proxy(self, raw: bytes | None):
            if not self._authorized():
                return self._send_json(401, {"error": {"message": "invalid gateway key"}})
            path = self.path
            turn = None
            stream = False
            if raw and path.split("?", 1)[0].rstrip("/").endswith("/chat/completions"):
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = None
                if isinstance(body, dict):
                    try:
                        body, turn = gw.prepare(body, self.headers.get("X-OB-Session", ""))
                    except Exception as exc:  # 记忆这一侧任何问题都不挡对话
                        _log(f"准备记忆失败：{type(exc).__name__}: {exc}")
                        turn = None
                    stream = bool(body.get("stream"))
                    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(gw.upstream_url(path), data=raw, method=self.command,
                                             headers=self._upstream_headers())
            try:
                upstream = _open(request, 600)
            except urllib.error.HTTPError as exc:
                upstream = exc  # 4xx/5xx 原样转回去，不存
                turn = None
            except (urllib.error.URLError, OSError) as exc:
                return self._send_json(502, {"error": {"message": f"upstream unreachable: {exc}"}})
            with upstream:
                if stream and upstream.status == 200:
                    return self._relay_stream(upstream, turn)
                data = upstream.read()
                self.send_response(upstream.status)
                for k, v in upstream.headers.items():
                    if k.lower() not in _HOP:
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            if turn and upstream.status == 200:
                try:
                    choice = (json.loads(data).get("choices") or [{}])[0]
                    gw.remember(turn, text_of((choice.get("message") or {}).get("content")))
                except (ValueError, AttributeError, IndexError, TypeError):
                    pass

        def _relay_stream(self, upstream, turn):
            self.send_response(200)
            for k, v in upstream.headers.items():
                if k.lower() not in _HOP:
                    self.send_header(k, v)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            pieces: list[str] = []
            finished = False
            try:
                for line in upstream:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
                    self.wfile.flush()
                    delta = _sse_delta(line.strip())
                    if delta is None:
                        finished = True
                    elif delta:
                        pieces.append(delta)
                self.wfile.write(b"0\r\n\r\n")
                finished = True
            except (BrokenPipeError, ConnectionResetError):
                pass  # 客户端中途断开（点了停止）：已生成的部分不算完整回复，不存
            if finished:
                gw.remember(turn, "".join(pieces))

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ombre Brain 记忆网关（OpenAI 兼容）")
    parser.add_argument("--upstream", default=os.environ.get("OB_UPSTREAM", ""), help="模型商地址，写到 /v1 为止")
    parser.add_argument("--ob", default=os.environ.get("OB_URL", "http://127.0.0.1:18001"))
    parser.add_argument("--token", default=os.environ.get("OB_HOOK_TOKEN", ""))
    parser.add_argument("--scope", default=os.environ.get("OB_SCOPE", "gateway"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("OB_GATEWAY_PORT", "18080")))
    parser.add_argument("--key", default=os.environ.get("OB_GATEWAY_KEY", ""))
    parser.add_argument("--upstream-key", default=os.environ.get("OB_UPSTREAM_KEY", ""))
    parser.add_argument("--recall-timeout", type=float, default=6.0)
    args = parser.parse_args(argv)
    if not args.upstream:
        parser.error("需要 --upstream（例如 https://api.deepseek.com/v1）")
    if not _is_local(args.host) and not args.key:
        parser.error("监听非本机地址时必须设 --key：网关会把记忆注入回复，谁能连上谁就能读到记忆")
    if args.upstream_key and not args.key:
        parser.error("设了 --upstream-key 时必须同时设 --key，否则任何人都能用你的模型额度")
    if not args.token:
        _log("没有 OB_HOOK_TOKEN：只转发，不带记忆")
    gw = Gateway(args.upstream, OB(args.ob, args.token, args.scope), key=args.key,
                 upstream_key=args.upstream_key, recall_timeout=args.recall_timeout)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(gw))
    server.daemon_threads = True
    _log(f"监听 http://{args.host}:{args.port}/v1 → {gw.upstream}（作用域 {args.scope}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
