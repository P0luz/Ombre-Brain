"""Runtime-scoped idempotency for public ``grow`` retries.

``grow`` may outlive an MCP/client response deadline because dehydration and
bucket creation are intentionally completed before the tool returns.  A user
who retries the same payload must therefore join/reuse the original operation
instead of starting a second dehydration pass and creating duplicate buckets.

The guard is deliberately bounded and process-local: it only recognizes exact
payload retries for a short window.  A later intentional grow of the same text
remains possible, and a failed operation is never cached.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field


RETRY_WINDOW_SECONDS = 30 * 60

_IN_PROGRESS_MESSAGE = (
    "⏳ 相同的 grow 仍在后台处理中；无需重复提交，完成后会自动入库。"
)
_REUSED_RESULT_PREFIX = "✅ 已识别为刚才 grow 的重试；未重复写入。\n"


@dataclass
class _LoopState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    inflight: dict[str, asyncio.Task[str]] = field(default_factory=dict)
    completed: dict[str, tuple[float, str]] = field(default_factory=dict)


_states: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _LoopState] = (
    weakref.WeakKeyDictionary()
)


def request_fingerprint(*, content: str, items: list | None, test_data: bool) -> str:
    """Return a privacy-preserving fingerprint for the exact public request."""

    normalized_content = (content or "").replace("\r\n", "\n").strip()
    payload = {
        "content": normalized_content,
        "items": items,
        "test_data": bool(test_data),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _state_for_running_loop() -> _LoopState:
    loop = asyncio.get_running_loop()
    state = _states.get(loop)
    if state is None:
        state = _LoopState()
        _states[loop] = state
    return state


def _consume_background_exception(task: asyncio.Task[str]) -> None:
    """Avoid an unobserved-task warning if the original client disconnected."""

    if not task.cancelled():
        task.exception()


async def run_once(
    fingerprint: str,
    operation: Callable[[], Awaitable[str]],
    *,
    retry_window_seconds: float = RETRY_WINDOW_SECONDS,
) -> str:
    """Run one grow request and safely recognize exact retries.

    The operation is shielded from cancellation by the request handler.  An
    identical retry while it is running receives an immediate progress result;
    a retry after completion reuses the prior result.  Exceptions remove the
    entry so a genuine failure can be retried normally.
    """

    state = _state_for_running_loop()
    now = time.monotonic()
    async with state.lock:
        expired = [
            key
            for key, (finished_at, _result) in state.completed.items()
            if now - finished_at > retry_window_seconds
        ]
        for key in expired:
            state.completed.pop(key, None)

        completed = state.completed.get(fingerprint)
        if completed is not None:
            return _REUSED_RESULT_PREFIX + completed[1]

        if fingerprint in state.inflight:
            return _IN_PROGRESS_MESSAGE

        async def execute() -> str:
            try:
                result = await operation()
            except BaseException:
                async with state.lock:
                    state.inflight.pop(fingerprint, None)
                raise
            async with state.lock:
                state.inflight.pop(fingerprint, None)
                state.completed[fingerprint] = (time.monotonic(), result)
            return result

        task = asyncio.create_task(execute(), name=f"grow:{fingerprint[:12]}")
        task.add_done_callback(_consume_background_exception)
        state.inflight[fingerprint] = task

    return await asyncio.shield(task)


def reset_for_tests() -> None:
    """Clear process-local state.  Tests only; production never calls this."""

    _states.clear()
