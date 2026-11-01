"""
========================================
migrate_engine.py — 完整记忆包导入引擎
========================================

把 /api/export 产生的 zip 包（buckets/*.md + sources/* + embeddings.db +
可选 you/you.sqlite3 + export_meta.json）
以增量 merge 方式写入当前系统。

关键行为：
- 解析 zip，识别 bucket 文件，读取 export_meta.json 中的 embedding 模型信息
- 对比导入包与当前系统的 embedding 模型，决定是否保留向量数据
- 检测 bucket ID 冲突，返回冲突列表等待她/他决策
- 冲突决策：skip（跳过）| overwrite（覆盖）| keep_both（保留两者，重分配 ID）
- embedding 模型一致 → 合并向量数据；不一致 → 仅导入 md 文件，完成后自动重新向量化

状态机：idle → parsing → parsed → applying → reindexing → done | error

不做什么：
- 不调用 LLM（不做内容解析/摘要/打标，只做文件迁移）
- 不修改 config
- 不做对话历史解析（那是 import_memory.py 的事）
- 不做 embedding 后端切换（backend 换成 local/api 时的全库重算是
  migration_engine.py 的事——两个文件名高度相似，改代码前务必确认
  自己改的是哪一个：这里是"导入别的 OB 实例导出的完整备份包"，
  migration_engine.py 是"给当前库所有记忆重新生成向量"）

对外暴露：MigrateEngine 类（被 server.py 实例化并注入路由）
========================================
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Optional

import frontmatter

from ombrebrain.storage.backup_archive import (
    BackupArchiveError,
    MIGRATE_MAX_SOURCE_BYTES,
    extract_backup_archive_file,
    validate_sqlite_bytes,
    validate_sqlite_file,
)
from ombrebrain.storage.source_store import (
    SOURCE_REF_RE,
    SourceStore,
    referenced_source_ids_from_metadata,
)
from ombrebrain.storage.relation_store import normalize_relation_links
from ombrebrain.them.store import (
    ThemStoreError,
    validate_them_snapshot_bytes,
    validate_them_snapshot_file,
)
from ombrebrain.you.store import (
    YouStoreError,
    validate_you_snapshot_bytes,
    validate_you_snapshot_file,
)
from ombrebrain.storage.vector_codec import decode_vector, encode_vector

try:
    from utils import (  # type: ignore
        _win_long_path,
        now_iso,
        publish_new_file,
        safe_path,
        sanitize_name,
    )
except ImportError:  # pragma: no cover
    from .utils import (  # type: ignore
        _win_long_path,
        now_iso,
        publish_new_file,
        safe_path,
        sanitize_name,
    )

logger = logging.getLogger("ombre_brain.migrate")

# ============================================================
# 状态常量
# ============================================================
PHASE_IDLE = "idle"
PHASE_PARSING = "parsing"
PHASE_PARSED = "parsed"
PHASE_APPLYING = "applying"
PHASE_REINDEXING = "reindexing"
PHASE_DONE = "done"
PHASE_ERROR = "error"

# bucket type → 存储子目录映射（与 bucket_manager.py 保持一致）
_TYPE_SUBDIR: dict[str, str] = {
    "permanent": "permanent",
    "dynamic": "dynamic",
    "archive": "archive",
    "archived": "archive",
    "feel": "feel",
    "plan": "plans",
    "letter": "letters",
}

# 默认子目录（unknown type 时）
_DEFAULT_SUBDIR = "dynamic"
_DEFAULT_MAX_BUCKET_BYTES = 50 * 1024
_DEFAULT_MAX_METADATA_BYTES = 16 * 1024
_MAX_UNLIMITED_MIGRATE_BUCKET_BYTES = 8 * 1024 * 1024
_MAX_UNLIMITED_MIGRATE_METADATA_BYTES = 1024 * 1024
_FRONTMATTER_OVERHEAD_BYTES = 64 * 1024
_EMBEDDING_FETCH_BATCH = 32
_MAX_EMBEDDING_CELL_BYTES = 1024 * 1024
_MAX_EMBEDDING_DIMENSIONS = 65_536
_MAX_EMBEDDING_ROWS = 10_000
_MAX_EMBEDDING_TIMESTAMP_BYTES = 256
_MAX_EMBEDDING_HASH_BYTES = 256
_PARSED_WORKSPACE_TTL_SECONDS = 3600.0
_PARSED_WORKSPACE_SWEEP_SECONDS = 60.0

_MIGRATE_ENGINES: weakref.WeakSet[Any] = weakref.WeakSet()
_MIGRATE_ENGINES_GUARD = threading.Lock()
_MIGRATE_SWEEPER_STARTED = False


def _register_migrate_engine(engine: Any) -> None:
    """Register an engine with one process-wide, weak-reference TTL sweeper."""

    global _MIGRATE_SWEEPER_STARTED
    with _MIGRATE_ENGINES_GUARD:
        _MIGRATE_ENGINES.add(engine)
        if _MIGRATE_SWEEPER_STARTED:
            return
        _MIGRATE_SWEEPER_STARTED = True

    def sweep() -> None:
        while True:
            time.sleep(_PARSED_WORKSPACE_SWEEP_SECONDS)
            with _MIGRATE_ENGINES_GUARD:
                engines = list(_MIGRATE_ENGINES)
            now = time.monotonic()
            for candidate in engines:
                try:
                    candidate._expire_parsed_workspace(now)
                except Exception as exc:
                    logger.warning("[migrate] parsed workspace TTL cleanup failed: %s", exc)

    try:
        threading.Thread(
            target=sweep,
            name="ombre-migrate-workspace-sweeper",
            daemon=True,
        ).start()
    except RuntimeError as exc:
        # Status/reservation calls also run the expiry check, so a constrained
        # runtime that cannot start this daemon still has a safe lazy fallback.
        logger.warning("[migrate] could not start workspace TTL sweeper: %s", exc)
        with _MIGRATE_ENGINES_GUARD:
            _MIGRATE_SWEEPER_STARTED = False


# ============================================================
# 数据类
# ============================================================

@dataclass
class _ParsedBucket:
    """zip 内解析到的单个 bucket 文件。"""
    bucket_id: str
    arc_path: str        # zip 内路径，e.g. "buckets/dynamic/foo/name_id.md"
    md_bytes: bytes | None  # compatibility path; production imports use md_path
    name: str
    bucket_type: str
    domain: list[str]
    created: str
    md_path: str = ""     # disk-backed extracted member owned by MigrateEngine


@dataclass
class ConflictInfo:
    """导入包内某 bucket_id 与当前系统冲突的描述。"""
    bucket_id: str
    import_name: str
    import_created: str
    current_name: str
    current_created: str


# ============================================================
# 辅助函数
# ============================================================

def _safe_unlink(path: str) -> None:
    """尽力删除一个暂存文件；失败只记日志，不让清理动作掩盖真正的异常。"""
    try:
        if path and os.path.exists(path):
            os.unlink(path)
    except OSError as e:
        logger.warning(f"[migrate] failed to clean up staged file {path}: {e}")


def _is_same_file(left: str, right: str) -> bool:
    """判断两个路径是不是同一个文件，兼容大小写不敏感的文件系统。

    只做字符串比较在 macOS / Windows 上会漏判：桶渲染出的 `Memory_x.md` 与
    磁盘上已有的 `memory_x.md` 落在同一个 inode 上，但 `os.path.normcase`
    在 POSIX 上是恒等函数、不折叠大小写，于是覆盖导入会判成「不是同一个文件，
    可目标又已存在」而直接抛 FileExistsError——旧记忆没归档、新内容没写进去，
    功能在这两个平台上整个不可用（Linux 因为大小写敏感反而绕开了）。

    先比字符串是为了覆盖目标尚不存在的正常新建路径（此时 samefile 必然抛错）；
    比不上再用 samefile 比 st_dev/st_ino，这才是「是不是同一个文件」的真答案。
    """

    if os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    ):
        return True
    try:
        return os.path.samefile(left, right)
    except OSError:
        # 任一路径不存在或不可 stat：那就不是同一个文件。
        return False


@asynccontextmanager
async def _noop_bucket_turn():
    yield


async def _to_thread_reaped(function: Any, *args: Any) -> Any:
    """Run sync work without letting cancellation orphan the worker thread.

    ``asyncio.to_thread`` only cancels its awaiter.  The underlying thread keeps
    running, so cleaning the migration workspace immediately would race that
    worker.  Shield it and absorb repeated task cancellations until it exits;
    only then propagate cancellation to the caller.
    """

    worker = asyncio.create_task(asyncio.to_thread(function, *args))
    cancelled = False
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        try:
            worker.result()
        except BaseException:
            pass
        raise asyncio.CancelledError
    return worker.result()


def _parse_md_meta(raw: bytes) -> tuple[dict, str]:
    """从 md 字节中解析 frontmatter 元数据 + 正文。失败返回空 dict + 空串。"""
    try:
        post = frontmatter.loads(raw.decode("utf-8", errors="replace"))
        return dict(post.metadata), post.content
    except Exception:
        return {}, ""


def _safe_str(val: Any, max_len: int = 512) -> str:
    """安全地将值转为字符串，并截断。"""
    return str(val)[:max_len] if val is not None else ""


# ============================================================
# MigrateEngine
# ============================================================

class MigrateEngine:
    """完整记忆包（zip）导入引擎。每个服务进程单例使用；同一时刻只允许一个任务。"""

    def __init__(self, config: dict, bucket_mgr: Any, embedding_engine: Any) -> None:
        self._config = config
        self._bucket_mgr = bucket_mgr
        self._embedding_engine = embedding_engine
        self._state_guard = threading.RLock()

        # ---- 状态 ----
        self._phase: str = PHASE_IDLE
        self._job_id: str = ""
        self._apply_reservation: str = ""

        # ---- 解析阶段产物 ----
        self._parsed_buckets: list[_ParsedBucket] = []
        self._conflicts: list[ConflictInfo] = []
        self._conflict_ids_at_parse: frozenset[str] = frozenset()
        self._import_model: str = ""
        self._import_model_dim: int = 0
        self._import_backend: str = ""
        self._has_embeddings: bool = False
        self._zip_db_bytes: Optional[bytes] = None
        self._zip_db_path: str = ""
        self._source_members: dict[str, bytes | str] = {}
        self._you_db_bytes: Optional[bytes] = None
        self._you_db_path: str = ""
        self._you_service: Any = None
        self._you_tool_gate: Any = None
        self._them_db_bytes: Optional[bytes] = None
        self._them_db_path: str = ""
        self._them_service: Any = None
        self._them_tool_gate: Any = None
        self._parse_temp_dir: str = ""
        self._parsed_at_monotonic: float = 0.0
        self._total_buckets: int = 0
        self._integrity_verified: bool = False
        self._integrity_warning: str = ""
        self._backup_manifest: Optional[dict[str, Any]] = None

        # ---- 执行阶段计数 ----
        self._apply_total: int = 0
        self._apply_done: int = 0
        self._apply_imported: int = 0
        self._apply_skipped: int = 0
        self._apply_errors: list[str] = []

        # ---- 重新向量化阶段 ----
        self._reindex_total: int = 0
        self._reindex_done: int = 0
        self._reindex_errors: int = 0
        self._buckets_to_reindex: list[tuple[str, str]] = []  # (bucket_id, markdown path)

        # ---- 错误信息 ----
        self._error_message: str = ""

        _register_migrate_engine(self)

    # ----------------------------------------------------------
    # 属性
    # ----------------------------------------------------------

    @property
    def phase(self) -> str:
        with self._state_guard:
            return self._phase

    @property
    def job_id(self) -> str:
        with self._state_guard:
            return self._job_id

    @property
    def is_busy(self) -> bool:
        with self._state_guard:
            return self._phase in (PHASE_PARSING, PHASE_APPLYING, PHASE_REINDEXING)

    def _begin_parse(self) -> str | None:
        """Atomically reserve the singleton parser before its first await."""

        with self._state_guard:
            if self._phase in (PHASE_PARSING, PHASE_APPLYING, PHASE_REINDEXING):
                return None
            self._job_id = uuid.uuid4().hex
            self._apply_reservation = ""
            self._phase = PHASE_PARSING
            return self._job_id

    def reserve_parse(self) -> str | None:
        """Reserve upload+parse before reading an untrusted request body."""

        return self._begin_parse()

    def _cleanup_parse_artifacts(self) -> None:
        """Release disk/memory payloads once they are no longer needed."""

        self._parsed_at_monotonic = 0.0
        temp_dir, self._parse_temp_dir = self._parse_temp_dir, ""
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        self._zip_db_bytes = None
        self._zip_db_path = ""
        self._source_members = {}
        self._you_db_bytes = None
        self._you_db_path = ""
        self._them_db_bytes = None
        self._them_db_path = ""
        for bucket in self._parsed_buckets:
            bucket.md_bytes = None
            bucket.md_path = ""

    def _reset_parse_state(self) -> None:
        self._cleanup_parse_artifacts()
        self._parsed_buckets = []
        self._conflicts = []
        self._conflict_ids_at_parse = frozenset()
        self._import_model = ""
        self._import_model_dim = 0
        self._import_backend = ""
        self._has_embeddings = False
        self._integrity_verified = False
        self._integrity_warning = ""
        self._backup_manifest = None
        self._total_buckets = 0
        self._apply_errors = []
        self._apply_imported = 0
        self._apply_skipped = 0
        self._apply_total = 0
        self._apply_done = 0
        self._buckets_to_reindex = []
        self._reindex_total = 0
        self._reindex_done = 0
        self._reindex_errors = 0
        self._error_message = ""

    def reserve_apply(self, expected_job_id: str) -> str | None:
        """Reserve one parsed generation for exactly one background apply."""

        self._expire_parsed_workspace()
        with self._state_guard:
            if (
                self._phase != PHASE_PARSED
                or not expected_job_id
                or expected_job_id != self._job_id
            ):
                return None
            reservation = uuid.uuid4().hex
            self._apply_reservation = reservation
            self._parsed_at_monotonic = 0.0
            self._phase = PHASE_APPLYING
            return reservation

    def _discard_parsed_locked(self, message: str) -> None:
        """Discard the current parsed generation while ``_state_guard`` is held."""

        self._cleanup_parse_artifacts()
        self._parsed_buckets = []
        self._conflicts = []
        self._conflict_ids_at_parse = frozenset()
        self._buckets_to_reindex = []
        self._apply_reservation = ""
        self._phase = PHASE_ERROR
        self._error_message = str(message)[:500]

    def _expire_parsed_workspace(self, now: float | None = None) -> bool:
        """Generation-bound expiry for an unapplied disk-backed parse."""

        current = time.monotonic() if now is None else float(now)
        with self._state_guard:
            if (
                self._phase != PHASE_PARSED
                or self._parsed_at_monotonic <= 0
                or current - self._parsed_at_monotonic
                < _PARSED_WORKSPACE_TTL_SECONDS
            ):
                return False
            self._discard_parsed_locked(
                "迁移解析结果已超过 1 小时有效期，请重新上传"
            )
            return True

    def abandon_parsed(self, expected_job_id: str, message: str = "迁移已取消") -> bool:
        """Explicitly discard one still-unapplied parsed generation."""

        with self._state_guard:
            if (
                not expected_job_id
                or self._phase != PHASE_PARSED
                or expected_job_id != self._job_id
            ):
                return False
            self._discard_parsed_locked(message)
            return True

    def abandon_apply(self, reservation_id: str, message: str) -> bool:
        """Release an apply reservation when its background task was not scheduled."""

        with self._state_guard:
            if (
                not reservation_id
                or self._phase != PHASE_APPLYING
                or reservation_id != self._apply_reservation
            ):
                return False
            self._apply_reservation = ""
            self._phase = PHASE_ERROR
            self._error_message = str(message)[:500]
        self._cleanup_parse_artifacts()
        self._parsed_buckets = []
        self._buckets_to_reindex = []
        return True

    def _embedding_match(self) -> bool:
        """当前 embedding 模型是否与导入包一致。"""
        if not self._import_model:
            return False
        current_model = str(getattr(self._embedding_engine, "model", "") or "")
        same_model = (
            self._import_model.strip().lower().removeprefix("models/")
            == current_model.strip().lower().removeprefix("models/")
        )
        if not same_model:
            return False
        backend = getattr(self._embedding_engine, "_backend", None)
        try:
            current_dim = int(backend.vector_dim()) if backend else 0
        except Exception:
            current_dim = 0
        return not self._import_model_dim or not current_dim or self._import_model_dim == current_dim

    # ----------------------------------------------------------
    # 状态查询
    # ----------------------------------------------------------

    def get_status(self) -> dict:
        self._expire_parsed_workspace()
        return {
            "phase": self._phase,
            "job_id": self._job_id,
            "total_buckets": self._total_buckets,
            "conflicts_count": len(self._conflicts),
            "conflicts": [
                {
                    "bucket_id": c.bucket_id,
                    "import_name": c.import_name,
                    "import_created": c.import_created,
                    "current_name": c.current_name,
                    "current_created": c.current_created,
                }
                for c in self._conflicts
            ],
            "import_model": self._import_model,
            "import_backend": self._import_backend,
            "current_model": getattr(self._embedding_engine, "model", ""),
            "embedding_match": self._embedding_match(),
            "has_embeddings": self._has_embeddings,
            "integrity_verified": self._integrity_verified,
            "integrity_warning": self._integrity_warning,
            "backup_manifest": {
                "schema_version": self._backup_manifest.get("schema_version"),
                "created_at": self._backup_manifest.get("created_at", ""),
                "version": self._backup_manifest.get("version", ""),
                "file_count": self._backup_manifest.get("file_count", 0),
                "total_bytes": self._backup_manifest.get("total_bytes", 0),
            } if self._backup_manifest else None,
            "apply_progress": {
                "done": self._apply_done,
                "total": self._apply_total,
            },
            "reindex_progress": {
                "done": self._reindex_done,
                "total": self._reindex_total,
                "errors": self._reindex_errors,
            },
            "apply_errors": self._apply_errors[-20:],
            "result": {
                "imported": self._apply_imported,
                "skipped": self._apply_skipped,
            },
            "error": self._error_message,
        }

    # ----------------------------------------------------------
    # 第一步：解析 zip
    # ----------------------------------------------------------

    async def parse_zip(self, zip_bytes: bytes) -> dict:
        """Compatibility byte API; HTTP uploads use disk-backed parse_zip_file."""
        job_id = self._begin_parse()
        if job_id is None:
            return {
                "ok": False,
                "busy": True,
                "error": f"当前状态为 {self._phase}，请等待任务完成后再上传",
            }
        self._reset_parse_state()
        worker = asyncio.create_task(asyncio.to_thread(self._parse_zip_sync, zip_bytes))
        try:
            parsed = await asyncio.shield(worker)
            await self._accept_parsed(parsed)
        except asyncio.CancelledError:
            # A to_thread worker cannot be killed.  Reap it before releasing
            # the singleton reservation so cancellation cannot stack parsers.
            orphan: dict[str, Any] | None = None
            try:
                orphan = await worker
            except Exception:
                pass
            if orphan:
                shutil.rmtree(str(orphan.get("temp_dir") or ""), ignore_errors=True)
            self._cleanup_parse_artifacts()
            self._phase = PHASE_ERROR
            self._error_message = "zip 预检已取消"
            raise
        except Exception as e:
            self._cleanup_parse_artifacts()
            self._phase = PHASE_ERROR
            self._error_message = f"zip 预检失败: {e}"
            logger.error(f"[migrate] parse_zip error: {e}", exc_info=True)
            return {"ok": False, "error": self._error_message}

        with self._state_guard:
            self._phase = PHASE_PARSED
            self._parsed_at_monotonic = time.monotonic()
        return {"ok": True, **self.get_status()}

    async def parse_zip_file(
        self,
        archive_path: str,
        *,
        reservation_id: str | None = None,
    ) -> dict:
        """Parse an uploaded ZIP from disk while retaining members on disk."""

        job_id = reservation_id
        if job_id is None:
            job_id = self._begin_parse()
        with self._state_guard:
            reservation_valid = bool(
                job_id
                and self._phase == PHASE_PARSING
                and job_id == self._job_id
            )
            current_phase = self._phase
        if not reservation_valid:
            return {
                "ok": False,
                "busy": True,
                "error": f"当前状态为 {current_phase}，请等待任务完成后再上传",
            }

        self._reset_parse_state()
        workspace = tempfile.mkdtemp(prefix="ombre-migrate-")
        worker = asyncio.create_task(
            asyncio.to_thread(self._parse_zip_path_sync, archive_path, workspace)
        )
        try:
            parsed = await asyncio.shield(worker)
            parsed["temp_dir"] = workspace
            await self._accept_parsed(parsed)
        except asyncio.CancelledError:
            try:
                await worker
            except Exception:
                pass
            shutil.rmtree(workspace, ignore_errors=True)
            self._cleanup_parse_artifacts()
            self._phase = PHASE_ERROR
            self._error_message = "zip 预检已取消"
            raise
        except Exception as e:
            shutil.rmtree(workspace, ignore_errors=True)
            self._cleanup_parse_artifacts()
            self._phase = PHASE_ERROR
            self._error_message = f"zip 预检失败: {e}"
            logger.error(f"[migrate] parse_zip_file error: {e}", exc_info=True)
            return {"ok": False, "error": self._error_message}

        with self._state_guard:
            self._phase = PHASE_PARSED
            self._parsed_at_monotonic = time.monotonic()
        return {"ok": True, **self.get_status()}

    def abandon_parse(self, reservation_id: str, message: str) -> bool:
        """Release a reserved upload that failed before the parser started."""

        with self._state_guard:
            if (
                not reservation_id
                or self._phase != PHASE_PARSING
                or reservation_id != self._job_id
            ):
                return False
            self._cleanup_parse_artifacts()
            self._phase = PHASE_ERROR
            self._error_message = str(message)[:500]
            return True

    async def _accept_parsed(self, parsed: dict[str, Any]) -> None:
        self._parsed_buckets = parsed["buckets"]
        self._total_buckets = len(self._parsed_buckets)
        self._import_model = parsed["import_model"]
        self._import_model_dim = parsed["import_model_dim"]
        self._import_backend = parsed["import_backend"]
        self._has_embeddings = parsed["has_embeddings"]
        self._zip_db_bytes = parsed.get("db_bytes")
        self._zip_db_path = str(parsed.get("db_path") or "")
        self._source_members = dict(parsed.get("source_members") or {})
        self._you_db_bytes = parsed.get("you_db_bytes")
        self._you_db_path = str(parsed.get("you_db_path") or "")
        self._them_db_bytes = parsed.get("them_db_bytes")
        self._them_db_path = str(parsed.get("them_db_path") or "")
        self._parse_temp_dir = str(parsed.get("temp_dir") or "")
        self._integrity_verified = bool(parsed.get("integrity_verified"))
        self._integrity_warning = str(parsed.get("integrity_warning") or "")
        manifest = parsed.get("manifest")
        self._backup_manifest = (
            {
                "schema_version": manifest.get("schema_version"),
                "created_at": manifest.get("created_at", ""),
                "version": manifest.get("version", ""),
                "file_count": manifest.get("file_count", 0),
                "total_bytes": manifest.get("total_bytes", 0),
            }
            if isinstance(manifest, dict)
            else None
        )
        if not self._parsed_buckets:
            raise BackupArchiveError(
                "zip 内未找到任何 bucket markdown 文件（期望路径前缀：buckets/）"
            )
        await self._identify_conflicts()

    def _configured_limit(self, name: str, default: int, unlimited_cap: int) -> int:
        limits = self._config.get("limits") or {}
        try:
            value = int(limits.get(name, default))
        except (TypeError, ValueError, OverflowError):
            value = default
        return unlimited_cap if value <= 0 else value

    def _bucket_content_limit(self) -> int:
        limits = self._config.get("limits") or {}
        if "max_migrate_bucket_bytes" in limits:
            return self._configured_limit(
                "max_migrate_bucket_bytes",
                _DEFAULT_MAX_BUCKET_BYTES,
                _MAX_UNLIMITED_MIGRATE_BUCKET_BYTES,
            )
        return self._configured_limit(
            "max_bucket_bytes",
            _DEFAULT_MAX_BUCKET_BYTES,
            _MAX_UNLIMITED_MIGRATE_BUCKET_BYTES,
        )

    def _metadata_limit(self) -> int:
        return self._configured_limit(
            "max_metadata_bytes",
            _DEFAULT_MAX_METADATA_BYTES,
            _MAX_UNLIMITED_MIGRATE_METADATA_BYTES,
        )

    def _source_content_limit(self) -> int:
        return min(
            MIGRATE_MAX_SOURCE_BYTES,
            self._configured_limit(
                "max_grow_input_bytes",
                2 * 1024 * 1024,
                MIGRATE_MAX_SOURCE_BYTES,
            ),
        )

    def _normalize_import_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        normalizer = getattr(self._bucket_mgr, "_normalize_metadata_value", None)
        normalized = normalizer(metadata) if callable(normalizer) else metadata
        if not isinstance(normalized, dict):
            raise BackupArchiveError("bucket metadata 必须是对象")
        if "relation_links" in normalized:
            try:
                normalized["relation_links"] = normalize_relation_links(normalized["relation_links"])
            except ValueError as exc:
                raise BackupArchiveError(f"relation_links invalid: {exc}") from exc
        try:
            encoded = json.dumps(
                normalized,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise BackupArchiveError(f"bucket metadata 不是 JSON-safe: {exc}") from exc
        if len(encoded) > self._metadata_limit():
            raise BackupArchiveError(
                f"bucket metadata 过大（{len(encoded)} bytes > {self._metadata_limit()}）"
            )
        return normalized

    @staticmethod
    def _read_member(source: bytes | str, *, limit: int, label: str) -> bytes:
        if isinstance(source, bytes):
            if len(source) > limit:
                raise BackupArchiveError(f"{label} 过大（{len(source)} bytes > {limit}）")
            return source
        try:
            size = os.path.getsize(source)
        except OSError as exc:
            raise BackupArchiveError(f"无法读取 {label}: {exc}") from exc
        if size > limit:
            raise BackupArchiveError(f"{label} 过大（{size} bytes > {limit}）")
        with open(source, "rb") as handle:
            data = handle.read(limit + 1)
        if len(data) > limit or len(data) != size:
            raise BackupArchiveError(f"{label} 读取长度异常")
        return data

    def _parse_zip_sync(self, zip_bytes: bytes) -> dict:
        """Compatibility byte API that still streams decompressed data to disk."""

        workspace = tempfile.mkdtemp(prefix="ombre-migrate-")
        fd, archive_path = tempfile.mkstemp(prefix="ombre-upload-", suffix=".zip")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(zip_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            parsed = self._parse_zip_path_sync(archive_path, workspace)
            parsed["temp_dir"] = workspace
            return parsed
        except Exception:
            shutil.rmtree(workspace, ignore_errors=True)
            raise
        finally:
            _safe_unlink(archive_path)

    def _parse_zip_path_sync(self, archive_path: str, workspace: str) -> dict:
        package = extract_backup_archive_file(archive_path, workspace)
        return self._parse_package(package, disk_backed=True)

    def _parse_package(self, package: dict[str, Any], *, disk_backed: bool) -> dict:
        buckets: list[_ParsedBucket] = []
        import_model = ""
        import_model_dim = 0
        import_backend = ""
        has_embeddings = False
        db_bytes: Optional[bytes] = None
        db_path = ""
        source_members: dict[str, bytes | str] = {}
        you_db_bytes: Optional[bytes] = None
        you_db_path = ""
        them_db_bytes: Optional[bytes] = None
        them_db_path = ""
        referenced_sources: set[str] = set()
        files: dict[str, bytes | str] = package["files"]
        names = set(files)

        # 1) 读取 export_meta.json → 获取 embedding 模型信息
        if "export_meta.json" in names:
            try:
                meta_raw = self._read_member(
                    files["export_meta.json"],
                    limit=self._metadata_limit(),
                    label="export_meta.json",
                )
                meta = json.loads(meta_raw.decode("utf-8"))
                emb_info = meta.get("embedding", {})
                import_model = str(emb_info.get("model", "") or "")
                import_model_dim = int(emb_info.get("dim") or 0)
                import_backend = str(emb_info.get("backend", "") or "")
            except Exception as e:
                logger.warning(f"[migrate] export_meta.json 解析失败，将跳过向量恢复: {e}")

        # 2) 检查是否包含 embeddings.db；损坏快照不能伪装成可恢复索引。
        if "embeddings.db" in names:
            source = files["embeddings.db"]
            if disk_backed:
                db_path = str(source)
                validate_sqlite_file(db_path)
                has_embeddings = os.path.getsize(db_path) > 0
            else:
                db_bytes = bytes(source)
                validate_sqlite_bytes(db_bytes)
                has_embeddings = bool(db_bytes)

        if "you/you.sqlite3" in names:
            you_source = files["you/you.sqlite3"]
            try:
                if disk_backed:
                    you_db_path = str(you_source)
                    validate_you_snapshot_file(you_db_path)
                else:
                    you_db_bytes = bytes(you_source)
                    validate_you_snapshot_bytes(you_db_bytes)
            except YouStoreError as exc:
                raise BackupArchiveError("You 快照结构校验失败") from exc

        if "them/them.sqlite3" in names:
            them_source = files["them/them.sqlite3"]
            try:
                if disk_backed:
                    them_db_path = str(them_source)
                    validate_them_snapshot_file(them_db_path)
                else:
                    them_db_bytes = bytes(them_source)
                    validate_them_snapshot_bytes(them_db_bytes)
            except ThemStoreError as exc:
                raise BackupArchiveError("them 快照结构校验失败") from exc

        # 3) 原文证据按文件名中的内容哈希预检。旧版备份可能完全
        # 没有 sources/，保持可导入并显式告警；但只要包已声称携带证据，
        # 任何损坏都必须在写入记忆之前整包失败。
        for arc_path in sorted(names):
            if not arc_path.startswith("sources/") or not arc_path.endswith(".source"):
                continue
            filename = arc_path.removeprefix("sources/")
            ref = filename[:-len(".source")]
            if "/" in filename or not SOURCE_REF_RE.fullmatch(ref):
                raise BackupArchiveError(f"原文证据路径非法: {arc_path}")
            raw = self._read_member(
                files[arc_path],
                limit=self._source_content_limit(),
                label=arc_path,
            )
            if hashlib.sha256(raw).hexdigest() != ref.removeprefix("src_"):
                raise BackupArchiveError(f"原文证据 SHA-256 校验失败: {arc_path}")
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise BackupArchiveError(f"原文证据不是 UTF-8: {arc_path}") from exc
            source_members[ref] = files[arc_path]

        # 4) 遍历 bucket markdown 文件。任何损坏项都会让整个恢复预检失败，
        # 避免界面显示“成功”但实际静默漏掉记忆。
        seen_ids: set[str] = set()
        for arc_path in sorted(names):
            if not arc_path.startswith("buckets/") or not arc_path.endswith(".md"):
                continue
            try:
                content_limit = self._bucket_content_limit()
                raw = self._read_member(
                    files[arc_path],
                    limit=content_limit + self._metadata_limit() + _FRONTMATTER_OVERHEAD_BYTES,
                    label=arc_path,
                )
                post = frontmatter.loads(raw.decode("utf-8"))
                meta = self._normalize_import_metadata(dict(post.metadata))
                referenced_sources.update(referenced_source_ids_from_metadata(meta))
                content_size = len((post.content or "").encode("utf-8"))
                if content_size > content_limit:
                    raise BackupArchiveError(
                        f"{arc_path} 正文过大（{content_size} bytes > {content_limit}）"
                    )

                bucket_id = str(meta.get("id") or meta.get("bucket_id") or "")
                if not bucket_id:
                    stem = os.path.splitext(os.path.basename(arc_path))[0]
                    parts = stem.rsplit("_", 1)
                    bucket_id = parts[-1] if len(parts) > 1 else stem

                if (
                    not bucket_id
                    or len(bucket_id) > 200
                    or any(ord(char) < 32 for char in bucket_id)
                    or "/" in bucket_id
                    or "\\" in bucket_id
                ):
                    raise BackupArchiveError(f"{arc_path} 的 bucket_id 不安全或为空")
                if bucket_id in seen_ids:
                    raise BackupArchiveError(f"备份中存在重复 bucket_id: {bucket_id}")
                seen_ids.add(bucket_id)

                domain = meta.get("domain") or []
                if isinstance(domain, str):
                    domain = [domain]
                elif not isinstance(domain, list):
                    domain = []
                buckets.append(_ParsedBucket(
                    bucket_id=bucket_id,
                    arc_path=arc_path,
                    md_bytes=None if disk_backed else raw,
                    name=_safe_str(meta.get("name", bucket_id), 200),
                    bucket_type=_safe_str(meta.get("type", "dynamic"), 32),
                    domain=[_safe_str(item, 100) for item in domain],
                    created=_safe_str(meta.get("created", ""), 32),
                    md_path=str(files[arc_path]) if disk_backed else "",
                ))
            except BackupArchiveError:
                raise
            except Exception as e:
                raise BackupArchiveError(f"bucket markdown 无法解析: {arc_path}: {e}") from e

        missing_sources = sorted(referenced_sources - set(source_members))
        integrity_warning = str(package["integrity_warning"] or "")
        if missing_sources:
            warning = (
                f"备份中有 {len(missing_sources)} 个原文证据引用没有对应文件；"
                "这通常来自 v2.10.0 及更早的备份，事件记忆仍可恢复，但这些原文无法核对"
            )
            integrity_warning = "; ".join(
                part for part in (integrity_warning, warning) if part
            )

        return {
            "buckets": buckets,
            "import_model": import_model,
            "import_model_dim": import_model_dim,
            "import_backend": import_backend,
            "has_embeddings": has_embeddings,
            "db_bytes": db_bytes,
            "db_path": db_path,
            "source_members": source_members,
            "you_db_bytes": you_db_bytes,
            "you_db_path": you_db_path,
            "them_db_bytes": them_db_bytes,
            "them_db_path": them_db_path,
            "integrity_verified": package["integrity_verified"],
            "integrity_warning": integrity_warning,
            "manifest": package["manifest"],
        }

    async def _identify_conflicts(self) -> None:
        """Find parse-time conflicts from one vault snapshot.

        Calling ``get`` once per imported ID turns a legitimate large import
        into an O(imported * existing) filesystem/frontmatter scan.  Production
        managers expose ``list_all``; build one ID map from that single scan.
        The fallback only supports minimal legacy/test managers.
        """
        conflicts: list[ConflictInfo] = []
        existing_by_id: dict[str, dict[str, Any]] = {}
        list_all = getattr(self._bucket_mgr, "list_all", None)
        if callable(list_all):
            try:
                existing_buckets = await list_all(include_archive=True)
            except TypeError:
                existing_buckets = await list_all()
            for existing in existing_buckets or []:
                if not isinstance(existing, dict):
                    continue
                bucket_id = existing.get("id")
                if isinstance(bucket_id, str) and bucket_id:
                    existing_by_id.setdefault(bucket_id, existing)

        for pb in self._parsed_buckets:
            if callable(list_all):
                existing = existing_by_id.get(pb.bucket_id)
            else:
                existing = await self._bucket_mgr.get(pb.bucket_id)
            if existing is not None:
                emeta = existing.get("metadata", {})
                conflicts.append(ConflictInfo(
                    bucket_id=pb.bucket_id,
                    import_name=pb.name,
                    import_created=pb.created,
                    current_name=_safe_str(emeta.get("name", pb.bucket_id), 200),
                    current_created=_safe_str(emeta.get("created", ""), 32),
                ))
        self._conflicts = conflicts
        self._conflict_ids_at_parse = frozenset(
            conflict.bucket_id for conflict in conflicts
        )

    # ----------------------------------------------------------
    # 第二步：执行导入（带冲突决策）
    # ----------------------------------------------------------

    async def apply(
        self,
        decisions: dict[str, str],
        *,
        reservation_id: str | None = None,
    ) -> None:
        """执行导入。

        decisions: {bucket_id: "skip" | "overwrite" | "keep_both"}
        冲突但未出现在 decisions 中的 bucket → 默认 skip（安全优先）。
        无冲突的 bucket 直接导入，无需决策。
        """
        if reservation_id is None:
            reservation_id = self.reserve_apply(self._job_id)
        with self._state_guard:
            apply_valid = bool(
                reservation_id
                and self._phase == PHASE_APPLYING
                and reservation_id == self._apply_reservation
            )
            current_phase = self._phase
        if not apply_valid:
            raise RuntimeError(f"当前状态为 {current_phase}，apply 需要先完成并占用 parse_zip")

        self._apply_total = len(self._parsed_buckets)
        self._apply_done = 0
        self._apply_imported = 0
        self._apply_skipped = 0
        self._apply_errors = []
        self._buckets_to_reindex = []

        embedding_matches = self._embedding_match()
        buckets_dir = self._config.get("buckets_dir", "buckets")
        imported_id_map: dict[str, str] = {}
        imported_files: dict[str, str] = {}
        package_bucket_ids = frozenset(pb.bucket_id for pb in self._parsed_buckets)

        try:
            # 先发布内容寻址的原文，然后才允许任何 bucket 引用落盘。
            # 若证据安装失败，整次 apply 在记忆写入前终止。
            if self._source_members:
                await _to_thread_reaped(self._install_source_members, buckets_dir)
            ensure_path_index = getattr(
                self._bucket_mgr,
                "_ensure_bucket_path_index",
                None,
            )
            if callable(ensure_path_index):
                await _to_thread_reaped(ensure_path_index)
            for pb in self._parsed_buckets:
                try:
                    result = await self._apply_one_bucket(
                        pb,
                        decisions.get(pb.bucket_id, "skip"),
                        buckets_dir,
                        conflicted_at_parse=(
                            pb.bucket_id in self._conflict_ids_at_parse
                        ),
                    )
                    if result is None:
                        self._apply_skipped += 1
                        continue
                    target_id, target_path = result
                    self._apply_imported += 1
                    imported_id_map[pb.bucket_id] = target_id
                    imported_files[target_id] = target_path

                except Exception as e:
                    err_msg = f"[{pb.bucket_id}] {pb.name[:60]}: {e}"
                    logger.error(f"[migrate] apply error: {err_msg}", exc_info=True)
                    self._apply_errors.append(err_msg)
                    self._apply_skipped += 1

                self._apply_done += 1

            # ---- 向量数据处理 ----
            await _to_thread_reaped(
                self._remap_imported_relation_targets, imported_files, imported_id_map,
                package_bucket_ids,
            )
            merged_ids: set[str] = set()
            if embedding_matches and self._has_embeddings and (
                self._zip_db_bytes or self._zip_db_path
            ):
                # 模型与维度一致时复用快照向量。keep_both 会把源 ID 映射到新 ID。
                try:
                    if self._zip_db_path:
                        merged_ids = await _to_thread_reaped(
                            self._merge_embeddings_path,
                            self._zip_db_path,
                            imported_id_map,
                        )
                    else:
                        merged_ids = await _to_thread_reaped(
                            self._merge_embeddings,
                            self._zip_db_bytes or b"",
                            imported_id_map,
                        )
                except Exception as e:
                    message = f"向量快照合并失败，已转入后台重建: {e}"
                    logger.warning("[migrate] %s", message)
                    self._apply_errors.append(message)

            self._buckets_to_reindex = [
                (target_id, path)
                for target_id, path in imported_files.items()
                if target_id not in merged_ids
            ]
            await self._schedule_reindex()

            # 两个模块各自判断有没有快照要装。exact_restore 只算一次：
            # 它问的是「这批记忆是不是按原 ID 完整导入的」，与哪个模块无关。
            exact_restore = (
                len(imported_id_map) == len(self._parsed_buckets)
                and all(source_id == target_id for source_id, target_id in imported_id_map.items())
            )
            for 标签, 有快照, 安装 in (
                ("You", bool(self._you_db_bytes or self._you_db_path),
                 self._install_you_snapshot),
                ("them", bool(self._them_db_bytes or self._them_db_path),
                 self._install_them_snapshot),
            ):
                if not 有快照:
                    continue
                if not exact_restore:
                    self._apply_errors.append(
                        f"{标签} 快照未恢复：记忆未按原 ID 完整导入"
                    )
                    continue
                try:
                    await _to_thread_reaped(安装, buckets_dir)
                except Exception as exc:
                    message = f"{标签} 快照恢复失败，已保留当前状态: {exc}"
                    logger.warning("[migrate] %s", message)
                    self._apply_errors.append(message)

            invalidate = getattr(self._bucket_mgr, "_invalidate_bm25", None)
            if callable(invalidate):
                invalidate()
            self._phase = PHASE_DONE

        except asyncio.CancelledError:
            self._phase = PHASE_ERROR
            self._error_message = "导入任务已取消"
            raise
        except Exception as e:
            self._phase = PHASE_ERROR
            self._error_message = str(e)
            logger.error(f"[migrate] apply failed: {e}", exc_info=True)
        finally:
            with self._state_guard:
                if self._apply_reservation == reservation_id:
                    self._apply_reservation = ""
            self._cleanup_parse_artifacts()
            self._parsed_buckets = []
            self._buckets_to_reindex = []

    def attach_you_runtime(self, service: Any, tool_gate: Any) -> None:
        self._you_service = service
        self._you_tool_gate = tool_gate

    def attach_them_runtime(self, service: Any, tool_gate: Any) -> None:
        self._them_service = service
        self._them_tool_gate = tool_gate

    def _install_module_snapshot(
        self,
        buckets_dir: str,
        *,
        目录名: str,
        文件名: str,
        标签: str,
        源路径: str,
        源字节: Optional[bytes],
        校验,
        service: Any,
        gate: Any,
    ) -> None:
        """把一份模块库快照落进 vault，落定之后同步该模块的 MCP 显隐。

        you 与 them 共用这一套。两边的恢复语义本来就一样，各写一份的结果是
        其中一边慢慢漏掉某个检查——而这里每一个检查都在防一件具体的事：
        符号链接（把写入引到 vault 之外）、先校验后 os.replace（半份库不落地）、
        以及恢复完必须重新对齐工具显隐（库里说开着、清单里没有，反过来更糟）。
        """
        base = os.path.abspath(buckets_dir)
        root = os.path.join(base, 目录名)
        if os.path.lexists(root) and os.path.islink(root):
            raise BackupArchiveError(f"{标签} 恢复目录不能是符号链接")
        os.makedirs(root, exist_ok=True)
        target = os.path.join(root, 文件名)
        if os.path.lexists(target) and os.path.islink(target):
            raise BackupArchiveError(f"{标签} 恢复目标不能是符号链接")
        descriptor, temp_path = tempfile.mkstemp(
            prefix=f"{标签.lower()}-restore-", suffix=".db", dir=root
        )
        try:
            with os.fdopen(descriptor, "wb") as output:
                if 源路径:
                    with open(源路径, "rb") as source:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
                else:
                    output.write(源字节 or b"")
                output.flush()
                os.fsync(output.fileno())
            校验(temp_path)
            os.replace(_win_long_path(temp_path), _win_long_path(target))
        finally:
            _safe_unlink(temp_path)

        if service is None or gate is None:
            return
        # 整库被替换掉了，缓存的开关状态一定是旧的。
        try:
            service.store.invalidate_state_cache()
        except Exception:
            pass
        state = service.status()
        try:
            gate.sync(state.enabled)
        except Exception:
            if state.enabled:
                try:
                    service.set_enabled(False, expected_revision=state.state_revision)
                except Exception:
                    pass
            try:
                gate.sync(False)
            except Exception:
                pass
            raise BackupArchiveError(f"{标签} MCP 状态同步失败")

    def _install_you_snapshot(self, buckets_dir: str) -> None:
        self._install_module_snapshot(
            buckets_dir,
            目录名=".you",
            文件名="you.sqlite3",
            标签="You",
            源路径=self._you_db_path,
            源字节=self._you_db_bytes,
            校验=validate_you_snapshot_file,
            service=self._you_service,
            gate=self._you_tool_gate,
        )

    def _install_them_snapshot(self, buckets_dir: str) -> None:
        self._install_module_snapshot(
            buckets_dir,
            目录名=".them",
            文件名="them.sqlite3",
            标签="them",
            源路径=self._them_db_path,
            源字节=self._them_db_bytes,
            校验=validate_them_snapshot_file,
            service=self._them_service,
            gate=self._them_tool_gate,
        )

    def _install_source_members(self, buckets_dir: str) -> None:
        source_limit = self._source_content_limit()
        store = SourceStore(buckets_dir, max_bytes=source_limit)
        for ref, source in sorted(self._source_members.items()):
            raw = self._read_member(
                source,
                limit=source_limit,
                label=f"sources/{ref}.source",
            )
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise BackupArchiveError(f"原文证据不是 UTF-8: {ref}") from exc
            installed_ref = store.put(content)
            if installed_ref != ref:
                raise BackupArchiveError(f"原文证据内容与引用不匹配: {ref}")

    def _remap_imported_relation_targets(
        self, files: dict[str, str], id_map: dict[str, str], package_bucket_ids: frozenset[str]
    ) -> None:
        """Remap Relation targets without retaining failed in-package edges."""
        for _target_id, path in files.items():
            post = frontmatter.load(path)
            links = normalize_relation_links(post.metadata.get("relation_links"))
            changed = False
            for link in links:
                target_id = link["target_bucket_id"]
                mapped = id_map.get(target_id)
                if mapped and mapped != target_id:
                    link["target_bucket_id"] = mapped
                    changed = True
                elif target_id in package_bucket_ids and not mapped and link["status"] != "detached":
                    link["status"] = "detached"
                    changed = True
                    logger.warning(
                        "[migrate] detached Relation target %s in %s because it was not imported",
                        target_id, _target_id,
                    )
            if changed:
                post["relation_links"] = links
                self._atomic_write(path, frontmatter.dumps(post))

    async def _apply_one_bucket(
        self,
        pb: _ParsedBucket,
        requested_decision: str,
        buckets_dir: str,
        *,
        conflicted_at_parse: bool,
    ) -> tuple[str, str] | None:
        """Recheck and commit one ID while holding its normal mutation lock."""

        turn_factory = getattr(self._bucket_mgr, "_bucket_turn", None)
        turn = turn_factory(pb.bucket_id) if callable(turn_factory) else _noop_bucket_turn()
        async with turn:
            finder = getattr(self._bucket_mgr, "_find_bucket_file", None)
            existing_path = finder(pb.bucket_id) if callable(finder) else None

            # The filesystem state under this lock is authoritative.  A caller
            # cannot forge overwrite/keep_both for an ID that was conflict-free
            # in the parse snapshot: a newly-created collision always wins.
            if existing_path:
                if not conflicted_at_parse:
                    message = (
                        f"[{pb.bucket_id}] apply 时出现新冲突，已跳过；"
                        "请重新解析后再选择冲突策略"
                    )
                    logger.warning("[migrate] %s", message)
                    self._apply_errors.append(message)
                    return None
                if requested_decision == "overwrite":
                    return await _to_thread_reaped(
                        self._overwrite_bucket_transaction,
                        pb,
                        pb.bucket_id,
                        buckets_dir,
                        existing_path,
                    )
                if requested_decision == "keep_both":
                    target_id = str(uuid.uuid4())
                else:
                    return None
            else:
                target_id = pb.bucket_id

            return await _to_thread_reaped(
                self._write_bucket_file,
                pb,
                target_id,
                buckets_dir,
            )

    def _render_bucket(
        self, pb: _ParsedBucket, target_id: str, buckets_dir: str
    ) -> tuple[str, str, str]:
        """（在线程中执行）纯计算：解析 frontmatter，算出目标路径和序列化后的
        markdown。除了 os.makedirs 建目录外不做任何磁盘写入。

        返回 (content, target_path, rendered)。
        """
        raw = self._read_member(
            pb.md_bytes if pb.md_bytes is not None else pb.md_path,
            limit=(
                self._bucket_content_limit()
                + self._metadata_limit()
                + _FRONTMATTER_OVERHEAD_BYTES
            ),
            label=pb.arc_path,
        )
        meta, content = _parse_md_meta(raw)
        meta = self._normalize_import_metadata(meta)
        content_size = len(content.encode("utf-8"))
        if content_size > self._bucket_content_limit():
            raise BackupArchiveError(
                f"{pb.arc_path} 正文过大（{content_size} bytes > {self._bucket_content_limit()}）"
            )

        # 始终写显式 ID；恢复不依赖文件名猜测。
        meta["id"] = target_id

        # 确定目标目录（按类型 + domain）
        btype = str(meta.get("type") or pb.bucket_type or "dynamic")
        subdir = _TYPE_SUBDIR.get(btype, _DEFAULT_SUBDIR)

        # 获取主 domain（与 bucket_manager 保持一致）
        domain = meta.get("domain") or pb.domain or []
        if btype == "feel":
            primary_domain = "沉淀物"
        elif btype == "plan":
            primary_domain = str(meta.get("status", "active") or "active")
        elif btype == "letter":
            primary_domain = "history"
        elif isinstance(domain, list) and domain:
            primary_domain = str(domain[0])
        elif isinstance(domain, str) and domain:
            primary_domain = str(domain)
        else:
            primary_domain = "general"

        primary_domain = sanitize_name(primary_domain)
        target_dir = str(safe_path(buckets_dir, os.path.join(subdir, primary_domain)))
        os.makedirs(target_dir, exist_ok=True)

        safe_id = re.sub(r"[^\w.-]", "_", target_id, flags=re.UNICODE)[:200]
        if not safe_id:
            raise BackupArchiveError("恢复目标 ID 无法生成安全文件名")
        safe_name = sanitize_name(str(meta.get("name") or pb.name or target_id))[:40]
        target_path = str(safe_path(target_dir, f"{safe_name}_{safe_id}.md"))

        # 重新序列化 frontmatter + 正文
        post = frontmatter.Post(content, **meta)
        rendered = frontmatter.dumps(post)
        return content, target_path, rendered

    @staticmethod
    def _atomic_write(path: str, rendered: str) -> None:
        # 用 _win_long_path 前缀绕开 Windows 260 字符 MAX_PATH：sanitize 后的
        # domain 嵌套路径在深层 buckets_dir 下真的会超限（同款问题 utils.
        # atomic_write_text 已经踩过并修过，这里保持一致而不是各写各的）。
        temp_path = f"{path}.{uuid.uuid4().hex}.tmp"
        temp_path_long = _win_long_path(temp_path)
        try:
            with open(temp_path_long, "w", encoding="utf-8") as f:
                f.write(rendered)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path_long, _win_long_path(path))
        finally:
            try:
                if os.path.exists(temp_path_long):
                    os.unlink(temp_path_long)
            except OSError:
                pass

    @staticmethod
    def _atomic_create(path: str, rendered: str) -> None:
        """Atomically create ``path`` while refusing to replace any file."""

        temp_path = f"{path}.{uuid.uuid4().hex}.tmp"
        temp_path_long = _win_long_path(temp_path)
        target_long = _win_long_path(path)
        try:
            with open(temp_path_long, "x", encoding="utf-8") as handle:
                handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
            # Hard-linking a complete same-filesystem staging inode gives us
            # O_EXCL semantics on both POSIX and Windows; os.replace would
            # silently overwrite an unrelated file with the same filename.
            # 硬链接不可用的文件系统（Termux/Android 的 FUSE、部分 NAS/SMB）走
            # publish_new_file 里的 O_CREAT|O_EXCL 兜底，语义一样，不退化成覆盖。
            publish_new_file(temp_path_long, target_long, rendered)
        finally:
            _safe_unlink(temp_path_long)

    def _write_bucket_file(
        self, pb: _ParsedBucket, target_id: str, buckets_dir: str
    ) -> tuple[str, str]:
        """Write one new bucket without replacing an existing path."""
        _content, target_path, rendered = self._render_bucket(pb, target_id, buckets_dir)
        self._atomic_create(target_path, rendered)
        logger.debug(f"[migrate] wrote {target_path} (id={target_id})")
        return target_id, target_path

    def _write_bucket_file_staged(
        self, pb: _ParsedBucket, target_id: str, buckets_dir: str
    ) -> tuple[str, str, str]:
        """（在线程中执行）把新内容写到跟 target_path 同目录的暂存文件，不动
        target_path 本身。

        专供 overwrite 冲突路径使用：写入成功之后调用方才决定要不要碰旧桶，
        写入失败则旧桶完全没被动过。返回 (content, target_path, staged_path)；
        调用方在确认旧桶已安全处理完之后自己 os.replace(staged_path, target_path)。
        """
        content, target_path, rendered = self._render_bucket(pb, target_id, buckets_dir)
        staged_path = f"{target_path}.staging-{uuid.uuid4().hex}"
        self._atomic_write(staged_path, rendered)
        logger.debug(f"[migrate] staged {staged_path} (id={target_id}, target={target_path})")
        return content, target_path, staged_path

    def _write_historical_copy(
        self,
        existing_path: str,
        bucket_id: str,
        buckets_dir: str,
    ) -> str:
        """Create the pre-overwrite version under a unique archived ID."""

        post = frontmatter.load(existing_path)
        new_id = f"{bucket_id[:160]}-superseded-{uuid.uuid4().hex[:12]}"
        post["id"] = new_id
        post["type"] = "archived"
        post["superseded_by"] = bucket_id
        post["archived_at"] = now_iso()
        archive_dir = str(
            getattr(self._bucket_mgr, "archive_dir", "")
            or safe_path(buckets_dir, "archive")
        )
        os.makedirs(archive_dir, exist_ok=True)
        safe_name = sanitize_name(str(post.get("name") or "memory"))[:40]
        target_path = str(safe_path(archive_dir, f"{safe_name}_{new_id}.md"))
        self._atomic_create(target_path, frontmatter.dumps(post))
        return target_path

    def _overwrite_bucket_transaction(
        self,
        pb: _ParsedBucket,
        target_id: str,
        buckets_dir: str,
        existing_path: str,
    ) -> tuple[str, str]:
        """Preserve old data and commit an overwrite with rollback on failure."""

        _content, target_path, staged_path = self._write_bucket_file_staged(
            pb, target_id, buckets_dir
        )
        historical_path = ""
        target_created = False
        same_target = _is_same_file(existing_path, target_path)
        if same_target:
            # 覆盖活动桶时沿用磁盘上现有的文件名拼写与位置，只换内容和元数据。
            # macOS 实测 os.replace 本来就会保留原拼写，但别的大小写不敏感
            # 文件系统未必一致——写明比依赖文件系统行为稳。
            target_path = existing_path
        try:
            if not same_target and os.path.exists(target_path):
                raise FileExistsError(f"恢复目标已存在: {target_path}")
            historical_path = self._write_historical_copy(
                existing_path,
                target_id,
                buckets_dir,
            )

            if same_target:
                os.replace(_win_long_path(staged_path), _win_long_path(target_path))
            else:
                # Publish without replacement, then remove the still-untouched
                # source.  If source removal fails, delete the publication and
                # historical copy; the original remains the sole truth.
                os.link(_win_long_path(staged_path), _win_long_path(target_path))
                target_created = True
                try:
                    os.unlink(_win_long_path(existing_path))
                except Exception:
                    _safe_unlink(target_path)
                    target_created = False
                    _safe_unlink(historical_path)
                    historical_path = ""
                    raise
                _safe_unlink(staged_path)

            outbox = getattr(self._bucket_mgr, "embedding_outbox", None)
            if outbox is not None and callable(getattr(outbox, "discard", None)):
                try:
                    outbox.discard(target_id)
                except Exception as exc:
                    logger.warning("[migrate] failed to discard old outbox item: %s", exc)
            embedding = getattr(self._bucket_mgr, "embedding_engine", None)
            if embedding is not None and callable(getattr(embedding, "delete_embedding", None)):
                try:
                    embedding.delete_embedding(target_id)
                except Exception as exc:
                    logger.warning("[migrate] failed to discard old embedding: %s", exc)
            return target_id, target_path
        except Exception:
            if target_created:
                _safe_unlink(target_path)
            if historical_path:
                _safe_unlink(historical_path)
            raise
        finally:
            _safe_unlink(staged_path)

    def _merge_embeddings(self, db_bytes: bytes, id_map: dict[str, str]) -> set[str]:
        """（在线程中执行）把 zip 内 embeddings.db 的向量合并进当前 db。

        兼容当前 bucket_id/embedding schema 和早期 id/vector schema。
        返回成功恢复向量的目标 bucket ID 集合。
        """
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            tf.write(db_bytes)
            tmp_path = tf.name

        try:
            return self._merge_embeddings_path(tmp_path, id_map)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _merge_embeddings_path(
        self,
        source_db: str,
        id_map: dict[str, str],
    ) -> set[str]:
        """Merge a validated snapshot in bounded, validated batches.

        The SQLite file is untrusted.  Never ``fetchall`` vector cells and never
        copy arbitrary SQLite values into the live index.  SQL ``CASE`` keeps an
        oversized cell out of the Python result entirely; valid JSON vectors are
        normalized before batches are committed.
        """

        current_db = getattr(self._embedding_engine, "db_path", "")
        if not current_db or not os.path.isfile(current_db):
            logger.warning("[migrate] 当前 embeddings.db 路径无效，跳过向量合并")
            return set()

        safe_id_map = {
            source_id: target_id
            for source_id, target_id in id_map.items()
            if (
                isinstance(source_id, str)
                and isinstance(target_id, str)
                and 0 < len(source_id) <= 200
                and 0 < len(target_id) <= 200
            )
        }
        if not safe_id_map:
            return set()

        src = sqlite3.connect(source_db, timeout=30)
        dst = sqlite3.connect(current_db, timeout=30)
        try:
            table = src.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'embeddings'"
            ).fetchone()
            if table is None:
                logger.warning("[migrate] 导入包 embeddings.db 缺少 embeddings 表，跳过")
                return set()

            columns = {
                str(row[1])
                for row in src.execute("PRAGMA table_info(embeddings)")
            }
            if {"bucket_id", "embedding"}.issubset(columns):
                id_column = "bucket_id"
                vector_column = "embedding"
                updated_column = "updated_at" if "updated_at" in columns else None
                hash_column = "content_hash" if "content_hash" in columns else None
            elif {"id", "vector"}.issubset(columns):
                id_column = "id"
                vector_column = "vector"
                updated_column = None
                hash_column = None
            else:
                raise BackupArchiveError("embeddings 表结构无法识别")

            src.execute(
                "CREATE TEMP TABLE ombre_migrate_wanted_ids "
                "(source_id TEXT PRIMARY KEY) WITHOUT ROWID"
            )
            source_ids = list(safe_id_map)
            for offset in range(0, len(source_ids), _EMBEDDING_FETCH_BATCH):
                batch = source_ids[offset:offset + _EMBEDDING_FETCH_BATCH]
                src.executemany(
                    "INSERT INTO ombre_migrate_wanted_ids (source_id) VALUES (?)",
                    ((source_id,) for source_id in batch),
                )

            def bounded_column(column: str | None, limit: int) -> str:
                if column is None:
                    return "'text', 0, ''"
                # Identifiers come only from the fixed schema names above.
                qualified = f"e.{column}"
                return (
                    f"typeof({qualified}), "
                    f"length(CAST({qualified} AS BLOB)), "
                    f"CASE WHEN typeof({qualified}) IN ('text', 'blob') "
                    f"AND length(CAST({qualified} AS BLOB)) <= {limit} "
                    f"THEN {qualified} ELSE NULL END"
                )

            # All interpolated identifiers are selected from the fixed schema
            # names above; user data remains parameterized in the temp table.
            query = (
                f"SELECT e.{id_column}, "  # nosec B608
                f"{bounded_column(vector_column, _MAX_EMBEDDING_CELL_BYTES)}, "
                f"{bounded_column(updated_column, _MAX_EMBEDDING_TIMESTAMP_BYTES)}, "
                f"{bounded_column(hash_column, _MAX_EMBEDDING_HASH_BYTES)} "
                "FROM embeddings AS e "
                f"JOIN ombre_migrate_wanted_ids AS wanted "
                f"ON e.{id_column} = wanted.source_id"
            )
            cursor = src.execute(query)
            expected_dim = self._expected_embedding_dimension()
            fallback_time = now_iso()
            merged: set[str] = set()
            processed = 0
            skipped = 0

            while rows := cursor.fetchmany(_EMBEDDING_FETCH_BATCH):
                normalized_rows: list[tuple[str, str, str, str]] = []
                normalized_ids: list[str] = []
                for row in rows:
                    processed += 1
                    if processed > _MAX_EMBEDDING_ROWS:
                        raise BackupArchiveError("embeddings 行数超过迁移上限")
                    (
                        source_id,
                        vector_type,
                        vector_size,
                        vector_value,
                        updated_type,
                        updated_size,
                        updated_value,
                        hash_type,
                        hash_size,
                        hash_value,
                    ) = row
                    target_id = safe_id_map.get(source_id)
                    normalized_vector = self._normalize_embedding_vector(
                        vector_value,
                        vector_type,
                        vector_size,
                        expected_dim,
                    )
                    updated_at = self._normalize_embedding_text(
                        updated_value,
                        updated_type,
                        updated_size,
                        _MAX_EMBEDDING_TIMESTAMP_BYTES,
                    )
                    content_hash = self._normalize_embedding_text(
                        hash_value,
                        hash_type,
                        hash_size,
                        _MAX_EMBEDDING_HASH_BYTES,
                    )
                    if (
                        target_id is None
                        or normalized_vector is None
                        or updated_at is None
                        or content_hash is None
                    ):
                        skipped += 1
                        if skipped <= 5:
                            logger.warning(
                                "[migrate] 跳过非法或过大的 embedding 行: %r",
                                source_id,
                            )
                        continue
                    normalized_rows.append(
                        (
                            target_id,
                            normalized_vector,
                            updated_at or fallback_time,
                            content_hash,
                        )
                    )
                    normalized_ids.append(target_id)

                if normalized_rows:
                    dst.executemany(
                        """INSERT OR REPLACE INTO embeddings
                           (bucket_id, embedding, updated_at, content_hash)
                           VALUES (?, ?, ?, ?)""",
                        normalized_rows,
                    )
                    dst.commit()
                    merged.update(normalized_ids)
                # Drop the current SQLite payloads before fetchmany builds the
                # next batch; otherwise Python briefly retains two batches.
                rows.clear()

            logger.info(
                "[migrate] 合并了 %d 条 embedding 向量，跳过 %d 条",
                len(merged),
                skipped,
            )
            return merged
        finally:
            src.close()
            dst.close()

    def _expected_embedding_dimension(self) -> int:
        if 0 < self._import_model_dim <= _MAX_EMBEDDING_DIMENSIONS:
            return self._import_model_dim
        backend = getattr(self._embedding_engine, "_backend", None)
        try:
            dimension = int(backend.vector_dim()) if backend else 0
        except (TypeError, ValueError, OverflowError):
            dimension = 0
        return dimension if 0 < dimension <= _MAX_EMBEDDING_DIMENSIONS else 0

    @staticmethod
    def _normalize_embedding_text(
        value: Any,
        value_type: Any,
        declared_size: Any,
        limit: int,
    ) -> str | None:
        if value_type not in {"text", "blob"}:
            return None
        if not isinstance(declared_size, int) or not 0 <= declared_size <= limit:
            return None
        if isinstance(value, bytes):
            try:
                result = value.decode("utf-8")
            except UnicodeDecodeError:
                return None
        elif isinstance(value, str):
            result = value
        else:
            return None
        try:
            encoded_size = len(result.encode("utf-8"))
        except UnicodeEncodeError:
            return None
        if encoded_size > limit or "\x00" in result:
            return None
        return result

    @classmethod
    def _normalize_embedding_vector(
        cls,
        value: Any,
        value_type: Any,
        declared_size: Any,
        expected_dimension: int,
    ) -> str | bytes | None:
        # BLOB 那条路不经过 _normalize_embedding_text——那个函数是给文本用的，
        # 会把二进制向量判成非法然后返回 None，于是整包记忆的向量在迁移时被
        # 静默丢光（向量能重建，但用户会莫名其妙地要重新跑一遍全量向量化）。
        if isinstance(value, (bytes, bytearray, memoryview)):
            if len(bytes(value)) > _MAX_EMBEDDING_CELL_BYTES:
                return None
            try:
                parsed = decode_vector(value).tolist()
            except (ValueError, TypeError):
                return None
            if (
                not parsed
                or len(parsed) > _MAX_EMBEDDING_DIMENSIONS
                or (expected_dimension and len(parsed) != expected_dimension)
            ):
                return None
            return encode_vector(parsed)

        payload = cls._normalize_embedding_text(
            value,
            value_type,
            declared_size,
            _MAX_EMBEDDING_CELL_BYTES,
        )
        if payload is None:
            return None
        try:
            parsed = json.loads(payload)
        except (json.JSONDecodeError, RecursionError, ValueError):
            return None
        if (
            not isinstance(parsed, list)
            or not parsed
            or len(parsed) > _MAX_EMBEDDING_DIMENSIONS
            or (expected_dimension and len(parsed) != expected_dimension)
        ):
            return None
        normalized: list[float] = []
        for item in parsed:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                return None
            try:
                number = float(item)
            except (TypeError, ValueError, OverflowError):
                return None
            if not math.isfinite(number):
                return None
            normalized.append(number)
        try:
            encoded = json.dumps(
                normalized,
                allow_nan=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError, OverflowError):
            return None
        if len(encoded.encode("utf-8")) > _MAX_EMBEDDING_CELL_BYTES:
            return None
        return encoded

    async def _schedule_reindex(self) -> None:
        """Durably queue missing derived indexes; only legacy runtimes index inline."""
        self._reindex_total = len(self._buckets_to_reindex)
        self._reindex_done = 0
        self._reindex_errors = 0
        if not self._buckets_to_reindex:
            return

        outbox = getattr(self._bucket_mgr, "embedding_outbox", None)
        if outbox is not None and callable(getattr(outbox, "enqueue", None)):
            for bucket_id, bucket_path in self._buckets_to_reindex:
                try:
                    content = await _to_thread_reaped(
                        self._read_bucket_content,
                        bucket_path,
                    )
                    if content.strip():
                        outbox.enqueue(bucket_id, content)
                except Exception as exc:
                    self._reindex_errors += 1
                    self._apply_errors.append(f"[{bucket_id}] 无法加入向量队列: {exc}")
                self._reindex_done += 1
            self._buckets_to_reindex = []
            return

        self._phase = PHASE_REINDEXING
        await self._reindex_all()
        self._buckets_to_reindex = []

    @staticmethod
    def _read_bucket_content(bucket_path: str) -> str:
        return frontmatter.load(bucket_path).content or ""

    async def _reindex_all(self) -> None:
        """对 embedding 不匹配时导入的 bucket 重新生成向量。"""
        emb = self._embedding_engine
        if not getattr(emb, "enabled", False):
            logger.warning("[migrate] embedding engine 未启用，跳过重新向量化")
            self._phase = PHASE_DONE
            return

        for bucket_id, bucket_path in self._buckets_to_reindex:
            try:
                content = await _to_thread_reaped(
                    self._read_bucket_content,
                    bucket_path,
                )
            except Exception as e:
                logger.warning(f"[migrate] read reindex source {bucket_id[:12]}: {e}")
                self._reindex_errors += 1
                self._reindex_done += 1
                continue
            if not content.strip():
                self._reindex_done += 1
                continue
            try:
                await emb.generate_and_store(bucket_id, content)
            except Exception as e:
                logger.warning(f"[migrate] reindex {bucket_id[:12]}: {e}")
                self._reindex_errors += 1
            self._reindex_done += 1

        logger.info(
            f"[migrate] 重新向量化完成: "
            f"{self._reindex_done - self._reindex_errors} 成功, "
            f"{self._reindex_errors} 失败"
        )
        self._phase = PHASE_DONE
