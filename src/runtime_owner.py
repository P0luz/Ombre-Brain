"""M-04 process runtime owner (source-wired, not deployed by this batch)."""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
import contextvars
import functools
import json
from typing import Any, AsyncIterator, Awaitable, Callable, Coroutine, Iterator

from runtime_control import (
    AdmissionClosed,
    BackgroundComponent,
    BackgroundTaskRegistry,
    RuntimeAdmissionGate,
)
from service_quiescence import ServiceQuiescenceAdapter


admission_gate = RuntimeAdmissionGate()
background_registry = BackgroundTaskRegistry()
service_quiescence: ServiceQuiescenceAdapter | None = None


class EphemeralTaskTracker:
    """Own cancellable one-shot tasks that must not escape maintenance freeze."""

    def __init__(self) -> None:
        self._enabled = True
        self._tasks: set[Any] = set()

    def is_running(self) -> bool:
        # Always participate while enabled so freeze atomically disables future
        # spawns even when the current task set happens to be empty.
        return self._enabled

    def spawn(
        self, coroutine: Coroutine[Any, Any, Any], *, loop: Any = None
    ) -> Any:
        if not self._enabled:
            coroutine.close()
            raise AdmissionClosed("background task admission is closed")
        target_loop = loop or __import__("asyncio").get_running_loop()
        task = target_loop.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def stop(self) -> None:
        self._enabled = False
        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await __import__("asyncio").gather(*tasks, return_exceptions=True)

    async def start(self) -> None:
        self._enabled = True


ephemeral_tasks = EphemeralTaskTracker()
background_registry.register(
    BackgroundComponent(
        name="ephemeral-tasks",
        is_running=ephemeral_tasks.is_running,
        stop=ephemeral_tasks.stop,
        start=ephemeral_tasks.start,
    )
)


def spawn_background(
    coroutine: Coroutine[Any, Any, Any], *, loop: Any = None
) -> Any:
    return ephemeral_tasks.spawn(coroutine, loop=loop)


def blocking_background_component(
    name: str, is_running: Callable[[], bool]
) -> BackgroundComponent:
    """Register a one-shot activity that must finish before maintenance."""

    async def refuse_stop() -> None:
        raise RuntimeError(f"active background operation blocks maintenance: {name}")

    async def no_restart() -> None:
        return None

    return BackgroundComponent(
        name=name,
        is_running=is_running,
        stop=refuse_stop,
        start=no_restart,
    )


def configure_service_quiescence(
    *,
    components: tuple[BackgroundComponent, ...],
    close_derived: Callable[[], Awaitable[Any]],
    reopen_derived: Callable[[], Awaitable[Any]],
) -> ServiceQuiescenceAdapter:
    """Wire real runtime callbacks once; does not enter maintenance by itself."""

    global service_quiescence
    if service_quiescence is not None:
        raise RuntimeError("service quiescence is already configured")
    for component in components:
        background_registry.register(component)
    snapshot_box: dict[str, Any] = {}

    async def stop_background() -> None:
        snapshot_box["snapshot"] = await background_registry.freeze()

    async def start_background() -> None:
        snapshot = snapshot_box.pop("snapshot", None)
        if snapshot is None:
            raise RuntimeError("background registry has no frozen snapshot")
        await background_registry.resume(snapshot)

    service_quiescence = ServiceQuiescenceAdapter(
        stop_admission=admission_gate.close,
        active_writers=admission_gate.active_count,
        stop_background=stop_background,
        close_derived=close_derived,
        reopen_derived=reopen_derived,
        start_background=start_background,
        resume_admission=admission_gate.open,
    )
    return service_quiescence


def runtime_status() -> dict[str, Any]:
    admission = admission_gate.status()
    background = background_registry.status()
    quiet = service_quiescence.status() if service_quiescence is not None else None
    return {
        "wiring": "active" if quiet is not None else "unconfigured",
        "admission": {
            "state": admission.state,
            "active": admission.active,
            "generation": admission.generation,
            "failure": admission.failure,
        },
        "background": {
            "state": background.state,
            "generation": background.generation,
            "registered": list(background.registered),
            "failure": background.failure,
        },
        "quiescence": (
            {
                "phase": quiet.phase,
                "admission_stopped": quiet.admission_stopped,
                "background_stopped": quiet.background_stopped,
                "derived_closed": quiet.derived_closed,
                "active_publications": quiet.active_publications,
                "failure": quiet.failure,
            }
            if quiet is not None
            else None
        ),
    }


class _AdmissionToken:
    active = True


_token_var: contextvars.ContextVar[_AdmissionToken | None] = contextvars.ContextVar(
    "m04_runtime_admission", default=None
)


@asynccontextmanager
async def admitted() -> AsyncIterator[None]:
    inherited = _token_var.get()
    if inherited is not None and inherited.active:
        yield
        return
    async with admission_gate.lease():
        token = _AdmissionToken()
        reset = _token_var.set(token)
        try:
            yield
        finally:
            token.active = False
            _token_var.reset(reset)


@contextmanager
def admitted_sync() -> Iterator[None]:
    inherited = _token_var.get()
    if inherited is not None and inherited.active:
        yield
        return
    with admission_gate.lease_sync():
        token = _AdmissionToken()
        reset = _token_var.set(token)
        try:
            yield
        finally:
            token.active = False
            _token_var.reset(reset)


def runtime_admitted(function: Callable[..., Awaitable[Any]]):
    """Wrap one MCP/stdin async entrypoint in the process admission lease."""

    @functools.wraps(function)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        async with admitted():
            return await function(*args, **kwargs)

    return wrapped


_EXEMPT_HTTP_PATHS = frozenset({"/health"})
_EXEMPT_HTTP_PREFIXES = ("/.well-known/", "/oauth/", "/mcp")


class RuntimeAdmissionMiddleware:
    """Hold one admission lease for each finite HTTP/MCP request."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        if path in _EXEMPT_HTTP_PATHS or path.startswith(_EXEMPT_HTTP_PREFIXES):
            await self.app(scope, receive, send)
            return
        try:
            async with admitted():
                await self.app(scope, receive, send)
        except AdmissionClosed:
            body = json.dumps(
                {"ok": False, "error": "service maintenance in progress"},
                ensure_ascii=False,
            ).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [
                        (b"content-type", b"application/json; charset=utf-8"),
                        (b"retry-after", b"30"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
