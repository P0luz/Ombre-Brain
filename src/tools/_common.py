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
from copy import deepcopy
from concurrent.futures import Future, InvalidStateError
from contextlib import AsyncExitStack, asynccontextmanager
import hashlib
import math
import threading

from bucket_manager import _filesystem_turn as _kernel_filesystem_turn
from utils import normalize_memory_title, now_iso, parse_bool
from ombrebrain.storage import bucket_paths as _bp
from ombrebrain.domain.plan_history import append_plan_change_log as append_plan_change_log

from . import _runtime as rt
from ._relation_link import link_new_bucket

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

# --- pinned 软阈值 ---
_PINNED_SOFT_GAP = 2                   # “软阈值 = cap - GAP”；cap=20 → soft=18

# --- check_duplicate_for / check_plan_resolution ---
_DUP_DEFAULT_THRESHOLD = 0.95          # 向量相似 >= 该值 → 标为疑似重复
_DUP_TOPK = 10                         # 检索前 N 个候选以判重复
_DUP_CHECK_CONCURRENCY = 4             # fire-and-forget 疑似重复检测的并发上限
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
        all_b = await rt.bucket_mgr.list_all(include_archive=False)
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
                outcome = await bucket_mgr.recover_archived_letter(bucket_id)
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

    # GET/dry-run 只使用 list_all 的当前读取结果。这里的结论仅供展示；POST
    # 不信任此快照，存储层会在桶租约内重新完整枚举与校验。
    buckets = await bucket_mgr.list_all(include_archive=True)
    grouped: dict[str, list[dict]] = {}
    for bucket in buckets:
        bucket_id = str((bucket or {}).get("id") or "").strip()
        if bucket_id:
            grouped.setdefault(bucket_id, []).append(bucket)

    candidate_ids: list[str] = []
    exclusions: list[dict[str, str]] = []
    for bucket_id, physical_rows in grouped.items():
        relevant_rows: list[dict] = []
        has_archived_signal = False
        for bucket in physical_rows:
            metadata = bucket.get("metadata") or {}
            if not isinstance(metadata, dict):
                continue
            strong = _bp.has_strong_letter_marker(metadata)
            ambiguous = _bp.has_ambiguous_letter_marker(metadata)
            if not (strong or ambiguous):
                continue
            relevant_rows.append(bucket)
            path = str(bucket.get("path") or "")
            if (
                str(metadata.get("type") or "").strip().casefold() == "archived"
                or _bp.path_is_within(path, bucket_mgr.archive_dir)
            ):
                has_archived_signal = True

        # 正常活跃 Letter 不属于这次历史兼容审计。
        if not relevant_rows or not has_archived_signal:
            continue
        if len(physical_rows) != 1:
            exclusions.append({"id": bucket_id, "reason": "duplicate_source"})
            continue

        bucket = relevant_rows[0]
        metadata = bucket.get("metadata") or {}
        path = str(bucket.get("path") or "")
        if not _bp.path_is_within(path, bucket_mgr.archive_dir):
            reason = "not_archived"
        elif str(metadata.get("type") or "").strip().casefold() != "archived":
            reason = "invalid_archived_type"
        else:
            reason = bucket_mgr.archived_letter_rejection(metadata)
        if reason:
            exclusions.append({"id": bucket_id, "reason": reason})
        else:
            candidate_ids.append(bucket_id)

    candidate_ids.sort()
    exclusions.sort(key=lambda item: item["id"])
    return {
        "candidate_count": len(candidate_ids),
        "candidate_ids": candidate_ids,
        "excluded_count": len(exclusions),
        "exclusions": exclusions,
    }


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
) -> Tuple[str, bool, str]:
    """
    检查是否有相似桶可合并，有则合并，无则新建。返回 (桶ID或名称, 是否合并, embed警告信息)。

    raw_merge=True (hold)：原文追加，不调 LLM 压缩。
    raw_merge=False (grow)：LLM 压缩老+新内容。

    iter 2.0 来源追踪：
    - source_tool: "hold" | "grow"，作为新建桶的 source_tool 写入；
      合并路径下保留原桶 source_tool 不变，但写 last_merged_by=source_tool。
    - grow_batch_id: 仅 grow 路径会传，新建时写入；合并路径不覆盖原桶的 batch_id
      （原桶可能来自上一次 grow 或 hold，硬覆盖会丢失最初批次信息）。

    Miss：meaning/media 是我自己的体验锚定，不是摘要。新建时直接写入；
    合并到老桶时两条 meaning 都保留（拼接），media 追加而不是覆盖。

    F-01 / F-08 fix：整个 search→create 路径在 per-content-hash Lock 下串行执行。
    同内容并发调用时后到的协程会阻塞，等前者写完后直接走合并分支，不产生重复桶。
    """
    async with _content_turn(content):
        result = await _merge_or_create_inner(
            content=content, tags=tags, importance=importance, domain=domain,
            valence=valence, arousal=arousal, name=name, title=title,
            source_refs=source_refs, quotes=quotes, raw_merge=raw_merge,
            why_remembered=why_remembered,
            merge_why_remembered=merge_why_remembered,
            source_tool=source_tool,
            grow_batch_id=grow_batch_id, meaning=meaning, media=media,
            test_data=test_data,
            _defer_derived_index=True,
        )

    # identical-content、merge-target 与 quota turns 都已释放。独立/兼容
    # 运行时即使需要同步调用 provider，也不能继续占用这些写入协调锁。
    post_index = getattr(rt.bucket_mgr, "_index_after_update", None)
    if callable(post_index) and result[0]:
        await post_index(
            result[0],
            content_changed=True,
            meaning_changed=bool(meaning),
        )
    return result


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
    _defer_derived_index: bool = False,
) -> Tuple[str, bool, str]:
    """实际的 search→merge/create 逻辑，由 merge_or_create 在 Lock 保护下调用。"""
    why_remembered = str(why_remembered or "").strip()[:_WHY_REMEMBERED_MAX_CHARS]
    merge_why_remembered = str(
        merge_why_remembered or ""
    ).strip()[:_WHY_REMEMBERED_MAX_CHARS]
    exact_storage_match = False
    try:
        existing = await rt.bucket_mgr.search(content, limit=1, domain_filter=domain or None)
    except Exception as e:
        rt.logger.warning(f"Search for merge failed, creating new / 合并搜索失败，新建: {e}")
        existing = []

    # Cache invalidation and a concurrent list_all() refresh can cross: an old
    # parsed snapshot may briefly hide a bucket that is already durable on disk.
    # Before any create, let Markdown truth override search/cache results.
    exact_finder = getattr(rt.bucket_mgr, "find_exact_content", None)
    if callable(exact_finder):
        try:
            # Byte-identical source text is the same write even when concurrent
            # Flash analyses choose different domains/tags. Metadata is a
            # derived classification and must not split one identical event.
            exact = exact_finder(content, domain_filter=None)
        except Exception as exc:
            rt.logger.warning(f"Exact-content storage check failed: {exc}")
        else:
            if exact:
                exact = dict(exact)
                exact["score"] = float("inf")
                existing = [exact]
                exact_storage_match = True

    merge_threshold = rt.config.get("merge_threshold") or 75
    if (
        not test_data
        and existing
        and existing[0].get("score", 0) > merge_threshold
    ):
        candidate_id = str(existing[0].get("id") or "").strip()
        merge_key = hashlib.sha256(
            candidate_id.encode("utf-8", errors="replace")
        ).hexdigest()[:_CONTENT_LOCK_KEY_HEX]
        try:
            # Different new texts can resolve to the same target bucket.  The
            # content-hash lock above cannot serialize that fan-in, so reserve
            # the logical target too and optimistically retry regular edits.
            async with _keyed_turn(f"merge-target-{merge_key}"):
                for _attempt in range(3):
                    bucket = await rt.bucket_mgr.get(candidate_id)
                    if not bucket:
                        break
                    metadata = bucket.get("metadata", {})
                    if not isinstance(metadata, dict):
                        metadata = {}
                    # Letter 是专用通道，生命周期类型即使被历史数据改写，也绝不
                    # 参与 hold/grow 的事件合并；延迟导入避免 plan.core 回引本模块。
                    from .plan.core import is_letter_bucket
                    if is_letter_bucket(bucket) or parse_bool(
                        metadata.get("pinned"), default=False
                    ) or parse_bool(
                        metadata.get("protected"), default=False
                    ) or is_terminal_memory_metadata(metadata) or str(
                        metadata.get("i_stage") or ""
                    ) == "candidate":
                        # 待沉淀的 I 候选不能当合并目标：它是「我对我自己的一个判断」，
                        # 不是时间里发生的事，把一件事追加进去语义上就错了；而且沉淀
                        # 要问的是「几轮梦之后它还站得住吗」，正文被改写就没有对象了。
                        # i_stage 的真源在 tools/i（这里不反向导入，避免循环依赖）。
                        break
                    snapshot_content = str(bucket.get("content") or "")
                    snapshot_metadata = deepcopy(metadata)

                    if not exact_storage_match:
                        judge = getattr(rt.dehydrator, "judge_same_event", None)
                        if not callable(judge):
                            rt.logger.warning(
                                "Same-event judge unavailable; creating new bucket / "
                                "同一事件判定器不可用，保守新建"
                            )
                            break
                        judgement = await judge(snapshot_content, content)
                        same_event = parse_bool(
                            judgement.get("same_event", False), default=False
                        )
                        try:
                            confidence = float(judgement.get("confidence", 0.0))
                        except (TypeError, ValueError):
                            confidence = 0.0
                        if not same_event or confidence < _SAME_EVENT_CONFIDENCE_MIN:
                            rt.logger.info(
                                "op=merge_or_create phase=branch branch=separate_event "
                                f"bucket_id={candidate_id} confidence={confidence:.3f} "
                                f"reason={str(judgement.get('reason', ''))[:_LOG_REASON_PREVIEW]}"
                            )
                            break

                    if raw_merge or exact_storage_match:
                        old_text = snapshot_content.rstrip()
                        new_text = content.strip()
                        if new_text and new_text not in old_text:
                            merged = (
                                f"{old_text}\n\n---\n{new_text}"
                                if old_text
                                else new_text
                            )
                        else:
                            merged = old_text or new_text
                    else:
                        merged = await rt.dehydrator.merge(
                            snapshot_content, content
                        )

                    old_v = metadata.get("valence") or 0.5
                    old_a = metadata.get("arousal") or 0.3
                    merged_valence = (
                        round((old_v + valence) / 2, 2)
                        if 0 <= valence <= 1
                        else old_v
                    )
                    merged_arousal = (
                        round((old_a + arousal) / 2, 2)
                        if 0 <= arousal <= 1
                        else old_a
                    )
                    merged_importance = max(
                        metadata.get("importance") or 5,
                        importance,
                    )
                    update_kwargs = {
                        "content": merged,
                        "tags": list(dict.fromkeys(tags + (metadata.get("tags") or []))),
                        "importance": merged_importance,
                        "domain": list(
                            dict.fromkeys(domain + (metadata.get("domain") or []))
                        ),
                        "valence": merged_valence,
                        "arousal": merged_arousal,
                    }
                    if title:
                        update_kwargs["title"] = title
                        old_name = str(metadata.get("name") or "")
                        timestamp_prefix = old_name[:19]
                        if (
                            len(timestamp_prefix) == 19
                            and timestamp_prefix[4] == "-"
                            and timestamp_prefix[7] == "-"
                            and timestamp_prefix[10] == " "
                            and timestamp_prefix[13] == "-"
                            and timestamp_prefix[16] == "-"
                        ):
                            update_kwargs["name"] = f"{timestamp_prefix} {title}"
                        else:
                            update_kwargs["name"] = title
                    if source_refs:
                        update_kwargs["source_refs_append"] = source_refs
                    if quotes:
                        # 合并到已有桶时引语追加，不覆盖：每条引语属于它自己的时刻，
                        # 不因为两段记忆被判定为同一件事就作废。超上限的处理见
                        # BucketManager._merge_quotes（丢弃并 OB-W006 明说）。
                        update_kwargs["quotes_append"] = quotes
                    if source_tool:
                        update_kwargs["last_merged_by"] = source_tool
                    # grow digest 在首次拆条时还没有稳定的目标桶，
                    # 只能在后续确认命中同一事件时补写。旧值优先，
                    # 自动整理永不覆盖已有的人工或历史理由。
                    if merge_why_remembered and not str(
                        metadata.get("why_remembered") or ""
                    ).strip():
                        update_kwargs["why_remembered"] = merge_why_remembered
                    if meaning:
                        update_kwargs["meaning_append"] = meaning
                    if media:
                        update_kwargs["media_append"] = media

                    derived_state = {}
                    async with AsyncExitStack() as commit_stack:
                        bucket_turn = getattr(rt.bucket_mgr, "_bucket_turn", None)
                        update_locked = getattr(
                            rt.bucket_mgr, "_update_locked", None
                        )
                        use_locked_update = callable(bucket_turn) and callable(
                            update_locked
                        )
                        if use_locked_update:
                            await commit_stack.enter_async_context(
                                bucket_turn(candidate_id)
                            )

                        locked_bucket = await rt.bucket_mgr.get(candidate_id)
                        if not locked_bucket:
                            break
                        locked_metadata = locked_bucket.get("metadata", {})
                        if not isinstance(locked_metadata, dict):
                            locked_metadata = {}
                        if is_terminal_memory_metadata(locked_metadata) or (
                            str(locked_bucket.get("content") or "")
                            != snapshot_content
                            or locked_metadata != snapshot_metadata
                        ):
                            continue

                        update_method = (
                            update_locked
                            if use_locked_update
                            else rt.bucket_mgr.update
                        )
                        if use_locked_update:
                            update_kwargs["_derived_state_out"] = derived_state
                        committed = await update_method(
                            candidate_id,
                            allow_embedding_fallback=(
                                raw_merge and source_tool == "hold"
                            ),
                            bump_active=True,
                            **update_kwargs,
                        )
                        if not committed:
                            break

                    queue_captured = getattr(
                        rt.bucket_mgr, "_queue_captured_derived_state", None
                    )
                    if use_locked_update and callable(queue_captured):
                        queue_captured(derived_state)

                    # _update_locked() 持有桶租约时只提交 Markdown。content/meaning
                    # 的 provider 索引必须等 AsyncExitStack 释放租约后执行，否则一次
                    # 慢 embedding 请求会让所有并发写入者等满 30 秒文件系统超时。
                    post_index = getattr(rt.bucket_mgr, "_index_after_update", None)
                    if (
                        not _defer_derived_index
                        and use_locked_update
                        and callable(post_index)
                    ):
                        await post_index(
                            candidate_id,
                            content_changed=True,
                            meaning_changed=bool(meaning),
                        )

                    try:
                        rt.dehydrator.invalidate_cache(snapshot_content)
                    except Exception:
                        pass
                    rt.logger.info(
                        "op=merge_or_create phase=branch branch=merge "
                        f"bucket_id={candidate_id} raw_merge={int(raw_merge)} "
                        f"source_tool={source_tool or '_'} "
                        f"score={existing[0].get('score', 0):.3f}"
                    )
                    return candidate_id, True, ""
                else:
                    rt.logger.warning(
                        "Merge target changed repeatedly; creating a new bucket "
                        "instead of overwriting concurrent edits: %s",
                        candidate_id,
                    )
        except Exception as e:
            rt.logger.warning(f"Merge failed, creating new / 合并失败，新建: {e}")

    async def create_bucket(final_importance: int) -> str:
        return await rt.bucket_mgr.create(
            content=content,
            tags=tags,
            importance=final_importance,
            domain=domain,
            valence=valence,
            arousal=arousal,
            name=name or None,
            title=title,
            why_remembered=why_remembered,
            source_tool=source_tool,
            event_actor="llm",
            grow_batch_id=grow_batch_id,
            meaning=meaning,
            media=media,
            test_data=test_data,
            source_refs=source_refs,
            quotes=quotes,
            defer_derived_index=_defer_derived_index,
            # hold 的铁律：正文优先落盘。打标/embedding 可降级，但绝不压缩或撤销记忆。
            allow_embedding_fallback=(raw_merge and source_tool == "hold"),
        )

    bucket_id = await create_bucket(importance)
    # create() 已在原文落盘后投递 embedding outbox，此处无需重复生成。
    # Managed runtime 下 queued 是正常成功态，不应在网络请求真正完成前误报
    # “向量失败”；没有 outbox 的兼容运行时才检查同步尝试的结果。
    embed_warn = ""
    embedding_state = "disabled"
    outbox = getattr(rt.bucket_mgr, "embedding_outbox", None)
    engine = rt.embedding_engine
    if outbox is not None:
        try:
            pending = bool(outbox.is_pending(bucket_id))
        except Exception as pending_exc:
            pending = False
            rt.logger.warning(
                "embedding outbox pending check failed for %s: %s",
                bucket_id,
                pending_exc,
            )
        if pending:
            embedding_state = "queued"
        else:
            existing = None
            lookup_error = None
            if engine and getattr(engine, "enabled", False):
                try:
                    existing = await engine.get_embedding(bucket_id)
                except Exception as exc:
                    lookup_error = exc
            if existing is not None:
                embedding_state = "indexed"
            else:
                # Defensive repair: a stale reconcile/path-index race must not
                # turn a transiently lost task into a permanent unindexed row
                # or tell the user to delete and recreate valid Markdown.
                repair_content = content
                try:
                    stored_bucket = await rt.bucket_mgr.get(bucket_id)
                    if stored_bucket is not None:
                        repair_content = str(
                            stored_bucket.get("content") or repair_content
                        )
                except Exception as read_exc:
                    rt.logger.warning(
                        "embedding repair could not reload bucket %s: %s",
                        bucket_id,
                        read_exc,
                    )
                try:
                    ensure_pending = getattr(outbox, "ensure_pending", None)
                    if callable(ensure_pending):
                        repaired = bool(ensure_pending(
                            bucket_id,
                            repair_content,
                        ))
                    else:
                        repaired = bool(outbox.enqueue(
                            bucket_id,
                            repair_content,
                            reset_retry=False,
                        ))
                except Exception as enqueue_exc:
                    try:
                        repaired = bool(outbox.is_pending(bucket_id))
                    except Exception:
                        repaired = False
                    rt.logger.warning(
                        "embedding outbox repair enqueue failed for %s: %s",
                        bucket_id,
                        enqueue_exc,
                    )
                if repaired:
                    embedding_state = "queued_repair"
                    rt.logger.warning(
                        "Requeued missing embedding task after create: %s%s",
                        bucket_id,
                        (
                            f" lookup_error={type(lookup_error).__name__}"
                            if lookup_error is not None else ""
                        ),
                    )
                else:
                    embedding_state = "missing"
                    embed_warn = _EMBED_WARN
                    rt.logger.info(
                        "op=merge_or_create phase=branch "
                        "branch=embed_degrade bucket_id=%s "
                        "reason=outbox_requeue_failed",
                        bucket_id,
                    )
    elif engine and getattr(engine, "enabled", False):
        try:
            existing = await engine.get_embedding(bucket_id)
            if existing is None:
                embedding_state = "missing"
                embed_warn = _EMBED_WARN
                rt.logger.info(
                    f"op=merge_or_create phase=branch branch=embed_degrade bucket_id={bucket_id} "
                    f"reason=no_embedding_after_create"
                )
            else:
                embedding_state = "indexed"
        except Exception as _embed_exc:
            embedding_state = "missing"
            embed_warn = _EMBED_WARN
            rt.logger.info(
                f"op=merge_or_create phase=branch branch=embed_degrade bucket_id={bucket_id} "
                f"reason={type(_embed_exc).__name__}"
            )
    rt.logger.info(
        f"op=merge_or_create phase=branch branch=create bucket_id={bucket_id} "
        f"source_tool={source_tool or '_'} grow_batch_id={grow_batch_id or '_'} "
        f"embedding_state={embedding_state}"
    )
    # 自动建立桶间关系：fire-and-forget，写入返回不等它。
    # 只在**新建**时触发——合并进已有桶时那条桶的关系已经建过了，
    # 重复推断只会反复撞每桶上限。关系建不出来不影响记忆本身。
    if not test_data:
        asyncio.create_task(link_new_bucket(bucket_id, content))

    return bucket_id, False, embed_warn


# grow/hold 等调用方以 asyncio.create_task(check_duplicate_for(...)) 的方式
# fire-and-forget 触发；同一批 grow 可能一次并发几十个 item，若不限流会
# 同时打满 embedding provider 的并发配额。信号量在函数体内获取，跟调用方
# 建了多少个 task 无关，只约束真正同时在跑 search_similar/update 的数量。
_dup_check_semaphore = asyncio.Semaphore(_DUP_CHECK_CONCURRENCY)


async def check_duplicate_for(new_bucket_id: str, new_text: str, threshold: float = _DUP_DEFAULT_THRESHOLD) -> None:
    """fire-and-forget：新桶写完后，向量相似 > threshold 的旧桶标为疑似重复。

    iter 1.6 §4：不自动合并，只在两边各写 dup_candidate=<对端 id> + dup_score=<0~1>，
    Dashboard 在桶详情里显示「疑似重复」提示，由她/他手动确认是否合并。
    """
    async with _dup_check_semaphore:
        try:
            if not rt.embedding_engine or not getattr(rt.embedding_engine, "enabled", False):
                return
            sims = await rt.embedding_engine.search_similar(new_text, top_k=_DUP_TOPK)
            for bid, score in sims:
                if bid == new_bucket_id:
                    continue
                if score < threshold:
                    continue
                try:
                    await rt.bucket_mgr.update(
                        new_bucket_id, dup_candidate=bid, dup_score=round(float(score), 4)
                    )
                    await rt.bucket_mgr.update(
                        bid, dup_candidate=new_bucket_id, dup_score=round(float(score), 4)
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

        all_b = await rt.bucket_mgr.list_all(include_archive=False)
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
            # 已经 resolved 就不重复操作（避免无意义 touch）
            if meta.get("resolved"):
                continue
            # plan 不联动 plan；letter 也跳过（永久保留）
            if meta.get("type") in ("plan", "letter"):
                continue
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
