"""
========================================
tools/_runtime.py — 工具模块共享的运行时上下文
========================================

这个文件解决一个工程问题：拆分后每个工具子模块都需要访问
config / bucket_mgr / dehydrator / decay_engine / embedding_engine /
logger 这些 server.py 创建的全局对象，但子模块不能反向 import
server.py（会循环 import）。

做法：server.py 在初始化所有组件后调用 init(...) 把引用塞进来；
工具模块全部 `from . import _runtime as rt` 然后用 `rt.bucket_mgr` 即可。

关键行为：
- 提供一个轻量级容器，保存共享对象的引用
- init() 一次性写入；后续工具模块直接读，不修改

不做什么（边界）：
- 不创建任何对象，不做配置加载，不做日志初始化
- 不做线程安全保护：写入只发生在 server.py 启动期，单次

对外暴露：init() / config / bucket_mgr / dehydrator / decay_engine /
         embedding_engine / import_engine / logger / fire_webhook / mark_op
========================================
"""

from typing import Any, Awaitable, Callable, Optional

from ombrebrain.app.execution import ExecutionEnvelope

# --- 共享对象引用，由 server.py 在启动时通过 init(...) 注入 ---
config: Any = None
bucket_mgr: Any = None
deletion_requests: Any = None
dehydrator: Any = None
decay_engine: Any = None
embedding_engine: Any = None
embedding_outbox: Any = None
import_engine: Any = None
source_store: Any = None
you_service: Any = None
them_service: Any = None
logger: Any = None
v3_runtime: Any = None

# --- 共享辅助回调（也由 server.py 注入，避免反向 import）---
fire_webhook: Optional[Callable[[str, dict], Awaitable[None]]] = None
mark_op: Optional[Callable[..., None]] = None


def init(**kwargs: Any) -> None:
    """server.py 在创建好所有组件后调用一次，把引用写到本模块全局上。
    测试 fixture 可以再次调用本函数覆盖个别字段，行为同 monkeypatch。"""
    g = globals()
    defaults = g.get("_DEFAULT_RUNTIME_HELPERS")
    if isinstance(defaults, dict):
        for name, fn in defaults.items():
            g[name] = fn
    for k, v in kwargs.items():
        g[k] = v


def _warn(message: str, *args: Any) -> None:
    log = globals().get("logger")
    warning = getattr(log, "warning", None)
    if callable(warning):
        try:
            warning(message, *args)
        except Exception:
            pass


def run_v3_capability(
    name: str,
    payload: Any,
    *,
    permissions: tuple[str, ...] = (),
    actor_name: str = "legacy-tool",
    source: str = "tools",
) -> Any:
    """Best-effort dispatch into the v3 capability registry."""
    runtime = globals().get("v3_runtime")
    dispatch = getattr(runtime, "dispatch_capability", None)
    if not callable(dispatch):
        return None
    try:
        return dispatch(
            name,
            payload,
            permissions=tuple(permissions),
            actor_name=actor_name,
            source=source,
        )
    except Exception as exc:
        _warn("v3 capability dispatch failed for %s: %s", name, exc)
        return None


def record_v3_tool_event(tool_name: str, payload: dict[str, Any] | None = None) -> Any:
    """Record a legacy tool call in the v3 side channel without affecting output."""
    runtime = globals().get("v3_runtime")
    recorder = getattr(runtime, "record_tool_event", None)
    if not callable(recorder):
        return None

    name = str(tool_name)
    event_payload = dict(payload or {})
    planner = getattr(runtime, "plan_legacy_command", None)
    if callable(planner):
        try:
            envelope = ExecutionEnvelope(
                module=f"tools.{name}",
                operation=name,
                payload=event_payload,
                actor_name="legacy-tool",
                source="tools.record_v3_tool_event",
                permissions=("mcp:call",),
            )
            plan = planner(envelope)
            event_payload["command_plan"] = (
                plan.to_dict() if hasattr(plan, "to_dict") else plan
            )
        except Exception as exc:
            _warn("v3 tool command planning failed for %s: %s", name, exc)
    try:
        return recorder(name, event_payload)
    except Exception as exc:
        _warn("v3 tool event record failed for %s: %s", name, exc)
        return None


def run_v3_operation(
    operation: str,
    payload: dict[str, Any] | None,
    handler: Callable[[], Any],
    *,
    module: str,
    permissions: tuple[str, ...] = (),
    required_permissions: tuple[str, ...] = (),
    actor_name: str = "legacy-tool",
    source: str = "tools",
    capability: str = "",
    writes_memory: bool = False,
    protected_paths: tuple[str, ...] = (),
    feature_flags: tuple[str, ...] = (),
) -> Any:
    runtime = globals().get("v3_runtime")
    runner = getattr(runtime, "run_operation", None)
    if not callable(runner):
        return handler()
    envelope = ExecutionEnvelope(
        module=module,
        operation=operation,
        payload=payload or {},
        actor_name=actor_name,
        source=source,
        permissions=permissions,
        required_permissions=required_permissions,
        capability=capability,
        writes_memory=writes_memory,
        protected_paths=protected_paths,
        feature_flags=feature_flags,
    )
    return runner(envelope, handler)


async def run_v3_async_operation(
    operation: str,
    payload: dict[str, Any] | None,
    handler: Callable[[], Awaitable[Any]],
    *,
    module: str,
    permissions: tuple[str, ...] = (),
    required_permissions: tuple[str, ...] = (),
    actor_name: str = "legacy-tool",
    source: str = "tools",
    capability: str = "",
    writes_memory: bool = False,
    protected_paths: tuple[str, ...] = (),
    feature_flags: tuple[str, ...] = (),
) -> Any:
    runtime = globals().get("v3_runtime")
    runner = getattr(runtime, "run_async_operation", None)
    if not callable(runner):
        return await handler()
    envelope = ExecutionEnvelope(
        module=module,
        operation=operation,
        payload=payload or {},
        actor_name=actor_name,
        source=source,
        permissions=permissions,
        required_permissions=required_permissions,
        capability=capability,
        writes_memory=writes_memory,
        protected_paths=protected_paths,
        feature_flags=feature_flags,
    )
    return await runner(envelope, handler)


_DEFAULT_RUNTIME_HELPERS = {
    "run_v3_capability": run_v3_capability,
    "record_v3_tool_event": record_v3_tool_event,
    "run_v3_operation": run_v3_operation,
    "run_v3_async_operation": run_v3_async_operation,
}
