"""
========================================
web/autorecall.py — Claude Code 自动召回的四个钩子端点
========================================

给 deploy/claude-code-hooks/ 里的脚本调用，逻辑都在 ombrebrain.autorecall.service：

- POST /autorecall/session-start  {session_id, source}           → {"context": str}  开窗 / 压缩后接续 + 今日 dream
- POST /autorecall/prompt         {session_id, prompt, prompt_id} → {"context": str}  存用户原文 + hold 提醒 + 唤醒召回
- POST /autorecall/stop           {session_id, messages:[...]}   → {"added", "links"} 存这一轮助手回复
- POST /autorecall/session-end    {session_id, messages:[...]}   → {"added", "links"} 关窗时整段补存

messages 每项 {"key": 消息 uuid, "role": "user"|"assistant", "text": str}。

另有面板用的设置接口（Dashboard 登录态）：
- GET  /api/settings/autorecall → 当前设置 + 原文库计数
- POST /api/settings/autorecall → 部分更新；先写 config.yaml 再改运行时，写盘失败就什么都不改

鉴权、跨站防护与 /breath-hook 相同（hook token 或 Dashboard 登录态）。限流单独计：
prompt / stop 每轮各打一次，/breath-hook 每分钟 10 次的额度对它们太紧。
开关关着时一律 200 + 空结果，钩子脚本什么都不注入、什么都不存。

对外暴露：register(mcp)。
========================================
"""

import asyncio
import json
import os
import threading
import time
from collections import OrderedDict, deque

from starlette.responses import JSONResponse

from . import _shared as sh

try:
    from ombrebrain.autorecall import autorecall_config
    from utils import atomic_update_config_yaml  # type: ignore
except ImportError:  # pragma: no cover
    from ..ombrebrain.autorecall import autorecall_config  # type: ignore
    from ..utils import atomic_update_config_yaml  # type: ignore
from .hooks import _header_value, _hook_setting, _is_hook_request_authorized, _truthy, _valid_hook_token

logger = sh.logger

_MAX_BODY_BYTES = 2 * 1024 * 1024
_MAX_MESSAGES = 2000
_RATE_WINDOW_SECONDS = 60.0
_RATE_SOURCE_LIMIT = 120
_RATE_GLOBAL_LIMIT = 600
_RATE_SOURCE_CAP = 1024
_TIMEOUTS = {"session-start": 40.0, "prompt": 10.0, "stop": 20.0, "session-end": 30.0}
_NO_STORE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}

_rate_lock = threading.Lock()
_source_events: OrderedDict[str, deque[float]] = OrderedDict()
_global_events: deque[float] = deque()


def _admit(request) -> bool:
    now = time.monotonic()
    cutoff = now - _RATE_WINDOW_SECONDS
    try:
        key = str(sh._client_key(request))[:200]
    except Exception:
        key = "unknown"
    with _rate_lock:
        while _global_events and _global_events[0] <= cutoff:
            _global_events.popleft()
        if len(_global_events) >= _RATE_GLOBAL_LIMIT:
            return False
        events = _source_events.setdefault(key, deque())
        while events and events[0] <= cutoff:
            events.popleft()
        if len(events) >= _RATE_SOURCE_LIMIT:
            return False
        events.append(now)
        _global_events.append(now)
        _source_events.move_to_end(key)
        while len(_source_events) > _RATE_SOURCE_CAP:
            _source_events.popitem(last=False)
        return True


def _reject(request) -> JSONResponse | None:
    if not _is_hook_request_authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401, headers=_NO_STORE)
    public = _truthy(_hook_setting("allow_public"))
    cross_site = _header_value(request, "sec-fetch-site").strip().lower() == "cross-site"
    if (_header_value(request, "origin") or cross_site) and not public and not _valid_hook_token(request):
        return JSONResponse({"error": "cross-site"}, status_code=403, headers=_NO_STORE)
    if not _admit(request):
        return JSONResponse({"error": "rate limited"}, status_code=429, headers={**_NO_STORE, "Retry-After": "60"})
    return None


async def _body(request) -> dict:
    declared = _header_value(request, "content-length")
    if declared.isdigit() and int(declared) > _MAX_BODY_BYTES:
        raise ValueError("body too large")
    raw = await request.body()
    if len(raw) > _MAX_BODY_BYTES:
        raise ValueError("body too large")
    body = json.loads(raw.decode("utf-8") or "{}")
    if not isinstance(body, dict):
        raise ValueError("JSON body must be an object")
    session_id = str(body.get("session_id") or "").strip()
    if not session_id or len(session_id) > 200:
        raise ValueError("session_id is required")
    body["session_id"] = session_id
    return body


def _messages(body: dict) -> list[dict]:
    raw = body.get("messages")
    if not isinstance(raw, list) or len(raw) > _MAX_MESSAGES:
        raise ValueError("messages must be a list")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("message must be an object")
        out.append({
            "key": str(item.get("key") or "")[:200],
            "role": str(item.get("role") or ""),
            "text": str(item.get("text") or ""),
        })
    return out


_BOOL_KEYS = ("enabled", "dream_on_start", "wake_on_names", "vector")
_INT_KEYS = {"reminder_every": (0, 500), "restore_rounds": (0, 50), "agree_top": (0, 20)}


def _validate_settings(body: dict) -> dict:
    unknown = set(body) - set(_BOOL_KEYS) - set(_INT_KEYS) - {"wake_words"}
    if unknown:
        raise ValueError(f"不支持的字段：{', '.join(sorted(unknown))}")
    out: dict = {}
    for key in _BOOL_KEYS:
        if key in body:
            if not isinstance(body[key], bool):
                raise ValueError(f"{key} 必须是 true / false")
            out[key] = body[key]
    for key, (low, high) in _INT_KEYS.items():
        if key in body:
            value = body[key]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{key} 必须是 {low}～{high} 的整数")
            out[key] = value
    if "wake_words" in body:
        words = body["wake_words"]
        if isinstance(words, str):
            words = words.replace("，", ",").split(",")
        if not isinstance(words, list) or not all(isinstance(w, str) for w in words):
            raise ValueError("wake_words 必须是字符串列表")
        cleaned = list(dict.fromkeys(w.strip() for w in words if w.strip()))
        if len(cleaned) > 100 or any(len(w) > 20 for w in cleaned):
            raise ValueError("唤醒词最多 100 个，每个不超过 20 字")
        out["wake_words"] = cleaned
    return out


def _settings_payload() -> dict:
    settings = autorecall_config(getattr(sh, "config", None))
    service = getattr(sh, "autorecall_service", None)
    try:
        counts = service.store.counts() if service is not None else {}
    except Exception:
        counts = {}
    token_set = bool(str(_hook_setting("token", "") or "").strip()) or bool(
        os.environ.get("OMBRE_HOOK_TOKEN", "").strip()
    )
    return {**settings, "store": counts, "hook_token_configured": token_set}


def register(mcp) -> None:
    settings_lock = threading.Lock()

    @mcp.custom_route("/api/settings/autorecall", methods=["GET"])
    async def get_autorecall_setting(request):
        error = sh._require_auth(request)
        if error:
            return error
        return JSONResponse(_settings_payload(), headers=_NO_STORE)

    @mcp.custom_route("/api/settings/autorecall", methods=["POST"])
    async def set_autorecall_setting(request):
        error = sh._require_auth(request)
        if error:
            return error
        try:
            body = await sh._read_json_object(request)
            values = _validate_settings(body)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc) or "无效 JSON"}, status_code=400)

        def _mutate(save_config: dict) -> None:
            section = save_config.get("autorecall")
            if not isinstance(section, dict):
                section = {}
                save_config["autorecall"] = section
            section.update(values)

        with settings_lock:
            try:
                await asyncio.to_thread(atomic_update_config_yaml, _mutate)
            except Exception as exc:
                logger.warning("autorecall settings persist failed: %s", type(exc).__name__)
                return JSONResponse({"error": "保存到 config.yaml 失败，设置未改动"}, status_code=503)
            runtime = sh.config.get("autorecall")
            if not isinstance(runtime, dict):
                runtime = {}
                sh.config["autorecall"] = runtime
            runtime.update(values)
        return JSONResponse(_settings_payload(), headers=_NO_STORE)

    async def _run(request, name: str, handler):
        rejected = _reject(request)
        if rejected is not None:
            return rejected
        service = getattr(sh, "autorecall_service", None)
        if service is None or not service.settings["enabled"]:
            return JSONResponse({"disabled": True}, headers=_NO_STORE)
        try:
            body = await _body(request)
        except (ValueError, UnicodeDecodeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400, headers=_NO_STORE)
        try:
            result = await asyncio.wait_for(handler(service, body), timeout=_TIMEOUTS[name])
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400, headers=_NO_STORE)
        except asyncio.TimeoutError:
            logger.warning("autorecall %s exceeded %ss", name, _TIMEOUTS[name])
            return JSONResponse({"error": "timeout"}, status_code=504, headers={**_NO_STORE, "Retry-After": "10"})
        except Exception as exc:
            # 钩子失败只记日志、返回空：不能让记忆出错卡住用户的对话。
            logger.warning("autorecall %s failed: %s", name, type(exc).__name__)
            return JSONResponse({"error": "failed"}, status_code=500, headers=_NO_STORE)
        return JSONResponse(result, headers=_NO_STORE)

    async def _session_start(service, body):
        source = str(body.get("source") or "startup")
        return {"context": await service.on_session_start(body["session_id"], source)}

    async def _prompt(service, body):
        key = str(body.get("prompt_id") or "")[:200]
        return {"context": await service.on_prompt(
            body["session_id"], str(body.get("prompt") or "")[:20000], key=f"u:{key}" if key else ""
        )}

    async def _stop(service, body):
        return await service.on_stop(body["session_id"], _messages(body))

    async def _session_end(service, body):
        return await service.on_session_end(body["session_id"], _messages(body))

    for name, handler in (
        ("session-start", _session_start),
        ("prompt", _prompt),
        ("stop", _stop),
        ("session-end", _session_end),
    ):
        async def endpoint(request, _name=name, _handler=handler):
            return await _run(request, _name, _handler)

        mcp.custom_route(f"/autorecall/{name}", methods=["POST"])(endpoint)
