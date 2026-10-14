"""
title: Ombre Brain 自动召回
description: 每段对话开头接上一段对话的原文；用户说到回忆词 / 名册里的名字时带一条记忆；对话原文存进 Ombre Brain。
version: 4.0.0
license: see Ombre Brain repository
"""

# 装法：Open WebUI「管理员面板 → Functions → +」，把本文件整段贴进去保存，打开开关并设为 Global（或挂到指定模型）。
# 然后在这个 Function 的 Valves 里填 OB 地址和 hook token。Open WebUI 跑在 Docker 里、OB 在宿主机时，
# 地址填 http://host.docker.internal:18001 。
#
# 对应关系：inlet = 用户发言（存原文 + 唤醒召回；新对话先取接续），outlet = 拿到完整回复（存下来）。
# 只用标准库；OB 不通、超时、关着都照常对话，只是不带记忆。

import asyncio
import hashlib
import json
import urllib.error
import urllib.request
from collections import OrderedDict
from typing import Optional

from pydantic import BaseModel, Field

_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_MAX_CHATS = 500
_RECALLS_KEPT = 3


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content
                         if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str))
    return ""


def _sha(*parts: str) -> str:
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()


class Filter:
    class Valves(BaseModel):
        priority: int = Field(default=0, description="执行顺序，越小越先")
        ob_url: str = Field(default="http://host.docker.internal:18001", description="Ombre Brain 地址")
        hook_token: str = Field(default="", description="与 OB 的 OMBRE_HOOK_TOKEN / hooks.token 相同")
        scope: str = Field(default="open-webui", description="作用域：「上一段对话」只在同一作用域里找")
        scope_per_user: bool = Field(default=True, description="多用户时每个用户各自一个作用域，互不接续")
        recall_timeout: float = Field(default=6.0, description="每句召回最多等几秒")

    def __init__(self):
        self.valves = self.Valves()
        # chat_id → {"start": 接续文本, "recalls": [...]}：Open WebUI 不保存 inlet 改过的消息，每轮重新带上
        self._chats: OrderedDict = OrderedDict()
        self._pending: set = set()  # 后台存回复的任务要留引用，否则可能被回收

    # ---- OB ----
    def _scope(self, user: Optional[dict]) -> str:
        if self.valves.scope_per_user and user and user.get("id"):
            return f"{self.valves.scope}:{user['id']}"
        return self.valves.scope

    def _post_sync(self, path: str, payload: dict, timeout: float) -> dict:
        if not self.valves.hook_token:
            return {}
        url = f"{self.valves.ob_url.rstrip('/')}/autorecall/{path}"
        request = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "X-Ombre-Hook-Token": self.valves.hook_token})
        host = urllib.request.urlparse(url).hostname or ""
        opener = _DIRECT.open if host in ("127.0.0.1", "localhost", "::1") else urllib.request.urlopen
        try:
            with opener(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8") or "{}") or {}
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"[ob_filter] OB {path} 失败：{type(exc).__name__}: {exc}")
            return {}

    async def _post(self, path: str, payload: dict, timeout: float) -> dict:
        return await asyncio.to_thread(self._post_sync, path, payload, timeout)

    def _session_id(self, body: dict, meta: dict, scope: str) -> str:
        """有 chat_id 用 chat_id；直连 /api/chat/completions 没带 chat_id 时，按作用域 + 首句用户发言算。
        不算 system：outlet 看到的 messages 已经带着 inlet 注入的记忆，算进去两边就对不上了（实测）。"""
        chat_id = str(meta.get("chat_id") or body.get("chat_id") or "")
        if chat_id:
            return f"owui-{chat_id}"[:200]
        messages = body.get("messages") or []
        first = next((_text(m.get("content")) for m in messages if m.get("role") == "user"), "")
        return "owui-h-" + _sha(scope, first)[:24] if first else ""

    def _state(self, chat_id: str) -> dict:
        state = self._chats.pop(chat_id, None) or {"start": "", "recalls": [], "started": False}
        self._chats[chat_id] = state
        while len(self._chats) > _MAX_CHATS:
            self._chats.popitem(last=False)
        return state

    # ---- 钩子 ----
    async def inlet(self, body: dict, __user__: Optional[dict] = None, __metadata__: Optional[dict] = None) -> dict:
        try:
            meta = __metadata__ or {}
            if meta.get("task"):  # 标题 / 标签 / 追问建议这类后台任务，不是用户在说话
                return body
            messages = body.get("messages") or []
            scope = self._scope(__user__)
            sid = self._session_id(body, meta, scope)
            if not sid or not messages or messages[-1].get("role") != "user":
                return body
            state = self._state(sid)
            user_turns = sum(1 for m in messages if m.get("role") == "user")
            if user_turns == 1 and not state["started"]:
                state["started"] = True
                state["start"] = (await self._post("session-start", {"session_id": sid, "cwd": scope,
                                                                     "source": "startup"}, 15)).get("context") or ""
            text = _text(messages[-1].get("content"))
            if text.strip():
                recalled = (await self._post("prompt", {
                    "session_id": sid, "cwd": scope, "prompt": text,
                    "prompt_id": _sha(sid, str(user_turns), text)[:16]}, self.valves.recall_timeout)).get("context") or ""
                if recalled:
                    state["recalls"] = (state["recalls"] + [recalled])[-_RECALLS_KEPT:]
            extra = "\n\n".join(x for x in [state["start"], *state["recalls"]] if x)
            if extra:
                body["messages"] = self._inject(messages, extra)
        except Exception as exc:  # 记忆这一侧任何问题都不挡对话
            print(f"[ob_filter] inlet 出错：{type(exc).__name__}: {exc}")
        return body

    async def outlet(self, body: dict, __user__: Optional[dict] = None, __metadata__: Optional[dict] = None) -> dict:
        try:
            messages = body.get("messages") or []
            scope = self._scope(__user__)
            sid = self._session_id(body, __metadata__ or {}, scope)
            if not sid or not messages or messages[-1].get("role") != "assistant":
                return body
            last = messages[-1]
            reply = _text(last.get("content"))
            if reply.strip():
                key = f"a:{last.get('id') or _sha(sid, str(len(messages)))[:16]}:{_sha(reply)[:8]}"
                task = asyncio.get_running_loop().create_task(self._post("stop", {
                    "session_id": sid, "cwd": scope,
                    "messages": [{"key": key, "role": "assistant", "text": reply}]}, 25))
                self._pending.add(task)
                task.add_done_callback(self._pending.discard)
        except Exception as exc:
            print(f"[ob_filter] outlet 出错：{type(exc).__name__}: {exc}")
        return body

    @staticmethod
    def _inject(messages: list, extra: str) -> list:
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
