"""Ombre Brain 自动召回的最小 Python 客户端（只用标准库）。给任何自制前端 / 机器人 / 脚本用。

    from ob_client import OBClient
    ob = OBClient("http://127.0.0.1:18001", token="你的 hook token", scope="我的前端")

    context = ob.session_start(session_id)          # 开新对话：把返回的文本拼进 system prompt
    context = ob.prompt(session_id, user_text)      # 每句用户输入：返回要注入的记忆（可能为空）
    reply = 调用你的模型(system=context, user=user_text)
    ob.stop(session_id, reply)                      # 拿到完整回复后存下来（流式就等流结束）
    ob.session_end(session_id)                      # 关闭对话（可选）

协议见 docs/AUTORECALL_PROTOCOL.md。任何网络错误都返回空串 / 空结果，不抛异常：记忆出问题不能卡住对话。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid


_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _open(request, timeout: float):
    """本机地址绕过系统代理（macOS 上 urllib 会把 127.0.0.1 也交给系统代理，实测 503）。"""
    if (urllib.request.urlparse(request.full_url).hostname or "") in ("127.0.0.1", "localhost", "::1"):
        return _DIRECT.open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)


class OBClient:
    def __init__(self, base_url: str, token: str, scope: str = "", timeout: float = 12.0,
                 client: str = "sdk") -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.scope = scope  # 对应协议里的 cwd：「上一段对话」只在同一个 scope 里找
        self.timeout = timeout
        self.client = client  # 面板上按客户端分开关；自制前端都算 sdk
        self.last_error = ""

    def _post(self, path: str, payload: dict, timeout: float | None = None) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}/autorecall/{path}",
            data=json.dumps({**payload, "cwd": self.scope, "client": self.client}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Ombre-Hook-Token": self.token},
            method="POST",
        )
        try:
            with _open(request, timeout or self.timeout) as response:
                self.last_error = ""
                return json.loads(response.read().decode("utf-8") or "{}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return {}

    def session_start(self, session_id: str, source: str = "startup") -> str:
        return self._post("session-start", {"session_id": session_id, "source": source}, 45).get("context") or ""

    def prompt(self, session_id: str, text: str, prompt_id: str = "") -> str:
        body = {"session_id": session_id, "prompt": text, "prompt_id": prompt_id or uuid.uuid4().hex}
        return self._post("prompt", body).get("context") or ""

    def stop(self, session_id: str, reply: str, prompt_id: str = "") -> dict:
        key = f"a:{prompt_id}" if prompt_id else f"a:{uuid.uuid4().hex}"
        return self._post("stop", {"session_id": session_id,
                                   "messages": [{"key": key, "role": "assistant", "text": reply}]}, 25)

    def session_end(self, session_id: str, messages: list | None = None) -> dict:
        return self._post("session-end", {"session_id": session_id, "messages": messages or []}, 30)
