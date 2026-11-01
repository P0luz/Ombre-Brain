"""
========================================
web/hooks.py — breath 浮现挂载点（HTTP hook）
========================================

- /breath-hook：对话开头由外部 hook 拉取，返回应浮现的记忆（pinned + 未解决采样）。
  protected 只防衰减，主池与 Letter/I 附加池都不通过 hook 主动注入。

不提供 /dream-hook：dream 按哲学不是义务、不该每次开场自动触发（详见下方端点处注释）。

给外部 SessionStart hook / 自动化用；默认需要 Dashboard 登录态或 hook token。
通过 sh.fire_webhook 推送事件。

对外暴露：register(mcp)。
========================================
"""

import asyncio
import os
import random
import threading
import time
from collections import OrderedDict, deque
from contextlib import asynccontextmanager

from ombrebrain.policy.surfacing import SurfacePolicyVM
from tools.i import disputing_candidates, superseded_by
from tools.plan.core import (
    is_letter_bucket,
    letter_lock_state,
    normalize_expired_lock,
)

from . import _shared as sh

logger = sh.logger
_SURFACE_POLICY = SurfacePolicyVM.default()

_HOOK_CONCURRENCY = 2
_HOOK_RATE_WINDOW_SECONDS = 60.0
_HOOK_RATE_SOURCE_LIMIT = 10
_HOOK_RATE_GLOBAL_LIMIT = 60
_HOOK_RATE_SOURCE_CAP = 2048
_HOOK_MIN_BLOCK_TOKENS = 120
_hook_slots = threading.BoundedSemaphore(_HOOK_CONCURRENCY)
_hook_rate_lock = threading.Lock()
_hook_source_events: OrderedDict[str, deque[float]] = OrderedDict()
_hook_global_events: deque[float] = deque()

try:
    from utils import strip_wikilinks, count_tokens_approx, get_ai_name  # type: ignore
except ImportError:  # pragma: no cover
    from ..utils import strip_wikilinks, count_tokens_approx, get_ai_name  # type: ignore


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _hook_setting(name: str, default=None):
    hooks_cfg = (getattr(sh, "config", {}) or {}).get("hooks") or {}
    return hooks_cfg.get(name, default)


def _header_value(request, name: str) -> str:
    headers = getattr(request, "headers", {}) or {}
    try:
        return str(headers.get(name, "") or "")
    except Exception:
        wanted = name.lower()
        for k, v in dict(headers).items():
            if str(k).lower() == wanted:
                return str(v or "")
    return ""


def _is_hook_request_authorized(request) -> bool:
    """Protect hook endpoints that can expose memory text.

    Public hooks can still be enabled deliberately with OMBRE_HOOK_ALLOW_PUBLIC=1
    or config hooks.allow_public=true. Otherwise a dashboard session or a hook
    token is required.
    """
    allow_public = _truthy(os.environ.get("OMBRE_HOOK_ALLOW_PUBLIC")) or _truthy(
        _hook_setting("allow_public")
    )
    if allow_public:
        return True

    token = (os.environ.get("OMBRE_HOOK_TOKEN") or str(_hook_setting("token", "") or "")).strip()
    if token:
        auth = _header_value(request, "authorization")
        supplied = [
            _header_value(request, "x-ombre-hook-token"),
            auth[7:] if auth.startswith("Bearer ") else "",
        ]
        if any(v and sh._constant_time_text_equal(v, token) for v in supplied):
            return True

    try:
        return bool(sh._is_authenticated(request))
    except Exception:
        return False


def _valid_hook_token(request) -> bool:
    token = (os.environ.get("OMBRE_HOOK_TOKEN") or str(_hook_setting("token", "") or "")).strip()
    if not token:
        return False
    auth = _header_value(request, "authorization")
    supplied = (
        _header_value(request, "x-ombre-hook-token"),
        auth[7:] if auth.startswith("Bearer ") else "",
    )
    return any(
        value and sh._constant_time_text_equal(value, token)
        for value in supplied
    )


def _hook_source_key(request) -> str:
    resolver = getattr(sh, "_client_key", None)
    if callable(resolver):
        try:
            return str(resolver(request))[:200]
        except Exception:
            pass
    client = getattr(request, "client", None)
    return str(getattr(client, "host", "unknown") or "unknown")[:200]


def _admit_hook_request(request) -> bool:
    """Bound provider-cost amplification with finite per-source/global state."""

    now = time.monotonic()
    cutoff = now - _HOOK_RATE_WINDOW_SECONDS
    key = _hook_source_key(request)
    with _hook_rate_lock:
        while _hook_global_events and _hook_global_events[0] <= cutoff:
            _hook_global_events.popleft()
        if len(_hook_global_events) >= _HOOK_RATE_GLOBAL_LIMIT:
            return False

        events = _hook_source_events.get(key)
        if events is None:
            events = deque()
            _hook_source_events[key] = events
        while events and events[0] <= cutoff:
            events.popleft()
        if len(events) >= _HOOK_RATE_SOURCE_LIMIT:
            _hook_source_events.move_to_end(key)
            return False

        events.append(now)
        _hook_global_events.append(now)
        _hook_source_events.move_to_end(key)
        while len(_hook_source_events) > _HOOK_RATE_SOURCE_CAP:
            _hook_source_events.popitem(last=False)
        return True


def _bounded_text(value, limit: int = 200) -> str:
    return str(value or "")[:limit]


@asynccontextmanager
async def _timeout_after(seconds: float):
    """Python 3.10-compatible total timeout that preserves external cancel."""

    task = asyncio.current_task()
    if task is None:
        yield
        return
    expired = False

    def cancel_for_timeout() -> None:
        nonlocal expired
        expired = True
        task.cancel()

    handle = asyncio.get_running_loop().call_later(max(0.0, seconds), cancel_for_timeout)
    try:
        yield
    except asyncio.CancelledError as exc:
        if expired:
            raise TimeoutError from exc
        raise
    finally:
        handle.cancel()


def register(mcp) -> None:

    @mcp.custom_route("/breath-hook", methods=["GET"])
    async def breath_hook(request):
        from starlette.responses import PlainTextResponse
        if not _is_hook_request_authorized(request):
            return PlainTextResponse("", status_code=401)

        # Token-authenticated SessionStart is the AI consumer.  A valid
        # Dashboard session is the human consumer.  Deliberately public hooks
        # remain unauthenticated and can never receive locked Letter content.
        if _valid_hook_token(request):
            caller_side = "ai"
        else:
            try:
                caller_side = "human" if sh._is_authenticated(request) else None
            except Exception:
                caller_side = None

        # This endpoint performs expensive provider work and is intended for a
        # non-browser SessionStart hook.  Do not let an ambient dashboard cookie
        # turn a cross-origin GET into provider spend; explicit hook tokens are
        # unaffected.
        public = _truthy(os.environ.get("OMBRE_HOOK_ALLOW_PUBLIC")) or _truthy(
            _hook_setting("allow_public")
        )
        cross_site = _header_value(request, "sec-fetch-site").strip().lower() == "cross-site"
        if (
            (_header_value(request, "origin") or cross_site)
            and not public
            and not _valid_hook_token(request)
        ):
            return PlainTextResponse("", status_code=403)
        if not _admit_hook_request(request):
            return PlainTextResponse("", status_code=429, headers={"Retry-After": "60"})
        if not _hook_slots.acquire(blocking=False):
            return PlainTextResponse("", status_code=429, headers={"Retry-After": "5"})

        def setting_int(name: str, default: int, minimum: int, maximum: int) -> int:
            try:
                value = int(_hook_setting(name, default))
            except (TypeError, ValueError, OverflowError):
                value = default
            return max(minimum, min(maximum, value))

        timeout_seconds = setting_int("timeout_seconds", 45, 5, 120)
        per_call_timeout = setting_int("dehydrate_timeout_seconds", 12, 2, 30)
        max_dehydrate_calls = setting_int("max_dehydrate_calls", 8, 0, 32)
        token_budget = setting_int("max_tokens", 10_000, 500, 50_000)
        no_store_headers = {
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        }

        try:
            async with _timeout_after(timeout_seconds):
                all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
                pinned = [
                    bucket for bucket in all_buckets
                    if _truthy(bucket["metadata"].get("pinned"))
                    and not _truthy(bucket["metadata"].get("protected"))
                    and _SURFACE_POLICY.evaluate_bucket(
                        bucket, mode="spontaneous"
                    ).allowed
                    and not is_letter_bucket(bucket)
                ]
                pinned.sort(
                    key=lambda bucket: (
                        int(bucket["metadata"].get("importance", 0) or 0),
                        str(bucket["metadata"].get("created", "")),
                    ),
                    reverse=True,
                )
                unresolved = [
                    bucket for bucket in all_buckets
                    if not bucket["metadata"].get("resolved", False)
                    and bucket["metadata"].get("type")
                    not in ("permanent", "feel", "plan", "letter", "self", "i")
                    and not _truthy(bucket["metadata"].get("pinned"))
                    and not _truthy(bucket["metadata"].get("protected"))
                    and not is_letter_bucket(bucket)
                    and _SURFACE_POLICY.evaluate_bucket(
                        bucket, mode="spontaneous"
                    ).allowed
                ]
                scored = sorted(
                    unresolved,
                    key=lambda bucket: sh.decay_engine.calculate_score(bucket["metadata"]),
                    reverse=True,
                )

                header = "[Ombre Brain - 记忆浮现]\n"
                remaining = token_budget - count_tokens_approx(header)
                parts: list[str] = []
                dehydrate_calls = 0

                def append_block(block: str) -> bool:
                    nonlocal remaining
                    cost = count_tokens_approx(block) + 2
                    if cost > remaining:
                        return False
                    parts.append(block)
                    remaining -= cost
                    return True

                async def append_summary(bucket: dict, *, prefix: str) -> bool:
                    nonlocal dehydrate_calls
                    if remaining < _HOOK_MIN_BLOCK_TOKENS:
                        return False
                    raw = strip_wikilinks(str(bucket.get("content") or ""))
                    if not raw:
                        return True
                    if dehydrate_calls >= max_dehydrate_calls:
                        return False
                    dehydrate_calls += 1
                    try:
                        summary = await asyncio.wait_for(
                            sh.dehydrator.dehydrate(
                                raw,
                                {
                                    key: value
                                    for key, value in (bucket.get("metadata") or {}).items()
                                    if key != "tags"
                                },
                            ),
                            timeout=per_call_timeout,
                        )
                    except Exception as exc:
                        logger.warning("breath_hook dehydration failed: %s", exc)
                        summary = raw[:1200]
                    summary = str(summary or "").strip()
                    if not summary:
                        summary = raw[:1200]
                    return append_block(prefix + summary)

                for bucket in pinned:
                    if not await append_summary(bucket, prefix="📌 [核心准则] "):
                        break

                candidates = list(scored)
                if len(candidates) > 1:
                    pool = candidates[1:min(20, len(candidates))]
                    random.shuffle(pool)
                    candidates = [candidates[0], *pool]
                for bucket in candidates[:20]:
                    if not await append_summary(bucket, prefix=""):
                        break

                letters = [
                    bucket for bucket in all_buckets
                    if is_letter_bucket(bucket)
                    and not _truthy(bucket["metadata"].get("protected"))
                ]
                normalized_letters = []
                letter_states = {}
                for letter in letters:
                    state = letter_lock_state(letter, caller_side)
                    letter, state = await normalize_expired_lock(
                        letter,
                        state,
                        caller_side,
                        bucket_mgr=sh.bucket_mgr,
                    )
                    if not letter:
                        continue
                    normalized_letters.append(letter)
                    letter_states[letter["id"]] = state
                letters = normalized_letters
                if letters:
                    def latest(*authors: str) -> dict | None:
                        wanted = set(authors)
                        pool = [
                            letter for letter in letters
                            if letter["metadata"].get("author") in wanted
                            and not letter_states[letter["id"]]["locked"]
                        ]
                        if not pool:
                            return None
                        pool.sort(
                            key=lambda bucket: (
                                bucket["metadata"].get("letter_date")
                                or bucket["metadata"].get("created", "")
                            ),
                            reverse=True,
                        )
                        return pool[0]

                    for tag, letter in (
                        ("user→你", latest("user")),
                        ("你→user", latest(get_ai_name(), "claude")),
                    ):
                        if letter is None:
                            continue
                        meta = letter["metadata"]
                        state = letter_states[letter["id"]]
                        if state["stored_lock_type"] != "none":
                            # Locked Letters created by V1 always snapshot the
                            # actual writer name.  Even the owner's full-text
                            # excerpt must not introduce generic side labels.
                            tag = str(meta.get("writer_name") or "").strip() or tag
                        date = meta.get("letter_date") or str(meta.get("created", ""))[:10]
                        title = _bounded_text(meta.get("title") or meta.get("name"), 200)
                        excerpt = strip_wikilinks(str(letter.get("content") or ""))[:400]
                        append_block(
                            f"💌 [{tag}] {date}{(' · ' + title) if title else ''}\n{excerpt}"
                        )

                    # Locked incoming Letters are an independent existence
                    # signal.  Do not let a newer ordinary Letter hide an older
                    # still-locked one, and do not change the normal "latest
                    # visible letter per direction" injection above.
                    if caller_side is not None:
                        incoming_by_writer: dict[str, list[tuple[dict, dict]]] = {}
                        for letter in letters:
                            state = letter_states[letter["id"]]
                            if not state["locked"]:
                                continue
                            meta = letter.get("metadata") or {}
                            writer_name = str(meta.get("writer_name") or "").strip()
                            if not writer_name:
                                continue
                            incoming_by_writer.setdefault(writer_name, []).append(
                                (letter, state)
                            )

                        for writer_name, incoming in incoming_by_writer.items():
                            _representative, state = incoming[0]
                            if len(incoming) > 1:
                                notice = f"{writer_name}给你留了 {len(incoming)} 封仍未解锁的信。"
                            elif state["lock_type"] == "timed":
                                when = str(state["unlock_date"] or "").replace("T", " ")[:16]
                                notice = f"{writer_name}给你留了一封带锁的信，将于 {when} 解锁。"
                            else:
                                notice = f"{writer_name}给你留了一封永久锁信，当前不可查看。"
                            append_block(notice)

                self_buckets = [
                    bucket for bucket in all_buckets
                    if not is_letter_bucket(bucket)
                    and not _truthy(bucket["metadata"].get("protected"))
                    and (
                        bucket["metadata"].get("type") == "i"
                        or "__i__" in (bucket["metadata"].get("tags") or [])
                    )
                ]
                self_buckets.sort(
                    key=lambda bucket: bucket["metadata"].get("created", ""),
                    reverse=True,
                )

                # 已被取代的、以及此刻正被自己的候选质疑的，都不占这三个名额。
                #
                # 这三条是模型每次开场读到的「我是谁」，读进去就是现在时的断言。
                # 一条自己已经写下质疑的旧认识继续坐在这里，就是拿一个已知有疑的
                # 信念当真理用——而且新的那条还在候选区排队，短期内换不上来。
                # 挪走它不等于给不出答案：名额让给下一条真的还成立的认识。
                buckets_by_id = {bucket["id"]: bucket for bucket in all_buckets}
                live_disputes: list[str] = []
                current_self: list[dict] = []
                for bucket in self_buckets:
                    if superseded_by(bucket):
                        continue
                    if disputing_candidates(bucket, buckets_by_id):
                        live_disputes.append(bucket["id"])
                        continue
                    current_self.append(bucket)

                for bucket in current_self[:3]:
                    meta = bucket["metadata"]
                    tags = meta.get("tags") or []
                    aspect = next(
                        (
                            _bounded_text(tag, 100).removeprefix("aspect:")
                            for tag in tags
                            if isinstance(tag, str) and tag.startswith("aspect:")
                        ),
                        "",
                    )
                    raw = strip_wikilinks(str(bucket.get("content") or ""))
                    excerpt = raw[:300]
                    # 早期条目是直写进来的，没经过任何碰撞。I(read=True) 一直
                    # 标着这件事，而这里——模型每次会话开头真正形成自我感的
                    # 那条路径——反而不标，于是没检验过的和沉淀下来的长得一样。
                    origin = "" if meta.get("i_from_candidate") else "（未经沉淀）"
                    append_block(
                        f"🪞{str(meta.get('created') or '')[:10]}"
                        f"{f' [{aspect}]' if aspect else ''}{origin}\n{excerpt}"
                    )

                # 只说「你正在改这几条」，不把正文带回来——带回来就等于没挪走。
                if live_disputes:
                    append_block(
                        f"🪞你正在改其中 {len(live_disputes)} 条对自己的看法"
                        f"（{'、'.join(live_disputes[:3])}），"
                        "新的还没沉淀下来。I(read=True) 能看到它们。"
                    )

                if not parts:
                    try:
                        await asyncio.wait_for(
                            sh.fire_webhook("breath_hook", {"surfaced": 0}),
                            timeout=3,
                        )
                    except Exception as exc:
                        logger.warning("breath_hook telemetry failed: %s", exc)
                    return PlainTextResponse("", headers=no_store_headers)

                body_text = header + "\n---\n".join(parts)
                try:
                    await asyncio.wait_for(
                        sh.fire_webhook(
                            "breath_hook",
                            {"surfaced": len(parts), "chars": len(body_text)},
                        ),
                        timeout=3,
                    )
                except Exception as exc:
                    logger.warning("breath_hook telemetry failed: %s", exc)
                return PlainTextResponse(body_text, headers=no_store_headers)
        except TimeoutError:
            logger.warning("Breath hook exceeded %ss total timeout", timeout_seconds)
            return PlainTextResponse(
                "",
                status_code=504,
                headers={**no_store_headers, "Retry-After": "10"},
            )
        except Exception as e:
            logger.warning(f"Breath hook failed: {e}")
            return PlainTextResponse("", headers=no_store_headers)
        finally:
            _hook_slots.release()

    # 注意：这里**故意不再提供 /dream-hook**。
    # 按 OB 的设计哲学，dream（做梦消化）不是义务、不该在每次会话开始被自动触发——
    # 它只应在「需要消化时」由模型主动调用 MCP 的 dream 工具。把它做成 SessionStart hook
    # 会把「主动消化」异化成「每次开场的强制动作」，与哲学冲突，故移除该端点。
