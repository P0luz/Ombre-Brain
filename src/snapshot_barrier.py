"""M-04 batch 2: isolated coordination for a future Markdown snapshot.

This module deliberately does *not* copy a vault, read SQLite, create an
archive, publish a restore, or attach itself to a running server.  It supplies
only a cross-process shared/exclusive barrier and a fail-closed journal
preflight.  A later batch must place ``markdown_writer_turn`` around every
Markdown writer before ``authoritative_markdown_snapshot_turn`` may be used on
a live vault.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager, suppress
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time
from typing import AsyncIterator, Iterator


M03_JOURNAL_DIR = ".import-transactions"
EMIG_JOURNAL_NAME = ".embedding-publish.json"
_M03_SCHEMA_VERSION = 1
_M03_TERMINAL_STATES = frozenset({"COMMITTED", "ROLLED_BACK"})
_EMIG_TERMINAL_STATES = frozenset({"COMMITTED", "ABORTED_RESTORED"})
_MAX_JOURNAL_BYTES = 1024 * 1024

# This is a declared future order, not a claim that existing writers already
# participate.  M-03's internal order is preserved unchanged after the gate.
FUTURE_MARKDOWN_WRITER_LOCK_ORDER = (
    "m04-markdown-snapshot(shared)",
    "m03-import-apply (M-03 only)",
    "m01-content-quota-pinned (M-03 only)",
    "m01-content-quota-high_importance (M-03 only)",
    "m01-sorted-bucket-leases (M-03 only)",
)


class SnapshotBarrierError(RuntimeError):
    """Base class for snapshot coordination failures."""


class SnapshotBarrierTimeout(SnapshotBarrierError):
    """A shared or exclusive lease did not become available before its deadline."""


class SnapshotPreconditionError(SnapshotBarrierError):
    """A nonterminal or unsafe M-03/E-MIG journal blocks a snapshot."""


def _is_reparse_point(info: os.stat_result) -> bool:
    return bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _canonical_vault_root(value: str | os.PathLike[str]) -> Path:
    path = Path(value).expanduser()
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise SnapshotPreconditionError("snapshot root cannot be inspected") from exc
    if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info) or not stat.S_ISDIR(info.st_mode):
        raise SnapshotPreconditionError("snapshot root must be a regular directory")
    return path.resolve(strict=True)


def _default_lock_root() -> Path:
    # This lives outside the vault so merely coordinating future work never
    # creates a `.locks` directory or other artifact inside authoritative data.
    return Path(tempfile.gettempdir()) / "ombre-brain-runtime" / "m04-snapshot-locks"


def _barrier_lock_path(root: Path, lock_root: str | os.PathLike[str] | None) -> Path:
    base = Path(lock_root) if lock_root is not None else _default_lock_root()
    base = base.expanduser().resolve(strict=False)
    digest = hashlib.sha256(
        os.path.normcase(str(root)).encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    return base / f"{digest}.lock"


if os.name == "nt":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
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


class _BarrierLease:
    """A one-byte shared/exclusive kernel lease with nonblocking acquisition."""

    def __init__(self, path: Path, *, exclusive: bool):
        self.path = path
        self.exclusive = exclusive
        self.fd: int | None = None
        self._overlapped: _OVERLAPPED | None = None

    def try_acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() or self.path.is_symlink():
            try:
                info = os.lstat(self.path)
            except OSError as exc:
                raise SnapshotBarrierError("snapshot barrier lock cannot be inspected") from exc
            if (
                stat.S_ISLNK(info.st_mode)
                or _is_reparse_point(info)
                or not stat.S_ISREG(info.st_mode)
                or info.st_size > 1
            ):
                raise SnapshotBarrierError("snapshot barrier lock state is unsafe")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(str(self.path), flags, 0o600)
        try:
            size = os.fstat(fd).st_size
            if size == 0:
                os.write(fd, b"\0")
                os.fsync(fd)
            elif size != 1:
                raise SnapshotBarrierError("snapshot barrier lock state is corrupt")
            if os.name == "nt":
                import msvcrt

                overlapped = _OVERLAPPED()
                flags = _LOCKFILE_FAIL_IMMEDIATELY
                if self.exclusive:
                    flags |= _LOCKFILE_EXCLUSIVE_LOCK
                if not _LockFileEx(
                    msvcrt.get_osfhandle(fd), flags, 0, 1, 0, ctypes.byref(overlapped)
                ):
                    if ctypes.get_last_error() == _ERROR_LOCK_VIOLATION:
                        os.close(fd)
                        return False
                    raise ctypes.WinError(ctypes.get_last_error())
                self._overlapped = overlapped
            else:  # pragma: no cover - production host is Windows
                import fcntl

                operation = fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH
                try:
                    fcntl.flock(fd, operation | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(fd)
                    return False
            self.fd = fd
            return True
        except BaseException:
            if self.fd is None:
                with suppress(OSError):
                    os.close(fd)
            raise

    def close(self) -> None:
        fd, self.fd = self.fd, None
        if fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                if self._overlapped is not None and not _UnlockFileEx(
                    msvcrt.get_osfhandle(fd), 0, 1, 0, ctypes.byref(self._overlapped)
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
            else:  # pragma: no cover - production host is Windows
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


async def _acquire_lease(
    lease: _BarrierLease, *, timeout_seconds: float
) -> None:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    deadline = time.monotonic() + timeout_seconds
    while True:
        attempt = asyncio.create_task(asyncio.to_thread(lease.try_acquire))
        try:
            acquired = await asyncio.shield(attempt)
        except asyncio.CancelledError:
            acquired = False
            with suppress(BaseException):
                acquired = await attempt
            if acquired:
                with suppress(BaseException):
                    await asyncio.shield(asyncio.to_thread(lease.close))
            raise
        if acquired:
            return
        if time.monotonic() >= deadline:
            raise SnapshotBarrierTimeout("timed out waiting for M-04 snapshot barrier")
        await asyncio.sleep(0.01)


def _acquire_lease_sync(lease: _BarrierLease, *, timeout_seconds: float) -> None:
    """Blocking peer of ``_acquire_lease`` using the identical lock file."""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    deadline = time.monotonic() + timeout_seconds
    while True:
        if lease.try_acquire():
            return
        if time.monotonic() >= deadline:
            raise SnapshotBarrierTimeout("timed out waiting for M-04 snapshot barrier")
        time.sleep(0.01)


@asynccontextmanager
async def _barrier_turn(
    root: Path,
    *,
    exclusive: bool,
    timeout_seconds: float,
    lock_root: str | os.PathLike[str] | None,
) -> AsyncIterator[Path]:
    lease = _BarrierLease(
        _barrier_lock_path(root, lock_root),
        exclusive=exclusive,
    )
    await _acquire_lease(lease, timeout_seconds=timeout_seconds)
    try:
        yield lease.path
    finally:
        await asyncio.shield(asyncio.to_thread(lease.close))


def _read_regular_json(path: Path, label: str) -> dict:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise SnapshotPreconditionError(f"{label} is missing or unreadable") from exc
    if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info) or not stat.S_ISREG(info.st_mode):
        raise SnapshotPreconditionError(f"{label} is not a regular file")
    if info.st_size < 0 or info.st_size > _MAX_JOURNAL_BYTES:
        raise SnapshotPreconditionError(f"{label} exceeds the preflight size limit")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotPreconditionError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise SnapshotPreconditionError(f"{label} must contain an object")
    return value


def _assert_no_active_m03_journal(root: Path) -> tuple[str, ...]:
    journal_root = root / M03_JOURNAL_DIR
    if not journal_root.exists():
        return ()
    try:
        info = os.lstat(journal_root)
    except OSError as exc:
        raise SnapshotPreconditionError("M-03 journal root cannot be inspected") from exc
    if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info) or not stat.S_ISDIR(info.st_mode):
        raise SnapshotPreconditionError("M-03 journal root is unsafe")
    terminal: list[str] = []
    for txdir in sorted(journal_root.iterdir(), key=lambda item: item.name):
        try:
            tx_info = os.lstat(txdir)
        except OSError as exc:
            raise SnapshotPreconditionError("M-03 transaction cannot be inspected") from exc
        if stat.S_ISLNK(tx_info.st_mode) or _is_reparse_point(tx_info) or not stat.S_ISDIR(tx_info.st_mode):
            raise SnapshotPreconditionError("M-03 transaction workspace is unsafe")
        manifest = _read_regular_json(txdir / "manifest.json", "M-03 manifest")
        if manifest.get("schema_version") != _M03_SCHEMA_VERSION or manifest.get("txid") != txdir.name:
            raise SnapshotPreconditionError("M-03 manifest schema or transaction ID is invalid")
        state = manifest.get("state")
        if not isinstance(state, str) or state not in _M03_TERMINAL_STATES:
            raise SnapshotPreconditionError(f"active M-03 journal blocks snapshot: {txdir.name}")
        terminal.append(txdir.name)
    return tuple(terminal)


def _assert_no_active_emig_journal(
    root: Path, embedding_db_path: str | os.PathLike[str] | None
) -> str:
    db = Path(embedding_db_path) if embedding_db_path is not None else root / "embeddings.db"
    manifest_path = db.expanduser().resolve(strict=False).parent / EMIG_JOURNAL_NAME
    if not manifest_path.exists():
        return "absent"
    manifest = _read_regular_json(manifest_path, "E-MIG manifest")
    state = manifest.get("state")
    if not isinstance(manifest.get("schema"), str) or not isinstance(state, str):
        raise SnapshotPreconditionError("E-MIG manifest schema or state is invalid")
    if state not in _EMIG_TERMINAL_STATES:
        raise SnapshotPreconditionError(f"active E-MIG journal blocks snapshot: {state}")
    return state


@dataclass(frozen=True)
class SnapshotCoordination:
    """A non-mutating snapshot preflight result, not a snapshot payload."""

    buckets_dir: str
    barrier_lock_path: str
    terminal_m03_transactions: tuple[str, ...]
    emig_state: str
    sqlite_policy: str = "derived:not-captured:rebuild-required"


def future_markdown_writer_lock_order() -> tuple[str, ...]:
    """Expose the required order for the later writer-integration batch."""

    return FUTURE_MARKDOWN_WRITER_LOCK_ORDER


@asynccontextmanager
async def markdown_writer_turn(
    buckets_dir: str | os.PathLike[str],
    *,
    timeout_seconds: float = 30.0,
    lock_root: str | os.PathLike[str] | None = None,
) -> AsyncIterator[None]:
    """Outermost shared gate for one authoritative Markdown mutation."""

    root = _canonical_vault_root(buckets_dir)
    async with _barrier_turn(
        root,
        exclusive=False,
        timeout_seconds=timeout_seconds,
        lock_root=lock_root,
    ):
        yield


@contextmanager
def markdown_writer_turn_sync(
    buckets_dir: str | os.PathLike[str],
    *,
    timeout_seconds: float = 30.0,
    lock_root: str | os.PathLike[str] | None = None,
) -> Iterator[None]:
    """Synchronous shared peer of ``markdown_writer_turn``.

    Legacy synchronous writers use the same canonical vault digest, external
    lock root, and one-byte shared kernel lease as async writers.  It never
    creates a vault-local coordination artifact.
    """

    root = _canonical_vault_root(buckets_dir)
    lease = _BarrierLease(
        _barrier_lock_path(root, lock_root),
        exclusive=False,
    )
    _acquire_lease_sync(lease, timeout_seconds=timeout_seconds)
    try:
        yield
    finally:
        lease.close()


@asynccontextmanager
async def authoritative_markdown_snapshot_turn(
    buckets_dir: str | os.PathLike[str],
    *,
    embedding_db_path: str | os.PathLike[str] | None = None,
    timeout_seconds: float = 30.0,
    lock_root: str | os.PathLike[str] | None = None,
) -> AsyncIterator[SnapshotCoordination]:
    """Acquire the future exclusive gate and reject active durable journals.

    The yielded value only records the explicit derived-SQLite policy.  No file
    from the vault or SQLite is opened for copy, mutation, or restoration.
    """

    root = _canonical_vault_root(buckets_dir)
    async with _barrier_turn(
        root,
        exclusive=True,
        timeout_seconds=timeout_seconds,
        lock_root=lock_root,
    ) as barrier_path:
        terminal_m03 = _assert_no_active_m03_journal(root)
        emig_state = _assert_no_active_emig_journal(root, embedding_db_path)
        yield SnapshotCoordination(
            buckets_dir=str(root),
            barrier_lock_path=str(barrier_path),
            terminal_m03_transactions=terminal_m03,
            emig_state=emig_state,
        )


__all__ = [
    "SnapshotBarrierError",
    "SnapshotBarrierTimeout",
    "SnapshotPreconditionError",
    "SnapshotCoordination",
    "FUTURE_MARKDOWN_WRITER_LOCK_ORDER",
    "future_markdown_writer_lock_order",
    "markdown_writer_turn",
    "markdown_writer_turn_sync",
    "authoritative_markdown_snapshot_turn",
]
