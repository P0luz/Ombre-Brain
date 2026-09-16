"""Crash-recoverable whole-database embedding publication.

E-MIG-01 is deliberately narrow.  This module provides:

* a cross-process shared/exclusive gate for every live embedding operation;
* a cross-process single-flight reservation for a whole-database migration;
* private, internally named shadow generations and strict SQLite validation;
* a durable manifest and a compensating two-step namespace exchange; and
* fail-closed startup recovery and a process-visible health state.

It does *not* implement an embedding outbox, ordinary-write retries, content
hash CAS, import recovery, or backup retention policy.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import ctypes
import hashlib
import inspect
import json
import math
import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Iterator, Mapping
from urllib.parse import urlsplit

try:
    import yaml
except ImportError:  # pragma: no cover - config recovery requires PyYAML in prod
    yaml = None  # type: ignore[assignment]

try:
    from config_transaction import (
        ConfigTransactionSession,
        PreparedConfig,
        async_config_yaml_turn as _shared_async_config_yaml_turn,
        async_open_config_transaction,
        config_mutation_guard as _shared_config_mutation_guard,
        config_yaml_turn as _shared_config_yaml_turn,
        open_config_transaction,
        run_config_transaction,
    )
except ImportError:  # pragma: no cover
    from .config_transaction import (  # type: ignore
        ConfigTransactionSession,
        PreparedConfig,
        async_config_yaml_turn as _shared_async_config_yaml_turn,
        async_open_config_transaction,
        config_mutation_guard as _shared_config_mutation_guard,
        config_yaml_turn as _shared_config_yaml_turn,
        open_config_transaction,
        run_config_transaction,
    )


_MANIFEST_SCHEMA = 1
_MANIFEST_LEAF = ".embedding-publish.json"
_GENERATIONS_DIR = ".embedding-generations"
_LOCKS_DIR = ".locks"
_TXID_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SHADOW_RE = re.compile(r"^(?P<txid>[0-9a-f]{32})\.shadow\.db$")
_TERMINAL_STATES = {"COMMITTED", "ABORTED_RESTORED"}
_EMBEDDING_CONFIG_FIELDS = {
    "backend",
    "enabled",
    "api_format",
    "base_url",
    "model",
    "dim",
}

Callback = Callable[..., Any | Awaitable[Any]]


class EmbeddingPublishError(RuntimeError):
    """Base class for a fail-closed E-MIG-01 error."""


class ShadowValidationError(EmbeddingPublishError):
    """The shadow SQLite generation is not publishable."""


class PublishRecoveryError(EmbeddingPublishError):
    """A pending publication cannot be recovered without guessing."""


class PublishRollbackError(EmbeddingPublishError):
    """Publication failed and its compensating rollback also failed."""

    def __init__(self, original: BaseException, rollback: BaseException):
        super().__init__(
            "embedding publish failed and rollback failed: "
            f"original={type(original).__name__}: {original}; "
            f"rollback={type(rollback).__name__}: {rollback}"
        )
        self.original = original
        self.rollback = rollback


_health_guard = threading.Lock()
_publish_health: dict[str, Any] = {
    "ok": True,
    "state": "idle",
    "error": "",
    "txid": "",
}


def _set_health(
    ok: bool,
    state: str,
    *,
    error: str = "",
    txid: str = "",
) -> None:
    with _health_guard:
        _publish_health.update(
            {"ok": bool(ok), "state": state, "error": error, "txid": txid}
        )


def get_publish_health() -> dict[str, Any]:
    """Return a copy of the terminal publication/recovery health state."""

    with _health_guard:
        return dict(_publish_health)


def _resolved_db(db_path: str | os.PathLike[str]) -> Path:
    path = Path(db_path).expanduser().resolve(strict=False)
    if not path.name:
        raise ValueError("embedding db_path must name a file")
    return path


def _logical_lock_path(path: Path, purpose: str) -> Path:
    digest = hashlib.sha256(
        f"ombre-brain:E-MIG-01:{purpose}:{os.path.normcase(str(path))}".encode(
            "utf-8"
        )
    ).hexdigest()
    lock_dir = path.parent / _LOCKS_DIR
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir / f"{digest}.lock"


if os.name == "nt":
    import msvcrt
    from ctypes import wintypes

    _LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
    _LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
    _ERROR_LOCK_VIOLATION = 33

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _LockFileEx = _kernel32.LockFileEx
    _LockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_OVERLAPPED),
    ]
    _LockFileEx.restype = wintypes.BOOL
    _UnlockFileEx = _kernel32.UnlockFileEx
    _UnlockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_OVERLAPPED),
    ]
    _UnlockFileEx.restype = wintypes.BOOL


class _ByteLease:
    """One kernel lease. Windows locks a byte; POSIX flocks one lock file."""

    def __init__(
        self,
        lock_path: Path,
        *,
        offset: int,
        exclusive: bool,
        blocking: bool = True,
    ):
        self.lock_path = lock_path
        self.offset = offset
        self.exclusive = exclusive
        self.blocking = blocking
        self.fd: int | None = None
        self._overlapped: Any = None

    def acquire(self) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.fstat(fd).st_size < 2:
                os.lseek(fd, 1, os.SEEK_SET)
                os.write(fd, b"\0")
                os.fsync(fd)
            if os.name == "nt":
                handle = msvcrt.get_osfhandle(fd)
                ov = _OVERLAPPED()
                ov.Offset = self.offset
                flags = _LOCKFILE_EXCLUSIVE_LOCK if self.exclusive else 0
                if not self.blocking:
                    flags |= _LOCKFILE_FAIL_IMMEDIATELY
                ok = _LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(ov))
                if not ok:
                    err = ctypes.get_last_error()
                    if not self.blocking and err == _ERROR_LOCK_VIOLATION:
                        os.close(fd)
                        return False
                    raise ctypes.WinError(err)
                self._overlapped = ov
            else:  # pragma: no cover - production host is Windows
                import fcntl

                op = fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH
                if not self.blocking:
                    op |= fcntl.LOCK_NB
                try:
                    fcntl.flock(fd, op)
                except BlockingIOError:
                    os.close(fd)
                    return False
            self.fd = fd
            return True
        except BaseException:
            if self.fd is None:
                with contextlib.suppress(OSError):
                    os.close(fd)
            raise

    def close(self) -> None:
        fd, self.fd = self.fd, None
        if fd is None:
            return
        try:
            if os.name == "nt":
                handle = msvcrt.get_osfhandle(fd)
                ov = self._overlapped
                if ov is not None and not _UnlockFileEx(
                    handle, 0, 1, 0, ctypes.byref(ov)
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
            else:  # pragma: no cover - production host is Windows
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "_ByteLease":
        if not self.acquire():
            raise BlockingIOError("cross-process lease is already held")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class _EmbeddingTurn:
    """Two-stage resource gate.

    A reader briefly takes shared intent, then shared resource, then drops
    intent.  A publisher holds exclusive intent while it drains and holds the
    resource exclusively.  On POSIX, flock is whole-file, so resource uses a
    fixed sibling file; on Windows both leases are byte ranges in one file.
    """

    def __init__(self, db_path: str | os.PathLike[str], *, exclusive: bool):
        db = _resolved_db(db_path)
        base = _logical_lock_path(db, "embedding-resource")
        resource = base if os.name == "nt" else base.with_suffix(".resource.lock")
        self.intent = _ByteLease(base, offset=0, exclusive=exclusive)
        self.resource = _ByteLease(resource, offset=1, exclusive=exclusive)
        self.exclusive = exclusive
        self._entered = False

    def __enter__(self) -> "_EmbeddingTurn":
        self.intent.__enter__()
        try:
            self.resource.__enter__()
        except BaseException:
            self.intent.close()
            raise
        if not self.exclusive:
            self.intent.close()
        self._entered = True
        return self

    def __exit__(self, *_exc: object) -> None:
        if not self._entered:
            return
        self._entered = False
        resource_error: BaseException | None = None
        try:
            self.resource.close()
        except BaseException as exc:  # pragma: no cover - OS unlock failure
            resource_error = exc
        finally:
            if self.exclusive:
                self.intent.close()
        if resource_error is not None:
            raise resource_error


@contextlib.contextmanager
def embedding_db_turn(
    db_path: str | os.PathLike[str],
) -> Iterator[None]:
    """Hold a shared live-DB gate until all provider/SQLite work is closed."""

    with _EmbeddingTurn(db_path, exclusive=False):
        yield


@contextlib.contextmanager
def _embedding_publish_turn(
    db_path: str | os.PathLike[str],
) -> Iterator[None]:
    with _EmbeddingTurn(db_path, exclusive=True):
        yield


@contextlib.asynccontextmanager
async def async_embedding_db_turn(
    db_path: str | os.PathLike[str],
) -> AsyncIterator[None]:
    """Cancellation-safe async adapter for :func:`embedding_db_turn`."""

    turn = _EmbeddingTurn(db_path, exclusive=False)
    enter_task = asyncio.create_task(asyncio.to_thread(turn.__enter__))
    try:
        await asyncio.shield(enter_task)
    except asyncio.CancelledError:
        # A blocking kernel acquisition cannot be cancelled.  Wait until it
        # acquires, release it, and only then propagate cancellation.
        with contextlib.suppress(BaseException):
            await enter_task
        with contextlib.suppress(BaseException):
            await asyncio.to_thread(turn.__exit__, None, None, None)
        raise
    try:
        yield
    finally:
        await asyncio.shield(
            asyncio.to_thread(turn.__exit__, None, None, None)
        )


@contextlib.asynccontextmanager
async def _async_embedding_publish_turn(
    db_path: str | os.PathLike[str],
) -> AsyncIterator[None]:
    turn = _EmbeddingTurn(db_path, exclusive=True)
    enter_task = asyncio.create_task(asyncio.to_thread(turn.__enter__))
    try:
        await asyncio.shield(enter_task)
    except asyncio.CancelledError:
        with contextlib.suppress(BaseException):
            await enter_task
        with contextlib.suppress(BaseException):
            await asyncio.to_thread(turn.__exit__, None, None, None)
        raise
    try:
        yield
    finally:
        await asyncio.shield(
            asyncio.to_thread(turn.__exit__, None, None, None)
        )


@contextlib.contextmanager
def config_yaml_turn(
    config_path: str | os.PathLike[str],
    *,
    exclusive: bool = False,
) -> Iterator[None]:
    """Compatibility alias for the M-02 shared configuration gate."""

    with _shared_config_yaml_turn(config_path, exclusive=exclusive):
        yield


@contextlib.asynccontextmanager
async def async_config_yaml_turn(
    config_path: str | os.PathLike[str],
    *,
    exclusive: bool = False,
) -> AsyncIterator[None]:
    async with _shared_async_config_yaml_turn(
        config_path,
        exclusive=exclusive,
    ):
        yield


def _infer_config_path(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> str:
    for key in ("config_path", "path"):
        value = kwargs.get(key)
        if isinstance(value, (str, os.PathLike)):
            return os.fspath(value)
    for obj in args:
        value = getattr(obj, "config_path", None)
        if isinstance(value, (str, os.PathLike)):
            return os.fspath(value)
        runtime = getattr(obj, "runtime", None)
        value = getattr(runtime, "config_path", None)
        if isinstance(value, (str, os.PathLike)):
            return os.fspath(value)
    raise RuntimeError(
        "config_mutation_guard could not infer config_path; "
        "pass config_path= or use path_getter="
    )


def config_mutation_guard(
    func: Callable[..., Awaitable[Any]] | None = None,
    *,
    path_getter: Callable[..., str | os.PathLike[str]] | None = None,
) -> Any:
    """Compatibility alias; M-02 writers use exclusive transactions."""

    return _shared_config_mutation_guard(func, path_getter=path_getter)


@dataclass
class MigrationReservation:
    """Transferable ownership of the cross-process migration single-flight."""

    db_path: str
    txid: str
    _lease: _ByteLease
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._lease.close()

    def __enter__(self) -> "MigrationReservation":
        if self._closed:
            raise RuntimeError("migration reservation is closed")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def reserve_migration(
    db_path: str | os.PathLike[str],
) -> MigrationReservation | None:
    """Try to reserve one whole-database migration across all processes."""

    db = _resolved_db(db_path)
    lease = _ByteLease(
        _logical_lock_path(db, "migration-orchestrator"),
        offset=0,
        exclusive=True,
        blocking=False,
    )
    if not lease.acquire():
        return None
    return MigrationReservation(str(db), uuid.uuid4().hex, lease)


def create_shadow_path(
    db_path: str | os.PathLike[str],
    *,
    reservation: MigrationReservation | None = None,
) -> Path:
    """Return an internally named same-volume private shadow path."""

    shadow = shadow_path_for_reservation(db_path, reservation=reservation)
    if shadow.exists():
        raise FileExistsError(f"shadow generation already exists: {shadow.name}")
    return shadow


def shadow_path_for_reservation(
    db_path: str | os.PathLike[str],
    *,
    reservation: MigrationReservation | None = None,
) -> Path:
    """Return the deterministic internal leaf owned by a reservation.

    Unlike :func:`create_shadow_path`, this is also valid after the shadow has
    been created and is used to verify ownership at migration start.
    """

    db = _resolved_db(db_path)
    if reservation is not None:
        if reservation._closed:
            raise RuntimeError("migration reservation is closed")
        if _resolved_db(reservation.db_path) != db:
            raise ValueError("migration reservation belongs to another database")
        txid = reservation.txid
    else:
        txid = uuid.uuid4().hex
    generations = db.parent / _GENERATIONS_DIR
    generations.mkdir(parents=True, exist_ok=True)
    shadow = generations / f"{txid}.shadow.db"
    _validate_generation_path(db, shadow, kind="shadow")
    return shadow


def _validate_generation_path(db: Path, candidate: Path, *, kind: str) -> None:
    resolved = candidate.expanduser().resolve(strict=False)
    generations = (db.parent / _GENERATIONS_DIR).resolve(strict=False)
    if resolved.parent != generations:
        raise ValueError(f"{kind} generation must be inside {_GENERATIONS_DIR}")
    if kind == "shadow" and not _SHADOW_RE.fullmatch(resolved.name):
        raise ValueError("shadow generation leaf is not internally generated")
    if resolved.anchor != db.anchor:
        raise ValueError("generation must be on the live database volume")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    # Windows' CRT rejects fsync/FlushFileBuffers on a read-only descriptor.
    # Generations are private/drained while this helper runs, so opening the
    # file read-write is safe and does not itself mutate the contents.
    fd = os.open(str(path), os.O_RDWR)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sidecars(path: Path) -> list[Path]:
    return [
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
        Path(f"{path}-journal"),
    ]


def _assert_no_sidecars(path: Path) -> None:
    unknown = [item.name for item in _sidecars(path) if item.exists()]
    if unknown:
        raise ShadowValidationError(
            "SQLite generation has live sidecars: " + ", ".join(unknown)
        )


def _sqlite_uri(path: Path, *, immutable: bool = False) -> str:
    suffix = "?mode=ro"
    if immutable:
        suffix += "&immutable=1"
    return path.as_uri() + suffix


def _schema_fingerprint(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        """
        SELECT type, name, tbl_name, sql
          FROM sqlite_master
         WHERE name NOT LIKE 'sqlite_%'
         ORDER BY type, name
        """
    ).fetchall()
    encoded = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def checkpoint_close_and_fsync(db_path: str | os.PathLike[str]) -> None:
    """Checkpoint a private generation, close SQLite, and fsync its main file."""

    path = Path(db_path).expanduser().resolve(strict=True)
    wal = Path(f"{path}-wal")
    shm = Path(f"{path}-shm")
    journal = Path(f"{path}-journal")
    # A normal WAL-mode generation can have a WAL+SHM pair before checkpoint.
    # An orphan SHM or rollback journal is not an explainable clean shadow and
    # must not be silently deleted by opening SQLite.
    if shm.exists() and not wal.exists():
        raise ShadowValidationError("SQLite generation has an orphan SHM sidecar")
    if journal.exists():
        raise ShadowValidationError(
            "SQLite generation has an unexpected rollback journal"
        )
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if row is None or int(row[0]) != 0:
            raise ShadowValidationError(f"WAL checkpoint was not clean: {row!r}")
        conn.commit()
    finally:
        conn.close()
    _assert_no_sidecars(path)
    _fsync_file(path)


def validate_shadow_generation(
    shadow_path: str | os.PathLike[str],
    *,
    live_db_path: str | os.PathLike[str],
    expected_model: str,
    expected_dim: int,
    expected_count: int,
    expected_bucket_ids: set[str] | None = None,
    expected_generation: str | None = None,
) -> dict[str, Any]:
    """Strictly validate and fingerprint a clean, closed shadow generation."""

    live = _resolved_db(live_db_path)
    shadow = Path(shadow_path).expanduser().resolve(strict=True)
    _validate_generation_path(live, shadow, kind="shadow")
    if (
        isinstance(expected_dim, bool)
        or not isinstance(expected_dim, int)
        or expected_dim <= 0
    ):
        raise ShadowValidationError("expected vector dimension must be positive")
    if (
        isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or expected_count < 0
    ):
        raise ShadowValidationError("expected row count must be non-negative")
    checkpoint_close_and_fsync(shadow)
    _assert_no_sidecars(shadow)

    conn = sqlite3.connect(_sqlite_uri(shadow, immutable=True), uri=True)
    try:
        quick = conn.execute("PRAGMA quick_check").fetchall()
        full = conn.execute("PRAGMA integrity_check").fetchall()
        if quick != [("ok",)] or full != [("ok",)]:
            raise ShadowValidationError(
                f"SQLite integrity failed: quick={quick!r}, full={full!r}"
            )
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if not {"embeddings", "embeddings_meta"}.issubset(tables):
            raise ShadowValidationError(
                "shadow schema is missing embeddings/embeddings_meta"
            )
        columns = {
            str(row[1]): (str(row[2]).upper(), int(row[5]))
            for row in conn.execute("PRAGMA table_info(embeddings)").fetchall()
        }
        for name in ("bucket_id", "embedding", "updated_at"):
            if name not in columns:
                raise ShadowValidationError(f"shadow schema missing column {name}")
        if columns["bucket_id"][1] != 1:
            raise ShadowValidationError("bucket_id must be the primary key")
        meta = {
            str(key): str(value)
            for key, value in conn.execute(
                "SELECT key, value FROM embeddings_meta"
            ).fetchall()
        }
        if meta.get("model_name", "") != str(expected_model):
            raise ShadowValidationError("shadow model metadata mismatch")
        if meta.get("vector_dim", "") != str(expected_dim):
            raise ShadowValidationError("shadow dimension metadata mismatch")
        if expected_generation is not None:
            if (
                not _TXID_RE.fullmatch(expected_generation)
                or meta.get("generation", "") != expected_generation
            ):
                raise ShadowValidationError("shadow generation metadata mismatch")

        seen: set[str] = set()
        row_count = 0
        for bucket_id, raw in conn.execute(
            "SELECT bucket_id, embedding FROM embeddings"
        ):
            bucket = str(bucket_id or "")
            if not bucket or bucket in seen:
                raise ShadowValidationError("empty or duplicate bucket ID")
            seen.add(bucket)
            row_count += 1
            try:
                vector = json.loads(raw)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ShadowValidationError(
                    f"malformed embedding JSON for one row: {type(exc).__name__}"
                ) from exc
            if not isinstance(vector, list) or len(vector) != expected_dim:
                raise ShadowValidationError("embedding vector dimension mismatch")
            if any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                for value in vector
            ):
                raise ShadowValidationError("embedding vector contains non-finite data")
        if row_count != expected_count:
            raise ShadowValidationError(
                f"shadow row count mismatch: expected={expected_count}, actual={row_count}"
            )
        if expected_bucket_ids is not None and seen != expected_bucket_ids:
            missing = len(expected_bucket_ids - seen)
            extra = len(seen - expected_bucket_ids)
            raise ShadowValidationError(
                f"shadow bucket set mismatch: missing={missing}, extra={extra}"
            )
        schema_fingerprint = _schema_fingerprint(conn)
    finally:
        conn.close()

    _assert_no_sidecars(shadow)
    fresh_hash = _sha256_file(shadow)
    return {
        "leaf": shadow.name,
        "sha256": fresh_hash,
        "size": shadow.stat().st_size,
        "schema_fingerprint": schema_fingerprint,
        "row_count": row_count,
        "model": str(expected_model),
        "dimensions": expected_dim,
        "generation": meta.get("generation", ""),
    }


def inspect_generation(db_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Return non-content metadata for a clean current/old generation."""

    path = Path(db_path).expanduser().resolve(strict=True)
    _assert_no_sidecars(path)
    _fsync_file(path)
    conn = sqlite3.connect(_sqlite_uri(path, immutable=True), uri=True)
    try:
        quick = conn.execute("PRAGMA quick_check").fetchall()
        if quick != [("ok",)]:
            raise ShadowValidationError(f"SQLite quick_check failed: {quick!r}")
        meta = {
            str(key): str(value)
            for key, value in conn.execute(
                "SELECT key, value FROM embeddings_meta"
            ).fetchall()
        }
        row_count = int(
            conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        )
        schema = _schema_fingerprint(conn)
    finally:
        conn.close()
    return {
        "leaf": path.name,
        "sha256": _sha256_file(path),
        "size": path.stat().st_size,
        "schema_fingerprint": schema,
        "row_count": row_count,
        "model": meta.get("model_name", ""),
        "dimensions": int(meta.get("vector_dim", "0") or 0),
        "generation": meta.get("generation", ""),
    }


def _manifest_path(db: Path) -> Path:
    return db.parent / _MANIFEST_LEAF


def _write_through_replace(source: Path, target: Path) -> None:
    if (
        source.resolve(strict=False).anchor.lower()
        != target.resolve(strict=False).anchor.lower()
    ):
        raise ValueError("write-through replace requires one volume")
    if os.name == "nt":
        from ctypes import wintypes

        move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move.restype = wintypes.BOOL
        flags = 0x1 | 0x8  # MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
        if not move(str(source), str(target), flags):
            raise ctypes.WinError(ctypes.get_last_error())
    else:  # pragma: no cover - production host is Windows
        os.replace(source, target)
        try:
            fd = os.open(str(target.parent), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _durable_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)
    try:
        _write_through_replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    reread = json.loads(path.read_text(encoding="utf-8"))
    if reread != dict(value):
        raise EmbeddingPublishError("durable manifest reread mismatch")


def _load_manifest(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublishRecoveryError(
            f"embedding publish manifest is unreadable: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(value, dict) or value.get("schema") != _MANIFEST_SCHEMA:
        raise PublishRecoveryError("embedding publish manifest schema is invalid")
    txid = value.get("txid")
    if not isinstance(txid, str) or not _TXID_RE.fullmatch(txid):
        raise PublishRecoveryError("embedding publish manifest txid is invalid")
    return value


def _validate_manifest_for_db(db: Path, manifest: Mapping[str, Any]) -> None:
    txid = str(manifest.get("txid", ""))
    if not _TXID_RE.fullmatch(txid):
        raise PublishRecoveryError("embedding publish manifest txid is invalid")
    old_info = manifest.get("old_db")
    new_info = manifest.get("shadow_db")
    if not isinstance(old_info, Mapping) or not isinstance(new_info, Mapping):
        raise PublishRecoveryError("embedding publish manifest DB records are invalid")
    if old_info.get("leaf") != db.name:
        raise PublishRecoveryError("manifest current DB leaf does not match live DB")
    if new_info.get("leaf") != f"{txid}.shadow.db":
        raise PublishRecoveryError("manifest shadow leaf is not internally generated")
    for label, info in (("old", old_info), ("shadow", new_info)):
        digest = info.get("sha256")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise PublishRecoveryError(f"manifest {label} DB hash is invalid")


def _semantic_config_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def embedding_config_semantic_hash(
    config_or_path: Mapping[str, Any] | str | os.PathLike[str],
) -> str:
    """Hash only the whitelisted, non-secret embedding config projection."""

    if isinstance(config_or_path, Mapping):
        config = config_or_path
    else:
        config = _read_yaml(Path(config_or_path))
    return _semantic_config_hash(_embedding_config_view(config))


def _verify_config_hash(
    config_path: str | os.PathLike[str],
    expected: str,
    *,
    phase: str,
) -> None:
    if not expected:
        raise EmbeddingPublishError(f"{phase} config semantic hash is required")
    actual = embedding_config_semantic_hash(config_path)
    if actual != expected:
        raise EmbeddingPublishError(
            f"{phase} config semantic hash mismatch: "
            f"expected={expected}, actual={actual}"
        )


def _read_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise PublishRecoveryError("PyYAML is required for config recovery")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PublishRecoveryError(
            f"config reread failed: {type(exc).__name__}: {exc}"
        ) from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise PublishRecoveryError("config root must be a mapping")
    return raw


def _embedding_config_view(config: Mapping[str, Any]) -> dict[str, Any]:
    embedding = config.get("embedding", {})
    if not isinstance(embedding, Mapping):
        raise PublishRecoveryError("config embedding section must be a mapping")
    return {
        key: embedding.get(key)
        for key in sorted(_EMBEDDING_CONFIG_FIELDS)
        if key in embedding and embedding.get(key) is not None
    }


def _validate_inverse_patch(patch: Mapping[str, Any] | None) -> dict[str, Any]:
    result = dict(patch or {})
    unknown = set(result) - _EMBEDDING_CONFIG_FIELDS
    if unknown:
        raise ValueError(
            "config inverse patch contains non-E-MIG fields: "
            + ", ".join(sorted(unknown))
        )
    for key, value in result.items():
        if key in {"api_key"}:
            raise ValueError("secret embedding fields must not enter the manifest")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"non-finite config value for {key}")
        if key == "base_url" and value is not None:
            parsed = urlsplit(str(value))
            if (
                parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "base_url with credentials/query/fragment must not enter "
                    "the E-MIG manifest"
                )
    return result


def _mutate_embedding_patch(
    config: dict[str, Any],
    patch: Mapping[str, Any],
) -> None:
    embedding = config.setdefault("embedding", {})
    if not isinstance(embedding, dict):
        raise PublishRecoveryError("config embedding section is not mutable")
    for key, value in patch.items():
        if value is None:
            embedding.pop(key, None)
        else:
            embedding[key] = value


@dataclass
class _ConfigPatchContext:
    path: Path
    session: ConfigTransactionSession
    prepared: PreparedConfig
    forward: dict[str, Any]
    persisted: bool = False


_config_patch_context: contextvars.ContextVar[_ConfigPatchContext | None] = (
    contextvars.ContextVar("embedding_publish_config_patch", default=None)
)


def _apply_embedding_patch(path: Path, patch: Mapping[str, Any]) -> None:
    """E-MIG fault seam backed by the shared M-02 transaction primitive.

    Existing fault-injection tests monkeypatch this symbol.  Inside a live
    publish it either persists the already prepared candidate or restores the
    session's exact old bytes.  Standalone recovery calls use a complete M-02
    transaction rather than a private YAML writer.
    """

    context = _config_patch_context.get()
    resolved = Path(path).expanduser().resolve(strict=False)
    if context is not None and resolved == context.path:
        if dict(patch) == context.forward and not context.persisted:
            context.session.persist(context.prepared)
            context.persisted = True
        else:
            if context.session.disk_changed:
                context.session.restore_old()
            context.persisted = False
        return

    run_config_transaction(
        resolved,
        lambda config: _mutate_embedding_patch(config, patch),
    )


async def _call(callback: Callback | None, *args: Any) -> Any:
    if callback is None:
        return None
    result = callback(*args)
    if inspect.isawaitable(result):
        return await result
    return result


def _old_leaf(txid: str) -> str:
    return f"{txid}.old.db"


def _failed_leaf(txid: str) -> str:
    return f"{txid}.failed.db"


def _classify(path: Path, old_hash: str, new_hash: str) -> str:
    if not path.exists():
        return "absent"
    actual = _sha256_file(path)
    if actual == old_hash:
        return "old"
    if actual == new_hash:
        return "new"
    return "unknown"


def _rollback_db(db: Path, manifest: Mapping[str, Any]) -> None:
    _validate_manifest_for_db(db, manifest)
    txid = str(manifest["txid"])
    generations = db.parent / _GENERATIONS_DIR
    old = generations / _old_leaf(txid)
    shadow = generations / str(manifest["shadow_db"]["leaf"])
    failed = generations / _failed_leaf(txid)
    old_hash = str(manifest["old_db"]["sha256"])
    new_hash = str(manifest["shadow_db"]["sha256"])
    for candidate in (db, old, shadow):
        if candidate.exists():
            _assert_no_sidecars(candidate)
    current_kind = _classify(db, old_hash, new_hash)
    old_kind = _classify(old, old_hash, new_hash)
    shadow_kind = _classify(shadow, old_hash, new_hash)

    if current_kind == "unknown" or old_kind not in {"absent", "old"}:
        raise PublishRecoveryError(
            "cannot identify current/old generation without guessing"
        )
    if shadow_kind not in {"absent", "new"}:
        raise PublishRecoveryError("cannot identify shadow generation")
    if current_kind == "old":
        if old_kind != "absent":
            raise PublishRecoveryError("duplicate old generation is ambiguous")
        if shadow_kind == "new":
            if failed.exists():
                raise PublishRecoveryError("failed quarantine leaf already exists")
            _write_through_replace(shadow, failed)
        return
    if current_kind == "new":
        if old_kind != "old":
            raise PublishRecoveryError("new current has no exact old generation")
        if failed.exists():
            raise PublishRecoveryError("failed quarantine leaf already exists")
        _write_through_replace(db, failed)
        _write_through_replace(old, db)
        return
    if current_kind == "absent":
        if old_kind != "old":
            raise PublishRecoveryError("current is absent and old generation missing")
        _write_through_replace(old, db)
        if shadow_kind == "new":
            if failed.exists():
                raise PublishRecoveryError("failed quarantine leaf already exists")
            _write_through_replace(shadow, failed)
        return
    raise PublishRecoveryError("unhandled generation classification")


async def publish_shadow_generation(
    *,
    db_path: str | os.PathLike[str],
    shadow_path: str | os.PathLike[str],
    expected_model: str,
    expected_dim: int,
    expected_count: int,
    config_path: str | os.PathLike[str] | None = None,
    config_forward_patch: Mapping[str, Any] | None = None,
    old_config_sha256: str = "",
    candidate_config_sha256: str = "",
    config_inverse_patch: Mapping[str, Any] | None = None,
    config_commit: Callback | None = None,
    config_restore: Callback | None = None,
    runtime_close: Callback | None = None,
    runtime_apply: Callback | None = None,
    runtime_restore: Callback | None = None,
    runtime_open_probe: Callback | None = None,
    reservation: MigrationReservation | None = None,
    expected_bucket_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Validate and atomically publish one private shadow generation.

    Callbacks are zero-argument closures.  They may be synchronous or async.
    Prefer ``config_forward_patch``: under the exclusive config gate the core
    fresh-reads config, derives the inverse and both semantic hashes, and owns
    durable write/reread/rollback.  The callback/hash arguments remain for
    integration adapters, but their fresh old hash is verified under that same
    gate before the DB namespace can change.  Runtime callbacks publish/restore
    the complete component reference snapshot.
    """

    db = _resolved_db(db_path)
    shadow = Path(shadow_path).expanduser().resolve(strict=True)
    _validate_generation_path(db, shadow, kind="shadow")
    match = _SHADOW_RE.fullmatch(shadow.name)
    assert match is not None
    txid = match.group("txid")
    if reservation is not None:
        if reservation._closed or reservation.txid != txid:
            raise ValueError("shadow does not belong to the active reservation")
        if _resolved_db(reservation.db_path) != db:
            raise ValueError("reservation belongs to another live DB")
    inverse = _validate_inverse_patch(config_inverse_patch)
    forward = (
        _validate_inverse_patch(config_forward_patch)
        if config_forward_patch is not None
        else None
    )
    if (forward is not None or config_commit is not None) and config_path is None:
        raise ValueError("config_path is required for a config mutation")
    shadow_info = validate_shadow_generation(
        shadow,
        live_db_path=db,
        expected_model=expected_model,
        expected_dim=expected_dim,
        expected_count=expected_count,
        expected_bucket_ids=expected_bucket_ids,
        expected_generation=txid,
    )

    generations = db.parent / _GENERATIONS_DIR
    old = generations / _old_leaf(txid)
    if old.exists():
        raise FileExistsError(f"old generation already exists: {old.name}")
    manifest_path = _manifest_path(db)
    manifest: dict[str, Any] = {}
    runtime_close_attempted = False
    config_attempted = False

    async with contextlib.AsyncExitStack() as stack:
        config_session: ConfigTransactionSession | None = None
        if config_path is not None:
            config_session = await stack.enter_async_context(
                async_open_config_transaction(config_path)
            )
            if forward is not None:
                prepared_config = config_session.prepare(
                    lambda config: _mutate_embedding_patch(config, forward)
                )
                current_embedding = prepared_config.old.mapping.get(
                    "embedding", {}
                )
                if not isinstance(current_embedding, Mapping):
                    raise EmbeddingPublishError(
                        "config embedding section must be a mapping"
                    )
                inverse = {
                    key: (
                        current_embedding.get(key)
                        if key in current_embedding
                        else None
                    )
                    for key in forward
                }
                old_config_sha256 = embedding_config_semantic_hash(
                    prepared_config.old.mapping
                )
                candidate_config_sha256 = embedding_config_semantic_hash(
                    prepared_config.candidate
                )
                patch_context = _ConfigPatchContext(
                    Path(config_path).expanduser().resolve(strict=False),
                    config_session,
                    prepared_config,
                    dict(forward),
                )
                context_token = _config_patch_context.set(patch_context)
                stack.callback(_config_patch_context.reset, context_token)
            elif config_commit is not None:
                raise ValueError(
                    "legacy config_commit callback is not supported; "
                    "use config_forward_patch"
                )
            else:
                fresh_config = config_session.read_fresh()
                fresh_hash = embedding_config_semantic_hash(fresh_config)
                old_config_sha256 = old_config_sha256 or fresh_hash
                candidate_config_sha256 = candidate_config_sha256 or fresh_hash
                if old_config_sha256 != fresh_hash:
                    raise EmbeddingPublishError(
                        "fresh-old config semantic hash mismatch"
                    )

        await stack.enter_async_context(_async_embedding_publish_turn(db))
        try:
            runtime_close_attempted = True
            await _call(runtime_close)
            # The current database may legitimately be in WAL mode.  Under the
            # exclusive gate no writer can race this checkpoint; only after
            # checkpoint+close may the main-file hash describe its semantics.
            checkpoint_close_and_fsync(db)
            old_info = inspect_generation(db)
            manifest = {
                "schema": _MANIFEST_SCHEMA,
                "txid": txid,
                "state": "PUBLISH_INTENT",
                "old_generation": f"old-{txid}",
                "new_generation": f"new-{txid}",
                "old_db": old_info,
                "shadow_db": shadow_info,
                "old_config_sha256": str(old_config_sha256),
                "candidate_config_sha256": str(candidate_config_sha256),
                "config_inverse_patch": inverse,
            }
            _durable_json(manifest_path, manifest)
            _set_health(True, "publishing", txid=txid)

            _write_through_replace(db, old)
            manifest["state"] = "OLD_STAGED"
            _durable_json(manifest_path, manifest)

            _write_through_replace(shadow, db)
            manifest["state"] = "DB_PUBLISHED"
            _durable_json(manifest_path, manifest)
            published = inspect_generation(db)
            if (
                published["sha256"] != shadow_info["sha256"]
                or published["schema_fingerprint"]
                != shadow_info["schema_fingerprint"]
                or published["row_count"] != shadow_info["row_count"]
            ):
                raise EmbeddingPublishError("published DB reread verification failed")

            if forward is not None:
                assert config_path is not None
                assert config_session is not None
                config_attempted = True
                _apply_embedding_patch(Path(config_path), forward)
                _verify_config_hash(
                    config_path,
                    candidate_config_sha256,
                    phase="candidate",
                )
            elif config_commit is not None:
                raise AssertionError("legacy config callback passed validation")
            manifest["state"] = "CONFIG_COMMITTED"
            _durable_json(manifest_path, manifest)

            await _call(runtime_apply)
            await _call(runtime_open_probe)
            manifest["state"] = "COMPONENT_OPENED"
            _durable_json(manifest_path, manifest)

            manifest["state"] = "COMMITTED"
            _durable_json(manifest_path, manifest)
            _set_health(True, "committed", txid=txid)
            return dict(manifest)
        except BaseException as original:
            try:
                await _call(runtime_close)
                # A failure before the prepared manifest cannot have mutated
                # the DB namespace.  Do not turn a runtime-close error into a
                # spurious rollback failure by indexing an empty manifest.
                if manifest:
                    _rollback_db(db, manifest)
                if config_attempted and config_path is not None:
                    if config_restore is not None:
                        raise ValueError(
                            "legacy config_restore callback is not supported"
                        )
                    _apply_embedding_patch(Path(config_path), inverse)
                    _verify_config_hash(
                        config_path,
                        old_config_sha256,
                        phase="restored",
                    )
                if runtime_close_attempted:
                    await _call(runtime_restore)
                await _call(runtime_open_probe)
                restored = inspect_generation(db)
                if manifest and restored["sha256"] != manifest["old_db"]["sha256"]:
                    raise PublishRecoveryError("restored DB hash mismatch")
                if manifest:
                    manifest["state"] = "ABORTED_RESTORED"
                    # Manifests are durable operational evidence, not error
                    # logs.  Exception text can contain provider URLs or
                    # credentials, so persist only the exception class.
                    manifest["error_type"] = type(original).__name__
                    _durable_json(manifest_path, manifest)
                _set_health(
                    True,
                    "aborted_restored",
                    error=type(original).__name__,
                    txid=txid,
                )
            except BaseException as rollback:
                combined = PublishRollbackError(original, rollback)
                _set_health(
                    False,
                    "rollback_failed",
                    error=(
                        f"{type(original).__name__}+"
                        f"{type(rollback).__name__}"
                    ),
                    txid=txid,
                )
                raise combined from rollback
            raise


def recover_pending_publish(
    config_path: str | os.PathLike[str] | None,
    db_path: str | os.PathLike[str],
    runtime_open_probe: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Restore the old generation for an interrupted nonterminal publish.

    This must run before constructing the live ``EmbeddingEngine``.  Recovery
    never rolls forward and never selects a generation by mtime.
    """

    db = _resolved_db(db_path)
    manifest_path = _manifest_path(db)
    txid = ""
    try:
        # Same global order as live publication: config, then embedding
        # intent/resource.  Config handlers take only the shared config gate and
        # may construct an engine; reversing these two gates would deadlock.
        with contextlib.ExitStack() as stack:
            config_session: ConfigTransactionSession | None = None
            if config_path is not None:
                config_session = stack.enter_context(
                    open_config_transaction(config_path)
                )
            stack.enter_context(_embedding_publish_turn(db))
            manifest = _load_manifest(manifest_path)
            if manifest is None:
                _set_health(True, "idle")
                return get_publish_health()
            _validate_manifest_for_db(db, manifest)
            state = str(manifest.get("state", ""))
            txid = str(manifest["txid"])
            if state == "COMMITTED":
                current = inspect_generation(db)
                if current["sha256"] != manifest["shadow_db"]["sha256"]:
                    raise PublishRecoveryError(
                        "stable manifest does not match current embedding DB"
                    )
                if config_path is not None:
                    _verify_config_hash(
                        config_path,
                        str(manifest.get("candidate_config_sha256", "")),
                        phase="committed",
                    )
                if runtime_open_probe is not None:
                    runtime_open_probe()
                _set_health(True, "committed", txid=txid)
                return get_publish_health()
            if state == "ABORTED_RESTORED":
                current = inspect_generation(db)
                if current["sha256"] != manifest["old_db"]["sha256"]:
                    raise PublishRecoveryError(
                        "restored manifest does not match current embedding DB"
                    )
                if config_path is not None:
                    _verify_config_hash(
                        config_path,
                        str(manifest.get("old_config_sha256", "")),
                        phase="aborted-restored",
                    )
                if runtime_open_probe is not None:
                    runtime_open_probe()
                _set_health(True, "aborted_restored", txid=txid)
                return get_publish_health()
            if state not in {
                "PUBLISH_INTENT",
                "OLD_STAGED",
                "DB_PUBLISHED",
                "CONFIG_COMMITTED",
                "COMPONENT_OPENED",
            }:
                raise PublishRecoveryError(
                    f"unknown embedding publish state: {state!r}"
                )

            _rollback_db(db, manifest)
            if config_path is not None and manifest.get("config_inverse_patch"):
                assert config_session is not None
                inverse_patch = _validate_inverse_patch(
                    manifest["config_inverse_patch"]
                )
                prepared_config = config_session.prepare(
                    lambda config: _mutate_embedding_patch(
                        config,
                        inverse_patch,
                    )
                )
                config_session.persist(prepared_config)
                _verify_config_hash(
                    config_path,
                    str(manifest.get("old_config_sha256", "")),
                    phase="startup-restored",
                )
            elif config_path is not None:
                _verify_config_hash(
                    config_path,
                    str(manifest.get("old_config_sha256", "")),
                    phase="startup-restored",
                )
            current = inspect_generation(db)
            if current["sha256"] != manifest["old_db"]["sha256"]:
                raise PublishRecoveryError("startup-restored DB hash mismatch")
            if runtime_open_probe is not None:
                runtime_open_probe()
            manifest["state"] = "ABORTED_RESTORED"
            _durable_json(manifest_path, manifest)
        _set_health(True, "aborted_restored", txid=txid)
        return get_publish_health()
    except BaseException as exc:
        _set_health(
            False,
            "recovery_failed",
            error=type(exc).__name__,
            txid=txid,
        )
        raise


__all__ = [
    "EmbeddingPublishError",
    "ShadowValidationError",
    "PublishRecoveryError",
    "PublishRollbackError",
    "MigrationReservation",
    "embedding_db_turn",
    "async_embedding_db_turn",
    "config_yaml_turn",
    "async_config_yaml_turn",
    "config_mutation_guard",
    "embedding_config_semantic_hash",
    "reserve_migration",
    "create_shadow_path",
    "shadow_path_for_reservation",
    "checkpoint_close_and_fsync",
    "validate_shadow_generation",
    "inspect_generation",
    "publish_shadow_generation",
    "recover_pending_publish",
    "get_publish_health",
]
