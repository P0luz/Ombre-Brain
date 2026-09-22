"""派生 embedding 索引的持久 write-behind 队列。

Bucket Markdown 是真源。本 outbox 只保存 bucket ID、content/meaning 哈希与
重试元数据；provider 不可用时不会阻塞或回滚记忆写入，也不复制记忆正文。
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any

from utils import now_iso, parse_bool, positive_float
from runtime_owner import spawn_background


logger = logging.getLogger("ombre_brain.embedding_outbox")

_OUTBOX_VERSION = 3
_OUTBOX_FILENAME = ".embedding_outbox.json"
_OUTBOX_LOCK_FILENAME = ".embedding_outbox.lock"
_OUTBOX_LOCK_TIMEOUT_SECONDS = 30.0
_DEFAULT_RETRY_BASE_SECONDS = 5.0
_DEFAULT_RETRY_MAX_SECONDS = 300.0
_DEFAULT_CIRCUIT_FAILURE_THRESHOLD = 3
_DEFAULT_CIRCUIT_BASE_SECONDS = 30.0
_DEFAULT_CIRCUIT_MAX_SECONDS = 600.0
_IDLE_POLL_SECONDS = 30.0

_COMPONENT_KINDS = ("content", "meaning")
_COMPONENT_RETRY_FIELDS = (
    "attempts",
    "next_attempt_at",
    "last_attempt_at",
    "last_error",
    "queued_at",
)

_LOCAL_FILE_LOCKS_GUARD = threading.Lock()
_LOCAL_FILE_LOCKS: dict[str, threading.RLock] = {}


def _local_file_lock(path: str) -> threading.RLock:
    normalized = os.path.normcase(os.path.abspath(path))
    with _LOCAL_FILE_LOCKS_GUARD:
        lock = _LOCAL_FILE_LOCKS.get(normalized)
        if lock is None:
            lock = threading.RLock()
            _LOCAL_FILE_LOCKS[normalized] = lock
        return lock


@contextmanager
def _exclusive_file_turn(path: str, timeout: float = _OUTBOX_LOCK_TIMEOUT_SECONDS):
    """Serialize whole-file outbox read/modify/replace transactions.

    The in-process lock covers multiple ``EmbeddingOutbox`` instances in one
    interpreter.  The byte-range/flock lease covers independent processes that
    share the same vault.  The sibling lock file is stable across replacement
    of the JSON data file.
    """

    local_lock = _local_file_lock(path)
    deadline = time.monotonic() + max(0.0, timeout)
    local_acquired = local_lock.acquire(timeout=max(0.0, timeout))
    if not local_acquired:
        raise TimeoutError(f"timed out waiting for local outbox lease: {path}")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = os.open(path, flags, 0o600)
        try:
            handle = os.fdopen(descriptor, "r+b", buffering=0)
        except Exception:
            os.close(descriptor)
            raise

        acquired = False
        busy_errnos = {
            errno.EACCES,
            errno.EAGAIN,
            getattr(errno, "EWOULDBLOCK", errno.EAGAIN),
        }

        def is_busy(exc: OSError) -> bool:
            return (
                exc.errno in busy_errnos
                or getattr(exc, "winerror", None) in {32, 33}
            )

        def try_acquire() -> bool:
            try:
                if os.name == "nt":  # pragma: no branch - platform-specific
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:  # pragma: no cover - exercised in Linux CI
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError as exc:
                if is_busy(exc):
                    return False
                raise

        def release() -> None:
            if os.name == "nt":  # pragma: no branch - platform-specific
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - exercised in Linux CI
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

        try:
            while True:
                handle.seek(0, os.SEEK_END)
                if handle.tell() > 0:
                    break
                try:
                    handle.write(b"\0")
                    break
                except OSError as exc:
                    # Windows byte-range locking needs the target byte to
                    # exist. Two processes may initialize a new sidecar at
                    # once; retry only genuine sharing/lock contention.
                    if os.name != "nt" or not is_busy(exc):
                        raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"timed out initializing outbox lease: {path}"
                        ) from exc
                    time.sleep(0.01)
            handle.seek(0)
            while not acquired:
                acquired = try_acquire()
                if acquired:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for outbox lease: {path}")
                time.sleep(0.01)
            yield
        finally:
            if acquired:
                try:
                    release()
                except OSError as exc:
                    logger.warning("Embedding outbox lease unlock failed: %s", exc)
            try:
                handle.close()
            except OSError as exc:
                logger.warning("Embedding outbox lease close failed: %s", exc)
    finally:
        local_lock.release()


def content_hash(content: str) -> str:
    """Return the stable identity of the exact text represented by a vector."""
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def _latest_meaning(metadata: dict[str, Any]) -> str:
    values = metadata.get("meaning") or []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        return ""
    normalized = [str(value).strip() for value in values if str(value).strip()]
    return normalized[-1] if normalized else ""


class EmbeddingOutbox:
    """Persist and retry embedding work without storing memory content twice."""

    def __init__(self, config: dict, bucket_mgr: Any, embedding_engine: Any) -> None:
        self.config = config
        self.bucket_mgr = bucket_mgr
        self.embedding_engine = embedding_engine
        self.path = os.path.join(config["buckets_dir"], _OUTBOX_FILENAME)
        self._file_lock_path = os.path.join(
            config["buckets_dir"], _OUTBOX_LOCK_FILENAME
        )

        embed_cfg = config.get("embedding", {}) or {}
        self.background_enabled = parse_bool(
            embed_cfg.get("background_indexing", True), default=True
        )
        self.retry_base_seconds = positive_float(
            embed_cfg.get("retry_base_seconds"), _DEFAULT_RETRY_BASE_SECONDS
        )
        self.retry_max_seconds = positive_float(
            embed_cfg.get("retry_max_seconds"), _DEFAULT_RETRY_MAX_SECONDS
        )
        if self.retry_max_seconds < self.retry_base_seconds:
            self.retry_max_seconds = self.retry_base_seconds
        try:
            self.circuit_failure_threshold = max(
                1,
                int(
                    embed_cfg.get(
                        "circuit_failure_threshold",
                        _DEFAULT_CIRCUIT_FAILURE_THRESHOLD,
                    )
                ),
            )
        except (TypeError, ValueError):
            self.circuit_failure_threshold = _DEFAULT_CIRCUIT_FAILURE_THRESHOLD
        self.circuit_base_seconds = positive_float(
            embed_cfg.get("circuit_base_seconds"),
            _DEFAULT_CIRCUIT_BASE_SECONDS,
        )
        self.circuit_max_seconds = positive_float(
            embed_cfg.get("circuit_max_seconds"),
            _DEFAULT_CIRCUIT_MAX_SECONDS,
        )
        if self.circuit_max_seconds < self.circuit_base_seconds:
            self.circuit_max_seconds = self.circuit_base_seconds

        self._lock = threading.RLock()
        self._items: dict[str, dict[str, Any]] = self._load_items()
        self._event: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self._worker_loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._processed = 0
        self._last_success = ""
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._circuit_trips = 0
        # 熔断只该在「不同的桶接连失败」时才跳闸——那才是供应商级故障的信号。
        # 同一个桶反复失败更像是那条内容本身有毒（比如触发了 provider 的内容
        # 过滤，永远拿不到向量），不该连累队列里所有其他合法待处理的记忆一起
        # 陪绑最长 10 分钟。见 _record_provider_failure()。
        self._last_failure_bucket_id = ""

    @staticmethod
    def _hash_field(kind: str) -> str:
        return "meaning_hash" if kind == "meaning" else "content_hash"

    @classmethod
    def _component_pending(cls, item: dict[str, Any], kind: str) -> bool:
        value = item.get(cls._hash_field(kind))
        return isinstance(value, str) if kind == "meaning" else bool(value)

    @staticmethod
    def _component_field(kind: str, field: str) -> str:
        return f"{kind}_{field}"

    @classmethod
    def _component_attempts(cls, item: dict[str, Any], kind: str) -> int:
        try:
            return max(
                0,
                int(item.get(cls._component_field(kind, "attempts")) or 0),
            )
        except (TypeError, ValueError, OverflowError):
            return 0

    @classmethod
    def _component_due_at(cls, item: dict[str, Any], kind: str) -> float:
        try:
            return max(
                0.0,
                float(
                    item.get(cls._component_field(kind, "next_attempt_at"))
                    or 0.0
                ),
            )
        except (TypeError, ValueError, OverflowError):
            return 0.0

    @classmethod
    def _normalize_item(cls, raw: dict[str, Any]) -> dict[str, Any]:
        """Upgrade v1/v2 shared retry metadata to per-component state."""

        item = dict(raw)
        try:
            legacy_attempts = max(0, int(item.get("attempts") or 0))
        except (TypeError, ValueError, OverflowError):
            legacy_attempts = 0
        try:
            legacy_due = max(0.0, float(item.get("next_attempt_at") or 0.0))
        except (TypeError, ValueError, OverflowError):
            legacy_due = 0.0
        legacy_attempted = str(item.get("last_attempt_at") or "")
        legacy_error = str(item.get("last_error") or "")
        legacy_queued = str(
            item.get("queued_at") or item.get("updated_at") or ""
        )

        for kind in _COMPONENT_KINDS:
            if not cls._component_pending(item, kind):
                cls._drop_component_state(item, kind)
                continue
            defaults = {
                "attempts": legacy_attempts,
                "next_attempt_at": legacy_due,
                "last_attempt_at": legacy_attempted,
                "last_error": legacy_error,
                "queued_at": legacy_queued,
            }
            for field, default in defaults.items():
                item.setdefault(cls._component_field(kind, field), default)
        cls._refresh_aggregate(item)
        return item

    @classmethod
    def _refresh_aggregate(cls, item: dict[str, Any]) -> None:
        """Maintain legacy item-level status fields for API compatibility."""

        pending = [
            kind for kind in _COMPONENT_KINDS if cls._component_pending(item, kind)
        ]
        if not pending:
            item.update(
                attempts=0,
                next_attempt_at=0.0,
                last_attempt_at="",
                last_error="",
            )
            return

        queued_values = [
            str(item.get(cls._component_field(kind, "queued_at")) or "")
            for kind in pending
        ]
        queued_values = [value for value in queued_values if value]
        if queued_values:
            item["queued_at"] = min(queued_values)

        states = []
        for kind in pending:
            states.append(
                (
                    cls._component_attempts(item, kind),
                    cls._component_due_at(item, kind),
                    str(
                        item.get(cls._component_field(kind, "last_attempt_at"))
                        or ""
                    ),
                    str(
                        item.get(cls._component_field(kind, "last_error")) or ""
                    ),
                )
            )
        item["attempts"] = max(state[0] for state in states)
        retry_times = [state[1] for state in states if state[1] > 0]
        item["next_attempt_at"] = min(retry_times) if retry_times else 0.0
        latest = max(states, key=lambda state: state[2])
        item["last_attempt_at"] = latest[2]
        item["last_error"] = latest[3]

    @classmethod
    def _drop_component_state(cls, item: dict[str, Any], kind: str) -> None:
        item.pop(cls._hash_field(kind), None)
        for field in _COMPONENT_RETRY_FIELDS:
            item.pop(cls._component_field(kind, field), None)

    @classmethod
    def _queue_component(
        cls,
        item: dict[str, Any],
        kind: str,
        digest: str,
        now: str,
        *,
        reset_retry: bool,
    ) -> None:
        hash_field = cls._hash_field(kind)
        same_digest = item.get(hash_field) == digest
        item[hash_field] = digest
        queued_field = cls._component_field(kind, "queued_at")
        if not same_digest or not item.get(queued_field):
            item[queued_field] = now
        if not same_digest or reset_retry:
            item[cls._component_field(kind, "attempts")] = 0
            item[cls._component_field(kind, "next_attempt_at")] = 0.0
            item[cls._component_field(kind, "last_attempt_at")] = ""
            item[cls._component_field(kind, "last_error")] = ""
        else:
            item.setdefault(cls._component_field(kind, "attempts"), 0)
            item.setdefault(cls._component_field(kind, "next_attempt_at"), 0.0)
            item.setdefault(cls._component_field(kind, "last_attempt_at"), "")
            item.setdefault(cls._component_field(kind, "last_error"), "")
        item["updated_at"] = now
        cls._refresh_aggregate(item)

    def _reload_for_update_locked(self) -> None:
        self._items = self._load_items(strict=True)

    def _refresh_from_disk_locked(self) -> None:
        try:
            self._items = self._load_items(strict=True)
        except Exception as exc:
            # Keep the last known in-memory snapshot on a transient read error.
            logger.warning("Embedding outbox refresh failed: %s", exc)

    @property
    def running(self) -> bool:
        return self._running

    def set_embedding_engine(self, engine: Any) -> None:
        self.embedding_engine = engine
        self.reset_circuit()
        self._wake()

    def enqueue(self, bucket_id: str, content: str, *, reset_retry: bool = True) -> bool:
        """Upsert one desired index state and durably persist it."""
        bucket_id = str(bucket_id or "").strip()
        if not bucket_id:
            return False
        if not (content or "").strip():
            self._discard_component(bucket_id, "content")
            return False

        now = now_iso()
        digest = content_hash(content)
        with self._lock:
            with _exclusive_file_turn(self._file_lock_path):
                self._reload_for_update_locked()
                item = dict(self._items.get(bucket_id) or {})
                self._queue_component(
                    item,
                    "content",
                    digest,
                    now,
                    reset_retry=reset_retry,
                )
                self._items[bucket_id] = item
                self._persist_locked()
        self._wake()
        return True

    def enqueue_meaning(
        self,
        bucket_id: str,
        meaning_text: str,
        *,
        reset_retry: bool = True,
    ) -> bool:
        """只持久登记 meaning 的期望哈希，不在 outbox 中复制原文。"""
        bucket_id = str(bucket_id or "").strip()
        if not bucket_id:
            return False
        now = now_iso()
        digest = content_hash(str(meaning_text or "").strip())
        with self._lock:
            with _exclusive_file_turn(self._file_lock_path):
                self._reload_for_update_locked()
                item = dict(self._items.get(bucket_id) or {})
                self._queue_component(
                    item,
                    "meaning",
                    digest,
                    now,
                    reset_retry=reset_retry,
                )
                self._items[bucket_id] = item
                self._persist_locked()
        self._wake()
        return True

    def ensure_pending(self, bucket_id: str, content: str) -> bool:
        """Atomically add a task only when the ID has no pending version.

        Repair callers may hold a stale copy of content.  A normal ``enqueue``
        would overwrite a newer task that arrived between their status check
        and repair attempt; keeping the existing item is safe because the
        worker always re-reads Markdown and corrects stale hashes itself.
        """
        bucket_id = str(bucket_id or "").strip()
        if not bucket_id:
            return False
        if not (content or "").strip():
            return False

        now = now_iso()
        with self._lock:
            with _exclusive_file_turn(self._file_lock_path):
                self._reload_for_update_locked()
                item = dict(self._items.get(bucket_id) or {})
                if item.get("content_hash"):
                    return True
                self._queue_component(
                    item,
                    "content",
                    content_hash(content),
                    now,
                    reset_retry=True,
                )
                self._items[bucket_id] = item
                self._persist_locked()
        self._wake()
        return True

    def discard(self, bucket_id: str) -> bool:
        with self._lock:
            with _exclusive_file_turn(self._file_lock_path):
                self._reload_for_update_locked()
                if bucket_id not in self._items:
                    return False
                self._items.pop(bucket_id, None)
                self._persist_locked()
        return True

    def _discard_component(self, bucket_id: str, kind: str) -> bool:
        """Remove one pending component without deleting its sibling task."""

        with self._lock:
            with _exclusive_file_turn(self._file_lock_path):
                self._reload_for_update_locked()
                item = self._items.get(bucket_id)
                if not item or not self._component_pending(item, kind):
                    return False
                self._drop_component_state(item, kind)
                if any(
                    self._component_pending(item, sibling)
                    for sibling in _COMPONENT_KINDS
                ):
                    item["updated_at"] = now_iso()
                    self._refresh_aggregate(item)
                else:
                    self._items.pop(bucket_id, None)
                self._persist_locked()
        return True

    def complete_content(self, bucket_id: str, content: str) -> None:
        self._complete_component(bucket_id, content_hash(content), "content")

    def complete_meaning(self, bucket_id: str, meaning_text: str) -> None:
        self._complete_component(
            bucket_id,
            content_hash(str(meaning_text or "").strip()),
            "meaning",
        )

    def is_pending(self, bucket_id: str) -> bool:
        with self._lock:
            self._refresh_from_disk_locked()
            return bucket_id in self._items

    def pending_ids(self) -> set[str]:
        """Return a snapshot of IDs awaiting derived-index work."""
        with self._lock:
            self._refresh_from_disk_locked()
            return set(self._items)

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_from_disk_locked()
            items = [dict(item) for item in self._items.values()]
        failed = [
            item
            for item in items
            if any(
                self._component_attempts(item, kind) > 0
                for kind in _COMPONENT_KINDS
                if self._component_pending(item, kind)
            )
        ]
        next_retry = min(
            (
                self._component_due_at(item, kind)
                for item in items
                for kind in _COMPONENT_KINDS
                if self._component_pending(item, kind)
                and self._component_due_at(item, kind) > 0
            ),
            default=0.0,
        )
        last_error = ""
        if failed:
            attempts = [
                (
                    str(
                        item.get(self._component_field(kind, "last_attempt_at"))
                        or ""
                    ),
                    str(
                        item.get(self._component_field(kind, "last_error")) or ""
                    ),
                )
                for item in failed
                for kind in _COMPONENT_KINDS
                if self._component_attempts(item, kind) > 0
            ]
            if attempts:
                last_error = max(attempts, key=lambda value: value[0])[1]
        return {
            "running": self._running,
            "background_enabled": self.background_enabled,
            "provider_ready": bool(
                self.embedding_engine
                and getattr(self.embedding_engine, "enabled", False)
            ),
            "pending": len(items),
            "retrying": len(failed),
            "processed": self._processed,
            "last_success": self._last_success,
            "last_error": last_error,
            "next_retry_at": max(next_retry, self._circuit_open_until),
            "path": self.path,
            "circuit": {
                "state": "open" if self._circuit_delay() > 0 else "closed",
                "consecutive_failures": self._consecutive_failures,
                "failure_threshold": self.circuit_failure_threshold,
                "open_until": self._circuit_open_until,
                "trips": self._circuit_trips,
            },
        }

    def reset_circuit(self) -> None:
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._last_failure_bucket_id = ""

    def retry_now(self) -> int:
        """Close the circuit and make every pending item immediately due."""
        self.reset_circuit()
        changed = 0
        with self._lock:
            with _exclusive_file_turn(self._file_lock_path):
                self._reload_for_update_locked()
                for item in self._items.values():
                    item_changed = False
                    for kind in _COMPONENT_KINDS:
                        if not self._component_pending(item, kind):
                            continue
                        field = self._component_field(kind, "next_attempt_at")
                        if self._component_due_at(item, kind) > 0:
                            item[field] = 0.0
                            item_changed = True
                    if item_changed:
                        self._refresh_aggregate(item)
                        changed += 1
                if changed:
                    self._persist_locked()
        self._wake()
        return changed

    async def start(self, *, reconcile: bool = True) -> bool:
        if self._running or not self.background_enabled:
            return False
        self._running = True
        self._worker_loop = asyncio.get_running_loop()
        self._event = asyncio.Event()
        if reconcile:
            try:
                await self.reconcile(include_archive=True)
            except Exception as exc:
                logger.warning("Embedding outbox startup reconciliation failed: %s", exc)
        self._task = spawn_background(self._run())
        self._task.set_name("ombre-embedding-outbox")
        self._wake()
        logger.info(
            "Embedding outbox started / embedding 后台索引队列已启动: pending=%s",
            self.status()["pending"],
        )
        return True

    async def stop(self) -> None:
        self._running = False
        self._wake()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._event = None
        self._worker_loop = None

    async def reconcile(
        self,
        *,
        include_archive: bool = True,
        buckets: list[dict] | None = None,
    ) -> int:
        """Monotonically queue missing or hash-stale vectors.

        ``buckets`` is often a caller-owned snapshot captured well before this
        method runs (decay and Dashboard backfill both do substantial work in
        between).  It is therefore never authoritative enough to delete or
        replace an existing pending item.  Only ``_process`` may acknowledge a
        task after checking the current Markdown truth; managed delete paths
        explicitly call ``discard``.
        """
        if buckets is None:
            buckets = await self.bucket_mgr.list_all(include_archive=include_archive)

        current: dict[str, tuple[str, str]] = {}
        for bucket in buckets:
            metadata = bucket.get("metadata") or {}
            content = str(bucket.get("content") or "")
            bucket_id = str(bucket.get("id") or "")
            if not bucket_id or not content.strip() or metadata.get("deleted_at"):
                continue
            current[bucket_id] = (content, content_hash(content))

        engine = self.embedding_engine

        def read_index_state():
            # ``list_all_ids`` includes meaning-only rows. New engines expose a
            # content-specific reader; the fallback keeps adapters compatible.
            content_id_reader = getattr(engine, "list_content_ids", None)
            if not callable(content_id_reader):
                content_id_reader = getattr(engine, "list_all_ids", None)
            try:
                content_ids = (
                    set(content_id_reader())
                    if callable(content_id_reader)
                    else set()
                )
            except Exception as exc:
                logger.warning(
                    "Embedding outbox could not list content index IDs: %s", exc
                )
                # Unknown is not the same as empty. Queueing the whole vault on
                # a transient SQLite read failure creates an API storm.
                return None

            hash_reader = getattr(engine, "list_content_hashes", None)
            hashes_supported = callable(hash_reader)
            try:
                indexed_hashes = (
                    dict(hash_reader()) if hashes_supported else {}
                )
            except Exception as exc:
                logger.warning(
                    "Embedding outbox could not read index hashes: %s", exc
                )
                indexed_hashes = {}
                hashes_supported = False
            return content_ids, indexed_hashes, hashes_supported

        initial_state = read_index_state()
        if initial_state is None:
            return 0
        content_ids, indexed_hashes, hashes_supported = initial_state

        # Caller-owned bucket lists can be stale. Use them only to identify
        # likely candidates, then re-read Markdown before queueing.
        candidate_ids: list[str] = []
        for bucket_id, (_content, digest) in current.items():
            stored_hash = str(indexed_hashes.get(bucket_id) or "")
            needs_index = bucket_id not in content_ids or (
                hashes_supported and bool(stored_hash) and stored_hash != digest
            )
            if needs_index:
                candidate_ids.append(bucket_id)

        latest_current: dict[str, tuple[str, str]] = {}
        for bucket_id in candidate_ids:
            try:
                bucket = await self.bucket_mgr.get(bucket_id)
            except Exception as exc:
                logger.warning(
                    "Embedding outbox could not re-read bucket %s: %s",
                    bucket_id,
                    exc,
                )
                continue
            if not bucket:
                continue
            metadata = bucket.get("metadata") or {}
            content = str(bucket.get("content") or "")
            if not content.strip() or metadata.get("deleted_at"):
                continue
            latest_current[bucket_id] = (content, content_hash(content))

        queued = 0
        changed = False
        now = now_iso()
        with self._lock:
            # Keep provider/SQLite reads outside the sidecar lease. Local
            # completion is still serialized by ``self._lock``; a different
            # process may cause a harmless duplicate queue entry, which is
            # preferable to an Outbox -> Engine lock inversion.
            final_state = read_index_state()
            if final_state is None:
                return 0
            content_ids, indexed_hashes, hashes_supported = final_state
            with _exclusive_file_turn(self._file_lock_path):
                self._reload_for_update_locked()
                for bucket_id, (_content, digest) in latest_current.items():
                    stored_hash = str(indexed_hashes.get(bucket_id) or "")
                    needs_index = bucket_id not in content_ids or (
                        hashes_supported
                        and bool(stored_hash)
                        and stored_hash != digest
                    )
                    existing = self._items.get(bucket_id)
                    if not needs_index:
                        continue
                    # A pending content hash may be newer than this snapshot.
                    if existing is not None and existing.get("content_hash"):
                        continue
                    item = dict(existing or {})
                    self._queue_component(
                        item,
                        "content",
                        digest,
                        now,
                        reset_retry=True,
                    )
                    self._items[bucket_id] = item
                    queued += 1
                    changed = True

                if changed:
                    self._persist_locked()
        if changed:
            self._wake()
        return queued

    async def process_once(self) -> bool:
        """Process one due item; useful for deterministic maintenance/tests."""
        engine = self.embedding_engine
        if not engine or not getattr(engine, "enabled", False):
            return False
        if self._circuit_delay() > 0:
            return False
        bucket_id, item, _delay = self._next_due()
        if not bucket_id or item is None:
            return False
        await self._process(bucket_id, item, engine)
        return True

    async def wait_until_idle(self, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if self.status()["pending"] == 0:
                return True
            await asyncio.sleep(0.02)
        return self.status()["pending"] == 0

    async def _run(self) -> None:
        while self._running:
            if self._event:
                self._event.clear()
            engine = self.embedding_engine
            if not engine or not getattr(engine, "enabled", False):
                await self._wait(_IDLE_POLL_SECONDS)
                continue
            circuit_delay = self._circuit_delay()
            if circuit_delay > 0:
                await self._wait(circuit_delay)
                continue
            bucket_id, item, delay = self._next_due()
            if bucket_id and item is not None:
                await self._process(bucket_id, item, engine)
                continue
            await self._wait(delay)

    @classmethod
    def _selected_component_kind(cls, item: dict[str, Any]) -> str:
        selected = str(item.get("_component_kind") or "")
        if selected in _COMPONENT_KINDS and cls._component_pending(item, selected):
            return selected
        candidates = [
            kind for kind in _COMPONENT_KINDS if cls._component_pending(item, kind)
        ]
        if not candidates:
            return ""
        return min(
            candidates,
            key=lambda kind: (
                cls._component_due_at(item, kind),
                str(item.get(cls._component_field(kind, "queued_at")) or ""),
                kind,
            ),
        )

    def _current_component_snapshot(
        self,
        bucket_id: str,
        kind: str,
        digest: str,
    ) -> dict[str, Any] | None:
        """Revalidate a scheduled component after acquiring the derived turn."""

        with self._lock:
            self._refresh_from_disk_locked()
            current = self._items.get(bucket_id)
            if not current or current.get(self._hash_field(kind)) != digest:
                return None
            if self._component_due_at(current, kind) > time.time():
                return None
            snapshot = dict(current)
            snapshot["_component_kind"] = kind
            return snapshot

    async def _process(self, bucket_id: str, item: dict[str, Any], engine: Any) -> None:
        kind = self._selected_component_kind(item)
        if not kind:
            return
        raw_digest = item.get(self._hash_field(kind))
        if (kind == "content" and not raw_digest) or (
            kind == "meaning" and not isinstance(raw_digest, str)
        ):
            return
        digest = str(raw_digest)

        turn_factory = getattr(self.bucket_mgr, "_derived_index_turn", None)
        if not callable(turn_factory):
            await self._process_component(bucket_id, item, engine)
            return

        try:
            # The sidecar lease is deliberately not held across this await or
            # the provider call. The derived lease orders all workers/processes
            # for one bucket; once inside it, reload the durable CAS state so a
            # stale scheduler snapshot cannot resurrect completed work.
            async with turn_factory(bucket_id):
                current = self._current_component_snapshot(
                    bucket_id, kind, digest
                )
                if current is None:
                    return
                await self._process_component(bucket_id, current, engine)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Embedding derived turn failed; component remains queued: "
                "bucket=%s kind=%s error=%s",
                bucket_id,
                kind,
                exc,
            )
            self._fail_component(bucket_id, digest, exc, kind)

    async def _process_component(
        self,
        bucket_id: str,
        item: dict[str, Any],
        engine: Any,
    ) -> None:
        bucket = await self.bucket_mgr.get(bucket_id)
        if not bucket:
            try:
                engine.delete_embedding(bucket_id)
            except Exception:
                pass
            self.discard(bucket_id)
            return
        metadata = bucket.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        if metadata.get("deleted_at") or parse_bool(
            metadata.get("tombstone"), default=False
        ):
            try:
                engine.delete_embedding(bucket_id)
            except Exception:
                pass
            self.discard(bucket_id)
            return

        selected_kind = self._selected_component_kind(item)
        if not selected_kind:
            return

        desired_meaning_hash = item.get("meaning_hash")
        if selected_kind == "meaning" and isinstance(desired_meaning_hash, str):
            meaning_text = _latest_meaning(metadata)
            digest = content_hash(meaning_text)
            if digest != desired_meaning_hash:
                self.enqueue_meaning(bucket_id, meaning_text)
                return

            try:
                if meaning_text:
                    generate_meaning = getattr(
                        engine, "generate_and_store_meaning", None
                    )
                    if not callable(generate_meaning):
                        self._complete_component(bucket_id, digest, "meaning")
                        return
                    ok = bool(await generate_meaning(bucket_id, meaning_text))
                else:
                    clear_meaning = getattr(
                        engine, "delete_meaning_embedding", None
                    )
                    if not callable(clear_meaning):
                        self._complete_component(bucket_id, digest, "meaning")
                        return
                    clear_meaning(bucket_id)
                    ok = True
            except Exception as exc:
                self._fail_component(bucket_id, digest, exc, "meaning")
                return
            if not ok:
                self._fail_component(
                    bucket_id,
                    digest,
                    "generate_and_store_meaning returned false",
                    "meaning",
                )
                return

            latest = await self.bucket_mgr.get(bucket_id)
            if not latest:
                try:
                    engine.delete_embedding(bucket_id)
                except Exception:
                    pass
                self.discard(bucket_id)
                return
            latest_metadata = latest.get("metadata") or {}
            if not isinstance(latest_metadata, dict):
                latest_metadata = {}
            if latest_metadata.get("deleted_at") or parse_bool(
                latest_metadata.get("tombstone"), default=False
            ):
                try:
                    engine.delete_embedding(bucket_id)
                except Exception:
                    pass
                self.discard(bucket_id)
                return
            latest_meaning = _latest_meaning(latest_metadata)
            if content_hash(latest_meaning) != digest:
                self.enqueue_meaning(bucket_id, latest_meaning)
                return
            self._complete_component(bucket_id, digest, "meaning")
            return

        content = str(bucket.get("content") or "")
        desired_content_hash = item.get("content_hash")
        if not content.strip():
            if desired_content_hash:
                self._complete_component(
                    bucket_id, str(desired_content_hash), "content"
                )
            return
        digest = content_hash(content)
        if not desired_content_hash:
            return
        if digest != desired_content_hash:
            self.enqueue(bucket_id, content)
            return

        try:
            ok = bool(await engine.generate_and_store(bucket_id, content))
        except Exception as exc:
            self._fail(bucket_id, digest, exc)
            return
        if not ok:
            self._fail(bucket_id, digest, "generate_and_store returned false")
            return

        latest = await self.bucket_mgr.get(bucket_id)
        if not latest:
            try:
                engine.delete_embedding(bucket_id)
            except Exception:
                pass
            self.discard(bucket_id)
            return
        latest_content = str(latest.get("content") or "")
        if content_hash(latest_content) != digest:
            self.enqueue(bucket_id, latest_content)
            return
        self._complete_component(bucket_id, digest, "content")

    def _complete(self, bucket_id: str, digest: str) -> None:
        """兼容旧测试/调用点：确认 content 分量完成。"""
        self._complete_component(bucket_id, digest, "content")

    def _complete_component(self, bucket_id: str, digest: str, kind: str) -> None:
        remaining = False
        with self._lock:
            with _exclusive_file_turn(self._file_lock_path):
                self._reload_for_update_locked()
                current = self._items.get(bucket_id)
                field = self._hash_field(kind)
                if not current or current.get(field) != digest:
                    return
                self._drop_component_state(current, kind)
                remaining = any(
                    self._component_pending(current, sibling)
                    for sibling in _COMPONENT_KINDS
                )
                if remaining:
                    current["updated_at"] = now_iso()
                    self._refresh_aggregate(current)
                else:
                    self._items.pop(bucket_id, None)
                self._processed += 1
                self._last_success = now_iso()
                self._persist_locked()
        self.reset_circuit()
        if remaining:
            self._wake()

    def _fail(self, bucket_id: str, digest: str, error: Any) -> None:
        """兼容旧调用点：记录 content 分量失败。"""
        self._fail_component(bucket_id, digest, error, "content")

    def _fail_component(
        self,
        bucket_id: str,
        digest: str,
        error: Any,
        kind: str,
    ) -> None:
        with self._lock:
            with _exclusive_file_turn(self._file_lock_path):
                self._reload_for_update_locked()
                current = self._items.get(bucket_id)
                field = self._hash_field(kind)
                if not current or current.get(field) != digest:
                    return
                attempts = self._component_attempts(current, kind) + 1
                delay = min(
                    self.retry_max_seconds,
                    self.retry_base_seconds * (2 ** min(attempts - 1, 16)),
                )
                attempted_at = now_iso()
                current[self._component_field(kind, "attempts")] = attempts
                current[self._component_field(kind, "last_attempt_at")] = (
                    attempted_at
                )
                current[self._component_field(kind, "last_error")] = str(error)[
                    :300
                ]
                current[self._component_field(kind, "next_attempt_at")] = (
                    time.time() + delay
                )
                current["updated_at"] = attempted_at
                self._refresh_aggregate(current)
                self._persist_locked()
        # 同一个桶连续失败不计入熔断计数：那是内容本身有毒的信号，不是供应商
        # 挂了的信号。只有失败发生在不同的桶身上，才可能是供应商级故障。
        if bucket_id != self._last_failure_bucket_id:
            self._last_failure_bucket_id = bucket_id
            self._record_provider_failure()
        logger.warning(
            "Embedding queued for retry / embedding 将后台重试: "
            "bucket=%s kind=%s attempt=%s delay=%.1fs error=%s",
            bucket_id,
            kind,
            attempts,
            delay,
            str(error)[:160],
        )

    def _record_provider_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures < self.circuit_failure_threshold:
            return
        exponent = min(
            self._consecutive_failures - self.circuit_failure_threshold,
            16,
        )
        delay = min(
            self.circuit_max_seconds,
            self.circuit_base_seconds * (2 ** exponent),
        )
        was_open = self._circuit_delay() > 0
        self._circuit_open_until = max(
            self._circuit_open_until,
            time.time() + delay,
        )
        if not was_open:
            self._circuit_trips += 1
        logger.warning(
            "Embedding provider circuit open / embedding 供应商熔断: "
            "failures=%s delay=%.1fs",
            self._consecutive_failures,
            delay,
        )

    def _circuit_delay(self) -> float:
        return max(0.0, self._circuit_open_until - time.time())

    def _next_due(self) -> tuple[str, dict[str, Any] | None, float]:
        now = time.time()
        with self._lock:
            self._refresh_from_disk_locked()
            if not self._items:
                return "", None, _IDLE_POLL_SECONDS
            candidates: list[tuple[float, str, str, str, dict[str, Any]]] = []
            for bucket_id, item in self._items.items():
                for kind in _COMPONENT_KINDS:
                    if not self._component_pending(item, kind):
                        continue
                    candidates.append(
                        (
                            self._component_due_at(item, kind),
                            str(
                                item.get(
                                    self._component_field(kind, "queued_at")
                                )
                                or item.get("queued_at")
                                or ""
                            ),
                            bucket_id,
                            kind,
                            item,
                        )
                    )
            if not candidates:
                return "", None, _IDLE_POLL_SECONDS
            due_at, _queued_at, bucket_id, kind, item = min(candidates)
            if due_at <= now:
                snapshot = dict(item)
                snapshot["_component_kind"] = kind
                return bucket_id, snapshot, 0.0
            return "", None, min(_IDLE_POLL_SECONDS, max(0.01, due_at - now))

    async def _wait(self, timeout: float) -> None:
        if not self._event:
            await asyncio.sleep(timeout)
            return
        try:
            await asyncio.wait_for(self._event.wait(), timeout=max(0.01, timeout))
        except asyncio.TimeoutError:
            pass

    def _wake(self) -> None:
        event = self._event
        loop = self._worker_loop
        if event is None:
            return
        try:
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(event.set)
            else:
                event.set()
        except RuntimeError:
            # loop 正在关闭时任务仍保留在磁盘，下次 start() 会重新唤醒。
            pass

    def _load_items(self, *, strict: bool = False) -> dict[str, dict[str, Any]]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            raw_items = payload.get("items", {}) if isinstance(payload, dict) else {}
            if not isinstance(raw_items, dict):
                raise ValueError("embedding outbox items must be an object")
            return {
                str(bucket_id): self._normalize_item(item)
                for bucket_id, item in raw_items.items()
                if bucket_id
                and isinstance(item, dict)
                and (
                    item.get("content_hash")
                    or isinstance(item.get("meaning_hash"), str)
                )
            }
        except FileNotFoundError:
            return {}
        except Exception as exc:
            if strict:
                raise
            logger.warning("Embedding outbox is unreadable; rebuilding from buckets: %s", exc)
            return {}

    def _persist_locked(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        payload = {
            "version": _OUTBOX_VERSION,
            "updated_at": now_iso(),
            "items": self._items,
        }
        temp_path = f"{self.path}.{uuid.uuid4().hex}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
