"""
========================================
tools/_common.py — 跨工具共享的辅助逻辑
========================================

这个文件收纳被多个工具同时复用的、与具体工具语义无关的小工具：
配额检查（单桶字节上限 / pinned/protected 数量上限）、
合并或新建（hold/grow 共用）、
新桶疑似重复扫描、新事件触发的 plan 完成建议判定。

关键行为：
- check_content_size / check_pinned_quota / check_protected_quota：读取
  config.limits，超限返回中文提示串
- merge_or_create：先用语义检索找近似桶；超过阈值则合并（hold 用原文拼接，
  grow 用 LLM 压缩），否则新建；写完投递 embedding 队列并刷新脱水缓存
- iter 2.0：merge_or_create 接受 ``source_tool`` / ``grow_batch_id``，
  新建时写入 frontmatter；合并时不动原桶 source_tool，只追加 ``last_merged_by``
- check_duplicate_for：fire-and-forget 标记疑似重复对（不自动合并）
- check_plan_resolution：fire-and-forget 用关键词/向量双通道预筛 + LLM 保守判断，
  只记录可能已完成的建议，保留 active 状态等待显式确认

不做什么（边界）：
- 不持有任何全局对象，所有依赖都从 _runtime 取
- 不做日志格式化以外的副作用包装；调用方自行决定是否 await

对外暴露：limits_cfg / max_bucket_bytes / max_pinned / max_protected /
         check_content_size / count_pinned / count_protected /
         check_pinned_quota / check_protected_quota / restore_archived_letters /
         merge_or_create /
         check_duplicate_for / check_plan_resolution
========================================
"""

from typing import Tuple
import asyncio
from concurrent.futures import Future, InvalidStateError
from contextlib import AsyncExitStack, asynccontextmanager
import hashlib
import math
import threading

from bucket_manager import _filesystem_turn as _kernel_filesystem_turn
from snapshot_barrier import markdown_writer_turn
from utils import normalize_memory_title, now_iso, parse_bool
from ombrebrain.domain.plan_history import append_plan_change_log as append_plan_change_log

from . import _identity, _runtime as rt


def is_logical_letter(bucket: dict) -> bool:
    """Use the canonical spoof-resistant Letter classifier at generic seams."""
    from .plan.core import is_letter_bucket

    return is_letter_bucket(bucket)


_EMBED_WARN = (
    "向量暂未完成，该桶当前仅支持关键词匹配；正文已保存。"
    "请检查向量队列与 embedding 提供商配置后重试补齐。"
)

# ============================================================
# 常量 / Named constants
# ------------------------------------------------------------
# rule.md §①：禁止裸魔法数字。下面这些原本散在 helper 默认参数与
# 业务逻辑中，集中后调参一眼看完。
# ============================================================

# --- 桶与配额默认值 ---
_DEFAULT_MAX_BUCKET_BYTES = 50 * 1024  # 50 KB 单桶上限（超过建议走 grow 拆存）
_DEFAULT_MAX_PINNED = 20               # pinned 桶上限（哲学边界：重要必须稀缺）；与 config.example.yaml limits.max_pinned 同步
_DEFAULT_MAX_PROTECTED = 20            # protected 桶独立上限；与 config.example.yaml limits.max_protected 同步
_DEFAULT_MAX_GROW_INPUT_BYTES = 2 * 1024 * 1024
_DEFAULT_MAX_QUERY_BYTES = 16 * 1024
_DEFAULT_MAX_METADATA_BYTES = 16 * 1024
_DEFAULT_MAX_GROW_ITEMS = 100
_WHY_REMEMBERED_MAX_CHARS = 500
_GROW_ITEM_FIELDS = frozenset({
    "content", "title", "name", "tags", "importance", "domain",
    "valence", "arousal", "source_ranges", "why_remembered",
    # quotes 只在 grow(items=[...]) 这条路上有效：items 是我自己拆好的，
    # 每条都经过我的手。digest 路径（grow(content=...)）拆出来的条目是 LLM
    # 的产物，我没有逐条决定过——那里不该有引语，见架构说明 §5.2「谁决定」。
    "quotes",
})

# --- importance 审计范围排除的类型（is_importance_audit_candidate 复用）---
# 注意：这不是配额机制。rule.md §2 的稀缺性哲学由 pinned(20)/anchor(24) 两个
# 结构承担；importance 只是普通评分字段，不再对 >=9 设硬配额/自动降级。
_HIGH_IMP_EXEMPT_TYPES = frozenset({"feel", "plan", "letter", "archived"})
_HIGH_IMP_THRESHOLD = 9
_HIGH_IMP_HARD_CAP = 24
_HIGH_IMP_SOFT_WARN = 22
_HIGH_IMP_DEGRADE_TO = 8

# --- pinned 软阈值 ---
_PINNED_SOFT_GAP = 2                   # “软阈值 = cap - GAP”；cap=20 → soft=18

# --- check_duplicate_for / check_plan_resolution ---
_DUP_DEFAULT_THRESHOLD = 0.95          # 向量相似 >= 该值 → 标为疑似重复
_DUP_TOPK = 10                         # 检索前 N 个候选以判重复
_DUP_CHECK_CONCURRENCY = 4             # fire-and-forget 疑似重复检测的并发上限
_dup_check_semaphore = asyncio.Semaphore(_DUP_CHECK_CONCURRENCY)
_PLAN_VECTOR_TOPK = 20                 # plan 判定的向量预筛范围
_PLAN_VECTOR_THRESHOLD = 0.7           # 超过才交给 LLM 判定是否已完成
_PLAN_LLM_CONFIDENCE_MIN = 0.7         # LLM judgement.confidence 下限
_SAME_EVENT_CONFIDENCE_MIN = 0.85      # 自动合并必须高置信，疑似时新建
_PLAN_FALLBACK_CAP = 10                # 无向量时直接送 LLM 的 plan 上限（防止过多 LLM 调用）

# --- 字段截断长度（下游存储 / 日志可读性）---
_RESOLUTION_REASON_MAX = 200           # 写入桶 frontmatter 的理由上限
_LOG_REASON_PREVIEW = 60               # 日志里预览的理由长度

# --- content lock 哈希 key 长度 ---
_CONTENT_LOCK_KEY_HEX = 16             # 64 bit 空间，碰撞概率徽不足道
_CONTENT_LOCK_WAIT_MIN_SECONDS = 300.0
_CONTENT_LOCK_STALE_GRACE_SECONDS = 60.0
_OWNER_SAFE_MERGE_CANDIDATES = 50
_MERGE_COMMIT_MAX_ATTEMPTS = 3

# Per-content turns use concurrent futures rather than asyncio.Lock. FastMCP may
# dispatch independent HTTP sessions from different event loops/threads;
# asyncio.Lock is not a cross-loop primitive and allowed two first writes to race.
_merge_content_tails: dict[str, Future[None]] = {}
_merge_content_tails_guard = threading.Lock()


def _complete_content_turn(key: str, turn: Future[None]) -> None:
    # Future callbacks may complete the next cancelled turn in the same
    # thread.  Never call ``set_result`` while holding the non-reentrant tail
    # guard, or that callback chain deadlocks trying to reacquire it.
    with _merge_content_tails_guard:
        if _merge_content_tails.get(key) is turn:
            _merge_content_tails.pop(key, None)
    if not turn.done():
        try:
            turn.set_result(None)
        except InvalidStateError:
            # Another completion/cancellation won the race after ``done``.
            pass


@asynccontextmanager
async def _filesystem_content_turn(key: str):
    """用内核持有的文件租约保护跨 loop/进程的同内容写入。"""
    base_dir = str(getattr(rt.bucket_mgr, "base_dir", "") or "").strip()
    if not base_dir:
        yield
        return

    try:
        llm_timeout = float(
            (rt.config.get("dehydration") or {}).get("timeout_seconds", 120)
        )
    except (AttributeError, TypeError, ValueError, OverflowError):
        llm_timeout = 120.0
    if not math.isfinite(llm_timeout) or llm_timeout <= 0:
        llm_timeout = 120.0
    wait_seconds = max(
        _CONTENT_LOCK_WAIT_MIN_SECONDS,
        llm_timeout * 2 + _CONTENT_LOCK_STALE_GRACE_SECONDS,
    )
    # 旧实现按 mtime 删除“过期”锁；两次串行 provider 调用可能超过该阈值，
    # 从而让第二进程偷走仍存活的锁。内核租约只会在描述符关闭/进程退出时释放。
    async with _kernel_filesystem_turn(
        base_dir,
        f"content-{key}",
        timeout_seconds=wait_seconds,
    ):
        yield


@asynccontextmanager
async def _keyed_turn(key: str):
    """Serialize operations sharing ``key`` across tasks, loops, and request threads."""
    turn: Future[None] = Future()
    with _merge_content_tails_guard:
        previous = _merge_content_tails.get(key)
        _merge_content_tails[key] = turn

    acquired = previous is None
    try:
        if previous is not None:
            # Do not let cancellation of this waiter cancel its predecessor's
            # shared Future; later turns still depend on that predecessor as
            # the serialization barrier.
            await asyncio.shield(asyncio.wrap_future(previous))
            acquired = True
        async with _filesystem_content_turn(key):
            yield
    finally:
        if acquired:
            _complete_content_turn(key, turn)
        elif previous is not None:
            # A waiter can be cancelled before its predecessor finishes.  Its
            # turn must still be completed once the predecessor releases;
            # otherwise every later waiter for this key blocks forever on the
            # abandoned Future.
            previous.add_done_callback(
                lambda _completed: _complete_content_turn(key, turn)
            )


@asynccontextmanager
async def _content_turn(content: str):
    """Serialize identical writes across tasks, loops, and request threads."""
    key = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:_CONTENT_LOCK_KEY_HEX]
    async with _keyed_turn(key):
        yield


@asynccontextmanager
async def _quota_turn(name: str):
    """串行化配额检查与落盘，防止并发请求基于同一过期快照
    同时通过 pinned/protected/importance 配额。

    复用 ``_content_turn`` 的跨事件循环、跨进程文件锁；FastMCP
    可能从不同事件循环调度请求，普通 ``asyncio.Lock`` 无法覆盖该边界。
    """
    async with _keyed_turn(f"quota-{name}"):
        yield


@asynccontextmanager
async def _bucket_turn(bucket_id: str):
    """Use the production bucket lease when the manager exposes it."""
    factory = getattr(rt.bucket_mgr, "_bucket_turn", None)
    if callable(factory):
        async with factory(bucket_id):
            yield
    else:
        yield


async def _commit_bucket_update(bucket_id: str, updates: dict) -> bool:
    locked = getattr(rt.bucket_mgr, "_update_locked", None)
    if callable(locked):
        return bool(await locked(bucket_id, **updates))
    return bool(await rt.bucket_mgr.update(bucket_id, **updates))


@asynccontextmanager
async def _m04_merge_commit_turn():
    async with markdown_writer_turn(rt.bucket_mgr.base_dir):
        yield


async def _create_bucket_deferred(**kwargs) -> str:
    try:
        return await rt.bucket_mgr.create(
            **kwargs,
            defer_embedding=True,
            _m04_gate_held=True,
        )
    except TypeError as exc:
        if "defer_embedding" not in str(exc):
            raise
        return await rt.bucket_mgr.create(**kwargs)


async def _sync_after_commit(bucket_id: str) -> bool:
    sync = getattr(rt.bucket_mgr, "sync_embedding_after_commit", None)
    if callable(sync):
        return bool(await sync(bucket_id))
    bucket = await rt.bucket_mgr.get(bucket_id)
    if not bucket:
        return False
    engine = getattr(rt, "embedding_engine", None)
    if engine and getattr(engine, "enabled", False):
        await engine.generate_and_store(bucket_id, str(bucket.get("content") or ""))
    return True


def _push_warning_safe(code: str, msg: str) -> None:
    """安全调用 errors.push_warning；import 失败时静默降级。

    原因：push_warning 在两个 quota helper 里被调 4 次，每次都要重复
    “三层 try/except import”的定位代码。集中后：
      ① 业务代码变成干净的一行调用；
      ② import 后退逻辑只需调一处；
      ③ 测试打档只需 patch 本函数。

    路径优先级（跟 imports.md 一致）：
      1. from errors        —— src/ 在 sys.path 顶层的生产/测试环境
      2. from ..errors      —— 包内相对导入的兑底
      3. 均失败 → 静默跳过（不能因 warning 传递失败让业务报错）
    """
    try:
        from errors import push_warning  # type: ignore
    except ImportError:
        try:
            from ..errors import push_warning  # type: ignore
        except Exception:  # pragma: no cover
            return
    try:
        push_warning(code, msg)
    except Exception:  # pragma: no cover
        # 警告通道崩了也不能拖垃业务路径
        pass


def limits_cfg() -> dict:
    """读 config.limits 段；缺省为 50KB 单桶 / 20 pinned / 20 protected。"""
    config = rt.config if isinstance(rt.config, dict) else {}
    return config.get("limits", {}) or {}


async def _fresh_active_buckets() -> list[dict]:
    """Read disk truth; only legacy test doubles may omit ``fresh``."""
    try:
        return await rt.bucket_mgr.list_all(include_archive=False, fresh=True)
    except TypeError as exc:
        if "fresh" not in str(exc):
            raise
        return await rt.bucket_mgr.list_all(include_archive=False)


def _configured_limit(name: str, default: int) -> int:
    raw = limits_cfg().get(name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        return default
    return value if value >= 0 else default


def max_bucket_bytes() -> int:
    return _configured_limit("max_bucket_bytes", _DEFAULT_MAX_BUCKET_BYTES)


def max_pinned() -> int:
    return _configured_limit("max_pinned", _DEFAULT_MAX_PINNED)


def max_protected() -> int:
    return _configured_limit("max_protected", _DEFAULT_MAX_PROTECTED)


def max_grow_input_bytes() -> int:
    return _configured_limit("max_grow_input_bytes", _DEFAULT_MAX_GROW_INPUT_BYTES)


def max_query_bytes() -> int:
    return _configured_limit("max_query_bytes", _DEFAULT_MAX_QUERY_BYTES)


def max_metadata_bytes() -> int:
    return _configured_limit("max_metadata_bytes", _DEFAULT_MAX_METADATA_BYTES)


def max_grow_items() -> int:
    return _configured_limit("max_grow_items", _DEFAULT_MAX_GROW_ITEMS)


def check_content_size(content: str) -> str | None:
    """超过单桶上限返回中文提示串；否则返回 None。"""
    cap = max_bucket_bytes()
    if cap <= 0:
        return None
    size = len(content.encode("utf-8"))
    if size > cap:
        return (
            f"内容过大（{size / 1024:.1f} KB > 上限 {cap / 1024:.0f} KB）。"
            "请改用 grow 拆分存入，或在 config.limits.max_bucket_bytes 调高上限。"
        )
    return None


def check_grow_input_size(content: str) -> str | None:
    cap = max_grow_input_bytes()
    if cap <= 0:
        return None
    size = len(str(content or "").encode("utf-8"))
    if size > cap:
        return (
            f"grow 输入过大（{size / 1024:.1f} KB > 上限 {cap / 1024:.0f} KB）。"
            "请分批调用，或调整 config.limits.max_grow_input_bytes。"
        )
    return None


def check_query_size(query: str) -> str | None:
    cap = max_query_bytes()
    if cap <= 0:
        return None
    size = len(str(query or "").encode("utf-8"))
    if size > cap:
        return (
            f"查询过大（{size / 1024:.1f} KB > 上限 {cap / 1024:.0f} KB）。"
            "请缩短查询，或调整 config.limits.max_query_bytes。"
        )
    return None


def check_metadata_size(**fields: object) -> str | None:
    cap = max_metadata_bytes()
    if cap <= 0:
        return None
    try:
        size = sum(len(str(value or "").encode("utf-8")) for value in fields.values())
    except Exception:
        return "元数据参数无法安全序列化。"
    if size > cap:
        labels = ", ".join(fields)
        return (
            f"元数据过大（{size / 1024:.1f} KB > 上限 {cap / 1024:.0f} KB；字段: {labels}）。"
            "请缩短标签、名称或筛选条件。"
        )
    return None


def check_grow_items_payload(items: list) -> str | None:
    item_cap = max_grow_items()
    if item_cap > 0 and len(items) > item_cap:
        return f"grow items 过多（{len(items)} > 上限 {item_cap}）。请分批调用，或调整 config.limits.max_grow_items。"

    from ombrebrain.storage.source_store import normalize_source_ranges

    byte_cap = max_grow_input_bytes()
    total = 0
    metadata_values: list[object] = []
    for index, item in enumerate(items, start=1):
        if isinstance(item, str):
            value = item
        elif isinstance(item, dict):
            unknown = sorted(str(key) for key in item if key not in _GROW_ITEM_FIELDS)
            if unknown:
                return f"grow items 第 {index} 项包含未支持字段: {', '.join(unknown)}"
            value = item.get("content")
            if not isinstance(value, str):
                return f"grow items 第 {index} 项 content 必须是字符串。"
            for field in ("title", "name"):
                raw_text = item.get(field)
                if raw_text is not None and not isinstance(raw_text, str):
                    return f"grow items 第 {index} 项 {field} 必须是字符串。"
            if item.get("quotes") not in (None, "", []):
                from ombrebrain.storage.quote_store import normalize_quotes

                try:
                    normalize_quotes(item["quotes"])
                except ValueError as exc:
                    return f"grow items 第 {index} 项引语无效，未创建任何桶：{exc}"
            try:
                normalize_memory_title(item.get("title"))
            except ValueError as exc:
                return f"grow items 第 {index} 项 {exc}"
            raw_why = item.get("why_remembered")
            if raw_why is not None:
                if not isinstance(raw_why, str):
                    return (
                        f"grow items 第 {index} 项 why_remembered "
                        "必须是字符串。"
                    )
                if len(raw_why.strip()) > _WHY_REMEMBERED_MAX_CHARS:
                    return (
                        f"grow items 第 {index} 项 why_remembered "
                        f"不能超过 {_WHY_REMEMBERED_MAX_CHARS} 个字符。"
                    )
            for field in ("tags", "domain"):
                raw_list = item.get(field)
                if raw_list is not None and not (
                    isinstance(raw_list, str)
                    or (
                        isinstance(raw_list, list)
                        and all(isinstance(part, str) for part in raw_list)
                    )
                ):
                    return f"grow items 第 {index} 项 {field} 必须是字符串或字符串列表。"
            if item.get("importance") is not None:
                importance = item["importance"]
                if isinstance(importance, bool) or not isinstance(importance, int):
                    return f"grow items 第 {index} 项 importance 必须是 1-10 的整数。"
                if not 1 <= importance <= 10:
                    return f"grow items 第 {index} 项 importance 必须是 1-10 的整数。"
            for field in ("valence", "arousal"):
                raw_number = item.get(field)
                if raw_number is None:
                    continue
                if isinstance(raw_number, bool):
                    return f"grow items 第 {index} 项 {field} 必须是 0-1 的数字。"
                try:
                    number = float(raw_number)
                except (TypeError, ValueError, OverflowError):
                    return f"grow items 第 {index} 项 {field} 必须是 0-1 的数字。"
                if not math.isfinite(number) or not 0 <= number <= 1:
                    return f"grow items 第 {index} 项 {field} 必须是 0-1 的数字。"
            try:
                normalize_source_ranges(item.get("source_ranges"))
            except ValueError as exc:
                return f"grow items 第 {index} 项 {exc}"
            for field in _GROW_ITEM_FIELDS:
                if field == "content" or item.get(field) is None:
                    continue
                metadata_value = item[field]
                if field == "why_remembered":
                    metadata_value = metadata_value.strip()
                metadata_values.append(metadata_value)
        else:
            return f"grow items 第 {index} 项必须是字符串或对象。"
        if not value.strip():
            return f"grow items 第 {index} 项 content 不能为空，未创建任何桶。"
        try:
            total += len(value.encode("utf-8"))
        except Exception:
            return "grow items 包含无法安全序列化的 content。"
        if byte_cap > 0 and total > byte_cap:
            return f"grow items 正文总量过大（{total / 1024:.1f} KB > 上限 {byte_cap / 1024:.0f} KB）。请分批调用。"
    if metadata_values:
        metadata_err = check_metadata_size(items=metadata_values)
        if metadata_err:
            return f"grow items {metadata_err}"
    return None


async def count_pinned() -> int:
    """统计当前 pinned 桶数量。失败时返回 0（保守，不阻断）。

    配额的唯一真相是 metadata.pinned。type=permanent 是正式固化类型，
    不等同于 pinned=True，也不占用 pinned 配额。
    """
    try:
        all_b = await _fresh_active_buckets()
        seen_ids: set[str] = set()
        count = 0
        for bucket in all_b:
            bucket_id = str(bucket.get("id") or "").strip()
            if bucket_id:
                if bucket_id in seen_ids:
                    continue
                seen_ids.add(bucket_id)
            metadata = bucket.get("metadata", {})
            if is_terminal_memory_metadata(metadata):
                continue
            if isinstance(metadata, dict) and parse_bool(
                metadata.get("pinned"), default=False
            ):
                count += 1
        return count
    except Exception as e:
        warning = getattr(getattr(rt, "logger", None), "warning", None)
        if callable(warning):
            warning(f"count_pinned failed: {e}")
        return 0


async def count_protected() -> int:
    """统计活跃、非终态的 protected 逻辑桶数量。

    protected 与 pinned 是独立资源：type=permanent 不等于
    protected=True，不占用这一配额。历史物理副本按 bucket ID 去重。
    读取失败时保守返回 0，不因诊断通道异常阻断其他工具。
    """
    try:
        all_b = await rt.bucket_mgr.list_all(include_archive=False)
        all_b = _identity.filter_default(all_b)
        seen_ids: set[str] = set()
        count = 0
        for bucket in all_b:
            bucket_id = str(bucket.get("id") or "").strip()
            if bucket_id:
                if bucket_id in seen_ids:
                    continue
                seen_ids.add(bucket_id)
            metadata = bucket.get("metadata", {})
            if is_terminal_memory_metadata(metadata):
                continue
            if isinstance(metadata, dict) and parse_bool(
                metadata.get("protected"), default=False
            ):
                count += 1
        return count
    except Exception as e:
        warning = getattr(getattr(rt, "logger", None), "warning", None)
        if callable(warning):
            warning(f"count_protected failed: {e}")
        return 0


def _is_pinned_orphan(meta: dict) -> bool:
    """Return True only for confidently repairable pinned/type desync.

    `type == "permanent"` is now a first-class bucket type, not just the
    storage side effect of `pinned=True`.  Metadata alone cannot safely
    distinguish a legacy unpinned-pinned bucket from an intentionally permanent
    bucket, so automatic demotion is intentionally disabled.
    """
    return False


async def repair_pinned_desync(bucket_mgr, apply: bool = False) -> dict:
    """扫描 pinned/type 脱钩项；当前不会自动降级 permanent。

    type=permanent 现在是正式固化类型。仅凭 metadata 无法安全地区分
    历史取消钉选残留和用户显式创建的 permanent 桶，所以自动降级已禁用。

    返回 dict：{total, pinned, orphans:[{id,name,importance}], applied, demoted, failed}。"""
    buckets = await bucket_mgr.list_all(include_archive=False)
    unique_buckets: list[dict] = []
    seen_ids: set[str] = set()
    for bucket in buckets:
        bucket_id = str(bucket.get("id") or "").strip()
        if bucket_id:
            if bucket_id in seen_ids:
                continue
            seen_ids.add(bucket_id)
        unique_buckets.append(bucket)
    pinned_now = [
        bucket
        for bucket in unique_buckets
        if isinstance(bucket.get("metadata"), dict)
        and not is_terminal_memory_metadata(bucket["metadata"])
        and parse_bool(bucket["metadata"].get("pinned"), default=False)
    ]
    orphans = [
        b for b in unique_buckets
        if _is_pinned_orphan(b.get("metadata", {}))
    ]

    result: dict = {
        "total": len(unique_buckets),
        "pinned": len(pinned_now),
        "orphans": [
            {
                "id": b["id"],
                "name": b.get("metadata", {}).get("name") or "",
                "importance": b.get("metadata", {}).get("importance"),
            }
            for b in orphans
        ],
        "applied": apply,
        "demoted": 0,
        "failed": 0,
    }
    if not apply or not orphans:
        return result

    for b in orphans:
        try:
            ok = await bucket_mgr.update(b["id"], pinned=False)
            if ok:
                result["demoted"] += 1
            else:
                result["failed"] += 1
                rt.logger.warning(f"repair_pinned_desync: update returned False for {b['id']}")
        except Exception as e:
            result["failed"] += 1
            rt.logger.warning(f"repair_pinned_desync: update failed for {b['id']}: {e}")
    return result


async def restore_archived_letters(
    bucket_mgr,
    *,
    ids: list[str] | None = None,
    revisions: dict[str, str] | None = None,
    apply: bool = False,
) -> dict:
    """审计或显式恢复历史误归档 Letter，不回传正文或标题。

    ``apply=False`` 只读取 Markdown 并报告强标记候选；不会调用任何写方法。
    ``apply=True`` 只处理调用方明确给出的 ID，最终授权仍由
    ``BucketManager.recover_archived_letter`` 在同一桶租约内重读物理真源后决定。
    """
    if apply:
        requested: list[str] = []
        seen: set[str] = set()
        for value in ids or []:
            bucket_id = str(value or "").strip()
            if bucket_id and bucket_id not in seen:
                seen.add(bucket_id)
                requested.append(bucket_id)
        if not requested:
            raise ValueError("apply requires explicit non-empty ids")

        results: list[dict[str, str]] = []
        restored_count = 0
        unchanged_count = 0
        failed_count = 0
        for bucket_id in requested:
            try:
                outcome = await bucket_mgr.recover_archived_letter(
                    bucket_id,
                    expected_revision=(revisions or {}).get(bucket_id),
                )
                reason = str((outcome or {}).get("reason") or "failed")
            except Exception as exc:
                reason = "internal_error"
                warning = getattr(getattr(rt, "logger", None), "warning", None)
                if callable(warning):
                    warning(
                        "restore_archived_letters failed for %s: %s",
                        bucket_id,
                        exc,
                    )
            results.append({"id": bucket_id, "reason": reason})
            if reason == "restored":
                restored_count += 1
            elif reason == "already_restored":
                unchanged_count += 1
            else:
                failed_count += 1
        return {
            "requested_count": len(requested),
            "restored_count": restored_count,
            "unchanged_count": unchanged_count,
            "failed_count": failed_count,
            "results": results,
        }

    from historical_letter_restore import audit_archived_letters

    return await audit_archived_letters(bucket_mgr)


async def check_pinned_quota() -> str | None:
    """到达 pinned 上限返回提示串；否则返回 None。

    （store_pinned 在严格模式下用此函数硬拒绝；新的"自动降级"路径请改用
    enforce_pinned_quota，达到上限时返回 (False, msg) 让调用方走普通桶。）"""
    cap = max_pinned()
    if cap <= 0:
        return None
    cur = await count_pinned()
    if cur >= cap:
        return (
            f"pinned 桶已达上限（{cur}/{cap}），建议先用 trace(bucket_id, pinned=0) "
            "清理低优先级钉选；或在 config.limits.max_pinned 调高上限。"
        )
    return None


async def check_protected_quota() -> str | None:
    """显式设为 protected 时的独立硬配额检查。

    达到上限时返回可直接给 trace 的拒绝提示；调用方必须把
    配额判定与落盘放在同一个 ``_quota_turn("protected")`` 内。
    """
    cap = max_protected()
    if cap <= 0:
        return None
    cur = await count_protected()
    if cur >= cap:
        return (
            f"protected 桶已达上限（{cur}/{cap}），请先用 "
            "trace(bucket_id, protected=0, importance=1..10) "
            "取消不再需要的保护；"
            "或在 config.limits.max_protected 调高上限。"
        )
    return None


# ============================================================
# 配额 helpers（统一错误体系 OB-W004 + OB-I002）
# ------------------------------------------------------------
# 设计：把"配额预警"和"自动降级"两步分开，分别对应 W 与 I。
# 业务代码调用前者拿到提示后，自动经 _push_warning_safe 送去 MCP 返回末尾。
# rule.md §2 的稀缺性哲学由 pinned(20)/anchor(24) 两个结构承担；
# importance 只是普通评分字段，这里不再对 importance>=9 设硬配额。
# ============================================================


def is_terminal_memory_metadata(metadata: dict | None) -> bool:
    """Whether metadata represents an archived/deleted terminal memory."""
    if not isinstance(metadata, dict):
        return False
    return bool(
        metadata.get("deleted_at")
        or parse_bool(metadata.get("tombstone"), default=False)
        or str(metadata.get("type") or "").strip().lower() == "archived"
    )


def is_importance_audit_candidate(
    metadata: dict | None,
    minimum: int,
) -> bool:
    """Shared visible ordinary-memory scope for importance audit and quota."""
    if not isinstance(metadata, dict):
        return False
    try:
        importance = int(metadata.get("importance") or 0)
    except (OverflowError, TypeError, ValueError):
        return False
    if importance < minimum:
        return False
    if parse_bool(metadata.get("dont_surface"), default=False):
        return False
    if is_terminal_memory_metadata(metadata):
        return False
    bucket_type = str(metadata.get("type") or "dynamic").strip().lower()
    return bucket_type not in _HIGH_IMP_EXEMPT_TYPES


async def enforce_pinned_quota(pinned: bool) -> bool:
    """pinned 配额检查 + 自动退出。

    - 当前数 ≥ 硬上限 → push OB-I002 并返回 False（走普通桶）
    - 当前数 ≥ 软阈值 → push OB-W004（仅提醒，不动数据）
    传入 pinned=False 时直接返回 False。
    """
    if not pinned:
        return False
    cap = max_pinned()
    cur = await count_pinned()
    # 软阈值 = cap - GAP；cap=20、GAP=2 → soft=18。cap 太小（≤GAP）退化为硬上限。
    soft = max(1, cap - _PINNED_SOFT_GAP) if cap > _PINNED_SOFT_GAP else cap
    if cap > 0 and cur >= cap:
        rt.logger.info(
            f"op=quota phase=branch branch=pinned_degrade current={cur} cap={cap}"
        )
        _push_warning_safe(
            "OB-I002",
            f"当前已有 {cur} 条 pinned（硬上限 {cap}），本次未钉成功，已保留为普通桶",
        )
        return False
    if cap > 0 and cur >= soft:
        _push_warning_safe(
            "OB-W004",
            f"当前已有 {cur} 条 pinned（硬上限 {cap}），接近上限",
        )
    return True


async def count_high_importance() -> int:
    """Count visible ordinary active buckets whose importance is at least 9."""
    try:
        buckets = await _fresh_active_buckets()
        return sum(
            1
            for bucket in buckets
            if is_importance_audit_candidate(
                bucket.get("metadata", {}), _HIGH_IMP_THRESHOLD
            )
            and not parse_bool(bucket.get("metadata", {}).get("pinned"), default=False)
            and not parse_bool(bucket.get("metadata", {}).get("protected"), default=False)
        )
    except Exception as exc:
        warning = getattr(getattr(rt, "logger", None), "warning", None)
        if callable(warning):
            warning(f"count_high_importance failed: {exc}")
        return 0


async def enforce_high_importance_quota(importance: int) -> int:
    importance = int(importance)
    if importance < _HIGH_IMP_THRESHOLD:
        return importance
    current = await count_high_importance()
    if current >= _HIGH_IMP_HARD_CAP:
        _push_warning_safe(
            "OB-I001",
            f"当前已有 {current} 条 importance≥{_HIGH_IMP_THRESHOLD}（硬上限 {_HIGH_IMP_HARD_CAP}），新桶 importance 自动降级为 {_HIGH_IMP_DEGRADE_TO}",
        )
        return _HIGH_IMP_DEGRADE_TO
    if current >= _HIGH_IMP_SOFT_WARN:
        _push_warning_safe(
            "OB-W003",
            f"当前已有 {current} 条 importance≥{_HIGH_IMP_THRESHOLD}（硬上限 {_HIGH_IMP_HARD_CAP}），接近上限",
        )
    return importance


async def quota_safe_update(
    bucket_id: str,
    updates: dict,
) -> tuple[bool, dict, str]:
    """Commit protected/pinned edits under production writer and bucket locks."""
    applied = dict(updates)
    content_changed = "content" in applied
    if content_changed:
        require_embedding = getattr(rt.bucket_mgr, "_require_embedding_available", None)
        if callable(require_embedding):
            require_embedding()

    async with markdown_writer_turn(rt.bucket_mgr.base_dir):
        async with AsyncExitStack() as stack:
            if "pinned" in applied:
                await stack.enter_async_context(_quota_turn("pinned"))
            if "protected" in applied:
                await stack.enter_async_context(_quota_turn("protected"))
            await stack.enter_async_context(_bucket_turn(bucket_id))
            bucket = await rt.bucket_mgr.get(bucket_id)
            if not bucket:
                return False, applied, f"未找到记忆桶: {bucket_id}"
            meta = bucket.get("metadata", {}) or {}
            current_pinned = bool(meta.get("pinned"))
            current_protected = bool(meta.get("protected"))
            current_importance = int(meta.get("importance") or 5)
            final_pinned = bool(applied.get("pinned", current_pinned))
            final_protected = bool(applied.get("protected", current_protected))

            if "importance" in applied and (current_pinned or current_protected):
                if int(applied["importance"]) != current_importance:
                    return False, applied, "pinned/protected 记忆桶的 importance 锁定为 10"
                applied.pop("importance")
            if final_pinned and not current_pinned:
                quota_error = await check_pinned_quota()
                if quota_error:
                    return False, applied, quota_error
                applied["importance"] = 10
            if final_protected and not current_protected:
                quota_error = await check_protected_quota()
                if quota_error:
                    return False, applied, quota_error
                applied["importance"] = 10

            committed = await _commit_bucket_update(bucket_id, applied)
            if not committed:
                return False, applied, f"修改失败: {bucket_id}"

    warning = ""
    if content_changed:
        try:
            await _sync_after_commit(bucket_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            rt.logger.warning(
                "post-commit update embedding failed for %s: %s",
                bucket_id,
                type(exc).__name__,
            )
            warning = _EMBED_WARN
    return True, applied, warning


async def merge_or_create(
    content: str,
    tags: list,
    importance: int,
    domain: list,
    valence: float,
    arousal: float,
    name: str = "",
    title: str = "",
    source_refs: list | None = None,
    quotes: list | None = None,
    raw_merge: bool = False,
    why_remembered: str = "",
    merge_why_remembered: str = "",
    source_tool: str = "",
    grow_batch_id: str = "",
    meaning: str = "",
    media: list | str | None = None,
    test_data: bool = False,
    exact_only: bool = False,
    footprint_origin: dict | None = None,
    owner: str | None = None,
) -> Tuple[str, bool, str]:
    """
    检查是否有相似桶可合并，有则合并，无则新建。返回 (桶ID或名称, 是否合并, embed警告信息)。

    raw_merge=True (hold)：原文追加，不调 LLM 压缩。
    raw_merge=False (grow)：LLM 压缩老+新内容。
    exact_only=True：只折叠同 owner、同正文的精确重复，不做模糊合并。

    iter 2.0 来源追踪：
    - source_tool: "hold" | "grow"，作为新建桶的 source_tool 写入；
      合并路径下保留原桶 source_tool 不变，但写 last_merged_by=source_tool。
    - grow_batch_id: 仅 grow 路径会传，新建时写入；合并路径不覆盖原桶的 batch_id
      （原桶可能来自上一次 grow 或 hold，硬覆盖会丢失最初批次信息）。

    M-04 precondition: selection and provider preparation hold no content,
    quota, bucket, or Markdown-snapshot turn.  The final durable commit phase
    revalidates fresh disk state under its existing M-01 lock order.
    """
    tags = _identity.ensure_write_owner(tags, caller=owner)
    origin = (
        footprint_origin
        if footprint_origin is not None
        else _identity.origin_for_mcp(source_tool)
    )
    # BucketManager canonicalizes dangerous controls before durable storage.
    # Use the same canonical bytes for exact selection, content leases, merge
    # preparation, and the final raced-create review; otherwise two identical
    # raw inputs can both miss a previously stored sanitized body.
    sanitize_text = getattr(rt.bucket_mgr, "_sanitize_text", None)
    if callable(sanitize_text):
        content = sanitize_text(str(content))
    else:
        content = str(content)
    require_embedding = getattr(rt.bucket_mgr, "_require_embedding_available", None)
    if callable(require_embedding):
        require_embedding()
    bucket_id, is_merged, old_content = await _merge_or_create_inner(
        content=content, tags=tags, importance=importance, domain=domain,
        valence=valence, arousal=arousal, name=name, title=title,
        source_refs=source_refs, quotes=quotes, raw_merge=raw_merge,
        why_remembered=why_remembered,
        merge_why_remembered=merge_why_remembered,
        source_tool=source_tool, grow_batch_id=grow_batch_id,
        meaning=meaning, media=media, test_data=test_data,
        exact_only=exact_only,
        footprint_origin=origin,
    )

    # The Markdown commit is already durable and every content/quota/bucket turn
    # is released before the external provider is awaited.
    embed_warn = ""
    try:
        await _sync_after_commit(bucket_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        embed_warn = _EMBED_WARN
        rt.logger.warning(
            "post-commit embedding failed for %s: %s",
            bucket_id,
            type(exc).__name__,
        )
    if old_content:
        try:
            rt.dehydrator.invalidate_cache(old_content)
        except Exception:
            pass
    rt.logger.info(
        f"op=merge_or_create phase=commit bucket_id={bucket_id} "
        f"merged={int(is_merged)} source_tool={source_tool or '_'} "
        f"grow_batch_id={grow_batch_id or '_'} embed_ok={int(not embed_warn)}"
    )
    return bucket_id, is_merged, embed_warn


async def _merge_or_create_inner(
    content: str,
    tags: list,
    importance: int,
    domain: list,
    valence: float,
    arousal: float,
    name: str = "",
    title: str = "",
    source_refs: list | None = None,
    quotes: list | None = None,
    raw_merge: bool = False,
    why_remembered: str = "",
    merge_why_remembered: str = "",
    source_tool: str = "",
    grow_batch_id: str = "",
    meaning: str = "",
    media: list | str | None = None,
    test_data: bool = False,
    exact_only: bool = False,
    footprint_origin: dict | None = None,
) -> Tuple[str, bool, str]:
    """Prepare/provider/revalidate/commit without holding provider under locks."""
    prepared_merges: dict[tuple[str, str], str] = {}
    for _attempt in range(_MERGE_COMMIT_MAX_ATTEMPTS):
        selected = await _select_merge_target(content, tags, domain, exact_only)
        if selected is None:
            created = await _create_after_merge_review(
                content=content, tags=tags, importance=importance, domain=domain,
                valence=valence, arousal=arousal, name=name, title=title,
                source_refs=source_refs, quotes=quotes,
                why_remembered=why_remembered, source_tool=source_tool,
                grow_batch_id=grow_batch_id, meaning=meaning, media=media,
                test_data=test_data, event_actor="llm",
                footprint_origin=footprint_origin,
            )
            if created is not None:
                return created
            # Another process published the same owner/content after our
            # unlocked selection.  Re-enter the normal merge path so incoming
            # tags/importance/domain are committed instead of silently lost.
            continue
        target_id, exact_selected = selected
        prepared = await _prepare_merge_target(
            target_id, content, tags, exact_selected
        )
        if prepared is None:
            continue
        old_text, _prepared_meta = prepared
        if not exact_selected:
            judge_same_event = getattr(rt.dehydrator, "judge_same_event", None)
            if callable(judge_same_event):
                judgement = await judge_same_event(old_text, content)
                same_event = bool(
                    isinstance(judgement, dict)
                    and judgement.get("same_event") is True
                    and float(judgement.get("confidence") or 0)
                    >= _SAME_EVENT_CONFIDENCE_MIN
                )
                if not same_event:
                    created = await _create_after_merge_review(
                        content=content, tags=tags, importance=importance,
                        domain=domain, valence=valence, arousal=arousal,
                        name=name, title=title, source_refs=source_refs,
                        quotes=quotes, why_remembered=why_remembered,
                        source_tool=source_tool, grow_batch_id=grow_batch_id,
                        meaning=meaning, media=media, test_data=test_data,
                        event_actor="llm", footprint_origin=footprint_origin,
                    )
                    if created is not None:
                        return created
                    continue
        if old_text == content:
            merged = old_text
            merge_method = "exact_dup"
        elif raw_merge:
            old_clean = old_text.rstrip()
            new_clean = content.strip()
            merged = (
                f"{old_clean}\n\n---\n{new_clean}"
                if old_clean and new_clean and new_clean not in old_clean
                else old_clean or new_clean
            )
            merge_method = "raw_concat"
        else:
            cache_key = (target_id, old_text)
            if cache_key not in prepared_merges:
                # Provider work is deliberately outside every merge/commit turn.
                prepared_merges[cache_key] = await rt.dehydrator.merge(
                    old_text, content
                )
            merged = prepared_merges[cache_key]
            merge_method = "llm"

        committed = await _revalidate_and_commit_merge(
            target_id=target_id,
            expected_old_text=old_text,
            merged=merged,
            content=content,
            tags=tags,
            importance=importance,
            domain=domain,
            valence=valence,
            arousal=arousal,
            source_tool=source_tool,
            title=title,
            source_refs=source_refs,
            quotes=quotes,
            merge_why_remembered=merge_why_remembered,
            meaning=meaning,
            media=media,
            merge_method=merge_method,
        )
        if committed:
            return target_id, True, old_text

    # Preserve the bounded fail-closed behavior: a repeatedly stale target is
    # never overwritten, and a concurrently published exact duplicate is never
    # bypassed by an unconditional create.
    created = await _create_after_merge_review(
        content=content, tags=tags, importance=importance, domain=domain,
        valence=valence, arousal=arousal, name=name, title=title,
        source_refs=source_refs, quotes=quotes,
        why_remembered=why_remembered, source_tool=source_tool,
        grow_batch_id=grow_batch_id, meaning=meaning, media=media,
        test_data=test_data, event_actor="llm",
        footprint_origin=footprint_origin,
    )
    if created is not None:
        return created
    raise RuntimeError("merge/create state changed repeatedly; retry the write")


def _mergeable_for_owner(snapshot: dict, tags: list) -> bool:
    metadata = snapshot.get("metadata", {}) or {}
    return bool(
        _identity.owners_compatible(metadata, tags)
        and not is_logical_letter(snapshot)
        and not metadata.get("pinned")
        and not metadata.get("protected")
        and not metadata.get("deleted_at")
        and metadata.get("type") != "archived"
    )


async def _select_merge_target(
    content: str,
    tags: list,
    domain: list,
    exact_only: bool,
) -> tuple[str, bool] | None:
    """Return an unlocked candidate ID and whether it was exact at selection."""
    threshold = rt.config.get("merge_threshold") or 75
    fresh_buckets = await _fresh_active_buckets()
    exact = next(
        (
            bucket for bucket in fresh_buckets
            if (bucket.get("content") or "") == content
            and _mergeable_for_owner(bucket, tags)
        ),
        None,
    )
    candidate = exact
    score = 100.0 if exact else 0.0
    if candidate is None and not exact_only:
        try:
            try:
                candidates = await rt.bucket_mgr.search(
                    content,
                    limit=_OWNER_SAFE_MERGE_CANDIDATES,
                    domain_filter=domain or None,
                    fresh=True,
                )
            except TypeError as exc:
                if "fresh" not in str(exc):
                    raise
                candidates = await rt.bucket_mgr.search(
                    content,
                    limit=_OWNER_SAFE_MERGE_CANDIDATES,
                    domain_filter=domain or None,
                )
            compatible = [
                bucket for bucket in candidates if _mergeable_for_owner(bucket, tags)
            ]
            if compatible:
                candidate = compatible[0]
                score = float(candidate.get("score", 0) or 0)
        except Exception as exc:
            rt.logger.warning(
                f"Search for merge failed, creating new / 合并搜索失败，新建: {exc}"
            )
    if candidate is None or (exact is None and score <= threshold):
        return None
    target_id = str(candidate.get("id") or "")
    return (target_id, exact is not None) if target_id else None


async def _prepare_merge_target(
    target_id: str,
    content: str,
    tags: list,
    exact_selected: bool,
) -> tuple[str, dict] | None:
    """Read an unlocked candidate for provider preparation; never commit here."""
    snapshot = await rt.bucket_mgr.get(target_id)
    if not snapshot or not _mergeable_for_owner(snapshot, tags):
        return None
    old_text = str(snapshot.get("content") or "")
    if exact_selected and old_text != content:
        return None
    return old_text, dict(snapshot.get("metadata", {}) or {})


async def _revalidate_and_commit_merge(
    *,
    target_id: str,
    expected_old_text: str,
    merged: str,
    content: str,
    tags: list,
    importance: int,
    domain: list,
    valence: float,
    arousal: float,
    source_tool: str,
    title: str = "",
    source_refs: list | None = None,
    quotes: list | None = None,
    merge_why_remembered: str = "",
    meaning: str = "",
    media: list | str | None = None,
    merge_method: str = "llm",
) -> bool:
    """Acquire the future M-04 seam, then revalidate and atomically commit."""
    async with _m04_merge_commit_turn():
        async with _content_turn(content):
            async with _keyed_turn(f"merge-target-{target_id}"):
                before_quota = await rt.bucket_mgr.get(target_id)
                if (
                    not before_quota
                    or not _mergeable_for_owner(before_quota, tags)
                    or str(before_quota.get("content") or "") != expected_old_text
                ):
                    return False
                async with AsyncExitStack() as stack:
                    # The high-importance quota turn must precede the bucket
                    # lease.  Acquire it conservatively for every incoming high
                    # write, then decide the actual transition from the fresh
                    # metadata read under the bucket lease.
                    if importance >= _HIGH_IMP_THRESHOLD:
                        await stack.enter_async_context(
                            _quota_turn("high_importance")
                        )
                    await stack.enter_async_context(_bucket_turn(target_id))
                    current = await rt.bucket_mgr.get(target_id)
                    if (
                        not current
                        or not _mergeable_for_owner(current, tags)
                        or str(current.get("content") or "") != expected_old_text
                    ):
                        return False
                    current_meta = current.get("metadata", {}) or {}
                    current_importance = int(
                        current_meta.get("importance") or 5
                    )
                    final_importance = max(current_importance, importance)
                    if (
                        current_importance < _HIGH_IMP_THRESHOLD
                        and final_importance >= _HIGH_IMP_THRESHOLD
                    ):
                        final_importance = await enforce_high_importance_quota(
                            final_importance
                        )

                    # ---- remainder sidecar: prepare ----
                    sidecar_entry = None
                    sidecar_gen = None
                    base_dir = str(
                        getattr(rt.bucket_mgr, "base_dir", "") or ""
                    ).strip()
                    if base_dir:
                        from remainder_sidecar import prepare_remainder
                        from remainder_integration import RemainderIntegrationError
                        try:
                            sidecar_entry, sidecar_gen = prepare_remainder(
                                base_dir,
                                target_id,
                                old_text=expected_old_text,
                                new_text=content,
                                merged_text=merged,
                                merge_method=merge_method,
                                metadata=current_meta,
                                current_content=expected_old_text,
                            )
                        except Exception as _prep_exc:
                            rt.logger.error(
                                "remainder prepare failed for %s: %s",
                                target_id, _prep_exc,
                            )
                            raise RemainderIntegrationError(
                                f"prepare failed for {target_id}: "
                                f"{type(_prep_exc).__name__}"
                            ) from _prep_exc

                    old_v = current_meta.get("valence") or 0.5
                    old_a = current_meta.get("arousal") or 0.3
                    updates = {
                        "content": merged,
                        "tags": list(
                            dict.fromkeys((current_meta.get("tags") or []) + tags)
                        ),
                        "importance": final_importance,
                        "domain": list(
                            dict.fromkeys((current_meta.get("domain") or []) + domain)
                        ),
                        "valence": (
                            round((old_v + valence) / 2, 2)
                            if 0 <= valence <= 1 else old_v
                        ),
                        "arousal": (
                            round((old_a + arousal) / 2, 2)
                            if 0 <= arousal <= 1 else old_a
                        ),
                    }
                    if source_tool:
                        updates["last_merged_by"] = source_tool
                    if title:
                        updates["title"] = title
                        old_name = str(current_meta.get("name") or "")
                        timestamp_prefix = old_name[:19]
                        if (
                            len(timestamp_prefix) == 19
                            and timestamp_prefix[4] == "-"
                            and timestamp_prefix[7] == "-"
                            and timestamp_prefix[10] == " "
                            and timestamp_prefix[13] == "-"
                            and timestamp_prefix[16] == "-"
                        ):
                            updates["name"] = f"{timestamp_prefix} {title}"
                        else:
                            updates["name"] = title
                    if source_refs:
                        updates["source_refs_append"] = source_refs
                    if quotes:
                        updates["quotes_append"] = quotes
                    if merge_why_remembered and not str(
                        current_meta.get("why_remembered") or ""
                    ).strip():
                        updates["why_remembered"] = merge_why_remembered
                    if meaning:
                        updates["meaning_append"] = meaning
                    if media:
                        updates["media_append"] = media
                    locked = getattr(rt.bucket_mgr, "_update_locked", None)
                    if not callable(locked):
                        raise RuntimeError(
                            "owner-safe merge requires an atomic locked update helper"
                        )

                    # ---- Markdown commit ----
                    from remainder_integration import (
                        RemainderIntegrationError,
                        mark_unresolved,
                    )
                    md_ok = False
                    try:
                        md_ok = bool(await locked(target_id, **updates))
                    except BaseException as _md_exc:
                        # Markdown failed: try to abort remainder
                        if sidecar_entry and base_dir:
                            try:
                                from remainder_sidecar import abort_remainder
                                abort_remainder(
                                    base_dir, target_id,
                                    sidecar_entry.entry_id, sidecar_gen,
                                )
                            except Exception as _abort_exc:
                                mark_unresolved(
                                    code="md_abort_double_fail",
                                    bucket_id=target_id,
                                    exc_type=type(_abort_exc).__name__,
                                )
                                rt.logger.error(
                                    "remainder abort also failed for %s "
                                    "after Markdown error: md=%s abort=%s",
                                    target_id, _md_exc, _abort_exc,
                                )
                                if isinstance(
                                    _md_exc, asyncio.CancelledError
                                ):
                                    raise _md_exc
                                raise RemainderIntegrationError(
                                    f"Markdown+abort both failed for "
                                    f"{target_id}: "
                                    f"md={type(_md_exc).__name__}, "
                                    f"abort={type(_abort_exc).__name__}"
                                ) from _md_exc
                        if isinstance(_md_exc, asyncio.CancelledError):
                            raise
                        raise

                    if not md_ok:
                        # _update_locked returned False
                        if sidecar_entry and base_dir:
                            try:
                                from remainder_sidecar import abort_remainder
                                abort_remainder(
                                    base_dir, target_id,
                                    sidecar_entry.entry_id, sidecar_gen,
                                )
                            except Exception as _abort_exc:
                                mark_unresolved(
                                    code="md_false_abort_fail",
                                    bucket_id=target_id,
                                    exc_type=type(_abort_exc).__name__,
                                )
                                rt.logger.error(
                                    "remainder abort failed for %s after "
                                    "Markdown returned False: %s",
                                    target_id, _abort_exc,
                                )
                                raise RemainderIntegrationError(
                                    f"Markdown false + abort failed for "
                                    f"{target_id}: "
                                    f"{type(_abort_exc).__name__}"
                                ) from _abort_exc
                        return False

                    # Markdown succeeded: commit remainder
                    if sidecar_entry and base_dir:
                        try:
                            from remainder_sidecar import commit_remainder
                            commit_remainder(
                                base_dir, target_id,
                                sidecar_entry.entry_id, sidecar_gen,
                            )
                        except Exception as _commit_exc:
                            from remainder_integration import mark_unresolved
                            mark_unresolved(
                                code="commit_failed",
                                bucket_id=target_id,
                                exc_type=type(_commit_exc).__name__,
                            )
                            rt.logger.error(
                                "remainder commit failed for %s "
                                "(Markdown is authority, merge returns True, "
                                "PREPARED preserved): %s",
                                target_id, _commit_exc,
                            )

                    # archive terminal entries
                    if base_dir:
                        try:
                            from remainder_sidecar import (
                                archive_if_needed as _arc,
                            )
                            _arc(base_dir, target_id)
                        except Exception as _arc_exc:
                            from remainder_integration import mark_unresolved
                            mark_unresolved(
                                code="archive_failed",
                                bucket_id=target_id,
                                exc_type=type(_arc_exc).__name__,
                            )
                            rt.logger.error(
                                "remainder archive failed for %s "
                                "(not rolling back): %s",
                                target_id, _arc_exc,
                            )

                    return True


async def _create_after_merge_review(**kwargs) -> Tuple[str, bool, str] | None:
    """Create under the commit seam, or request a retry after a raced exact write."""
    importance = int(kwargs["importance"])
    async with _m04_merge_commit_turn():
        async with _content_turn(str(kwargs["content"])):
            fresh = await _fresh_active_buckets()
            raced_exact = next(
                (
                    bucket for bucket in fresh
                    if (bucket.get("content") or "") == str(kwargs["content"])
                    and _mergeable_for_owner(bucket, kwargs["tags"])
                ),
                None,
            )
            if raced_exact is not None:
                return None
            async with AsyncExitStack() as stack:
                final_importance = importance
                if importance >= _HIGH_IMP_THRESHOLD:
                    await stack.enter_async_context(_quota_turn("high_importance"))
                    final_importance = await enforce_high_importance_quota(importance)
                create_kwargs = dict(kwargs)
                create_kwargs["importance"] = final_importance
                bucket_id = await _create_bucket_deferred(**create_kwargs)
    return bucket_id, False, ""


async def check_duplicate_for(new_bucket_id: str, new_text: str, threshold: float = _DUP_DEFAULT_THRESHOLD) -> None:
    """fire-and-forget：新桶写完后，向量相似 > threshold 的旧桶标为疑似重复。

    iter 1.6 §4：不自动合并，只在两边各写 dup_candidate=<对端 id> + dup_score=<0~1>，
    Dashboard 在桶详情里显示「疑似重复」提示，由她/他手动确认是否合并。
    """
    async with _dup_check_semaphore:
        try:
            if not rt.embedding_engine or not getattr(rt.embedding_engine, "enabled", False):
                return
            new_bucket = await rt.bucket_mgr.get(new_bucket_id)
            if not new_bucket:
                return
            new_tags = (new_bucket.get("metadata") or {}).get("tags") or []
            sims = await rt.embedding_engine.search_similar(new_text, top_k=_DUP_TOPK)
            for bid, score in sims:
                if bid == new_bucket_id:
                    continue
                if score < threshold:
                    continue
                try:
                    candidate = await rt.bucket_mgr.get(bid)
                    if not candidate or not _identity.owners_compatible(
                        candidate.get("metadata") or {}, new_tags
                    ):
                        continue
                    new_owner = _identity.mutation_owner(
                        new_bucket.get("metadata") or {}
                    )
                    candidate_owner = _identity.mutation_owner(
                        candidate.get("metadata") or {}
                    )
                    with _identity.manager_mutation_guard(
                        rt.bucket_mgr,
                        {new_bucket_id: new_owner, bid: candidate_owner},
                    ):
                        await rt.bucket_mgr.update(
                            new_bucket_id,
                            dup_candidate=bid,
                            dup_score=round(float(score), 4),
                        )
                        await rt.bucket_mgr.update(
                            bid,
                            dup_candidate=new_bucket_id,
                            dup_score=round(float(score), 4),
                        )
                    rt.logger.info(
                        f"duplicate candidate: {new_bucket_id} ↔ {bid} (sim={score:.3f})"
                    )
                except Exception as e:
                    rt.logger.warning(f"dup mark failed: {e}")
                break  # 只标最相似的一对
        except Exception as e:
            rt.logger.warning(f"check_duplicate_for outer error: {e}")


async def _rank_active_plans_by_query(
    new_event_text: str,
    active_plans: list[dict],
) -> list[dict]:
    """用 BucketManager 的关键词/BM25 通道排序 active plan，不调用向量。"""
    active_by_id = {str(plan.get("id") or ""): plan for plan in active_plans}
    try:
        ranked = await rt.bucket_mgr.search(
            new_event_text,
            limit=max(len(active_plans), _PLAN_FALLBACK_CAP),
            vector_scores={},
        )
    except Exception as exc:
        rt.logger.warning(f"plan resolution: keyword pre-filter failed: {exc}")
        return []
    return [
        active_by_id[bucket_id]
        for bucket in ranked
        if (bucket_id := str(bucket.get("id") or "")) in active_by_id
    ]


async def check_plan_resolution(new_event_text: str, source_bucket_id: str = "") -> None:
    """新事件只检查检索命中的 active plan，并把 LLM 结果记录为建议。"""
    try:
        from .plan.core import is_letter_bucket

        all_b = await _fresh_active_buckets()
        all_b = [b for b in all_b if _identity.admitted_mutation(b)]
        active_plans = [
            b for b in all_b
            if b["metadata"].get("type") == "plan"
            and not is_letter_bucket(b)
            and b["metadata"].get("status", "active") == "active"
        ]
        if not active_plans:
            return
        keyword_candidates = await _rank_active_plans_by_query(
            new_event_text, active_plans
        )
        vector_candidates = []
        if rt.embedding_engine and getattr(rt.embedding_engine, "enabled", False):
            try:
                sims = await rt.embedding_engine.search_similar(new_event_text, top_k=_PLAN_VECTOR_TOPK)
                sim_map = {bid: sc for bid, sc in sims}
                for p in active_plans:
                    if sim_map.get(p["id"], 0.0) > _PLAN_VECTOR_THRESHOLD:
                        vector_candidates.append(p)
            except Exception as e:
                rt.logger.warning(f"plan resolution: vector pre-filter failed, falling back: {e}")
        # 关键词是不可缺失的基础召回；向量只补充语义候选。去重后仍限制
        # 小模型调用数，避免 active plan 很多时一次写入触发无界 API 请求。
        plan_candidates = []
        seen_plan_ids: set[str] = set()
        for candidate in keyword_candidates + vector_candidates:
            candidate_id = str(candidate.get("id") or "")
            if not candidate_id or candidate_id in seen_plan_ids:
                continue
            seen_plan_ids.add(candidate_id)
            plan_candidates.append(candidate)
            if len(plan_candidates) >= _PLAN_FALLBACK_CAP:
                break
        for p in plan_candidates:
            try:
                judgement = await rt.dehydrator.judge_plan_resolution(
                    p["content"], new_event_text
                )
                confidence = float(judgement.get("confidence") or 0.0)
                if judgement.get("resolved") and confidence >= _PLAN_LLM_CONFIDENCE_MIN:
                    reason = str(judgement.get("reason") or "")[:_RESOLUTION_REASON_MAX]
                    owner = _identity.mutation_owner(p.get("metadata") or {})
                    with _identity.manager_mutation_guard(
                        rt.bucket_mgr, {p["id"]: owner}
                    ):
                        await rt.bucket_mgr.update(
                            p["id"],
                            resolution_suggested={
                                "reason": reason,
                                "confidence": confidence,
                                "suggested_by": "plan_resolution_judge",
                                "source_bucket_id": source_bucket_id or "",
                                "ts": now_iso(),
                            },
                        )
                    rt.logger.info(
                        f"plan resolution suggested: {p['id']} — {reason[:_LOG_REASON_PREVIEW]}"
                    )
            except Exception as e:
                rt.logger.warning(f"plan resolution judgement failed for {p['id']}: {e}")
    except Exception as e:
        rt.logger.warning(f"check_plan_resolution outer error: {e}")


# ============================================================
# 显式 plan→bucket 联动（人工/AI 路径）
# ------------------------------------------------------------
# 当 plan 桶被「人工或 AI 显式」标为 resolved 时，把它指向的
# related_bucket / resolved_by 两个普通桶也同步标 resolved=True。
# 这是 rule.md §1 哲学落地：plan 是承诺，承诺被放下，承载这条承诺
# 的事件桶也不该再浮上来。
#
# check_plan_resolution（LLM 自动二判）只写 resolution_suggested，
# 不改变 plan status，因此也不会进入这条联动路径。
#
# 反向不做：bucket trace(resolved=1) 不联动 plan（plan 是独立承诺，
# 单条事件结束不等于承诺达成）。
# ============================================================
async def cascade_plan_resolved_to_buckets(plan_meta: dict, plan_id: str) -> list[str]:
    """把 plan_meta 里 related_bucket / resolved_by 指向的普通桶标 resolved。

    入参：plan 桶的 metadata + plan_id（仅用于日志）。
    出参：实际被联动到的 bucket_id 列表（已存在、未删除、未本来就 resolved）。
    异常：单个桶失败不影响其他；外层异常仅记日志、返回已联动列表。
    """
    linked: list[str] = []
    if not isinstance(plan_meta, dict):
        return linked
    plan_owner = _identity.owner_of(plan_meta)
    candidates: list[str] = []
    for key in ("related_bucket", "resolved_by"):
        val = (plan_meta.get(key) or "").strip() if isinstance(plan_meta.get(key), str) else ""
        # resolved_by 可能是 "manual" / "llm_judge"，不是 bucket_id，跳过
        if not val or val in ("manual", "llm_judge"):
            continue
        if val not in candidates:
            candidates.append(val)
    for bid in candidates:
        try:
            b = await rt.bucket_mgr.get(bid)
            if not b:
                continue
            meta = b.get("metadata", {})
            target_owner = _identity.mutation_owner(meta)
            if plan_owner and target_owner != plan_owner:
                continue
            # 已经 resolved 就不重复操作（避免无意义 touch）
            if meta.get("resolved"):
                continue
            # plan 不联动 plan；letter 也跳过（永久保留）
            if meta.get("type") in ("plan", "letter"):
                continue
            with _identity.manager_mutation_guard(
                rt.bucket_mgr, {bid: target_owner}
            ):
                ok = await rt.bucket_mgr.update(bid, resolved=True)
            if ok:
                linked.append(bid)
                rt.logger.info(
                    f"plan→bucket cascade: plan={plan_id} → bucket={bid} resolved=True"
                )
        except Exception as e:
            rt.logger.warning(
                f"plan→bucket cascade failed: plan={plan_id} bucket={bid} err={e}"
            )
    return linked


# 向后兼容：保留下划线别名（部分历史调用点用 _ 前缀）
_check_duplicate_for = check_duplicate_for
_check_plan_resolution = check_plan_resolution
