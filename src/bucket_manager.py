"""
========================================
bucket_manager.py — 记忆桶的增删改查与多维索引
========================================

一个「记忆桶」就是一份带 YAML frontmatter 的 Markdown 文件。
这个文件负责把它们读出来、写回去、按主题域+情感坐标+文本模糊匹配筛出来。

关键行为：
- 每个桶 = 一个 .md 文件，按 permanent / dynamic / archive / feel / plans / letters 分目录存
- 创建/读取/更新/删除/搬家（move）都在这里
- 检索 = 先按 domain 预筛，再按情感坐标 + 文本相似度加权排序
- 情感坐标是 Russell 环形模型的连续值：valence 0~1（消极→积极），arousal 0~1（平静→激动）
- create()/update(content=...) 先落盘再投递 embedding outbox；delete() 清理派生索引
- 所有记忆类型都以 Markdown 为真源；向量化失败不会回滚原文，由后台统一重试
- iter 2.0：create() 接受 ``bucket_id_override``（feel 用分钟级可读 id），
  以及 ``source_tool`` / ``grow_batch_id`` 用于来源追踪

不做什么（边界）：
- 不做衰减打分（那是 decay_engine 的事）
- 不做 LLM 调用、不做向量化（那是 dehydrator / embedding_engine 的事）
- 不直接对外提供 MCP 工具（被 tools/* 通过 _runtime 引用）

对外暴露：BucketManager 类（create / get / update / delete / search / list_by_type 等）
========================================
"""
# ============================================================

import os
import re
import asyncio
import errno
import hashlib
import json
import logging
import math
import threading
import time
import tempfile
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

from ombrebrain.domain.plan_history import append_plan_change_log
from ombrebrain.eventsourcing.footprint import FootprintSnapshot

# 统一错误体系：越界 clamp 时上报 OB-W001/OB-W002（rule.md §11）
try:
    from errors import push_warning as _ob_push_warning  # type: ignore
except Exception:
    try:
        from .errors import push_warning as _ob_push_warning  # type: ignore
    except Exception:
        def _ob_push_warning(*_a, **_kw):  # type: ignore
            return None


class _CrossLoopAsyncLock:
    """An async mutex that is safe across FastMCP event loops and threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    async def __aenter__(self):
        while not self._lock.acquire(blocking=False):
            await asyncio.sleep(0.01)
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        self._lock.release()


@asynccontextmanager
async def _filesystem_turn(base_dir: str, key: str, timeout_seconds: float = 30.0):
    """Cross-thread/cross-loop/process mutual exclusion via an OS file lease.

    A plain ``asyncio.Lock`` only serializes tasks scheduled on the same event
    loop; FastMCP may dispatch requests from different loops/threads (see the
    identical rationale in tools/_common.py's ``_filesystem_content_turn``), so
    quota check-then-write sequences (anchor's 24-cap) need an OS-level guard
    instead of an in-process one.

    The kernel owns the lease for as long as this context keeps its descriptor
    open.  This has two important properties that an mtime-based lock file does
    not: a slow live operation can never have its lock stolen after an arbitrary
    age, while a crashed process releases the lease automatically.  The key is
    hashed before it becomes a filename, so an untrusted bucket id cannot escape
    ``.locks`` or create nested paths.
    """
    if not base_dir:
        yield
        return
    lock_dir = Path(base_dir) / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_id = hashlib.sha256(str(key).encode("utf-8", errors="surrogatepass")).hexdigest()
    lock_path = lock_dir / f"{lock_id}.lock"
    token = f"{os.getpid()}:{threading.get_ident()}:{uuid.uuid4().hex}"
    deadline = time.monotonic() + timeout_seconds
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        handle = os.fdopen(descriptor, "r+b", buffering=0)
    except Exception:
        os.close(descriptor)
        raise

    busy_errnos = {
        errno.EACCES,
        errno.EAGAIN,
        getattr(errno, "EWOULDBLOCK", errno.EAGAIN),
    }
    busy_winerrors = {32, 33}  # Windows 共享冲突 / 锁冲突

    def _is_busy(exc: OSError) -> bool:
        return (
            exc.errno in busy_errnos
            or getattr(exc, "winerror", None) in busy_winerrors
        )

    def _owner_preview() -> str:
        try:
            previous = handle.tell()
            handle.seek(0)
            raw = handle.read(1024)
            handle.seek(previous)
            preview = raw.decode("ascii", errors="replace").strip("\0\r\n ")
            return preview[:512] or "empty"
        except (OSError, ValueError):
            return "unavailable"

    def _log_non_contention_error(exc: OSError, phase: str) -> None:
        logger.error(
            "Filesystem lease %s failed: key=%r lock_id=%s path=%s "
            "errno=%s winerror=%s owner=%s",
            phase,
            key,
            lock_id,
            lock_path,
            exc.errno,
            getattr(exc, "winerror", None),
            _owner_preview(),
        )

    def _try_acquire() -> bool:
        try:
            if os.name == "nt":  # pragma: no branch - platform-specific
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - exercised in Linux CI/container
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            if _is_busy(exc):
                return False
            _log_non_contention_error(exc, "acquire")
            raise

    def _release() -> None:
        if os.name == "nt":  # pragma: no branch - platform-specific
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover - exercised in Linux CI/container
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    acquired = False
    try:
        # Windows 字节范围锁要求目标字节已存在。两个进程可能同时打开刚创建的
        # 零字节文件，因此初始化必须放在最外层 try 内，确保任务取消也会关闭句柄。
        while True:
            handle.seek(0, os.SEEK_END)
            if handle.tell() > 0:
                break
            try:
                handle.write(b"\0")
                break
            except OSError as exc:
                if os.name != "nt" or not _is_busy(exc):
                    _log_non_contention_error(exc, "initialization")
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "timed out initializing filesystem lease "
                        f"{lock_id} key={key!r} owner={_owner_preview()}"
                    )
                await asyncio.sleep(0.01)
        handle.seek(0)

        while not acquired:
            acquired = _try_acquire()
            if acquired:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "timed out waiting for filesystem lease "
                    f"{lock_id} key={key!r} owner={_owner_preview()}"
                )
            await asyncio.sleep(0.01)

        owner = json.dumps(
            {
                "state": "held",
                "token": token,
                "pid": os.getpid(),
                "thread": threading.get_ident(),
                "acquired_at": time.time(),
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        handle.seek(0)
        handle.write(owner)
        handle.truncate()
        yield
    finally:
        if acquired:
            try:
                released = json.dumps(
                    {
                        "state": "released",
                        "token": token,
                        "pid": os.getpid(),
                        "released_at": time.time(),
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                ).encode("ascii")
                handle.seek(0)
                handle.write(released)
                handle.truncate()
            except OSError as exc:
                logger.warning(
                    "Filesystem lease release marker failed: key=%r lock_id=%s "
                    "errno=%s winerror=%s",
                    key,
                    lock_id,
                    exc.errno,
                    getattr(exc, "winerror", None),
                )
            try:
                _release()
            except OSError as exc:
                # 即使显式解锁失败，下面关闭 descriptor 仍是最终释放保障。
                logger.warning(
                    "Filesystem lease explicit unlock failed: key=%r lock_id=%s "
                    "errno=%s winerror=%s",
                    key,
                    lock_id,
                    exc.errno,
                    getattr(exc, "winerror", None),
                )
        try:
            handle.close()
        except OSError as exc:
            logger.warning(
                "Filesystem lease descriptor close failed: key=%r lock_id=%s "
                "errno=%s winerror=%s",
                key,
                lock_id,
                exc.errno,
                getattr(exc, "winerror", None),
            )


def _clamp_importance(v, source: str) -> int:
    """importance 越界 → clamp 到 [1,10]，并产生 OB-W001 提示。"""
    try:
        iv = int(v)
    except (TypeError, ValueError, OverflowError):
        _ob_push_warning("OB-W001", f"importance={v!r} 无法解析，回退为 5（{source}）")
        return 5
    if iv < 1 or iv > 10:
        clamped = max(1, min(10, iv))
        _ob_push_warning("OB-W001", f"importance={iv} 超出 [1,10]，已修正为 {clamped}（{source}）")
        return clamped
    return iv


def _clamp_unit(v, field: str, source: str) -> float:
    """valence/arousal 越界 → clamp 到 [0.0,1.0]，并产生 OB-W002 提示。"""
    try:
        fv = float(v)
    except (TypeError, ValueError):
        _ob_push_warning("OB-W002", f"{field}={v!r} 无法解析，回退为 0.5（{source}）")
        return 0.5
    if not math.isfinite(fv):
        _ob_push_warning("OB-W002", f"{field}={v!r} 不是有限数，回退为 0.5（{source}）")
        return 0.5
    if fv < 0.0 or fv > 1.0:
        clamped = max(0.0, min(1.0, fv))
        _ob_push_warning("OB-W002", f"{field}={fv} 超出 [0.0,1.0]，已修正为 {clamped}（{source}）")
        return clamped
    return fv


from pathlib import Path
from typing import Any, Optional

import frontmatter

from utils import (
    atomic_write_text,
    generate_bucket_id,
    sanitize_name,
    safe_path,
    now_iso,
    normalize_memory_title,
    parse_bool,
    parse_iso_datetime,
    publish_new_file,
)
from ombrebrain.storage import bucket_paths as _bp
from ombrebrain.storage import metadata_normalize as _mn
from ombrebrain.storage.media_store import MediaStore
from ombrebrain.retrieval.bucket_scoring import (
    calc_topic_score,
    calc_emotion_score,
    calc_time_score,
    calc_touch_score,
)
from ombrebrain.eventsourcing.ledger_mirror import LedgerMirror
from ombrebrain.eventsourcing.ledger_replay import LedgerReplayValidator
from ombrebrain.projection.projection_mirror import TraceCatalogProjection
from ombrebrain.projection.projection_sqlite import TraceSQLiteProjection
from ombrebrain.projection.projection_vector import TraceVectorProjectionManifest
from ombrebrain.policy.formal_invariants import FormalInvariantChecker


def _letter_lock_revision(metadata: Any) -> tuple[str, str, str]:
    if not hasattr(metadata, "get"):
        return ("", "", "")
    return (
        str(metadata.get("lock_type") or "").strip().casefold(),
        str(metadata.get("unlock_date") or "").strip(),
        str(metadata.get("locked_by") or "").strip().casefold(),
    )


try:
    from bm25_index import BM25Index as _BM25Index
except ImportError:
    _BM25Index = None  # type: ignore

logger = logging.getLogger("ombre_brain.bucket")


_atomic_write_text = atomic_write_text  # Backward-compatible private alias.


def _atomic_create_text(path: str, text: str) -> None:
    """Publish a complete text file atomically, refusing an existing target.

    ``atomic_write_text`` intentionally replaces its destination, which is the
    right behavior for updates but unsafe for creation races.  Build the full
    file beside the destination and publish it with a hard link: link creation
    is atomic and fails with ``FileExistsError`` instead of overwriting.

    硬链接不是所有文件系统都支持（Termux/Android 的 FUSE 挂载、部分 NAS/SMB
    卷）。``publish_new_file`` 在那种环境下退到 ``O_CREAT|O_EXCL``，仍然保住
    「已存在就拒绝」这条——退成 ``os.replace`` 是不行的，那会把「创建」悄悄
    变成「覆盖」。
    """

    target = os.path.abspath(path)
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{os.path.basename(target)}.create.",
        dir=parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        publish_new_file(temporary, target, text)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


# ============================================================
# 调参面板 / Tunable constants
# ------------------------------------------------------------
# rule.md §①：禁裸魔法数字。检索评分、时间涾漪、字段截断上限集中在这里。
# 修改这些数值 → 请同步跑 tests/regression 验证评分行为。
# ============================================================

# --- 默认元数据值（与 dehydrator/import_memory 保持一致）---
_DEFAULT_VALENCE = 0.5
_DEFAULT_AROUSAL = 0.3
_DEFAULT_IMPORTANCE = 5
_PINNED_IMPORTANCE = 10           # pinned/protected 桶 importance 锁定值
_DEFAULT_DOMAIN_NAME = "未分类"     # 未提供 domain 时的占位
_EDITABLE_BUCKET_TYPES = frozenset(
    {"dynamic", "permanent", "feel", "plan", "letter", "i", "self"}
)
_PLAN_STATUSES = frozenset({"active", "resolved", "abandoned"})
_ARCHIVED_LETTER_TERMINAL_VALUE_FIELDS = (
    "deleted_at",
    "tombstoned_at",
    "erasure_mode",
    "erased_at",
)
_ARCHIVED_LETTER_TERMINAL_BOOL_FIELDS = (
    "tombstone",
    "deleted",
    "physical_erasure",
)

# --- 字段截断长度（避免 frontmatter 肨胀）---
_SOURCE_TOOL_MAX = 32
_GROW_BATCH_ID_MAX = 64
_WHY_REMEMBERED_MAX = 500
_TRIGGERED_BY_MAX = 64
_DEFAULT_MAX_BUCKET_BYTES = 50 * 1024
_MAX_TAGS = 64
_MAX_TAG_CHARS = 128
_MAX_DOMAINS = 16
_MAX_DOMAIN_CHARS = 128

# --- Miss：meaning / media（hold 的体验锚定扩展）---
# meaning 存储为 list[str]：同一条记忆可能在不同时刻被反复触动，每次 hold
# 传入的是新增的一条，追加到列表，不覆盖已有的（见 tools/_common.py merge_or_create）。
_MEANING_ITEM_MAX = 2000        # 单条 meaning 的长度上限
_MEANING_LIST_MAX_ITEMS = 50    # 一个桶最多累积多少条 meaning
_MEDIA_MAX_ITEMS = 20           # 单条记忆最多关联多少个 media 引用
_MEDIA_PATH_MAX = 500
_MEDIA_TITLE_MAX = 200
_MEDIA_TYPE_MAX = 32
_MEDIA_NOTE_MAX = 500

_METADATA_TEXT_LIMITS = {
    "status": 32,
    "type": 32,
    "resolution_reason": 500,
    "resolved_by": 128,
    "related_bucket": 128,
    "author": 120,
    "user_name": 120,
    "title": 120,
    "letter_date": 64,
    "lock_type": 16,
    "unlock_date": 64,
    "locked_by": 16,
    "lock_owner_source": 32,
    "writer_name": 120,
    "why_remembered": _WHY_REMEMBERED_MAX,
    "triggered_by": _TRIGGERED_BY_MAX,
    "source_tool": _SOURCE_TOOL_MAX,
    "grow_batch_id": _GROW_BATCH_ID_MAX,
    "last_merged_by": _SOURCE_TOOL_MAX,
    "_pre_anchor_source_tool": _SOURCE_TOOL_MAX,
}

# --- _time_ripple：时间涾漪 ---
_RIPPLE_HOURS = 48.0       # ±该小时内的桶被轻微唤醒
_RIPPLE_MAX_BUCKETS = 5    # 一次 touch 最多唤醒几个邻居（有界 I/O）
_RIPPLE_BOOST = 0.3        # 唤醒时 activation_count 增量
_MAX_METADATA_DEPTH = 16
_MAX_METADATA_NODES = 10_000

# --- search 评分 ---
_VECTOR_TOPK = 50          # embedding 预取 top_k（仅作 semantic 分源，不窄化候选集）
# 纯语义候选进入结果池的最低余弦相似度。config.matching.vector_recall_threshold 可覆盖。
#
# 2026-08-18 从 0.65 下调到 0.55，依据是对 917 桶真实记忆的只读扫描：
#
#   阈值    平均新增/查询   双通道印证率   新增相似度中位
#   0.65        0.1          100.0%         0.661   ← 旧值
#   0.55        8.6           88.3%         0.566   ← 拐点
#   0.50       55.1           69.0%         0.520
#   0.45      170.8           60.6%         0.483
#
# 0.65 之下这条语义直通路**事实上不存在**：9 个宽泛查询一共只有 1 条桶能靠它
# 进来，「我的工作」「同事」「情绪」全是 0。代码里写着 text_match or
# semantic_match，但后一支从来不为真——OB 名义上是混合检索，实际是纯关键词检索。
#
# 原因是 semantic 权重只占 2.5/13.5≈18.5%：一条桶哪怕相似度 0.9，单靠这一维也
# 只贡献约 16.7 分，离 fuzzy_threshold=50 差得远，必须同时在 topic（关键词重合）
# 上得分才过得去。而「我的工作」这几个字根本不会字面出现在记忆里。
#
# 选 0.55 而不是更低：双通道印证率（新召回的桶里同时被关键词命中的比例）在这里
# **不降反升**到 88.3%，说明捞回的是"关键词也认、只是加权分被七维稀释掉"的桶；
# 再往下印证率单调劣化，0.45 时每查询涌进 170 条、印证率只剩 60%，那是拿噪音换召回。
#
# ⚠️ 已知弱点：印证率用"关键词也命中"当作"真的相关"的代理，而宽泛查询恰恰是
# 关键词最不管用的场景——它能证明 0.45 是坏的，不能独立证明 0.55 是好的。
# 0.55 最终由人工逐条看过新召回内容后确认（面试、薪资与配得感、上线那一刻的
# 踏实感，都是该出现却一条都出不来的记忆）。调整前请重跑扫描，不要直接改数字。
_VECTOR_RECALL_THRESHOLD = 0.55
_RESOLVED_RANK_PENALTY = 0.3   # resolved 桶仅在排序时降权
_LITERAL_MATCH_BONUS = 25.0    # 查询串原样命中 name/tags/domain/正文时的召回加分（修短查询召回）

# topic/emotion/time/touch 四个评分维度的纯函数 + 权重常量已拆到
# ombrebrain.retrieval.bucket_scoring（search() 和 _calc_*_score wrapper 都从那里导入）。


def _clamp01(value, default: float) -> float:
    """将任意输入钳制到 [0.0, 1.0]；失败返回 default。

    专门处理身体里散落的 ``max(0.0, min(1.0, float(x)))`` 样板
    （model_valence / weight / bucket_type_defaults.weight 等）。
    哲学 valence/arousal 请走 _clamp_unit，那个会 push OB-W002。
    这个 helper 静默钳制，适用于“调用方保证范围、充其量充个防”的场景。
    """
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(numeric):
        return default
    return max(0.0, min(1.0, numeric))


class BucketManager:
    """
    Memory bucket manager — entry point for all bucket CRUD operations.
    Buckets are stored as Markdown files with YAML frontmatter for metadata
    and body for content. Natively compatible with Obsidian browsing/editing.
    记忆桶管理器 —— 所有桶的 CRUD 操作入口。
    桶以 Markdown 文件存储，YAML frontmatter 存元数据，正文存内容。
    天然兼容 Obsidian 直接浏览和编辑。
    """

    def __init__(self, config: dict, embedding_engine=None, v3_runtime=None):
        # iter 1.9 G: 保留原始 config 引用，让 create() 能读 bucket_type_defaults
        # Keep raw config so create() can look up bucket_type_defaults at write time.
        self.config = config
        self.v3_runtime = v3_runtime
        # --- Read storage paths from config / 从配置中读取存储路径 ---
        self.base_dir = config["buckets_dir"]
        self.media_store = MediaStore(
            self.base_dir,
            str(config.get("media_dir") or os.path.join(self.base_dir, "_media")),
            max_bytes=int(config.get("media_max_bytes") or 25 * 1024 * 1024),
            # 同一个上限传下去，让 MediaStore 在动手持久化之前就拒绝超量，
            # 而不是逐项写完再由下面的 _normalize_media 截断。
            max_items=_MEDIA_MAX_ITEMS,
        )
        self.permanent_dir = os.path.join(self.base_dir, "permanent")
        self.dynamic_dir = os.path.join(self.base_dir, "dynamic")
        self.archive_dir = os.path.join(self.base_dir, "archive")
        self.feel_dir = os.path.join(self.base_dir, "feel")
        self.plan_dir = os.path.join(self.base_dir, "plans")
        self.letter_dir = os.path.join(self.base_dir, "letters")
        self.fuzzy_threshold = config.get("matching", {}).get("fuzzy_threshold", 50)
        # 纯语义候选的门槛。见 _VECTOR_RECALL_THRESHOLD 上方的扫描依据。
        try:
            self.vector_recall_threshold = float(
                config.get("matching", {}).get(
                    "vector_recall_threshold", _VECTOR_RECALL_THRESHOLD
                )
            )
        except (TypeError, ValueError):
            self.vector_recall_threshold = _VECTOR_RECALL_THRESHOLD
        self.max_results = config.get("matching", {}).get("max_results", 5)

        # --- Search scoring weights / 检索权重配置 ---
        scoring = config.get("scoring_weights", {})
        self.w_topic = scoring.get("topic_relevance", 4.0)
        self.w_emotion = scoring.get("emotion_resonance", 2.0)
        self.w_time = scoring.get("time_proximity", 1.5)
        self.w_importance = scoring.get("importance", 1.0)
        self.content_weight = scoring.get("content_weight", 1.0)  # body×1, per spec
        # iter 2.1: touch + semantic 两个新维度
        # touch: 被主动召回越多加分越高（上限 10 次归一化）
        # semantic: embedding 余弦相似度（仅 embedding 启用时生效）
        self.w_touch = scoring.get("touch_weight", 1.0)
        self.w_semantic = scoring.get("semantic_weight", 2.5)
        # BM25: TF-IDF 加权关键词匹配（rank_bm25+jieba，软依赖）
        self.w_bm25 = scoring.get("bm25_weight", 1.5)

        # --- Optional embedding engine for pre-filtering / 可选 embedding 引擎，用于预筛候选集 ---
        self.embedding_engine = embedding_engine
        self.embedding_outbox = None
        ledger_path = config.get("ledger_path") or os.path.join(
            self.base_dir, "_ledger", "events.jsonl"
        )
        self.ledger_mirror = LedgerMirror(ledger_path)

        # BM25 稀疏索引（写操作后脏标记，search() 时懒重建）
        self._bm25: "_BM25Index | None" = _BM25Index() if _BM25Index is not None else None
        self._bm25_dirty: bool = True
        self._bm25_rebuilding: bool = False  # Avoid concurrent duplicate rebuilds.

        # Active-bucket cache and its on-disk fingerprint are invalidated after writes.
        self._active_cache: "list[dict] | None" = None
        self._active_file_state: dict[str, tuple[int, int]] = {}
        self._active_cache_state_guard = threading.RLock()
        self._active_cache_generation = 0
        self._active_cache_lock = _CrossLoopAsyncLock()
        # Synchronous CRUD paths ask for IDs repeatedly.  A complete index
        # turns migration conflict/apply checks from O(imported × vault) into
        # one O(vault) build plus O(1) lookups.  Managed writes invalidate it;
        # external changes do so when the normal vault poll observes them.
        self._bucket_path_index_guard = threading.RLock()
        self._bucket_path_index: dict[str, str] = {}
        self._bucket_path_index_ready = False
        # 见 _bucket_turn：archive()/update()/delete()/touch() 各自独立做
        # find_file → load → mutate → atomic_write，互不知会。并发命中同一个
        # bucket_id 时（比如衰减引擎后台 archive() 撞上一次 trace/hold 的
        # update()），后到的那个基于自己读到的旧 file_path 写回，可能在另一个
        # 已经把文件 move 进 archive/ 之后，在原路径「复活」一份带旧内容的
        # 桶。找茬会话（2026-07-15）发现，按 tools/_common.py 里 _quota_turn
        # 同一套跨 loop/进程文件锁方案修，见 _bucket_turn()。
        storage_cfg = config.get("storage", {}) or {}
        try:
            self.external_change_poll_seconds = max(
                0.0, float(storage_cfg.get("external_change_poll_seconds", 1.0))
            )
        except (TypeError, ValueError):
            self.external_change_poll_seconds = 1.0
        self._last_file_state_check = 0.0
        self._external_changes_detected = 0
        self._last_external_change = ""

    def attach_v3_runtime(self, runtime) -> None:
        self.v3_runtime = runtime

    def attach_embedding_outbox(self, outbox) -> None:
        """Attach the durable derived-index queue after both objects exist."""
        self.embedding_outbox = outbox

    def _queue_derived_state(
        self,
        bucket_id: str,
        *,
        content: str = "",
        meaning=None,
        queue_content: bool = False,
        queue_meaning: bool = False,
    ) -> None:
        """Markdown 提交后同步持久登记派生期望状态，不调用外部 provider。"""
        outbox = self.embedding_outbox
        if outbox is None:
            return
        try:
            if queue_content:
                outbox.enqueue(bucket_id, str(content or ""))
            if queue_meaning:
                meaning_list = self._normalize_meaning_list(meaning or [])
                meaning_text = meaning_list[-1] if meaning_list else ""
                enqueue_meaning = getattr(outbox, "enqueue_meaning", None)
                if callable(enqueue_meaning):
                    enqueue_meaning(bucket_id, meaning_text)
        except Exception as exc:
            logger.warning(
                "Derived outbox enqueue failed / 派生 outbox 入队失败: %s: %s",
                bucket_id,
                exc,
            )

    def _queue_captured_derived_state(self, state: dict | None) -> None:
        """Persist a commit snapshot after its bucket lease has been released."""
        if not state:
            return
        self._queue_derived_state(
            str(state.get("bucket_id") or ""),
            content=str(state.get("content") or ""),
            meaning=state.get("meaning") or [],
            queue_content=bool(state.get("queue_content")),
            queue_meaning=bool(state.get("queue_meaning")),
        )

    async def _discard_derived_index_if_terminal(self, bucket_id: str) -> None:
        """Drop queued/vector state outside the bucket lease, with restore CAS."""
        async with self._derived_index_turn(bucket_id):
            bucket = await self.get(bucket_id)
            metadata = (bucket or {}).get("metadata") or {}
            if bucket and not metadata.get("deleted_at") and not parse_bool(
                metadata.get("tombstone"), default=False
            ):
                # A concurrent explicit restore won after the delete commit.
                return
            if self.embedding_outbox is not None:
                try:
                    self.embedding_outbox.discard(bucket_id)
                except Exception as exc:
                    logger.warning(
                        "discard embedding outbox failed for %s: %s",
                        bucket_id,
                        exc,
                    )
            if self.embedding_engine is not None:
                try:
                    self.embedding_engine.delete_embedding(bucket_id)
                except Exception as exc:
                    logger.warning(
                        "delete embedding failed for %s: %s", bucket_id, exc
                    )

    def _record_v3_bucket_event(
        self,
        action: str,
        bucket_id: str,
        bucket_type: str,
        content: str,
        metadata: dict | None,
    ) -> None:
        runtime = getattr(self, "v3_runtime", None)
        recorder = getattr(runtime, "record_bucket_event", None)
        if not callable(recorder):
            return
        try:
            recorder(
                action=action,
                bucket_id=bucket_id,
                bucket_type=bucket_type,
                content=content,
                metadata=metadata or {},
            )
        except Exception as exc:
            logger.warning(f"v3 bucket event record failed for {action}:{bucket_id}: {exc}")

    def _record_ledger_event(
        self,
        event_type: str,
        bucket_id: str,
        bucket_type: str,
        content: str,
        metadata: dict | None,
        extra_payload: dict | None = None,
    ) -> None:
        payload = dict(metadata or {})
        if extra_payload:
            payload.update(extra_payload)
        try:
            self.ledger_mirror.append_event(
                event_type=event_type,
                trace_id=bucket_id,
                trace_kind=bucket_type,
                payload=payload,
                body=content,
            )
        except Exception as exc:
            logger.warning(f"ledger mirror record failed for {event_type}:{bucket_id}: {exc}")

    def ledger_integrity_report(self, *, rebuild_projections: bool = False) -> dict:
        """默认返回只读报告；仅在显式要求时重建持久化投影。"""
        report = self.ledger_mirror.verify_integrity()
        events = list(self.ledger_mirror.iter_events())
        projection = TraceCatalogProjection()
        projection.rebuild(events)
        report["trace_catalog_projection"] = projection.to_report(
            source_latest_seq=int(report.get("latest_seq", 0) or 0)
        )
        sqlite_projection_path = os.path.join(
            self.base_dir, "_ledger", "projections", "trace_catalog.sqlite3"
        )
        try:
            sqlite_projection = TraceSQLiteProjection(sqlite_projection_path)
            if rebuild_projections:
                sqlite_projection.rebuild(events)
            report["sqlite_projection"] = sqlite_projection.to_report(
                source_latest_seq=int(report.get("latest_seq", 0) or 0)
            )
        except Exception as exc:
            report["sqlite_projection"] = {
                "projection_name": "trace_catalog_sqlite",
                "projection_role": "shadow",
                "canonical": False,
                "path": sqlite_projection_path,
                "ok": False,
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:240],
            }
        vector_projection_path = getattr(
            self.embedding_engine,
            "db_path",
            os.path.join(self.base_dir, "embeddings.db"),
        )
        try:
            vector_projection = TraceVectorProjectionManifest(vector_projection_path)
            report["vector_projection"] = vector_projection.rebuild(events)
        except Exception as exc:
            report["vector_projection"] = {
                "projection_name": "trace_vector_manifest",
                "projection_role": "shadow",
                "canonical": False,
                "path": str(vector_projection_path),
                "ok": False,
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:240],
            }
        try:
            report["formal_invariants"] = FormalInvariantChecker.default().evaluate_ledger(events).to_dict()
        except Exception as exc:
            report["formal_invariants"] = {
                "projection_name": "formal_invariants",
                "projection_role": "shadow",
                "canonical": False,
                "ok": False,
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:240],
            }
        report["replay"] = LedgerReplayValidator.default().validate(events)
        return report

    def footprint_snapshot(self) -> FootprintSnapshot:
        """读取旧 Ledger 兼容存储，生成面向 breath 的一次性足迹快照。"""
        return FootprintSnapshot.from_events(self.ledger_mirror.iter_events())

    # ---------------------------------------------------------
    # Internal helpers【代码多复用、不作为公共 API】
    # 内部工具：目录遍历 / 主域路径 / 装入与开销完全一致于原原本
    # ---------------------------------------------------------
    @property
    def _active_dirs(self) -> list[str]:
        """不含 archive 的活跃桶目录（list_all/_collect_all_tags/查找均使用）。。。顺序不可随意调整：feel/plan/letter 在 dynamic 之后是为了与原代码扫描顺序保持一致。"""
        return [self.permanent_dir, self.dynamic_dir,
                self.feel_dir, self.plan_dir, self.letter_dir]

    def _iter_md_files(self, dirs: list[str]):
        """递归遍历多个目录下的 *.md，yield (root, filename, full_path)。

        原本中 5 处 ``for root, _, files in os.walk(…): for f in files: if not f.endswith('.md'): continue`` 同表现。。。
        这里不加任何过滤逻辑，调用方自己判断是否跳过。
        """
        for dir_path in dirs:
            if not os.path.exists(dir_path):
                continue
            for root, _, files in os.walk(dir_path):
                for fname in files:
                    if not fname.endswith(".md"):
                        continue
                    yield root, fname, os.path.join(root, fname)

    @staticmethod
    def _primary_domain(domain: list[str] | str | None) -> str:
        """取 domain[0] 作为主域子目录名，空/缺失 → 默认 ``未分类``。

        在 create / _move_bucket / archive 三处使用。sanitize_name 后才能当路径用。
        """
        if isinstance(domain, str):
            primary = domain.strip()
        elif domain:
            primary = str(domain[0]).strip()
        else:
            primary = ""
        return sanitize_name(primary) if primary else _DEFAULT_DOMAIN_NAME

    def _max_bucket_bytes(self) -> int:
        raw = (self.config.get("limits") or {}).get(
            "max_bucket_bytes", _DEFAULT_MAX_BUCKET_BYTES
        )
        try:
            value = int(raw)
        except (TypeError, ValueError, OverflowError):
            return _DEFAULT_MAX_BUCKET_BYTES
        return value if value >= 0 else _DEFAULT_MAX_BUCKET_BYTES

    def _validate_bucket_content(self, content: str) -> None:
        cap = self._max_bucket_bytes()
        if cap <= 0:
            return
        size = len(content.encode("utf-8"))
        if size > cap:
            raise ValueError(
                f"内容过大（{size / 1024:.1f} KB > 上限 {cap / 1024:.0f} KB）。"
                "请拆分后存入，或调整 config.limits.max_bucket_bytes。"
            )

    _normalize_metadata_list = staticmethod(_mn._normalize_metadata_list)

    _normalize_meaning_item = staticmethod(_mn._normalize_meaning_item)

    _normalize_meaning_list = staticmethod(_mn._normalize_meaning_list)

    _normalize_media = staticmethod(_mn._normalize_media)

    # ---------------------------------------------------------
    # Internal: keep embedding index in sync with markdown storage
    # 内部：保证向量索引与 markdown 存储层一致
    # ---------------------------------------------------------
    async def _sync_embedding(self, bucket_id: str, content: str) -> bool:
        """Best-effort inline indexing for runtimes without a queue worker."""
        if not self.embedding_engine or not getattr(self.embedding_engine, "enabled", False):
            return False
        if not content or not content.strip():
            return True
        return bool(
            await self.embedding_engine.generate_and_store(bucket_id, content)
        )

    async def _sync_meaning_embedding(self, bucket_id: str, meaning_list: list[str]) -> None:
        """Best-effort: embed the most recent meaning entry, separate from content.

        取列表最后一条：最新的感受通常最贴近当前语境。没有专门的 outbox/重试
        队列——meaning 向量失败不影响记忆本身已经落盘，稍后可通过再次
        hold/trace 追加新 meaning 时重新尝试。
        """
        meaning_text = meaning_list[-1] if meaning_list else ""
        outbox = self.embedding_outbox
        if outbox is not None:
            try:
                queued = bool(outbox.enqueue_meaning(bucket_id, meaning_text))
            except Exception as exc:
                queued = False
                logger.warning("meaning outbox enqueue failed for %s: %s", bucket_id, exc)
            if queued and getattr(outbox, "running", False):
                return
        engine = self.embedding_engine
        if not engine or not getattr(engine, "enabled", False):
            return
        if not meaning_text:
            clear_meaning = getattr(engine, "delete_meaning_embedding", None)
            if callable(clear_meaning):
                try:
                    clear_meaning(bucket_id)
                    if outbox is not None:
                        complete_meaning = getattr(
                            outbox, "complete_meaning", None
                        )
                        if callable(complete_meaning):
                            complete_meaning(bucket_id, meaning_text)
                except Exception as exc:
                    logger.warning(f"meaning embedding clear failed for {bucket_id}: {exc}")
            return
        store_meaning = getattr(engine, "generate_and_store_meaning", None)
        if not callable(store_meaning):
            return
        try:
            stored = bool(await store_meaning(bucket_id, meaning_text))
            if stored and outbox is not None:
                complete_meaning = getattr(outbox, "complete_meaning", None)
                if callable(complete_meaning):
                    complete_meaning(bucket_id, meaning_text)
            elif not stored:
                logger.warning("meaning embedding remained queued for %s", bucket_id)
        except Exception as exc:
            logger.warning(f"meaning embedding failed for {bucket_id}: {exc}")

    async def _index_after_write(self, bucket_id: str, content: str) -> None:
        """Queue derived indexing after Markdown is safely on disk.

        The server runtime starts a durable outbox worker, so this returns
        without waiting for network I/O.  Standalone/stdio users still get one
        inline attempt; failures remain queued for a later managed startup.
        """
        outbox = self.embedding_outbox

        # A preceding writer may already have converged this exact Markdown
        # version while the current request waited for the derived lease.
        # Avoid a second paid/provider call; acknowledge only the matching
        # content component, leaving a sibling meaning task untouched.
        engine = self.embedding_engine
        hash_reader = getattr(engine, "get_content_hash", None)
        if callable(hash_reader) and content:
            try:
                stored_hash = str(hash_reader(bucket_id) or "")
            except Exception:
                stored_hash = ""
            desired_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if stored_hash and stored_hash == desired_hash:
                if outbox is not None:
                    complete_content = getattr(
                        outbox, "complete_content", None
                    )
                    if callable(complete_content):
                        complete_content(bucket_id, content)
                return

        queued = False
        if outbox is not None:
            try:
                queued = bool(outbox.enqueue(bucket_id, content))
            except Exception as exc:
                logger.error(
                    "Failed to persist embedding outbox item for %s: %s",
                    bucket_id,
                    exc,
                )
            if queued and getattr(outbox, "running", False):
                return

        try:
            indexed = await self._sync_embedding(bucket_id, content)
        except Exception as exc:
            indexed = False
            logger.warning(
                "Inline embedding attempt failed; memory remains queued / "
                "同步向量尝试失败，记忆已保留待后台重试: %s: %s",
                bucket_id,
                exc,
            )
        if indexed and outbox is not None:
            try:
                complete_content = getattr(outbox, "complete_content", None)
                if callable(complete_content):
                    complete_content(bucket_id, content)
                else:
                    outbox.discard(bucket_id)
            except Exception:
                logger.warning("Failed to acknowledge embedding outbox item: %s", bucket_id)
        elif not indexed:
            logger.warning(
                "Memory saved without vector; pending retry / 记忆已落盘，向量待重试: %s",
                bucket_id,
            )

    async def _index_after_update(
        self,
        bucket_id: str,
        *,
        content_changed: bool = False,
        meaning_changed: bool = False,
    ) -> None:
        """释放桶写租约后刷新派生索引。

        Markdown 是真源。原子提交后重新读取，并串行化同桶的派生写入，避免旧请求
        晚返回后覆盖新向量；外部 provider 的延迟不得延长保护文件修改的桶租约。
        """
        if not content_changed and not meaning_changed:
            return

        # 托管服务已在提交后持久入队，由 worker 统一取得派生租约并调用
        # provider。写请求不能再取得同一租约，否则仍可能排在慢 worker 后等待。
        outbox = self.embedding_outbox
        if outbox is not None and getattr(outbox, "running", False):
            return

        await self._index_after_update_inner(
            bucket_id,
            content_changed=content_changed,
            meaning_changed=meaning_changed,
        )

    async def _index_after_update_inner(
        self,
        bucket_id: str,
        *,
        content_changed: bool,
        meaning_changed: bool,
    ) -> None:
        """在独立派生租约内把向量收敛到最新 Markdown 状态。"""
        try:
            # 这里故意使用与 ``_bucket_turn`` 不同的租约：慢 provider 运行时
            # Markdown 写入仍可继续，但同桶派生写入在跨进程场景下保持有序。
            async with self._derived_index_turn(bucket_id):
                for _reconcile_pass in range(3):
                    bucket = await self.get(bucket_id)
                    if not bucket:
                        return
                    metadata = bucket.get("metadata") or {}
                    if not isinstance(metadata, dict):
                        metadata = {}
                    if (
                        str(metadata.get("type") or "").strip().lower()
                        == "archived"
                        or metadata.get("deleted_at")
                        or parse_bool(metadata.get("tombstone"), default=False)
                    ):
                        return

                    indexed_content = str(bucket.get("content") or "")
                    indexed_meaning = self._normalize_meaning_list(
                        metadata.get("meaning") or []
                    )
                    if content_changed:
                        await self._index_after_write(bucket_id, indexed_content)
                    if meaning_changed:
                        await self._sync_meaning_embedding(
                            bucket_id,
                            indexed_meaning,
                        )

                    # 写入在独立派生租约期间仍可继续。provider 返回后再次读取；
                    # 若正文/meaning 已变化，就在释放派生租约前按最新值再生成一次。
                    latest = await self.get(bucket_id)
                    latest_metadata = (latest or {}).get("metadata") or {}
                    if not isinstance(latest_metadata, dict):
                        latest_metadata = {}
                    became_deleted = not latest or (
                        latest_metadata.get("deleted_at")
                        or parse_bool(
                            latest_metadata.get("tombstone"), default=False
                        )
                    )
                    if became_deleted:
                        if self.embedding_outbox is not None:
                            try:
                                self.embedding_outbox.discard(bucket_id)
                            except Exception:
                                pass
                        if self.embedding_engine is not None:
                            try:
                                self.embedding_engine.delete_embedding(bucket_id)
                            except Exception as cleanup_exc:
                                logger.warning(
                                    "Late derived-index cleanup failed / "
                                    "迟到派生索引清理失败: %s: %s",
                                    bucket_id,
                                    cleanup_exc,
                                )
                        return

                    latest_content = str((latest or {}).get("content") or "")
                    latest_meaning = self._normalize_meaning_list(
                        latest_metadata.get("meaning") or []
                    )
                    content_stable = (
                        not content_changed or latest_content == indexed_content
                    )
                    meaning_stable = (
                        not meaning_changed or latest_meaning == indexed_meaning
                    )
                    if content_stable and meaning_stable:
                        return
                logger.warning(
                    "Derived index changed repeatedly / 派生索引连续变化，"
                    "已保留 outbox 最新期望状态: %s",
                    bucket_id,
                )
        except Exception as exc:
            # Markdown 已提交。派生索引失败独立记录并重试/对账，不回滚或误报正文写入。
            logger.warning(
                "Post-update indexing failed for %s: %s: %s",
                bucket_id,
                type(exc).__name__,
                exc,
            )

    def _invalidate_bm25(self) -> None:
        """写操作后调用：标记 BM25 需重建 + 清活跃桶缓存（集合已变，缓存作废）。

        名字沿用历史（各写路径已在调它），实际是「集合变更」的统一失效钩子。
        """
        with self._active_cache_state_guard:
            self._active_cache_generation += 1
            self._bm25_dirty = True
            self._active_cache = None
            self._active_file_state = {}
            self._last_file_state_check = 0.0
        with self._bucket_path_index_guard:
            self._bucket_path_index_ready = False
            self._bucket_path_index = {}

    def _cache_bump(
        self,
        bucket_id: str,
        *,
        last_active=None,
        activation_count=None,
        file_path: str = "",
    ) -> None:
        """touch/ripple 只改了某桶的激活字段（集合没变）→ 就地更新缓存，不清整表。"""
        with self._active_cache_state_guard:
            # Even with no published cache, a concurrent builder may have
            # parsed the pre-touch file.  Bumping the generation makes its
            # eventual publish fail the CAS and forces a rescan.
            self._active_cache_generation += 1
            if self._active_cache is None:
                return
            for b in self._active_cache:
                if b.get("id") == bucket_id:
                    m = b.get("metadata")
                    if isinstance(m, dict):
                        if last_active is not None:
                            m["last_active"] = last_active
                        if activation_count is not None:
                            m["activation_count"] = activation_count
                    break
            if file_path:
                self._refresh_cached_file_state(file_path)

    def _refresh_cached_file_state(self, file_path: str) -> None:
        """Acknowledge an internal in-place write without invalidating the cache."""
        with self._active_cache_state_guard:
            if self._active_cache is None:
                return
            normalized = os.path.normcase(os.path.abspath(file_path))
            try:
                stat = os.stat(file_path)
                self._active_file_state[normalized] = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                self._active_file_state.pop(normalized, None)
            self._last_file_state_check = time.monotonic()

    def _scan_active_file_state(self) -> dict[str, tuple[int, int]]:
        """Return a cheap metadata fingerprint for every active Markdown file."""
        state: dict[str, tuple[int, int]] = {}
        for _root, _fname, file_path in self._iter_md_files(self._active_dirs):
            try:
                stat = os.stat(file_path)
            except OSError:
                continue
            state[os.path.normcase(os.path.abspath(file_path))] = (
                stat.st_mtime_ns,
                stat.st_size,
            )
        return state

    def external_change_status(self) -> dict[str, Any]:
        with self._active_cache_state_guard:
            return {
                "poll_seconds": self.external_change_poll_seconds,
                "detected": self._external_changes_detected,
                "last_detected": self._last_external_change,
                "cached_files": len(self._active_file_state),
            }

    def _reconcile_external_changes(
        self,
        previous: list[dict],
        current: list[dict],
    ) -> None:
        """Propagate externally-created/edited/deleted Markdown to derived state."""
        old_by_id = {str(bucket.get("id") or ""): bucket for bucket in previous}
        new_by_id = {str(bucket.get("id") or ""): bucket for bucket in current}
        old_by_id.pop("", None)
        new_by_id.pop("", None)

        added_ids = set(new_by_id) - set(old_by_id)
        removed_ids = set(old_by_id) - set(new_by_id)
        content_changed_ids = {
            bucket_id
            for bucket_id in set(old_by_id) & set(new_by_id)
            if str(old_by_id[bucket_id].get("content") or "")
            != str(new_by_id[bucket_id].get("content") or "")
        }
        meaning_changed_ids = {
            bucket_id
            for bucket_id in set(old_by_id) & set(new_by_id)
            if self._normalize_meaning_list(
                (old_by_id[bucket_id].get("metadata") or {}).get("meaning") or []
            )
            != self._normalize_meaning_list(
                (new_by_id[bucket_id].get("metadata") or {}).get("meaning") or []
            )
        }
        updated_ids = {
            bucket_id
            for bucket_id in set(old_by_id) & set(new_by_id)
            if bucket_id in content_changed_ids
            or (old_by_id[bucket_id].get("metadata") or {})
            != (new_by_id[bucket_id].get("metadata") or {})
        }

        outbox = self.embedding_outbox
        if outbox is not None:
            for bucket_id in sorted(added_ids | content_changed_ids):
                try:
                    outbox.enqueue(
                        bucket_id,
                        str(new_by_id[bucket_id].get("content") or ""),
                    )
                except Exception as exc:
                    logger.warning(
                        "external edit embedding enqueue failed for %s: %s",
                        bucket_id,
                        exc,
                    )
            enqueue_meaning = getattr(outbox, "enqueue_meaning", None)
            if callable(enqueue_meaning):
                for bucket_id in sorted(added_ids | meaning_changed_ids):
                    try:
                        meaning = self._normalize_meaning_list(
                            (new_by_id[bucket_id].get("metadata") or {}).get(
                                "meaning"
                            )
                            or []
                        )
                        enqueue_meaning(
                            bucket_id,
                            meaning[-1] if meaning else "",
                        )
                    except Exception as exc:
                        logger.warning(
                            "external meaning enqueue failed for %s: %s",
                            bucket_id,
                            exc,
                        )
        for bucket_id in sorted(removed_ids):
            # Moving a file to archive is not physical deletion; keep its
            # derived vector. Only remove the index when the ID vanished from
            # every managed directory.
            if self._find_bucket_file(bucket_id) is not None:
                continue
            if outbox is not None:
                try:
                    outbox.discard(bucket_id)
                except Exception:
                    pass
            if self.embedding_engine is not None:
                try:
                    self.embedding_engine.delete_embedding(bucket_id)
                except Exception as exc:
                    logger.warning(
                        "external delete embedding cleanup failed for %s: %s",
                        bucket_id,
                        exc,
                    )

        for bucket_id in sorted(added_ids):
            bucket = new_by_id[bucket_id]
            self._record_v3_bucket_event(
                "external_create",
                bucket_id,
                str((bucket.get("metadata") or {}).get("type") or "dynamic"),
                str(bucket.get("content") or ""),
                dict(bucket.get("metadata") or {}),
            )
        for bucket_id in sorted(updated_ids):
            bucket = new_by_id[bucket_id]
            self._record_v3_bucket_event(
                "external_update",
                bucket_id,
                str((bucket.get("metadata") or {}).get("type") or "dynamic"),
                str(bucket.get("content") or ""),
                dict(bucket.get("metadata") or {}),
            )
        for bucket_id in sorted(removed_ids):
            bucket = old_by_id[bucket_id]
            self._record_v3_bucket_event(
                "external_delete",
                bucket_id,
                str((bucket.get("metadata") or {}).get("type") or "dynamic"),
                str(bucket.get("content") or ""),
                dict(bucket.get("metadata") or {}),
            )

        logger.info(
            "External vault change reconciled / 外部记忆文件变更已对账: "
            "added=%s changed=%s removed=%s",
            len(added_ids),
            len(updated_ids),
            len(removed_ids),
        )

    def _build_bm25_index(self, buckets: list):
        """在线程里构建一个**全新**的 BM25 索引并返回（性能 P4：jieba 全库分词很慢）。"""
        idx = _BM25Index()  # type: ignore[operator]
        idx.build(buckets)
        return idx

    async def _rebuild_bm25_async(self, buckets: list) -> None:
        """后台重建 BM25：to_thread 里建新索引，建好原子换入 self._bm25，不阻塞事件循环。"""
        try:
            fresh = await asyncio.to_thread(self._build_bm25_index, buckets)
            self._bm25 = fresh          # 原子替换（单次赋值）
            self._bm25_dirty = False
        except Exception as e:
            logger.warning(f"[bm25] 后台重建失败，保留旧索引: {e}")
        finally:
            self._bm25_rebuilding = False

    # ---------------------------------------------------------
    # Create a new bucket
    # 创建新桶
    # Write content and metadata into a .md file
    # 将内容和元数据写入一个 .md 文件
    # ---------------------------------------------------------
    async def create(
        self,
        content: str,
        tags: Optional[list[str]] = None,
        importance: int = 5,
        domain: Optional[list[str]] = None,
        valence: float = 0.5,
        arousal: float = 0.3,
        bucket_type: str = "dynamic",
        name: Optional[str] = None,
        title: str = "",
        pinned: bool = False,
        protected: bool = False,
        why_remembered: str = "",
        triggered_by: str = "",
        weight: Optional[float] = None,
        source_tool: str = "",
        grow_batch_id: str = "",
        bucket_id_override: str = "",
        allow_embedding_fallback: bool = False,
        meaning: str = "",
        media: Any = None,
        test_data: bool = False,
        defer_derived_index: bool = False,
        imported: bool = False,
        source_refs: Any = None,
        quotes: Any = None,
        event_actor: str = "system",
        lock_type: str = "",
        unlock_date: str | None = None,
        locked_by: str = "",
        writer_name: str = "",
    ) -> str:
        """
        Create a new memory bucket, return bucket ID.
        创建一个新的记忆桶，返回桶 ID。

        pinned/protected=True: bucket won't be merged, decayed, or have importance changed.
        Importance is locked to 10 for pinned/protected buckets.
        pinned/protected 桶不参与合并与衰减，importance 强制锁定为 10。

        iter 2.0 来源追踪：
        - source_tool: "hold" | "grow" | "import" — 记录创建来源。feel 走 hold
          分支，所以 feel 桶 source_tool="hold"，依靠 bucket_type 区分。
        - grow_batch_id: 同一次 grow 调用拆出的所有桶共享同一个 batch_id，
          dashboard 可按 batch 聚合显示。
        - bucket_id_override: 调用方提供的可读 id（如 feel 的
          ``feel_202605011423_V085``）。如果与已有桶冲突，自动追加秒级后缀。
          为空 → 走默认 ``generate_bucket_id()``（12 位 hex）。
        - imported=True: 对话导入桶的持久化来源标记；创建时间与最后活跃时间
          均使用本次导入时刻。
        """
        # 保留 ``allow_embedding_fallback`` 以兼容旧调用；所有记忆类型均先写入，
        # embedding 只是可重建的派生索引。
        pinned = parse_bool(pinned, default=False)
        protected = parse_bool(protected, default=False)
        if pinned and protected:
            raise ValueError("pinned 与 protected 不能同时为 True")

        # F-04: 清洗 content / tags / name 中的危险控制字符和双向覆写符
        content = self._sanitize_text(content)
        self._validate_bucket_content(content)
        if name:
            name = self._sanitize_text(name)
        title = normalize_memory_title(self._sanitize_text(title))
        if source_refs:
            from ombrebrain.storage.source_store import normalize_source_refs

            source_refs = normalize_source_refs(source_refs)

        # Candidate selection is finalized immediately before the no-overwrite
        # write while holding that exact ID's normal bucket turn.  The value
        # here is provisional so metadata normalization can include a useful
        # source label without doing an unsafe check-then-write.
        preferred_bucket_id = (
            sanitize_name(bucket_id_override) or generate_bucket_id()
            if bucket_id_override
            else generate_bucket_id()
        )
        bucket_id = preferred_bucket_id
        # 桶名 = "YYYY-MM-DD HH-MM-SS [LLM生成的标题]"，无标题时仅用时间戳。
        # 使用连字符替代冒号，避免 sanitize_name 后续编辑时把冒号去掉破坏可读性。
        _ts = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
        _clean = sanitize_name(title or name) if (title or name) else ""
        bucket_name = (f"{_ts} {_clean}" if (_clean and _clean != "unnamed") else _ts)[:80]
        # feel buckets are allowed to have empty domain; others default to ["未分类"]
        if bucket_type == "feel":
            domain = domain if domain is not None else []
        else:
            domain = domain or [_DEFAULT_DOMAIN_NAME]
        domain = self._normalize_metadata_list(
            domain,
            max_items=_MAX_DOMAINS,
            max_chars=_MAX_DOMAIN_CHARS,
        )
        if bucket_type != "feel" and not domain:
            domain = [_DEFAULT_DOMAIN_NAME]
        tags = self._normalize_metadata_list(
            tags,
            max_items=_MAX_TAGS,
            max_chars=_MAX_TAG_CHARS,
        )
        linked_content = content  # wikilink injection disabled; LLM adds [[]] via prompt

        # --- Pinned/protected buckets: lock importance to 10 ---
        # --- 钉选/保护桶：importance 强制锁定为 10 ---
        if pinned or protected:
            importance = _PINNED_IMPORTANCE

        # --- Build YAML frontmatter metadata / 构建元数据 ---
        # 越界不静默 clamp：会产生 OB-W001/OB-W002 提示走到 MCP 返回末尾
        created_at = now_iso()
        metadata = {
            "id": bucket_id,
            "name": bucket_name,
            "tags": tags,
            "domain": domain,
            "valence": _clamp_unit(valence, "valence", f"create:{bucket_id}"),
            "arousal": _clamp_unit(arousal, "arousal", f"create:{bucket_id}"),
            "importance": _clamp_importance(importance, f"create:{bucket_id}"),
            "type": bucket_type,
            "created": created_at,
            "last_active": created_at,
            "activation_count": 0,
        }
        if title:
            metadata["title"] = title
        # Letter access metadata is written atomically with the original
        # content.  A create-then-update window could briefly expose a locked
        # body to a concurrent reader or vector search.
        if bucket_type == "letter" and locked_by:
            metadata["lock_type"] = str(lock_type or "none")[:16]
            if unlock_date:
                metadata["unlock_date"] = str(unlock_date)[:64]
            metadata["locked_by"] = str(locked_by)[:16]
            if writer_name:
                metadata["writer_name"] = self._sanitize_text(
                    str(writer_name)
                ).strip()[:120]
        if source_refs:
            from ombrebrain.storage.source_store import source_links_from_metadata, active_source_refs_from_links

            metadata["source_links"] = source_links_from_metadata({"source_refs": source_refs})
            metadata["source_refs"] = active_source_refs_from_links(metadata["source_links"])
        # 引语：当时说出口、并且当时就知道它重要的那几句话，原样存下。
        # 落在 metadata 而不是正文，是为了让渲染路径**结构上就拿不到**——
        # render_stored_bucket / dream / catalog 都是白名单渲染，不会碰这个字段。
        # "平时不返回"不能靠"记得别渲染"。
        if quotes:
            metadata["quotes"] = self._sanitize_quotes(quotes)
        if imported:
            metadata["imported"] = True
        if test_data:
            metadata["provenance"] = {
                "kind": "test",
                "created_by": str(source_tool or "developer")[:_SOURCE_TOOL_MAX],
                "erasable": True,
            }
        if pinned:
            metadata["pinned"] = True
        if protected:
            metadata["protected"] = True
        if bucket_type == "permanent" or pinned:
            metadata["type"] = "permanent"

        # --- iter 2.0: 来源工具与 grow 批次 ---
        # 今后所有记忆必须声明来源。旧调用方未传时统一标为 direct，避免
        # footprint 只说“创建”却无法让模型判断这条记忆从哪里来。
        # grow_batch_id 仅 grow 路径会传，hold/feel 不会有这个字段。
        declared_source = str(source_tool or "direct").strip()[:_SOURCE_TOOL_MAX]
        metadata["source_tool"] = declared_source or "direct"
        if grow_batch_id:
            metadata["grow_batch_id"] = str(grow_batch_id).strip()[:_GROW_BATCH_ID_MAX]

        # --- iter 1.8: 让记忆带「为什么记得」 / why this is worth remembering ---
        # 自由文本字段。模型 / 人类手写。不参与评分，只参与展示与搜索。
        # Empty string = 没说原因，dashboard 直接不渲染该行。
        if why_remembered:
            metadata["why_remembered"] = str(why_remembered).strip()[:_WHY_REMEMBERED_MAX]
        # --- iter 1.8: feel 桶的因果链出口（暂不强校验存在性，只透传） ---
        # triggered_by = 触发这条 feel 的源 bucket_id。1.9 会做 UI 联动。
        if triggered_by:
            metadata["triggered_by"] = str(triggered_by).strip()[:_TRIGGERED_BY_MAX]
        # --- Miss: meaning / media —— 我自己觉得这条记忆为什么值得被想起 ---
        # meaning 是 list[str]：新建时只有一条（这次 hold 传入的那句）；后续
        # 每次 hold/trace 追加都会往这个列表里继续加。media 是不透明的外部引用列表。
        # 两者都可选，互不依赖，也不参与打分。
        meaning_item = self._normalize_meaning_item(meaning)
        if meaning_item:
            metadata["meaning"] = [meaning_item]
        # --- iter 1.8: plan 的「承诺重量」0.0-1.0，与 importance 不同 ---
        # importance = 这件事多重要；weight = 这件事压在我心头多重。
        if bucket_type == "plan" and weight is not None:
            metadata["weight"] = _clamp01(weight, _DEFAULT_VALENCE)
        # --- iter 1.9 G: bucket_type_defaults / 类型默认值 ---
        # config.bucket_type_defaults 里可以写 {letter: {weight: 1.0, dont_surface: false}, ...}
        # 仅在调用方未显式传该字段时套用。letter 默认 weight=1.0 体现「信件天然有重量」。
        # 老配置没这段时静默跳过。
        try:
            type_defaults = (self.config.get("bucket_type_defaults") or {}).get(bucket_type, {})
            if type_defaults:
                if "weight" in type_defaults and "weight" not in metadata and weight is None:
                    metadata["weight"] = _clamp01(type_defaults["weight"], _DEFAULT_VALENCE)
                if "dont_surface" in type_defaults and "dont_surface" not in metadata:
                    if parse_bool(type_defaults["dont_surface"], default=False):
                        metadata["dont_surface"] = True
                if "why_remembered" in type_defaults and not why_remembered:
                    metadata["why_remembered"] = str(type_defaults["why_remembered"]).strip()[:_WHY_REMEMBERED_MAX]
        except Exception as e:
            logger.warning(f"bucket_type_defaults apply failed / 类型默认值应用失败: {e}")
        # --- iter 1.8: 主动遗忘开关，默认 False。新桶不写 frontmatter 节省空间 ---
        # 通过 update(dont_surface=True) 后才会出现在 frontmatter 里。
        # --- iter 1.8: first_of_kind 自动判定 ---
        # 规则：当前桶的 tags 与全库已有 tags 完全无交集 → 这是一个「第一次」
        # 仅对带 tag 的桶判定。空 tag 桶不标。
        if tags:
            try:
                existing_tags = self._collect_all_tags()
                if existing_tags is not None and not (set(tags) & existing_tags):
                    metadata["first_of_kind"] = True
            except Exception as e:
                # 失败不阻塞写入主流程
                logger.warning(f"first_of_kind check failed / 首次标记检测失败: {e}")

        # --- Choose directory by type + primary domain ---
        # --- 按类型 + 主题域选择存储目录 ---
        if bucket_type == "permanent" or pinned:
            type_dir = self.permanent_dir
        elif bucket_type == "feel":
            type_dir = self.feel_dir
        elif bucket_type == "plan":
            type_dir = self.plan_dir
        elif bucket_type == "letter":
            type_dir = self.letter_dir
        else:
            type_dir = self.dynamic_dir
        if bucket_type == "feel":
            primary_domain = "沉淀物"  # feel subfolder name
        elif bucket_type == "plan":
            primary_domain = "active"  # plans/active/ by default; trace can move via status update
        elif bucket_type == "letter":
            primary_domain = "history"
        else:
            primary_domain = self._primary_domain(domain)
        target_dir = os.path.join(type_dir, primary_domain)
        os.makedirs(target_dir, exist_ok=True)

        def _candidate_ids():
            yield preferred_bucket_id
            if bucket_id_override:
                yield f"{preferred_bucket_id}_{datetime.now().strftime('%S')}"
                for _attempt in range(5):
                    yield f"{preferred_bucket_id}_{uuid.uuid4().hex[:2]}"
            while True:
                yield generate_bucket_id()

        # Finalize the ID, persist any ID-keyed media and publish the Markdown
        # while holding the same turn used by update/migrate/delete.  The
        # second existence check closes the former create-vs-migrate TOCTOU.
        collision_count = 0
        for candidate_id in _candidate_ids():
            async with self._bucket_turn(candidate_id):
                if self._find_bucket_file(candidate_id):
                    collision_count += 1
                    continue

                if bucket_name and bucket_name != candidate_id:
                    filename = f"{bucket_name}_{candidate_id}.md"
                else:
                    filename = f"{candidate_id}.md"
                candidate_path = safe_path(target_dir, filename)
                if os.path.exists(candidate_path):
                    collision_count += 1
                    continue

                bucket_id = candidate_id
                metadata["id"] = bucket_id
                metadata.pop("media", None)
                persisted_media = await self.media_store.persist(bucket_id, media)
                normalized_media = self._normalize_media(persisted_media)
                if normalized_media:
                    metadata["media"] = normalized_media

                post = frontmatter.Post(  # type: ignore[arg-type]
                    linked_content,
                    **metadata,
                )
                try:
                    # Publish the file and its ID lookup entry under the same
                    # path-index guard.  The collision check above can leave a
                    # complete, ready index that does not yet contain this new
                    # ID; without this hand-off an outbox worker can observe
                    # the committed Markdown as "missing" and discard its
                    # embedding task while create() awaits meaning indexing.
                    with self._bucket_path_index_guard:
                        _atomic_create_text(
                            candidate_path, frontmatter.dumps(post)
                        )
                        self._bucket_path_index[bucket_id] = candidate_path
                except FileExistsError:
                    # An unmanaged writer may not honor the bucket turn.  The
                    # no-overwrite publish still protects its file; choose a
                    # fresh ID instead of replacing it.
                    collision_count += 1
                    continue
                except OSError as e:
                    logger.error(
                        "Failed to write bucket file / 写入桶文件失败: "
                        "%s: %s",
                        candidate_path,
                        e,
                    )
                    raise
                break

        # 不在 bucket lease 内等待全局 outbox 文件事务。async context 退出后到
        # 这里没有任何 await，因而先持久登记派生期望状态，再允许取消点出现。
        self._queue_derived_state(
            bucket_id,
            content=linked_content,
            meaning=metadata.get("meaning") or [],
            queue_content=True,
            queue_meaning=bool(metadata.get("meaning")),
        )

        if collision_count > 6:
            logger.warning(
                "bucket_id_override %r repeatedly conflicted; used random id %s",
                preferred_bucket_id,
                bucket_id,
            )

        # Markdown becomes the visible source of truth before any network or
        # derived-index await.  This also changes the path index from the
        # precise hand-off above to a normal lazy rebuild for later lookups.
        self._invalidate_bm25()

        logger.info(
            f"Created bucket / 创建记忆桶: {bucket_id} ({bucket_name}) → {primary_domain}/"
            + (" [PINNED]" if pinned else "") + (" [PROTECTED]" if protected else "")
        )

        # Markdown 先提交，再处理派生索引。托管服务入队后立即返回，独立模式尝试一次。
        # meaning 保持独立向量，避免被可能很长的 content 正文稀释。
        if not defer_derived_index:
            await self._index_after_update(
                bucket_id,
                content_changed=True,
                meaning_changed=bool(metadata.get("meaning")),
            )
        self._record_v3_bucket_event(
            "create",
            bucket_id,
            str(metadata.get("type") or bucket_type),
            linked_content,
            metadata,
        )
        self._record_ledger_event(
            "TraceCreated",
            bucket_id,
            str(metadata.get("type") or bucket_type),
            linked_content,
            metadata,
            {"event_actor": str(event_actor or "system").strip().lower()},
        )
        return bucket_id

    # ---------------------------------------------------------
    # Read bucket content
    # 读取桶内容
    # Returns {"id", "metadata", "content", "path"} or None
    # ---------------------------------------------------------
    async def get(self, bucket_id: str) -> Optional[dict]:
        """
        Read a single bucket by ID.
        根据 ID 读取单个桶。
        F-10: 软删除的桶（带 deleted_at）对常规调用者透明，返回 None。
        """
        if not bucket_id or not isinstance(bucket_id, str):
            return None
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return None
        data = self._load_bucket(file_path)
        # F-10: 软删除的桶不应通过 get() 可见
        if data and data.get("metadata", {}).get("deleted_at"):
            return None
        return data

    async def get_including_archive(self, bucket_id: str) -> Optional[dict]:
        """Read one bucket by ID without hiding its archived/tombstoned state."""
        if not bucket_id or not isinstance(bucket_id, str):
            return None
        file_path = self._find_bucket_file(bucket_id)
        return self._load_bucket(file_path) if file_path else None

    def find_exact_content(
        self,
        content: str,
        domain_filter: Optional[list[str]] = None,
    ) -> Optional[dict]:
        """Read Markdown directly for an exact match, bypassing derived caches."""
        expected = self._sanitize_text(content)
        filter_set = {
            str(domain).strip().lower()
            for domain in (domain_filter or [])
            if str(domain).strip()
        }
        for _root, _fname, file_path in self._iter_md_files(self._active_dirs):
            bucket = self._load_bucket(file_path)
            if not bucket or bucket.get("content") != expected:
                continue
            metadata = bucket.get("metadata", {})
            if metadata.get("deleted_at"):
                continue
            domains = metadata.get("domain") or []
            if isinstance(domains, str):
                domains = [domains]
            if filter_set and not {
                str(domain).strip().lower() for domain in domains
            } & filter_set:
                continue
            return bucket
        return None

    # ---------------------------------------------------------
    # Move bucket between directories
    # 在目录间移动桶文件
    # ---------------------------------------------------------
    def _move_bucket(self, file_path: str, target_type_dir: str, domain: Optional[list[str]] = None) -> str:
        """
        Move a bucket file to a new type directory, preserving domain subfolder.
        Returns new file path.
        """
        primary_domain = self._primary_domain(domain)
        target_dir = os.path.join(target_type_dir, primary_domain)
        os.makedirs(target_dir, exist_ok=True)
        filename = os.path.basename(file_path)
        new_path = safe_path(target_dir, filename)
        if os.path.normpath(file_path) != os.path.normpath(new_path):
            os.rename(file_path, new_path)
            logger.info(f"Moved bucket / 移动记忆桶: {filename} → {target_dir}/")
        return str(new_path)

    def _bucket_target_path(
        self,
        file_path: str,
        bucket_type: str,
        domain: Optional[list[str]] = None,
        status: str = "",
    ) -> str:
        """Return the canonical storage path for an editable bucket type.

        The frontmatter ``type`` is the logical source of truth, but the vault
        layout is part of the public Obsidian contract too.  Keeping this
        mapping in one place prevents dashboard edits from changing metadata
        without moving the Markdown file.
        """
        normalized_type = str(bucket_type or "dynamic").strip().lower()
        if normalized_type not in _EDITABLE_BUCKET_TYPES:
            raise ValueError(f"unsupported editable bucket type: {normalized_type}")

        if normalized_type == "permanent":
            type_dir = self.permanent_dir
            subdir = self._primary_domain(domain)
        elif normalized_type == "feel":
            type_dir = self.feel_dir
            subdir = "沉淀物"
        elif normalized_type == "plan":
            type_dir = self.plan_dir
            normalized_status = str(status or "active").strip().lower()
            subdir = normalized_status if normalized_status in _PLAN_STATUSES else "active"
        elif normalized_type == "letter":
            type_dir = self.letter_dir
            subdir = "history"
        else:
            # ``i`` / ``self`` are private logical channels, not separate
            # physical stores.  They intentionally live under dynamic/<domain>.
            type_dir = self.dynamic_dir
            subdir = self._primary_domain(domain)

        target_dir = os.path.join(type_dir, subdir)
        os.makedirs(target_dir, exist_ok=True)
        return str(safe_path(target_dir, os.path.basename(file_path)))

    # 这两个被 tools / web 依赖。私有名被跨模块调用是自相矛盾的信号：
    # 要么不该被外部用，要么就该是公开接口。这里选后者，旧名保留不动，
    # 免得动到本文件内几十处调用点。
    def archived_letter_rejection(self, metadata: dict):
        return self._archived_letter_rejection(metadata)

    def invalidate_bm25(self) -> None:
        self._invalidate_bm25()

    _same_path = staticmethod(_bp.same_path)

    def _commit_bucket_update(
        self,
        file_path: str,
        target_path: str,
        serialized: str,
    ) -> str:
        """Commit an update without leaving type/path split-brain on failure.

        In-place edits use the normal atomic writer.  A directory migration is
        copy-on-commit: atomically write the complete new file at its canonical
        destination, then remove the untouched source.  If source removal
        fails, delete the new copy and keep the original as the sole truth.
        Existing destination files are never overwritten.
        """
        if self._same_path(file_path, target_path):
            _atomic_write_text(file_path, serialized)
            return file_path

        if os.path.exists(target_path):
            raise FileExistsError(
                f"bucket migration target already exists: {target_path}"
            )

        _atomic_write_text(target_path, serialized)
        try:
            os.remove(file_path)
        except Exception:
            try:
                os.remove(target_path)
            except OSError as rollback_error:
                logger.critical(
                    "Failed to roll back bucket migration target %s: %s",
                    target_path,
                    rollback_error,
                )
            raise

        logger.info(
            "Moved bucket / 移动记忆桶: %s → %s/",
            os.path.basename(file_path),
            os.path.dirname(target_path),
        )
        return target_path
    def _bucket_turn(self, bucket_id: str):
        """Serialize archive()/update()/delete()/touch() on the same bucket_id.
        Uses the same cross-loop/cross-process lock-file mechanism as
        ``tools/_common.py``'s ``_quota_turn`` rather than an ``asyncio.Lock``
        — FastMCP may dispatch requests from different event loops/threads,
        so an in-process lock would not actually serialize them.
        """
        return _filesystem_turn(str(self.base_dir), f"bucket-{bucket_id}")

    def _derived_index_turn(self, bucket_id: str):
        """不占用桶修改租约，单独保证同桶派生写入顺序。"""
        return _filesystem_turn(
            str(self.base_dir),
            f"derived-index-{bucket_id}",
            timeout_seconds=300.0,
        )

    def human_name_change_turn(self):
        """Serialize config + vault human-name migrations as one transaction.

        Per-bucket turns protect each Markdown write, but they cannot by
        themselves stop two full-vault rename jobs from interleaving.  Routes
        that change or synchronize the human display name hold this outer
        process/cross-process lease from the config read through the last
        bucket replacement.
        """

        return _filesystem_turn(
            str(self.base_dir),
            "settings-human-name",
            timeout_seconds=300.0,
        )

    async def replace_text_fields(self, old: str, new: str) -> dict[str, int]:
        """Replace a display term through managed per-bucket transactions.

        This is used when the configured human name changes.  It deliberately
        does not bump ``last_active``: a display-name migration is not a memory
        activation.  Each bucket is re-read while holding the normal
        cross-process bucket lock and then committed through ``_update_locked``
        so atomic writes, ledger/projection events and concurrent edits retain
        the same guarantees as every other mutation. 派生索引只在桶租约释放后刷新。
        """

        if not old or not new or old == new:
            return {"buckets_changed": 0, "replacements": 0}

        pattern = re.compile(re.escape(old))
        bucket_ids: list[str] = []
        seen: set[str] = set()
        directories = list(self._active_dirs) + [self.archive_dir]
        for _root, _filename, file_path in self._iter_md_files(directories):
            bucket = self._load_bucket(file_path)
            bucket_id = str((bucket or {}).get("id") or "").strip()
            if bucket_id and bucket_id not in seen:
                seen.add(bucket_id)
                bucket_ids.append(bucket_id)

        changed = 0
        total = 0
        for bucket_id in bucket_ids:
            committed = False
            content_changed = False
            replacements = 0
            derived_state: dict[str, Any] = {}
            async with self._bucket_turn(bucket_id):
                file_path = self._find_bucket_file(bucket_id)
                if not file_path:
                    continue
                try:
                    post = frontmatter.load(file_path)
                except Exception as exc:
                    logger.warning(
                        "Failed to load bucket for text replacement %s: %s",
                        bucket_id,
                        exc,
                    )
                    continue

                replacements = 0
                updates: dict[str, str] = {}
                # A callable replacement is literal.  Passing ``new`` directly
                # would interpret user-controlled ``\\1``/``\\g<name>`` syntax
                # as regular-expression group references.
                content, count = pattern.subn(lambda _match: new, post.content or "")
                if count:
                    updates["content"] = content
                    replacements += count
                for field in ("name", "why_remembered", "user_name"):
                    value = post.get(field)
                    if not isinstance(value, str) or not value:
                        continue
                    replaced, count = pattern.subn(lambda _match: new, value)
                    if count:
                        updates[field] = replaced
                        replacements += count

                if not updates:
                    continue
                try:
                    committed = await self._update_locked(
                        bucket_id,
                        _derived_state_out=derived_state,
                        **updates,
                    )
                except (OSError, ValueError) as exc:
                    logger.warning(
                        "Text replacement rejected for bucket %s: %s",
                        bucket_id,
                        exc,
                    )
                    continue
                content_changed = "content" in updates
            if committed:
                self._queue_captured_derived_state(derived_state)
                await self._index_after_update(
                    bucket_id,
                    content_changed=content_changed,
                )
                changed += 1
                total += replacements

        return {"buckets_changed": changed, "replacements": total}

    async def update_content_fragment(
        self,
        bucket_id: str,
        *,
        old_str: str,
        new_str: str,
        append_plan_history: bool = False,
        event_actor: str = "system",
        expected_lock_state: Optional[tuple[str, str, str]] = None,
        **kwargs,
    ) -> dict[str, Any]:
        """Atomically replace one unique literal fragment in a bucket body.

        The match and the write deliberately happen under the same per-bucket
        cross-process lock.  Computing the replacement from an earlier
        ``get()`` result would let a concurrent trace/update be overwritten by
        a stale full-body snapshot.

        ``new_str`` may be empty (delete the matched fragment).  Zero matches
        and multiple matches are both non-mutating results so callers never
        have to guess which occurrence was intended.
        """
        old_text = str(old_str)
        replacement = str(new_str)
        if not old_text:
            return {"ok": False, "error": "empty_old_str", "matches": 0}
        if "content" in kwargs:
            return {"ok": False, "error": "content_conflict", "matches": 0}

        meaning_changed = "meaning" in kwargs or "meaning_append" in kwargs
        derived_state: dict[str, Any] = {}
        async with self._bucket_turn(bucket_id):
            file_path = self._find_bucket_file(bucket_id)
            if not file_path:
                return {"ok": False, "error": "not_found", "matches": 0}
            try:
                post = frontmatter.load(file_path)
            except Exception as exc:
                logger.warning(
                    "Failed to load bucket for content patch %s: %s",
                    bucket_id,
                    exc,
                )
                return {"ok": False, "error": "read_failed", "matches": 0}

            if (
                expected_lock_state is not None
                and _letter_lock_revision(post) != tuple(expected_lock_state)
            ):
                return {"ok": False, "error": "concurrent_lock", "matches": 0}

            current_content = str(post.content or "")
            # ``str.count`` ignores overlapping occurrences ("aa" in "aaa"),
            # which could silently patch the first of two valid match starts.
            # Only 0/1/many matters, so stop at the second start rather than
            # scanning every pathological overlapping match.
            first_match = current_content.find(old_text)
            if first_match < 0:
                return {
                    "ok": False,
                    "error": "old_str_not_found",
                    "matches": 0,
                }
            second_match = current_content.find(old_text, first_match + 1)
            if second_match >= 0:
                return {
                    "ok": False,
                    "error": "old_str_ambiguous",
                    "matches": 2,
                }

            updated_content = self._sanitize_text(
                current_content.replace(old_text, replacement, 1)
            )
            if updated_content == current_content:
                return {"ok": False, "error": "unchanged", "matches": 1}
            if not updated_content.strip():
                return {
                    "ok": False,
                    "error": "invalid_content",
                    "matches": 1,
                    "message": "替换后正文不能为空；如需移除整个桶，请使用归档。",
                }

            updates = dict(kwargs)
            if append_plan_history and str(post.get("type") or "") == "plan":
                history = list(post.get("change_log") or [])
                old_status = post.get("status") or "active"
                if "status" in updates and updates["status"] != old_status:
                    history = append_plan_change_log(
                        history,
                        "status",
                        **{
                            "from": old_status,
                            "to": updates["status"],
                            "by": event_actor,
                        },
                    )
                updates["change_log"] = append_plan_change_log(
                    history, "edit", by=event_actor
                )
            updates["content"] = updated_content
            try:
                committed = await self._update_locked(
                    bucket_id,
                    _derived_state_out=derived_state,
                    event_actor=event_actor,
                    expected_lock_state=expected_lock_state,
                    **updates,
                )
            except ValueError as exc:
                return {
                    "ok": False,
                    "error": "invalid_content",
                    "matches": 1,
                    "message": str(exc),
                }
            result = {
                "ok": bool(committed),
                "error": "" if committed else "update_failed",
                "matches": 1,
            }
        if result["ok"]:
            self._queue_captured_derived_state(derived_state)
            await self._index_after_update(
                bucket_id,
                content_changed=True,
                meaning_changed=meaning_changed,
            )
        return result

    # ---------------------------------------------------------
    # Update bucket
    # 更新桶
    # Supports: content, tags, importance, valence, arousal, name, resolved
    # ---------------------------------------------------------
    async def update(
        self,
        bucket_id: str,
        *,
        allow_embedding_fallback: bool = False,
        bump_active: bool = False,
        event_actor: str = "system",
        expected_lock_state: Optional[tuple[str, str, str]] = None,
        **kwargs,
    ) -> bool:
        """
        Update bucket content or metadata fields.
        更新桶的内容或元数据字段。

        bump_active=False（默认）：纯元数据/内容编辑（trace、plan、anchor、后台
        自动 resolve、导入等）——**不**刷新 last_active，也不动 activation_count。
        bump_active=True：把这次写入视作一次真实激活（如 hold/grow 合并近邻桶），
        同步刷新 last_active 并累加 activation_count，语义与 touch() 一致。
        """
        content_changed = "content" in kwargs
        meaning_changed = "meaning" in kwargs or "meaning_append" in kwargs
        derived_state: dict[str, Any] = {}
        async with self._bucket_turn(bucket_id):
            committed = await self._update_locked(
                bucket_id,
                allow_embedding_fallback=allow_embedding_fallback,
                bump_active=bump_active,
                event_actor=event_actor,
                expected_lock_state=expected_lock_state,
                _derived_state_out=derived_state,
                **kwargs,
            )
        if committed:
            self._queue_captured_derived_state(derived_state)
            await self._index_after_update(
                bucket_id,
                content_changed=content_changed,
                meaning_changed=meaning_changed,
            )
        return committed

    async def mutate_relation_pair(
        self,
        left_bucket_id: str,
        right_bucket_id: str,
        mutation: Any,
    ) -> Any:
        """Atomically change both mirrored Relation ledgers under ordered locks.

        ``mutation(left_post, right_post)`` returns
        ``(left_changed, right_changed, result)``.  Both bucket files are loaded
        while holding the same two cross-process bucket turns.  If the second
        write fails after the first was committed, the first file is restored
        to its pre-mutation serialization before the error is re-raised.
        """
        left_bucket_id = str(left_bucket_id or "").strip()
        right_bucket_id = str(right_bucket_id or "").strip()
        if not left_bucket_id or not right_bucket_id or left_bucket_id == right_bucket_id:
            return None

        first_id, second_id = sorted((left_bucket_id, right_bucket_id))
        async with self._bucket_turn(first_id):
            async with self._bucket_turn(second_id):
                left_path = self._find_bucket_file(left_bucket_id)
                right_path = self._find_bucket_file(right_bucket_id)
                if not left_path or not right_path:
                    return None
                try:
                    left_post = frontmatter.load(left_path)
                    right_post = frontmatter.load(right_path)
                except Exception:
                    return None

                left_before = frontmatter.dumps(left_post)
                right_before = frontmatter.dumps(right_post)
                left_changed, right_changed, result = mutation(left_post, right_post)
                if not left_changed and not right_changed:
                    return result

                left_written = False
                right_written = False
                try:
                    if left_changed:
                        _atomic_write_text(left_path, frontmatter.dumps(left_post))
                        left_written = True
                    if right_changed:
                        _atomic_write_text(right_path, frontmatter.dumps(right_post))
                        right_written = True
                except Exception:
                    if left_written:
                        _atomic_write_text(left_path, left_before)
                    if right_written:
                        _atomic_write_text(right_path, right_before)
                    raise
                return result

    async def _update_locked(
        self,
        bucket_id: str,
        *,
        _derived_state_out: dict[str, Any],
        allow_embedding_fallback: bool = False,
        bump_active: bool = False,
        event_actor: str = "system",
        expected_lock_state: Optional[tuple[str, str, str]] = None,
        **kwargs,
    ) -> bool:
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False

        # Normalize public/migration inputs at the storage boundary.  A quoted
        # YAML value such as "false" must never be persisted as true merely
        # because Python considers non-empty strings truthy.
        for field in (
            "resolved",
            "pinned",
            "protected",
            "digested",
            "dont_surface",
            "first_of_kind",
            "anchor",
        ):
            if field in kwargs:
                kwargs[field] = parse_bool(kwargs[field])

        if "content" in kwargs:
            kwargs["content"] = self._sanitize_text(kwargs["content"])
            self._validate_bucket_content(kwargs["content"])
        if "tags" in kwargs:
            kwargs["tags"] = self._normalize_metadata_list(
                kwargs["tags"],
                max_items=_MAX_TAGS,
                max_chars=_MAX_TAG_CHARS,
            )
        if "domain" in kwargs:
            kwargs["domain"] = self._normalize_metadata_list(
                kwargs["domain"],
                max_items=_MAX_DOMAINS,
                max_chars=_MAX_DOMAIN_CHARS,
            ) or [_DEFAULT_DOMAIN_NAME]
        if "media" in kwargs:
            # Miss: media 是整体覆盖写入（trace 的 media_replace）。传空列表即清空该字段。
            kwargs["media"] = self._normalize_media(
                await self.media_store.persist(bucket_id, kwargs["media"])
            )
        if "media_append" in kwargs:
            # Miss: media_append 是追加写入（trace 的 media_append / hold 每次调用）。
            kwargs["media_append"] = self._normalize_media(
                await self.media_store.persist(bucket_id, kwargs["media_append"])
            )
        if "meaning" in kwargs:
            # Miss: meaning 整体覆盖写入（trace 的 meaning_replace，用于纠错/清理）。
            kwargs["meaning"] = self._normalize_meaning_list(kwargs["meaning"])
        if "meaning_append" in kwargs:
            # Miss: meaning_append 是追加一条新 meaning（trace 的 meaning_append / hold 每次调用）。
            kwargs["meaning_append"] = self._normalize_meaning_item(kwargs["meaning_append"])
        if "title" in kwargs:
            kwargs["title"] = normalize_memory_title(
                self._sanitize_text(kwargs["title"])
            )
        if "source_refs_append" in kwargs:
            from ombrebrain.storage.source_store import normalize_source_refs

            kwargs["source_refs_append"] = normalize_source_refs(
                kwargs["source_refs_append"]
            )
        if "quotes_append" in kwargs:
            # 早校验：非法引语在这里就报错，不要等到写文件那一步。
            kwargs["quotes_append"] = self._sanitize_quotes(kwargs["quotes_append"])
        if "quotes" in kwargs and kwargs["quotes"]:
            # 整体替换（trace quotes_replace）。空列表是「清空」，不必过校验。
            kwargs["quotes"] = self._sanitize_quotes(kwargs["quotes"])

        try:
            post = frontmatter.load(file_path)
        except Exception as e:
            logger.warning(f"Failed to load bucket for update / 加载桶失败: {file_path}: {e}")
            return False

        if (
            expected_lock_state is not None
            and _letter_lock_revision(post) != tuple(expected_lock_state)
        ):
            logger.info("update() rejected concurrent Letter lock change: %s", bucket_id)
            return False

        # Work out the final pin/type state before mutating the post.  Type is
        # also a physical-storage decision, so unsupported values must fail
        # here instead of being written into frontmatter and reported as a
        # successful edit.
        was_pinned = parse_bool(post.get("pinned", False), default=False)
        is_protected = parse_bool(post.get("protected", False), default=False)
        was_anchor = parse_bool(post.get("anchor", False), default=False)
        current_type = str(post.get("type") or "dynamic").strip().lower()
        if (
            current_type == "archived"
            or post.get("deleted_at")
            or parse_bool(post.get("tombstone"), default=False)
        ):
            # Archive is a terminal lifecycle transition.  Allowing an
            # ordinary update here is unsafe even when ``type`` was omitted:
            # pinned=True forces type=permanent below and could otherwise
            # resurrect an archived file into the active tree.
            logger.warning(
                "update() rejected mutation on terminal bucket=%s",
                bucket_id,
            )
            return False
        will_be_pinned = parse_bool(
            kwargs.get("pinned", was_pinned), default=was_pinned
        )
        will_be_protected = parse_bool(
            kwargs.get("protected", is_protected), default=is_protected
        )
        will_be_anchor = parse_bool(
            kwargs.get("anchor", was_anchor), default=was_anchor
        )
        if will_be_pinned and will_be_protected:
            logger.warning(
                "update() rejected incompatible pinned/protected state "
                "bucket=%s",
                bucket_id,
            )
            return False
        if will_be_anchor and will_be_protected:
            logger.warning(
                "update() rejected incompatible anchor/protected state "
                "bucket=%s",
                bucket_id,
            )
            return False

        requested_type: str | None = None
        explicit_type_requested = "type" in kwargs
        if explicit_type_requested:
            requested_type = str(kwargs["type"] or "").strip().lower()
            if requested_type not in _EDITABLE_BUCKET_TYPES:
                logger.warning(
                    "update() rejected unsupported type=%r for bucket=%s",
                    requested_type,
                    bucket_id,
                )
                return False
        forced_type: str | None = None
        if will_be_pinned:
            forced_type = "permanent"
        elif "pinned" in kwargs and was_pinned:
            # 真正的 pinned 桶在显式解除时降回 dynamic；原本就是
            # permanent（was_pinned=False）的记忆仍保持 permanent。
            # pinned -> protected 的同步切换仍在同一个事务内完成，
            # 但 protected 本身不强制任何存储类型。
            forced_type = "dynamic"

        if forced_type is not None:
            if requested_type is not None and requested_type != forced_type:
                logger.warning(
                    "update() rejected incompatible pinned/type transition "
                    "bucket=%s pinned=%s type=%s",
                    bucket_id,
                    will_be_pinned,
                    requested_type,
                )
                return False
            kwargs["type"] = forced_type
            requested_type = forced_type

        if (
            explicit_type_requested
            and requested_type is not None
            and requested_type != current_type
            and will_be_protected
            and requested_type != "permanent"
        ):
            logger.warning(
                "update() rejected protected bucket type transition "
                "bucket=%s type=%s",
                bucket_id,
                requested_type,
            )
            return False

        # 最终仍为 pinned/protected 时把 importance 锁定为 10；若在同一事务中
        # 解除最后一层保护，则允许恢复调用方显式选择的动态 importance。
        if will_be_pinned or will_be_protected:
            kwargs.pop("importance", None)

        # --- Update only fields that were passed in / 只改传入的字段 ---
        if "content" in kwargs:
            post.content = kwargs["content"]  # wikilink injection disabled; LLM adds [[]] via prompt
        if "tags" in kwargs:
            post["tags"] = kwargs["tags"]
        if "importance" in kwargs:
            post["importance"] = _clamp_importance(kwargs["importance"], f"update:{bucket_id}")
        if "domain" in kwargs:
            post["domain"] = kwargs["domain"]
        if "valence" in kwargs:
            post["valence"] = _clamp_unit(kwargs["valence"], "valence", f"update:{bucket_id}")
        if "arousal" in kwargs:
            post["arousal"] = _clamp_unit(kwargs["arousal"], "arousal", f"update:{bucket_id}")
        if "name" in kwargs:
            post["name"] = sanitize_name(kwargs["name"])
        if "title" in kwargs and kwargs["title"]:
            post["title"] = kwargs["title"]
        if "source_refs_append" in kwargs and kwargs["source_refs_append"]:
            from ombrebrain.storage.source_store import append_source_links, active_source_refs_from_links

            links = append_source_links(post.metadata, kwargs["source_refs_append"])
            post["source_links"] = links
            post["source_refs"] = active_source_refs_from_links(links)
        if "quotes_append" in kwargs and kwargs["quotes_append"]:
            # 合并到已有桶时两边的引语都保留——每条引语属于它自己的那个时刻，
            # 不因为两段记忆被合并就作废。超上限的部分丢弃并明说，不静默。
            merged, dropped = self._merge_quotes(
                post.metadata.get("quotes"), kwargs["quotes_append"]
            )
            if merged:
                post["quotes"] = merged
            if dropped:
                _ob_push_warning(
                    "OB-W006",
                    f"合并到已有记忆后引语超过上限，最早的几条被保留，"
                    f"另外 {dropped} 条未写入（update:{bucket_id}）",
                )
        if "resolved" in kwargs:
            post["resolved"] = kwargs["resolved"]
        if "pinned" in kwargs:
            post["pinned"] = kwargs["pinned"]
            if kwargs["pinned"]:
                post["importance"] = _PINNED_IMPORTANCE  # pinned → lock importance to 10
                post.metadata.pop("anchor", None)  # pinned 与 anchor 互斥：钉为核心准则即清除坐标系标记
        if "protected" in kwargs:
            if kwargs["protected"]:
                post["protected"] = True
                post["importance"] = _PINNED_IMPORTANCE
            else:
                # False 回到缺省态，不在新数据里留遗留假值字段。
                post.metadata.pop("protected", None)
        if "digested" in kwargs:
            post["digested"] = kwargs["digested"]
        if "model_valence" in kwargs:
            post["model_valence"] = _clamp01(kwargs["model_valence"], _DEFAULT_VALENCE)
        if "media" in kwargs:
            # Miss: 整体覆盖写入（trace media_replace）；空列表清空该字段。
            if kwargs["media"]:
                post["media"] = kwargs["media"]
            else:
                post.metadata.pop("media", None)
        if "media_append" in kwargs and kwargs["media_append"]:
            # Miss: 追加写入，去重同 path 的旧引用（trace media_append / hold 每次调用）。
            existing_media = post.get("media") or []
            existing_paths = {m.get("path") for m in existing_media if isinstance(m, dict)}
            appended = existing_media + [
                m for m in kwargs["media_append"] if m.get("path") not in existing_paths
            ]
            post["media"] = appended[:_MEDIA_MAX_ITEMS]
        if "quotes" in kwargs:
            # 整体覆盖写入（trace quotes_replace，用于订正与删除）；空列表清空该字段。
            # 与 quotes_append 的区别是「谁说了算」：append 是合并两段记忆时两边
            # 的引语都该留下，replace 是我回头看这几句，说其中某句不对或不该留。
            if kwargs["quotes"]:
                post["quotes"] = kwargs["quotes"]
            else:
                post.metadata.pop("quotes", None)
        if "meaning" in kwargs:
            # Miss: 整体覆盖写入（trace meaning_replace，用于纠错/清理）；空列表清空该字段。
            if kwargs["meaning"]:
                post["meaning"] = kwargs["meaning"]
            else:
                post.metadata.pop("meaning", None)
        if "meaning_append" in kwargs and kwargs["meaning_append"]:
            # Miss: 追加一条新 meaning，不覆盖已有的（trace meaning_append / hold 每次调用）。
            existing_meaning = post.get("meaning") or []
            if isinstance(existing_meaning, str):
                existing_meaning = [existing_meaning]
            post["meaning"] = (list(existing_meaning) + [kwargs["meaning_append"]])[:_MEANING_LIST_MAX_ITEMS]
        # --- Pass-through fields for plan/letter lifecycle ---
        # --- plan/letter/iter1.7 生命周期相关字段直接透传到 frontmatter ---
        # 这一组字段没有「校验/转换」逻辑，给什么写什么。新增字段往这个元组里加即可。
        # iter 1.7 §G3 在这里加入了 "change_log"——plan 桶的状态/编辑历史 list[dict]，
        # 由 server.py 的 plan() / trace() / /api/plans/{id}/action 维护，bucket_manager 不参与生成。
        for k in ("status", "type", "resolution_reason", "resolved_by",
                  "resolution_suggested",
                  "related_bucket", "author", "user_name", "letter_date",
                  "lock_type", "unlock_date", "locked_by", "lock_owner_source", "writer_name",
                  "change_log",
                  # iter 1.8 新增字段。除 weight 外全部透传不转换。
                  # weight 在 plan 上才有意义；这里不在这个循环里校验类型，由上层 server.py 保证传入范围。
                  "why_remembered", "dont_surface", "first_of_kind",
                  "weight", "triggered_by",
                  # iter 2.0 新增 anchor。bool 字段，不参与评分，硬上限 24。
                  # 上限校验在下面 anchor 分支里做（False→True 切换时计数），
                  # set_anchor() 仍是首选入口，update() 只是兜底兼容批量迁移脚本。
                  "anchor",
                  # iter 2.0 来源追踪字段：
                  # source_tool / grow_batch_id 一般在 create() 时定型，
                  # 这里的透传只服务于迁移脚本（给历史桶补字段）。
                  # last_merged_by 由 _common.merge_or_create 在 merge 后写入，
                  # 表示「最后一次合并是 hold 还是 grow 触发的」。
                  # _pre_anchor_source_tool 是 anchor 时保存的原始 source_tool，
                  # release 时自动恢复；None 表示删除该字段。
                  "source_tool", "grow_batch_id", "last_merged_by", "_pre_anchor_source_tool",
                  # I 沉淀机制字段（tools/i/core.py 维护，bucket_manager 不生成也不解读）：
                  # i_stage        "candidate" | "promoted"，标一条普通记忆是 I 候选
                  # i_dream_dates  被 dream 见证过的日期列表（按天去重），升级门槛的唯一依据
                  # i_promoted_to  候选升级后指向的正式 I 桶 ID
                  # i_from_candidate 正式 I 桶指回它的候选桶 ID
                  "i_stage", "i_dream_dates", "i_promoted_to", "i_from_candidate"):
            if k in kwargs:
                if k == "weight" and kwargs[k] is not None:
                    post[k] = _clamp01(kwargs[k], _DEFAULT_VALENCE)
                elif k == "dont_surface":
                    post[k] = kwargs[k]
                elif k == "first_of_kind":
                    post[k] = kwargs[k]
                elif k == "anchor":
                    # iter 2.0: anchor 是布尔；False 时直接删除字段保持 frontmatter 干净。
                    # 修复：透传路径之前会绕过 ANCHOR_LIMIT，导致批量脚本/前端直接 update(anchor=True)
                    # 可以让 anchor 总数突破 24 上限。这里补一道校验：
                    # 仅当从 False→True 切换时才计数；当前已是 anchor 的桶重复设置不计数。
                    if kwargs[k]:
                        already_anchor = parse_bool(
                            post.get("anchor", False), default=False
                        )
                        if not already_anchor:
                            # FIX (RED-02): count_anchors 是 async，必须 await，否则
                            # `coroutine >= int` 会 TypeError，整个上限校验失效。
                            current = await self.count_anchors()
                            if current >= self.ANCHOR_LIMIT:
                                logger.warning(
                                    f"update() 拒绝 anchor=True：已达上限 "
                                    f"{self.ANCHOR_LIMIT}（当前 {current}）。bucket={bucket_id}"
                                )
                                return False
                        post["anchor"] = True
                    else:
                        post.metadata.pop("anchor", None)
                else:
                    if kwargs[k] is None:
                        # None = 明确删除该 frontmatter 字段（用于 anchor release 清理临时字段）
                        post.metadata.pop(k, None)
                    elif k in _METADATA_TEXT_LIMITS:
                        post[k] = self._sanitize_text(str(kwargs[k])).strip()[
                            :_METADATA_TEXT_LIMITS[k]
                        ]
                    else:
                        post[k] = kwargs[k]

        # --- 激活时间 / 激活次数 ---
        # last_active 只代表「最后一次真实激活/召回」，并作为衰减 recency 打分的输入。
        # 元数据编辑（trace / plan / anchor / 后台自动 resolve 等）**不算「活跃」**：
        # 若在此无条件刷新，会重置遗忘时效，还会让 activation_count 与 last_active
        # 长期不一致（次数不涨、时间却变新）。只有真正的「新事件写入」才把这条记忆
        # 当作被重新激活一次——由 bump_active=True 显式触发（如 hold/grow 合并近邻桶），
        # 同步刷新 last_active 并累加 activation_count，语义与 touch() 一致。
        if bump_active:
            post["last_active"] = now_iso()
            post["activation_count"] = int(post.get("activation_count") or 0) + 1

        final_type = str(post.get("type") or current_type).strip().lower()
        target_path = file_path
        if final_type in _EDITABLE_BUCKET_TYPES:
            target_path = self._bucket_target_path(
                file_path,
                final_type,
                post.get("domain") or [_DEFAULT_DOMAIN_NAME],
                str(post.get("status") or "active"),
            )

        try:
            committed_path = self._commit_bucket_update(
                file_path,
                target_path,
                frontmatter.dumps(post),
            )
        except (OSError, ValueError) as e:
            logger.error(
                "Failed to commit bucket update / 提交桶更新失败: "
                "%s -> %s: %s",
                file_path,
                target_path,
                e,
            )
            return False

        derived_state = {
            "bucket_id": bucket_id,
            "content": post.content or "",
            "meaning": post.get("meaning") or [],
            "queue_content": "content" in kwargs,
            "queue_meaning": (
                "meaning" in kwargs or "meaning_append" in kwargs
            ),
        }
        _derived_state_out.clear()
        _derived_state_out.update(derived_state)

        if bump_active:
            self._cache_bump(
                bucket_id,
                last_active=post["last_active"],
                activation_count=post["activation_count"],
                file_path=committed_path,
            )

        logger.info(f"Updated bucket / 更新记忆桶: {bucket_id}")

        # 这里故意不等待 provider 派生索引：所有调用方执行 _update_locked() 时都持有
        # 桶租约；由公开包装层/调用点在本方法返回并释放租约后刷新 content/meaning 向量。
        self._invalidate_bm25()
        self._record_v3_bucket_event(
            "update",
            bucket_id,
            str(post.get("type") or "dynamic"),
            post.content or "",
            dict(post.metadata),
        )
        self._record_ledger_event(
            "TraceUpdated",
            bucket_id,
            str(post.get("type") or "dynamic"),
            post.content or "",
            dict(post.metadata),
            {
                "changed_fields": sorted(str(k) for k in kwargs.keys()),
                "event_actor": str(event_actor or "system").strip().lower(),
            },
        )
        return True

    async def hard_delete_test_bucket(self, bucket_id: str, *, reason: str = "") -> dict:
        """Erase only a bucket born as test data, with an explicit audit reason."""
        async with self._bucket_turn(bucket_id):
            result = await self._hard_delete_test_bucket_locked(
                bucket_id, reason=reason
            )
        if result.get("ok"):
            await self._discard_derived_index_if_terminal(bucket_id)
        return result

    async def _hard_delete_test_bucket_locked(
        self,
        bucket_id: str,
        *,
        reason: str = "",
    ) -> dict:
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return {"ok": False, "error": "not_found"}
        try:
            post = frontmatter.load(file_path)
        except Exception as exc:
            return {"ok": False, "error": f"read_failed: {exc}"}
        provenance = post.get("provenance")
        if not (isinstance(provenance, dict)
                and provenance.get("kind") == "test"
                and provenance.get("erasable") is True):
            return {"ok": False, "error": "not_erasable_test_data"}
        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            return {"ok": False, "error": "missing_delete_reason"}
        if len(normalized_reason) > 500:
            return {"ok": False, "error": "delete_reason_too_long"}
        bucket_type = str(post.get("type") or "dynamic")
        try:
            os.remove(file_path)
        except OSError as exc:
            return {"ok": False, "error": f"delete_failed: {exc}"}
        self._invalidate_bm25()
        self._record_ledger_event(
            "TraceHardDeleted", bucket_id, bucket_type, "",
            {"provenance": {"kind": "test", "erasable": True}},
            {"reason": normalized_reason, "content_erased": True},
        )
        logger.warning("Physically erased test bucket: %s", bucket_id)
        return {"ok": True, "deleted": bucket_id}

    # ---------------------------------------------------------
    # Wikilink injection — DISABLED
    # 自动添加 Obsidian 双链 — 已禁用
    # Now handled by LLM prompts (Gemini adds [[]] for proper nouns)
    # 现在由 LLM prompt 处理（Gemini 对人名/地名/专有名词加 [[]]）
    # ---------------------------------------------------------
    # def _apply_wikilinks(self, content, tags, domain, name): ...
    # def _collect_wikilink_keywords(self, content, tags, domain, name): ...
    # def _normalize_keywords(self, keywords): ...
    # def _extract_auto_keywords(self, content): ...

    # ---------------------------------------------------------
    # Delete bucket
    # 删除桶
    # ---------------------------------------------------------
    async def delete(self, bucket_id: str) -> bool:
        """
        Soft-delete a memory bucket: move to archive/ and stamp `deleted_at`.
        F-10: 记忆不消失，只是淡去。不做物理删除，将文件移入 archive/
        并在 frontmatter 中写入 deleted_at 时间戳；embedding 仍清理以节省空间。
        """
        async with self._bucket_turn(bucket_id):
            deleted = await self._delete_locked(bucket_id)
        if deleted:
            await self._discard_derived_index_if_terminal(bucket_id)
        return deleted

    async def restore_archived(
        self,
        bucket_id: str,
        *,
        importance_override: Optional[int] = None,
        protected_override: Optional[bool] = None,
    ) -> dict:
        """Restore an archived/tombstoned Markdown bucket to its original channel.

        Discovery never calls this method.  It is deliberately exposed only
        through an explicit ``trace(..., restore=True)`` decision.
        """
        async with self._bucket_turn(bucket_id):
            file_path = self._find_bucket_file(bucket_id)
            if not file_path:
                return {"ok": False, "error": "not_found"}
            try:
                post = frontmatter.load(file_path)
            except Exception as exc:
                return {"ok": False, "error": f"read_failed: {exc}"}

            normalized_path = os.path.normcase(os.path.abspath(file_path))
            normalized_archive = os.path.normcase(os.path.abspath(self.archive_dir))
            try:
                stored_in_archive = (
                    os.path.commonpath((normalized_path, normalized_archive))
                    == normalized_archive
                )
            except ValueError:
                stored_in_archive = False
            archived_state = (
                stored_in_archive
                or str(post.get("type") or "").strip().lower() == "archived"
                or bool(post.get("deleted_at"))
                or parse_bool(post.get("tombstone"), default=False)
            )
            if not archived_state:
                return {"ok": False, "error": "not_archived"}

            original_kind = self.footprint_snapshot().original_kind(
                bucket_id, dict(post.metadata)
            )
            if original_kind not in _EDITABLE_BUCKET_TYPES:
                original_kind = "dynamic"
            was_pinned = parse_bool(post.get("pinned"), default=False)
            is_protected = parse_bool(post.get("protected"), default=False)
            is_anchor = parse_bool(post.get("anchor"), default=False)
            if protected_override is not None and parse_bool(
                protected_override, default=False
            ):
                return {"ok": False, "error": "invalid_protected_override"}
            final_protected = (
                is_protected
                if protected_override is None
                else False
            )
            if final_protected and is_anchor:
                return {
                    "ok": False,
                    "error": "incompatible_protected_anchor",
                }
            if (
                is_protected
                and not final_protected
                and importance_override is None
            ):
                return {
                    "ok": False,
                    "error": "missing_importance_override",
                }
            if was_pinned:
                original_kind = "permanent"

            post["type"] = original_kind
            # 归档态不占 pinned 名额；恢复时也不能暗中重新占用。
            # 历史 pinned+protected 脏数据通常原子收敛为仅 protected；
            # 显式 protected_override=False 的恢复则同时解除 protected。
            post["pinned"] = False
            if final_protected:
                post["protected"] = True
                post["importance"] = _PINNED_IMPORTANCE
            else:
                post.metadata.pop("protected", None)
            if not final_protected and importance_override is not None:
                try:
                    normalized_importance = int(importance_override)
                except (TypeError, ValueError, OverflowError):
                    return {"ok": False, "error": "invalid_importance_override"}
                if not 1 <= normalized_importance <= 10:
                    return {"ok": False, "error": "invalid_importance_override"}
                post["importance"] = normalized_importance
            # 显式恢复应刷新衰减使用的活跃时钟；保留旧时间会让低分桶
            # 在下一轮衰减中立即二次归档。与类型恢复一起原子提交，避免分裂。
            post["last_active"] = now_iso()
            for field in (
                "deleted_at", "tombstone", "tombstoned_at", "erasure_mode"
            ):
                post.metadata.pop(field, None)
            try:
                target_path = self._bucket_target_path(
                    file_path,
                    original_kind,
                    post.get("domain") or [_DEFAULT_DOMAIN_NAME],
                    str(post.get("status") or "active"),
                )
                committed_path = self._commit_bucket_update(
                    file_path, target_path, frontmatter.dumps(post)
                )
            except (OSError, ValueError) as exc:
                logger.error("Failed to restore archived bucket %s: %s", bucket_id, exc)
                return {"ok": False, "error": f"restore_failed: {exc}"}

            self._invalidate_bm25()
            self._record_v3_bucket_event(
                "restore", bucket_id, original_kind, post.content or "", dict(post.metadata)
            )
            self._record_ledger_event(
                "TraceRestored",
                bucket_id,
                original_kind,
                post.content or "",
                dict(post.metadata),
            )
            logger.info("Restored archived bucket: %s -> %s", bucket_id, committed_path)
            result = {"ok": True, "restored": bucket_id, "type": original_kind}
            meaning_changed = bool(post.get("meaning"))
            derived_state = {
                "bucket_id": bucket_id,
                "content": post.content or "",
                "meaning": post.get("meaning") or [],
                "queue_content": True,
                "queue_meaning": meaning_changed,
            }

        self._queue_captured_derived_state(derived_state)
        await self._index_after_update(
            bucket_id,
            content_changed=True,
            meaning_changed=meaning_changed,
        )
        return result

    _path_is_within = staticmethod(_bp.path_is_within)

    def _physical_bucket_sources(self, bucket_id: str) -> tuple[list[tuple[str, Any]], bool]:
        """绕过路径缓存，枚举同一 ID 的全部 Markdown 物理真源。

        维护迁移不能信任早先扫描得到的路径，也不能使用只返回首个命中的
        ``_find_bucket_file``。调用方须在 ``_bucket_turn`` 内调用本方法。
        文件名明显属于目标 ID 却无法解析时，第二个返回值为 True，要求迁移
        保守停止，避免把潜在重复真源忽略掉。
        """
        sources: list[tuple[str, Any]] = []
        unreadable_candidate = False
        directories = list(self._active_dirs) + [self.archive_dir]
        for _root, filename, file_path in self._iter_md_files(directories):
            stem = filename[:-3]
            filename_matches = stem == bucket_id or stem.endswith(f"_{bucket_id}")
            try:
                post = frontmatter.load(file_path)
            except Exception:
                if filename_matches:
                    unreadable_candidate = True
                continue
            stored_id = str(post.get("id") or (stem if filename_matches else "")).strip()
            if stored_id == bucket_id:
                sources.append((file_path, post))
        return sources, unreadable_candidate

    _has_strong_letter_marker = staticmethod(_bp.has_strong_letter_marker)

    _has_ambiguous_letter_marker = staticmethod(_bp.has_ambiguous_letter_marker)

    @staticmethod
    def _archived_letter_rejection(post: Any) -> str:
        """返回专用迁移的拒绝原因；空串代表可继续校验物理位置。"""
        if any(post.get(field) for field in _ARCHIVED_LETTER_TERMINAL_VALUE_FIELDS):
            return "terminal_state"
        if any(
            parse_bool(post.get(field), default=False)
            for field in _ARCHIVED_LETTER_TERMINAL_BOOL_FIELDS
        ):
            return "terminal_state"
        if str(post.get("status") or "").strip().casefold() in {
            "deleted",
            "tombstone",
            "erased",
        }:
            return "terminal_state"
        if any(
            parse_bool(post.get(field), default=False)
            for field in ("pinned", "protected", "anchor")
        ):
            return "protected_state"
        if not BucketManager._has_strong_letter_marker(post):
            if BucketManager._has_ambiguous_letter_marker(post):
                return "ambiguous_letter_marker"
            return "not_letter"
        return ""

    async def recover_archived_letter(self, bucket_id: str) -> dict:
        """把误归档的历史 Letter 原子迁回 Letter 存储树。

        这是一次性兼容迁移原语，不是普通归档恢复：它只接受 archive 中
        ``type=archived`` 且带 ``source_tool=letter`` 或 ``__letter__`` 的唯一
        物理真源。删除终态与 pinned/protected/anchor 状态一律拒绝；正文、
        时间、作者和时间锁字段原样保留，且不会刷新 ``last_active``。
        """
        normalized_id = str(bucket_id or "").strip()
        if not normalized_id:
            return {"ok": False, "id": "", "reason": "invalid_id"}

        derived_state: dict[str, Any] | None = None
        async with self._bucket_turn(normalized_id):
            sources, unreadable_candidate = self._physical_bucket_sources(normalized_id)
            if unreadable_candidate:
                return {
                    "ok": False,
                    "id": normalized_id,
                    "reason": "unreadable_source",
                }
            if not sources:
                return {"ok": False, "id": normalized_id, "reason": "not_found"}
            if len(sources) != 1:
                return {
                    "ok": False,
                    "id": normalized_id,
                    "reason": "duplicate_source",
                }

            file_path, post = sources[0]
            rejection = self._archived_letter_rejection(post)
            if rejection:
                return {"ok": False, "id": normalized_id, "reason": rejection}

            stored_type = str(post.get("type") or "").strip().casefold()
            stored_in_archive = self._path_is_within(file_path, self.archive_dir)
            if not stored_in_archive:
                canonical_history = os.path.join(self.letter_dir, "history")
                if (
                    stored_type == "letter"
                    and os.path.normcase(os.path.realpath(os.path.dirname(file_path)))
                    == os.path.normcase(os.path.realpath(canonical_history))
                ):
                    return {
                        "ok": True,
                        "id": normalized_id,
                        "reason": "already_restored",
                    }
                return {
                    "ok": False,
                    "id": normalized_id,
                    "reason": "not_archived",
                }
            if stored_type != "archived":
                return {
                    "ok": False,
                    "id": normalized_id,
                    "reason": "invalid_archived_type",
                }

            # 仅改回 Letter 类型；其余 frontmatter 与正文必须保持原值。
            post["type"] = "letter"
            try:
                target_path = self._bucket_target_path(
                    file_path,
                    "letter",
                    post.get("domain") or ["letter"],
                )
                committed_path = self._commit_bucket_update(
                    file_path,
                    target_path,
                    frontmatter.dumps(post),
                )
            except (OSError, ValueError) as exc:
                logger.error(
                    "Failed to recover archived Letter / 历史 Letter 恢复失败: %s: %s",
                    normalized_id,
                    exc,
                )
                return {
                    "ok": False,
                    "id": normalized_id,
                    "reason": "commit_failed",
                }

            self._invalidate_bm25()
            self._record_v3_bucket_event(
                "restore",
                normalized_id,
                "letter",
                post.content or "",
                dict(post.metadata),
            )
            self._record_ledger_event(
                "TraceRestored",
                normalized_id,
                "letter",
                post.content or "",
                dict(post.metadata),
                {"event_actor": "maintenance", "compatibility": "archived_letter"},
            )
            logger.info(
                "Recovered archived Letter / 已恢复历史 Letter: %s -> %s",
                normalized_id,
                committed_path,
            )
            derived_state = {
                "bucket_id": normalized_id,
                "content": post.content or "",
                "meaning": post.get("meaning") or [],
                "queue_content": True,
                "queue_meaning": bool(post.get("meaning")),
            }

        assert derived_state is not None
        self._queue_captured_derived_state(derived_state)
        await self._index_after_update(
            normalized_id,
            content_changed=True,
            meaning_changed=bool(derived_state["meaning"]),
        )
        return {"ok": True, "id": normalized_id, "reason": "restored"}

    async def _delete_locked(self, bucket_id: str) -> bool:
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False

        # --- 读取文件，写入 deleted_at，移入 archive/ ---
        try:
            post = frontmatter.load(file_path)
            tombstone_at = now_iso()
            post["deleted_at"] = tombstone_at
            post["tombstone"] = True
            post["tombstoned_at"] = tombstone_at
            post["erasure_mode"] = "tombstone_only"
            os.makedirs(self.archive_dir, exist_ok=True)
            dest = os.path.join(self.archive_dir, os.path.basename(file_path))
            # 若 archive/ 里已有同名文件（极罕见），追加 bucket_id 后缀避免覆盖
            if os.path.exists(dest) and dest != file_path:
                dest = os.path.join(
                    self.archive_dir,
                    f"{os.path.splitext(os.path.basename(file_path))[0]}_{bucket_id}.md",
                )
            self._commit_bucket_update(
                file_path,
                str(dest),
                frontmatter.dumps(post),
            )
        except OSError as e:
            logger.error(f"Failed to soft-delete bucket / 软删除桶文件失败: {file_path}: {e}")
            return False

        self._invalidate_bm25()
        logger.info(f"Soft-deleted bucket (moved to archive) / 软删除记忆桶: {bucket_id}")
        self._record_v3_bucket_event(
            "delete",
            bucket_id,
            str(post.get("type") or "dynamic"),
            post.content or "",
            dict(post.metadata),
        )
        self._record_ledger_event(
            "TraceDeletedToArchive",
            bucket_id,
            str(post.get("type") or "dynamic"),
            post.content or "",
            dict(post.metadata),
        )
        return True

    # ---------------------------------------------------------
    # Touch bucket (refresh activation time + increment count)
    # 触碰桶（刷新激活时间 + 累加激活次数）
    # Called on every recall hit; affects decay score.
    # 每次检索命中时调用，影响衰减得分。
    # ---------------------------------------------------------
    async def touch(self, bucket_id: str, ripple: bool = True) -> None:
        """
        Update a bucket's last activation time and count.
        Also triggers time ripple: nearby memories get a slight activation boost.
        更新桶的最后激活时间和激活次数。
        同时触发时间涟漪：时间上相邻的记忆轻微唤醒。

        ripple=False 可跳过读全库的时间涟漪（性能 P2：批量浮现时不值当为它多跑 list_all）。
        """
        # Commit the source touch first, then release its turn before taking
        # any neighbour turns.  Keeping the source lock while acquiring a
        # target lock lets concurrent A->B and B->A ripples deadlock.
        async with self._bucket_turn(bucket_id):
            reference_time = await self._touch_locked(bucket_id)
        if ripple and reference_time is not None:
            await self._time_ripple(bucket_id, reference_time)

    async def _touch_locked(self, bucket_id: str) -> datetime | None:
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return None

        try:
            post = frontmatter.load(file_path)
            post["last_active"] = now_iso()
            post["activation_count"] = int(post.get("activation_count") or 0) + 1  # type: ignore[call-overload]

            _atomic_write_text(file_path, frontmatter.dumps(post))
            self._cache_bump(
                bucket_id,
                last_active=post["last_active"],
                activation_count=post["activation_count"],
                file_path=file_path,
            )

            current_time = parse_iso_datetime(
                post.get("created", post.get("last_active", ""))
            )
            self._record_ledger_event(
                "TraceTouched",
                bucket_id,
                str(post.get("type") or "dynamic"),
                post.content or "",
                dict(post.metadata),
            )
            return current_time
        except Exception as e:
            logger.warning(f"Failed to touch bucket / 触碰桶失败: {bucket_id}: {e}")
            return None

    async def touch_many(self, bucket_ids: list, ripple: bool = False) -> None:
        """批量 touch（性能 P2）：breath 浮现后一次性更新一批桶的激活，供后台任务调用。

        ripple 默认 False —— 时间涟漪是「可选的激活微调」，在批量浮现时不值当为它多跑
        list_all；需要时可显式开启（只对第一个桶做一次涟漪，避免 N×list_all）。
        单条失败不影响其他。
        """
        first = True
        for bid in bucket_ids:
            try:
                await self.touch(bid, ripple=ripple and first)
            except Exception as e:
                logger.warning(f"touch_many: 触碰 {bid} 失败: {e}")
            first = False

    async def _time_ripple(self, source_id: str, reference_time: datetime, hours: float = _RIPPLE_HOURS) -> None:
        """
        Slightly boost activation_count of buckets created/activated near the reference time.
        轻微提升时间相邻桶的激活次数（+0.3），不改 last_active 避免递归唤醒。
        Max 5 buckets rippled per touch to bound I/O.
        """
        try:
            all_buckets = await self.list_all(include_archive=False)
        except Exception:
            return

        rippled = 0
        for bucket in all_buckets:
            if rippled >= _RIPPLE_MAX_BUCKETS:
                break
            if bucket["id"] == source_id:
                continue
            target_id = str(bucket.get("id") or "")
            if not target_id:
                continue

            # The list_all row is only a candidate snapshot.  Take the normal
            # target turn, locate it again and re-read all eligibility fields
            # before writing so a concurrent edit/archive/delete cannot be
            # overwritten or resurrected from stale data.
            try:
                async with self._bucket_turn(target_id):
                    file_path = self._find_bucket_file(target_id)
                    if not file_path:
                        continue
                    normalized_path = os.path.normcase(os.path.abspath(file_path))
                    active = False
                    for active_dir in self._active_dirs:
                        normalized_dir = os.path.normcase(os.path.abspath(active_dir))
                        try:
                            if os.path.commonpath((normalized_path, normalized_dir)) == normalized_dir:
                                active = True
                                break
                        except ValueError:
                            continue
                    if not active:
                        continue

                    post = frontmatter.load(file_path)
                    if str(post.get("id") or target_id) != target_id:
                        continue
                    if (
                        parse_bool(post.get("pinned"), default=False)
                        or parse_bool(post.get("protected"), default=False)
                        or parse_bool(post.get("tombstone"), default=False)
                        or post.get("deleted_at")
                        or str(post.get("type") or "dynamic").strip().lower()
                        in ("permanent", "feel", "archived")
                    ):
                        continue

                    created_str = post.get("created", post.get("last_active", ""))
                    created = parse_iso_datetime(created_str)
                    delta_hours = abs((reference_time - created).total_seconds()) / 3600
                    if delta_hours > hours:
                        continue

                    current_count = float(post.get("activation_count") or 0)  # type: ignore[arg-type]
                    if not math.isfinite(current_count) or current_count < 0:
                        current_count = 0.0
                    # Store as float for fractional increments; calculate_score handles it
                    post["activation_count"] = round(current_count + _RIPPLE_BOOST, 1)
                    _atomic_write_text(file_path, frontmatter.dumps(post))
                    self._cache_bump(
                        target_id,
                        activation_count=post["activation_count"],
                        file_path=file_path,
                    )
                    rippled += 1
            except Exception as _ripple_exc:
                logger.warning(
                    f"[ripple] Failed to update activation_count for {target_id!r}: "
                    f"{type(_ripple_exc).__name__}: {_ripple_exc}"
                )
                continue

    # ---------------------------------------------------------
    # Multi-dimensional search (core feature)
    # 多维搜索（核心功能）
    #
    # Strategy: domain pre-filter → weighted multi-dim ranking
    # 策略：主题域预筛 → 多维加权精排
    #
    # Ranking formula:
    #   total = topic(×w_topic) + emotion(×w_emotion)
    #           + time(×w_time) + importance(×w_importance)
    #
    # Per-dimension scores (normalized to 0~1):
    #   topic     = rapidfuzz weighted match (name/tags/domain/body)
    #   emotion   = 1 - Euclidean distance (query v/a vs bucket v/a)
    #   time      = e^(-0.02 × days) (recent memories first)
    #   importance = importance / 10
    # ---------------------------------------------------------
    async def search(
        self,
        query: str,
        limit: Optional[int] = None,
        domain_filter: Optional[list[str]] = None,
        query_valence: Optional[float] = None,
        query_arousal: Optional[float] = None,
        vector_scores: Optional[dict[str, float]] = None,
        include_archive: bool = False,
        allowed_bucket_ids: Optional[set[str]] = None,
    ) -> list[dict]:
        """
        Multi-dimensional indexed search for memory buckets.
        多维索引搜索记忆桶。

        domain_filter: pre-filter by domain (None = search all)
        query_valence/arousal: emotion coordinates for resonance scoring
        """
        if not query or not query.strip():
            return []

        limit = limit or self.max_results
        # 字面召回：把查询原样（小写、去空白）留作子串匹配，保证显式搜的词必被召回
        q_norm = query.strip().lower()
        all_buckets = await self.list_all(include_archive=include_archive)

        if not all_buckets:
            return []
        searchable_buckets = all_buckets
        if allowed_bucket_ids is not None:
            allowed_ids = {str(bucket_id) for bucket_id in allowed_bucket_ids}
            searchable_buckets = [
                bucket for bucket in all_buckets
                if str(bucket.get("id")) in allowed_ids
            ]
        else:
            allowed_ids = None
        if not searchable_buckets:
            return []

        # --- Layer 0: bucket-id 直达通道（纯定位，短路）---
        # bucket id 是随机 hex、**没有语义**，不该进向量/BM25/模糊通道（塞进去只会
        # 污染语义空间）。这里独立做「完整 id 精确匹配」：查询串正好等于某个可见桶的
        # 完整 id → 直接返回该桶（满分），绕开语义排序。「我知道要哪条」的精确定位。
        # 只认完整 id（不做前缀匹配），避免普通关键词误触；软删除/归档桶不在 all_buckets
        # 中，故按 id 也搜不到已删除桶，与 get() 的可见性一致。
        q_exact = query.strip()
        if q_exact:
            for b in searchable_buckets:
                if str(b.get("id")) == q_exact:
                    hit = dict(b)
                    hit["score"] = 1.0
                    return [hit]

        # --- Layer 1: domain pre-filter (fast scope reduction) ---
        # --- 第一层：主题域预筛（快速缩小范围）---
        if domain_filter:
            filter_set = {d.lower() for d in domain_filter}
            candidates = [
                b for b in searchable_buckets
                if {d.lower() for d in b["metadata"].get("domain", [])} & filter_set
            ]
            # Fall back to full search if pre-filter yields nothing
            # 预筛为空则回退全量搜索
            if not candidates:
                candidates = searchable_buckets
        else:
            candidates = searchable_buckets

        # --- Layer 1.5: embedding 语义分数（仅作为打分维度，不再窄化候选集）---
        # 历史上这里把候选集替换成「在 embeddings.db 里的桶」，导致：
        #   - 任何缺少 embedding 的桶（落盘时 embed key 失败 / 旧脚本批量导入未补向量）
        #     只要查询命中过任意向量，就会被整体过滤掉 → breath 检索数对不上 pulse。
        # 修复：保留 vector_scores 给 Layer 2 的 semantic 维度用，但不动 candidates。
        # 没 embedding 的桶 semantic_score=0，仍可凭 topic/emotion/time/importance 命中。
        # ``None`` means this caller wants BucketManager to query the engine.
        # An explicit dict (including {}) lets an orchestration layer perform
        # the query once and reuse the same scores for ranking and recall.
        vector_scores_provided = vector_scores is not None
        if vector_scores is None:
            vector_scores = {}
        else:
            vector_scores = dict(vector_scores)
        if allowed_ids is not None:
            vector_scores = {
                bucket_id: score for bucket_id, score in vector_scores.items()
                if str(bucket_id) in allowed_ids
            }
        if (
            not vector_scores_provided
            and self.embedding_engine
            and self.embedding_engine.enabled
        ):
            try:
                if allowed_ids is None:
                    vector_results = await self.embedding_engine.search_similar(
                        query, top_k=_VECTOR_TOPK
                    )
                else:
                    vector_results = await self.embedding_engine.search_similar(
                        query,
                        top_k=_VECTOR_TOPK,
                        allowed_bucket_ids=allowed_ids,
                    )
                if vector_results:
                    vector_scores = {bid: score for bid, score in vector_results}
            except Exception as e:
                logger.warning(f"Embedding score failed, using fuzzy only / embedding 评分失败: {e}")

        # --- BM25 打分（性能 P4：脏了就后台线程重建，不在请求里同步阻塞 ~17s）---
        # 脏且没人在重建 → 起一个后台重建；本次查询用「当前索引」打分（首次为空，
        # 之后是上一版，略旧但有效）。向量+模糊+字面召回仍在，单次查询不会因 BM25 卡住。
        bm25_scores: dict[str, float] = {}
        if self._bm25 is not None:
            if self._bm25_dirty and not self._bm25_rebuilding:
                self._bm25_rebuilding = True
                asyncio.create_task(self._rebuild_bm25_async(all_buckets))
            try:
                bm25_scores = self._bm25.score(query)
            except Exception as e:
                logger.warning(f"[bm25] score 失败，本次跳过 BM25 维度: {e}")
                bm25_scores = {}

        # --- Layer 2: weighted multi-dim ranking ---
        # --- 第二层：多维加权精排 ---
        scored = []
        for bucket in candidates:
            meta = bucket.get("metadata", {})

            try:
                # 字面命中：查询串原样出现在 name/tags/domain/正文 → 召回保障 + 排序加分
                literal_hit = False
                if q_norm:
                    hay = " ".join([
                        str(meta.get("name", "")),
                        " ".join(str(t) for t in (meta.get("tags") or [])),
                        " ".join(str(d) for d in (meta.get("domain") or [])),
                        bucket.get("content", "") or "",
                    ]).lower()
                    literal_hit = q_norm in hay

                # Dim 1: topic relevance (fuzzy text, 0~1)
                topic_score = self._calc_topic_score(query, bucket)

                # Dim 2: emotion resonance (coordinate distance, 0~1)
                emotion_score = self._calc_emotion_score(
                    query_valence, query_arousal, meta
                )

                # Dim 3: time proximity (exponential decay, 0~1)
                time_score = self._calc_time_score(meta)

                # Dim 4: importance (direct normalization)
                importance_score = max(1, min(10, int(meta.get("importance") or 5))) / 10.0

                # Dim 5: touch frequency (召回频率, 0~1) — iter 2.1
                touch_score = self._calc_touch_score(meta)

                # --- Weighted sum / 加权求和 ---
                total = (
                    topic_score * self.w_topic
                    + emotion_score * self.w_emotion
                    + time_score * self.w_time
                    + importance_score * self.w_importance
                    + touch_score * self.w_touch
                )
                weight_sum = (
                    self.w_topic + self.w_emotion + self.w_time
                    + self.w_importance + self.w_touch
                )
                # Dim 6: semantic similarity — only when embedding is available (iter 2.1)
                # 仅 embedding 可用时加入语义相似度维度；不可用时不影响 weight_sum 平衡
                semantic_score = vector_scores.get(bucket["id"])
                if semantic_score is not None:
                    total += semantic_score * self.w_semantic
                    weight_sum += self.w_semantic
                # Dim 7: BM25 TF-IDF 关键词分（rank_bm25+jieba，软依赖，缺包时 bm25_scores={}）
                if bm25_scores:
                    total += bm25_scores.get(bucket["id"], 0.0) * self.w_bm25
                    weight_sum += self.w_bm25
                # Normalize to 0~100 for readability
                normalized = (total / weight_sum) * 100 if weight_sum > 0 else 0

                # 字面命中加分 + 召回保障：修复短查询（如 2 字"杭州"）即使正文里有也
                # 因加权分被各维度稀释到 fuzzy_threshold 以下而整条搜不到。
                # 用户显式搜的词必须召回，故 literal_hit 直接放行（OR），并给排序加分。
                if literal_hit:
                    normalized = min(100.0, normalized + _LITERAL_MATCH_BONUS)

                # Threshold check uses raw (pre-penalty) score so resolved buckets
                # 阈值用原始分数判定，确保 resolved 桶在关键词命中时仍可被搜出
                # remain reachable by keyword (penalty applied only to ranking).
                # ⚠️ 已知设计债：这道门混了两类不同的东西。
                #
                # `normalized` 是七维加权和，其中 topic / bm25 / semantic 回答的是
                # "这条记忆和查询有关吗"，而 emotion / time / importance / touch
                # 回答的是"这条记忆本身怎么样"（新不新、重不重要、被摸过几次）。
                # 两个问题被加成同一个分数，去过同一道门。
                #
                # 2026-08-18 对 917 桶真实记忆扫描过：相关性三维全为 0 却入选的
                # 命中数是 **0**。但那是**算术上的巧合，不是设计上的保证**——
                # 后四维权重合计 3.5/13.5，凑不满 fuzzy_threshold=50 而已。
                # 这几个权重都在 config.scoring 里，谁把 time_weight 从 1.5 调到
                # 4.0，门立刻就漏，而且是静默地漏：不报错、不变慢，只是开始返回
                # "最近、很重要、但跟你问的完全无关"的记忆。
                #
                # 对的形状是把召回与排序分开：
                #     门：  max(topic, bm25, semantic) >= 门槛   ← 只有相关性维度能开门
                #     排序：现在这套七维加权分                    ← 后四维在这里发挥作用
                # 一条相关的记忆因为更新、更重要而排前面完全合理；但它不该因为
                # 新和重要就变得"相关"。
                #
                # 没有立刻改，是因为当前没有故障、且这是召回主路径；真要动需要先
                # 攒一批带标准答案的查询（"我问了什么、期望返回什么"），否则无法
                # 验证新门是不是把该召回的挡在了外面。见 docs/INTERNALS.md §3.1。
                text_match = normalized >= self.fuzzy_threshold or literal_hit
                semantic_match = (
                    semantic_score is not None
                    and semantic_score >= self.vector_recall_threshold
                )
                if text_match or semantic_match:
                    # Resolved buckets get ranking penalty (but still reachable by keyword)
                    # 已解决的桶仅在排序时降权
                    if meta.get("resolved", False):
                        normalized *= _RESOLVED_RANK_PENALTY
                    bucket["score"] = round(normalized, 2)
                    if semantic_match and not text_match:
                        bucket["vector_match"] = True
                    else:
                        bucket.pop("vector_match", None)
                    scored.append(bucket)
            except Exception as e:
                logger.warning(
                    f"Scoring failed for bucket {bucket.get('id', '?')} / "
                    f"桶评分失败: {e}"
                )
                continue

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]

    # ---------------------------------------------------------
    # 四个评分维度的纯函数实现已拆到 ombrebrain.retrieval.bucket_scoring；这里保留同名
    # wrapper 方法 —— 测试和历史调用方一直用 bucket_mgr._calc_xxx_score(...)
    # 这种实例方法写法，wrapper 保持该接口不变，同时让实现本身可独立单测/复用。
    # ---------------------------------------------------------
    def _calc_topic_score(self, query: str, bucket: dict) -> float:
        return calc_topic_score(query, bucket, content_weight=self.content_weight)

    def _calc_emotion_score(
        self, q_valence: Optional[float], q_arousal: Optional[float], meta: dict
    ) -> float:
        return calc_emotion_score(q_valence, q_arousal, meta)

    def _calc_time_score(self, meta: dict) -> float:
        return calc_time_score(meta)

    def _calc_touch_score(self, meta: dict) -> float:
        return calc_touch_score(meta)

    # ---------------------------------------------------------
    # iter 2.0: anchor 系统（坐标系桶，硬上限 24）
    # anchor system — coordinate-system buckets, hard cap of 24
    # ---------------------------------------------------------
    ANCHOR_LIMIT = 24

    async def count_anchors(self) -> int:
        """Return current count of buckets with anchor=True."""
        # 用 list_all 数；规模小（最多 24）所以扫描成本可忽略。
        all_b = await self.list_all(include_archive=False)
        return sum(1 for b in all_b if b.get("metadata", {}).get("anchor"))

    async def set_anchor(self, bucket_id: str, value: bool) -> dict:
        """
        Toggle the anchor flag on a bucket. Hard-rejects if cap reached.
        切换桶的 anchor 标记。设为 True 且当前已满 24 时拒绝。

        Returns: {"ok": bool, "anchor": bool, "count": int, "limit": int, "error": Optional[str]}
        """
        # anchor 上限 24 是「先数后写」的两步操作；没有这把锁，两个并发
        # set_anchor(True) 都能在对方提交前读到同一个 count<limit，一起通过
        # 检查后各自 update()，把总数冲破硬上限。
        async with _filesystem_turn(str(self.base_dir), "quota-anchor"):
            return await self._set_anchor_locked(bucket_id, value)

    async def _set_anchor_locked(self, bucket_id: str, value: bool) -> dict:
        bucket = await self.get(bucket_id)
        if not bucket:
            return {"ok": False, "error": "找不到该记忆桶", "count": 0, "limit": self.ANCHOR_LIMIT}
        current_value = parse_bool(
            bucket["metadata"].get("anchor", False), default=False
        )
        target = parse_bool(value)
        # Idempotent: same state → noop
        if current_value == target:
            count = await self.count_anchors()
            return {"ok": True, "anchor": target, "count": count, "limit": self.ANCHOR_LIMIT, "noop": True}
        if target is True:
            # pinned/protected 与 anchor 互斥：pinned=永远置顶浮现（核心准则），
            # anchor=刻意不浮现（坐标系），两者语义直接矛盾。允许并存会让一个
            # pinned+anchor 桶每会话都以「核心准则」冒头，诱导模型反复 release
            # 却压不住它。这里直接拒绝，提示先 trace(pinned=0) 再改坐标系。
            if bucket["metadata"].get("pinned") or bucket["metadata"].get("protected"):
                return {
                    "ok": False,
                    "error": "这是 pinned 核心准则，不能同时设为 anchor（两者互斥）。要改成坐标系请先 trace(pinned=0)。",
                    "count": await self.count_anchors(),
                    "limit": self.ANCHOR_LIMIT,
                }
            count = await self.count_anchors()
            if count >= self.ANCHOR_LIMIT:
                return {
                    "ok": False,
                    "error": f"anchor 已达上限 {self.ANCHOR_LIMIT}。请先 release 一条再 anchor 新的。",
                    "count": count,
                    "limit": self.ANCHOR_LIMIT,
                }
        # iter 2.0：钉为 anchor 时同步把 source_tool 改为 "anchor"，
        # 释放时恢复为原始来源（保存在 _pre_anchor_source_tool 里）。
        # 这样 dashboard 「按来源筛选」能正确反映桶的当前状态。
        update_kwargs: dict = {"anchor": target}
        bucket_meta = bucket.get("metadata", {})
        if target:
            # 先把当前 source_tool 存为 _pre_anchor_source_tool，再覆写为 "anchor"
            original = bucket_meta.get("source_tool", "")
            update_kwargs["_pre_anchor_source_tool"] = original
            update_kwargs["source_tool"] = "anchor"
        else:
            # 释放：恢复原始 source_tool，清掉临时字段
            original = bucket_meta.get("_pre_anchor_source_tool", "")
            update_kwargs["source_tool"] = original
            update_kwargs["_pre_anchor_source_tool"] = None  # 删除字段
        ok = await self.update(bucket_id, **update_kwargs)
        if not ok:
            return {"ok": False, "error": "更新失败", "count": 0, "limit": self.ANCHOR_LIMIT}
        new_count = await self.count_anchors()
        return {"ok": True, "anchor": target, "count": new_count, "limit": self.ANCHOR_LIMIT}

    async def list_anchors(self) -> list[dict]:
        """Return all buckets with anchor=True, sorted by created ascending."""
        all_b = await self.list_all(include_archive=False)
        anchors = [b for b in all_b if b.get("metadata", {}).get("anchor")]
        anchors.sort(key=lambda b: b.get("metadata", {}).get("created", ""))
        return anchors

    # ---------------------------------------------------------
    # List all buckets
    # 列出所有桶
    # ---------------------------------------------------------
    async def get_triggered_feels(self, source_bucket_id: str) -> list[dict]:
        """
        Return all feel buckets whose triggered_by == source_bucket_id.
        只扫 feel_dir，O(feel桶数) 而非 O(全库)。iter 2.0 §10 U-04 优化反向链查询。
        每条返回 {id, name, created}。
        """
        results = []
        for _root, _fname, file_path in self._iter_md_files([self.feel_dir]):
            bucket = self._load_bucket(file_path)
            if not bucket:
                continue
            meta = bucket.get("metadata", {})
            if meta.get("triggered_by") == source_bucket_id:
                results.append({
                    "id": bucket["id"],
                    "name": meta.get("name") or bucket["id"],
                    "created": meta.get("created", ""),
                })
        results.sort(key=lambda x: x.get("created", ""), reverse=True)
        return results

    async def list_all(self, include_archive: bool = False) -> list[dict]:
        """
        Recursively walk directories (including domain subdirs), list all buckets.
        递归遍历目录（含域子目录），列出所有记忆桶。
        """
        if include_archive:
            buckets = []
            dirs = list(self._active_dirs) + [self.archive_dir]
            for _root, _fname, file_path in self._iter_md_files(dirs):
                bucket = self._load_bucket(file_path)
                if bucket:
                    buckets.append(bucket)
            return buckets

        # Active buckets use a parsed cache, but Obsidian/Git/manual edits may
        # bypass BucketManager.  The build mutex is cross-loop; the short state
        # guard and generation form a CAS with every managed write.  File state
        # is scanned both before and after parsing so an external editor cannot
        # make us publish old content paired with a new fingerprint.
        async with self._active_cache_lock:
            previous_cache: list[dict] | None = None
            while True:
                now = time.monotonic()
                with self._active_cache_state_guard:
                    generation = self._active_cache_generation
                    cached = self._active_cache
                    if cached is not None:
                        poll_due = (
                            self.external_change_poll_seconds == 0
                            or now - self._last_file_state_check
                            >= self.external_change_poll_seconds
                        )
                        if not poll_due:
                            return [dict(bucket) for bucket in cached]
                        cached_state = dict(self._active_file_state)
                    else:
                        poll_due = False
                        cached_state = {}

                if cached is not None and poll_due:
                    current_state = self._scan_active_file_state()
                    with self._active_cache_state_guard:
                        if generation != self._active_cache_generation:
                            previous_cache = None
                            continue
                        self._last_file_state_check = now
                        if current_state == cached_state:
                            current_cache = self._active_cache
                            if current_cache is not None:
                                return [dict(bucket) for bucket in current_cache]
                            continue

                        previous_cache = [dict(bucket) for bucket in cached]
                        self._active_cache_generation += 1
                        generation = self._active_cache_generation
                        self._active_cache = None
                        self._active_file_state = {}
                        self._bm25_dirty = True
                        self._external_changes_detected += 1
                        self._last_external_change = now_iso()
                        with self._bucket_path_index_guard:
                            self._bucket_path_index_ready = False
                            self._bucket_path_index = {}

                with self._active_cache_state_guard:
                    generation = self._active_cache_generation

                state_before = self._scan_active_file_state()
                buckets = []
                for _root, _fname, file_path in self._iter_md_files(
                    self._active_dirs
                ):
                    bucket = self._load_bucket(file_path)
                    if bucket:
                        buckets.append(bucket)
                state_after = self._scan_active_file_state()

                if state_before != state_after:
                    await asyncio.sleep(0)
                    continue

                with self._active_cache_state_guard:
                    if generation != self._active_cache_generation:
                        previous_cache = None
                        continue
                    self._active_cache = [dict(bucket) for bucket in buckets]
                    self._active_file_state = state_after
                    self._last_file_state_check = time.monotonic()

                if previous_cache is not None:
                    self._reconcile_external_changes(previous_cache, buckets)
                return buckets

    # ---------------------------------------------------------
    # Statistics (counts per category + total size)
    # 统计信息（各分类桶数量 + 总体积）
    # ---------------------------------------------------------
    async def get_stats(self) -> dict:
        """
        Return memory bucket statistics (including domain subdirs).
        返回记忆桶的统计数据。
        """
        stats: dict[str, Any] = {
            "permanent_count": 0,
            "dynamic_count": 0,
            "archive_count": 0,
            "feel_count": 0,
            "plan_count": 0,
            "letter_count": 0,
            "total_size_kb": 0.0,
            "domains": {},
        }

        for subdir, key in [
            (self.permanent_dir, "permanent_count"),
            (self.dynamic_dir, "dynamic_count"),
            (self.archive_dir, "archive_count"),
            (self.feel_dir, "feel_count"),
            (self.plan_dir, "plan_count"),
            (self.letter_dir, "letter_count"),
        ]:
            if not os.path.exists(subdir):
                continue
            for root, _, files in os.walk(subdir):
                for f in files:
                    if f.endswith(".md"):
                        stats[key] += 1
                        fpath = os.path.join(root, f)
                        try:
                            stats["total_size_kb"] += os.path.getsize(fpath) / 1024
                        except OSError:
                            pass
                        # Per-domain counts / 每个域的桶数量
                        domain_name = os.path.basename(root)
                        if domain_name != os.path.basename(subdir):
                            stats["domains"][domain_name] = stats["domains"].get(domain_name, 0) + 1

        return stats

    # ---------------------------------------------------------
    # Archive bucket (move from permanent/dynamic into archive)
    # 归档桶（从 permanent/dynamic 移入 archive）
    # Called by decay engine to simulate "forgetting"
    # 由衰减引擎调用，模拟"遗忘"
    # ---------------------------------------------------------
    async def archive(self, bucket_id: str) -> bool:
        """
        Move a bucket into the archive directory (preserving domain subdirs).
        将指定桶移入归档目录（保留域子目录结构）。
        """
        async with self._bucket_turn(bucket_id):
            return await self._archive_locked(bucket_id)

    async def _archive_locked(self, bucket_id: str) -> bool:
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False

        try:
            # Read once, get domain info and update type / 一次性读取
            post = frontmatter.load(file_path)
            domain: list[str] = post.get("domain") or [_DEFAULT_DOMAIN_NAME]  # type: ignore[assignment]
            primary_domain = self._primary_domain(domain)
            archive_subdir = os.path.join(self.archive_dir, primary_domain)
            os.makedirs(archive_subdir, exist_ok=True)

            dest = safe_path(archive_subdir, os.path.basename(file_path))
            # 防撞名：archive/ 里已有同名文件时，追加 bucket_id 后缀，避免
            # 把一条早先归档的记忆悄悄覆盖掉（与 delete() 的软删除保护一致）。
            if os.path.exists(dest) and os.path.abspath(dest) != os.path.abspath(file_path):
                stem = os.path.splitext(os.path.basename(file_path))[0]
                dest = safe_path(archive_subdir, f"{stem}_{bucket_id}.md")

            # Commit the archived metadata at the destination before removing
            # the untouched source.  A failed move must not leave an
            # ``type=archived`` file stranded in an active directory.
            post["type"] = "archived"
            self._commit_bucket_update(
                file_path,
                str(dest),
                frontmatter.dumps(post),
            )
        except Exception as e:
            logger.error(
                f"Failed to archive bucket / 归档桶失败: {bucket_id}: {e}"
            )
            return False

        self._invalidate_bm25()
        logger.info(f"Archived bucket / 归档记忆桶: {bucket_id} → archive/{primary_domain}/")
        self._record_v3_bucket_event(
            "archive",
            bucket_id,
            str(post.get("type") or "archived"),
            post.content or "",
            dict(post.metadata),
        )
        self._record_ledger_event(
            "TraceArchived",
            bucket_id,
            str(post.get("type") or "archived"),
            post.content or "",
            dict(post.metadata),
        )
        return True

    # ---------------------------------------------------------
    # iter 1.8: 收集全库已有 tag 集合，用于 first_of_kind 检测
    # Collect all tags currently in the vault (excluding archive)
    # 返回 set[str]；空 vault 返回空 set；遇异常返回 None 提示调用方放弃
    # ---------------------------------------------------------
    def _collect_all_tags(self) -> Optional[set]:
        tags = set()
        # 不包括 archive：归档桶代表“过去”，不应阻止“第一次”判定
        # archive_dir is excluded — archived buckets are "the past", they
        # shouldn't block a tag from being marked first_of_kind today.
        for _root, _fname, full_path in self._iter_md_files(self._active_dirs):
            try:
                post = frontmatter.load(full_path)
                for t in list(post.get("tags") or []):  # type: ignore[call-overload]
                    if t:
                        tags.add(str(t))
            except Exception:
                # 单个桶解析失败不影响整体；first_of_kind 是软特性
                continue
        return tags

    # ---------------------------------------------------------
    # Internal: find bucket file across all three directories
    # 内部：在三个目录中查找桶文件
    # ---------------------------------------------------------
    def _ensure_bucket_path_index(self) -> None:
        """Build the complete ID → path index once for bulk conflict checks."""

        with self._bucket_path_index_guard:
            if self._bucket_path_index_ready:
                return
            # 含 archive：软删除后的桶仍然需要可被内部路径查找。
            dirs = [
                self.permanent_dir,
                self.dynamic_dir,
                self.archive_dir,
                self.feel_dir,
                self.plan_dir,
                self.letter_dir,
            ]
            index: dict[str, str] = {}
            for _root, fname, full_path in self._iter_md_files(dirs):
                stem = fname[:-3]
                index.setdefault(stem, full_path)
                try:
                    post = frontmatter.load(full_path)
                    stored_id = str(post.get("id") or "")
                except Exception:
                    stored_id = ""
                if stored_id:
                    index.setdefault(stored_id, full_path)
            self._bucket_path_index = index
            self._bucket_path_index_ready = True

    def _find_bucket_file(self, bucket_id: str) -> Optional[str]:
        """
        Recursively search permanent/dynamic/archive for a bucket file
        matching the given ID.
        在 permanent/dynamic/archive 中递归查找指定 ID 的桶文件。
        """
        if not bucket_id:
            return None

        with self._bucket_path_index_guard:
            path = self._bucket_path_index.get(bucket_id)
            if path and os.path.isfile(path):
                return path
            if path:
                # An unmanaged move/delete can invalidate a hit before the
                # periodic full-vault poll.  Drop it; the next poll rebuilds
                # the complete index and discovers any replacement location.
                self._bucket_path_index.pop(bucket_id, None)
                self._bucket_path_index_ready = False
            index_ready = self._bucket_path_index_ready
        if index_ready:
            return None

        # Preserve the cheap common path for ordinary single-bucket CRUD: most
        # managed filenames contain the ID, so there is no reason to parse the
        # full vault merely to serve one get/update.  Bulk migration explicitly
        # prewarms the complete index via _ensure_bucket_path_index().
        dirs = [
            self.permanent_dir,
            self.dynamic_dir,
            self.archive_dir,
            self.feel_dir,
            self.plan_dir,
            self.letter_dir,
        ]
        for _root, fname, full_path in self._iter_md_files(dirs):
            stem = fname[:-3]
            if stem == bucket_id or stem.endswith(f"_{bucket_id}"):
                with self._bucket_path_index_guard:
                    self._bucket_path_index[bucket_id] = full_path
                return full_path

        # Imported/renamed files may not encode their ID in the filename.
        self._ensure_bucket_path_index()
        with self._bucket_path_index_guard:
            path = self._bucket_path_index.get(bucket_id)
            return path if path and os.path.isfile(path) else None

    # ---------------------------------------------------------
    # Internal: load bucket data from .md file
    # 内部：从 .md 文件加载桶数据
    # ---------------------------------------------------------
    _sanitize_text = staticmethod(_mn._sanitize_text)

    @staticmethod
    def _sanitize_quotes(value: Any) -> list[dict[str, str]]:
        """归一化 + 清洗引语，返回可直接写进 frontmatter 的结构。

        分工：`normalize_quotes` 管结构、条数与长度（超限直接 raise，不截断，
        因为截断过的引语已经不是原话）；这里只补 F-04 控制字符清洗。
        清洗只会让文本变短，所以不会绕过上面的长度校验。
        """
        from ombrebrain.storage.quote_store import normalize_quotes

        cleaned: list[dict[str, str]] = []
        for quote in normalize_quotes(value):
            entry = {
                key: BucketManager._sanitize_text(text).strip()
                for key, text in quote.items()
            }
            # 整条内容都是控制字符时 text 会被清空——那不是一句话，丢掉。
            if entry.get("text"):
                cleaned.append({key: text for key, text in entry.items() if text})
        return cleaned

    @staticmethod
    def _merge_quotes(
        existing: Any, incoming: Any
    ) -> tuple[list[dict[str, str]], int]:
        """合并两组引语，返回 (结果, 因超上限被丢弃的条数)。

        合并到已有桶时两边的引语都该留下——每条引语属于它自己的那个时刻，
        不因为两段记忆被合并就作废。但上限仍然要守，否则反复合并就能
        无限累积，这个功能会变回"存原文"。

        超出时保留**先来的**：早先记住的那几句是更早那个时刻的判断，
        新来的引语至少还在当次调用的返回里说明被丢弃了。

        已存在的引语用宽容读取（磁盘上的数据可能被手工编辑坏），
        本次传入的用严格校验——只有当下这次输入才该收到明确的报错。
        """
        from ombrebrain.storage.quote_store import MAX_QUOTES, quotes_from_metadata

        groups = (
            quotes_from_metadata({"quotes": existing}),
            BucketManager._sanitize_quotes(incoming),
        )
        merged: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for group in groups:
            for quote in group:
                key = (quote["text"], quote.get("speaker", ""))
                if key in seen:
                    continue
                seen.add(key)
                merged.append(quote)
        if len(merged) <= MAX_QUOTES:
            return merged, 0
        return merged[:MAX_QUOTES], len(merged) - MAX_QUOTES

    @staticmethod
    def _sanitize_float_field(value, default: float, field: str = "", source: str = "") -> float:
        """从任意格式提取 float（兼容 'V0.9'、'[我的视角:V0.3]'、0.9 等老格式）。

        越界会钳制到 [0,1]，**并且说出来**——importance 一直会报 OB-W001，而这几个
        字段过去是静默的。手改过桶文件的人无从知道自己写的 valence=99 变成了 1.0，
        而 Markdown 可手改正是这套存储的卖点。

        唯一的调用方在 `_load_bucket`，且外面套着 `if field in metadata`：能走到
        这里就说明这个键**写在文件里**。所以取不出数字时回退到默认值也要报——
        那不是「没写」，是「写了但读不懂」。
        """
        def _report(detail: str) -> float:
            if field:
                _ob_push_warning("OB-W001", f"{detail}（{source or 'load'}）")
            return default

        def _clamped(numeric: float) -> float:
            out = max(0.0, min(1.0, numeric))
            if out != numeric and field:
                _ob_push_warning(
                    "OB-W001",
                    f"{field}={numeric} 超出 [0,1]，已修正为 {out}（{source or 'load'}）",
                )
            return out

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            numeric = float(value)
            if not math.isfinite(numeric):
                return _report(f"{field}={value!r} 不是有限数，回退为 {default}")
            return _clamped(numeric)
        if value is None:
            # 上游 _normalize_metadata_value 把 inf/nan 归一成了 None——
            # 到这里已经看不出原值，但「这个键坏了」这件事必须留下来。
            return _report(f"{field} 不是有效数值，回退为 {default}")
        try:
            nums = re.findall(r'[-+]?\d*\.?\d+', str(value))
            if not nums:
                return _report(f"{field}={value!r} 里没有数字，回退为 {default}")
            numeric = float(nums[0])
            if not math.isfinite(numeric):
                return _report(f"{field}={value!r} 不是有限数，回退为 {default}")
            return _clamped(numeric)
        except Exception:
            return _report(f"{field}={value!r} 无法解析为数值，回退为 {default}")

    _normalize_metadata_value = staticmethod(_mn._normalize_metadata_value)

    def _load_bucket(self, file_path: str) -> Optional[dict]:
        """
        Parse a Markdown file and return structured bucket data.
        解析 Markdown 文件，返回桶的结构化数据。
        """
        try:
            post = frontmatter.load(file_path)
            # Normalize the metadata object as one graph so aliases shared by
            # different top-level keys cannot reset the node/depth budgets.
            metadata = self._normalize_metadata_value(dict(post.metadata))
            domain_value = metadata.get("domain")
            if isinstance(domain_value, str):
                metadata["domain"] = [domain_value] if domain_value.strip() else []
            elif domain_value is None:
                metadata["domain"] = []
            elif not isinstance(domain_value, list):
                metadata["domain"] = list(domain_value) if isinstance(domain_value, tuple) else [str(domain_value)]
            # 兼容老桶可能存储了 'V0.9'、'[我的视角:V0.3]' 等字符串格式
            for field, default in (
                ("valence", 0.5),
                ("arousal", 0.3),
                ("model_valence", 0.5),
                ("weight", 0.5),
            ):
                if field in metadata:
                    metadata[field] = self._sanitize_float_field(
                        metadata[field], default, field, f"load:{Path(file_path).name}"
                    )
            # YAML is an external input boundary (manual files, migration ZIP,
            # GitHub restore).  Never let arbitrary scalar strings reach JSON
            # consumers that treat these fields as numbers.
            metadata["importance"] = _clamp_importance(
                metadata.get("importance", 5), f"load:{Path(file_path).name}"
            )
            try:
                activation_count = float(metadata.get("activation_count", 0) or 0)
                if not math.isfinite(activation_count) or activation_count < 0:
                    raise ValueError("invalid activation_count")
                metadata["activation_count"] = (
                    int(activation_count)
                    if activation_count.is_integer()
                    else round(activation_count, 3)
                )
            except (TypeError, ValueError, OverflowError):
                metadata["activation_count"] = 0
            # Defense in depth: future scalar branches must not accidentally
            # reintroduce NaN/bytes/set values to web JSON consumers.
            json.dumps(metadata, ensure_ascii=False, allow_nan=False)
            return {
                "id": post.get("id", Path(file_path).stem),
                "metadata": metadata,
                # 手改过的文件可能带控制字符/bidi 覆写。写入路径处处 _sanitize_text，
                # 读取路径过去是直接透传的——而「文件可以手改」正是这套存储的卖点。
                "content": self._sanitize_text(post.content),
                "path": file_path,
            }
        except Exception as e:
            logger.warning(
                f"Failed to load bucket file / 加载桶文件失败: {file_path}: {e}"
            )
            return None
