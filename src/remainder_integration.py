"""Remainder integration: merge wiring, startup recovery, and health reporting.

Thread-safe process status tracking for remainder sidecar operations.
Does not acquire M-04 or M-01 locks internally; startup runs before
request admission opens.
"""

from __future__ import annotations

import errno
import logging
import os
import re
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import remainder_sidecar as rs
from remainder_sidecar import (
    REMAINDER_DIR,
    STATE_COMMITTED,
    STATE_ABORTED,
    STATE_CONFLICT,
    STATE_PREPARED,
    RemainderConflictError,
    RemainderQuarantineError,
    RemainderSidecarError,
    archive_if_needed,
    classify_prepared,
    recover_prepared_entries,
    validate_archive_member,
    validate_lock_member,
)

logger = logging.getLogger("remainder_integration")

BUCKET_DIRS = ("permanent", "dynamic", "feel", "plans", "letters", "archive")


class RemainderIntegrationError(RuntimeError):
    """Fail-closed integration error — merge must not retry or create."""

_BUCKET_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_ID_LINE_RE = re.compile(r"^id:\s*(.+)$", re.MULTILINE)


# ---- thread-safe process status ----

@dataclass
class _ProcessStatus:
    lock: threading.Lock = field(default_factory=threading.Lock)
    wiring: str = "inactive"
    startup_state: str = "pending"
    unresolved: int = 0
    errors: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    last_recovery: str | None = None

_status = _ProcessStatus()
_MAX_ERROR_TEXT = 200


_VALID_CODES = frozenset({
    "md_abort_double_fail",
    "md_false_abort_fail",
    "commit_failed",
    "archive_failed",
    "cancel_abort_fail",
    "startup_archive_fail",
    "startup_recovery_fail",
})
_ENTRY_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_EXC_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,120}$")


def mark_unresolved(
    *,
    code: str,
    bucket_id: str = "",
    entry_id: str = "",
    exc_type: str = "",
) -> None:
    if code not in _VALID_CODES:
        raise ValueError(f"mark_unresolved: invalid code {code!r}")
    if bucket_id and not _BUCKET_ID_RE.fullmatch(bucket_id):
        raise ValueError("mark_unresolved: invalid bucket_id")
    if entry_id and not _ENTRY_ID_RE.fullmatch(entry_id):
        raise ValueError("mark_unresolved: invalid entry_id")
    if exc_type and not _EXC_TYPE_RE.fullmatch(exc_type):
        raise ValueError("mark_unresolved: invalid exc_type")
    parts = [code]
    if bucket_id:
        parts.append(bucket_id)
    if entry_id:
        parts.append(entry_id)
    if exc_type:
        parts.append(exc_type)
    safe = ":".join(parts)
    with _status.lock:
        _status.unresolved += 1
        _status.errors.append(safe)


def clear_unresolved() -> None:
    with _status.lock:
        _status.unresolved = 0
        _status.errors.clear()


def _set_wiring(state: str) -> None:
    with _status.lock:
        _status.wiring = state


def _set_startup(state: str) -> None:
    with _status.lock:
        _status.startup_state = state


def runtime_status(root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    with _status.lock:
        result: dict[str, Any] = {
            "wiring": _status.wiring,
            "startup_state": _status.startup_state,
            "unresolved": _status.unresolved,
            "errors": list(_status.errors[-5:]),
            "counts": dict(_status.counts),
            "last_recovery": _status.last_recovery,
        }
    if root is not None:
        try:
            health = rs.health_check(str(root))
            result["sidecar_health"] = {
                "ok": health.get("ok", True),
                "prepared_count": health.get("prepared_count", 0),
                "conflict_count": health.get("conflict_count", 0),
                "quarantined_buckets": health.get("quarantined_buckets", []),
                "sidecar_count": health.get("sidecar_count", 0),
            }
        except Exception as exc:
            result["sidecar_health"] = {"ok": False, "error": type(exc).__name__}
    return result


# ---- bucket inventory ----

def _is_regular_nofollow(path: Path) -> bool:
    try:
        st = path.lstat()
    except OSError:
        return False
    if os.name == "nt":
        if st.st_file_attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
            return False
    return stat.S_ISREG(st.st_mode)


def _is_dir_nofollow(path: Path) -> bool:
    try:
        st = path.lstat()
    except OSError:
        return False
    if os.name == "nt":
        if st.st_file_attributes & 0x400:
            return False
    return stat.S_ISDIR(st.st_mode)


def _read_bucket_id_from_file(path: Path) -> str | None:
    if not _is_regular_nofollow(path):
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    fm = m.group(1)
    id_match = _ID_LINE_RE.search(fm)
    if id_match:
        bid = id_match.group(1).strip().strip("'\"")
        if _BUCKET_ID_RE.match(bid):
            return bid
    return path.stem


def _read_file_fd_identity(path: Path) -> bytes:
    """Read file bytes with fd-level identity verification (no-follow).

    Every low-level OSError exit (lstat / open / fstat / read / close /
    post-lstat) is normalized to RemainderSidecarError carrying only the
    exception *type*. Raw OSError text may contain caller-controlled or
    environment detail and can reach the startup fatal log, so it must never
    be interpolated into the message.
    """
    try:
        pre_st = path.lstat()
    except OSError as exc:
        raise RemainderSidecarError(
            f"cannot lstat {path}: {type(exc).__name__}"
        ) from exc
    if os.name == "nt" and pre_st.st_file_attributes & 0x400:
        raise RemainderSidecarError(f"reparse point: {path}")
    if not stat.S_ISREG(pre_st.st_mode):
        raise RemainderSidecarError(f"non-regular file: {path}")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(str(path), flags)
    except OSError as exc:
        raise RemainderSidecarError(
            f"cannot open {path}: {type(exc).__name__}"
        ) from exc

    main_exc: BaseException | None = None
    try:
        try:
            fd_st = os.fstat(fd)
        except OSError as exc:
            raise RemainderSidecarError(
                f"cannot fstat {path}: {type(exc).__name__}"
            ) from exc
        if (fd_st.st_dev, fd_st.st_ino) != (pre_st.st_dev, pre_st.st_ino):
            raise RemainderSidecarError(
                f"fd identity mismatch for {path}: "
                f"pre=({pre_st.st_dev},{pre_st.st_ino}) "
                f"fd=({fd_st.st_dev},{fd_st.st_ino})"
            )
        # Loop until EOF: a single os.read may legally return a short read for
        # regular files, which would silently truncate authoritative content.
        # Cap at st_size + 1 so a file that grew under us is still detectable.
        chunks: list[bytes] = []
        total = 0
        limit = fd_st.st_size + 1
        while total < limit:
            try:
                chunk = os.read(fd, limit - total)
            except OSError as exc:
                if getattr(exc, "errno", None) == errno.EINTR:
                    continue
                raise RemainderSidecarError(
                    f"cannot read {path}: {type(exc).__name__}"
                ) from exc
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        data = b"".join(chunks)
    except BaseException as _main:
        main_exc = _main
        raise
    finally:
        try:
            os.close(fd)
        except OSError as close_exc:
            # Never swallow a close failure, and never let it overwrite the
            # primary contract error — report both, types only.
            if main_exc is not None:
                raise RemainderSidecarError(
                    f"cannot close {path}: {type(close_exc).__name__}"
                    f" (during {type(main_exc).__name__})"
                ) from main_exc
            raise RemainderSidecarError(
                f"cannot close {path}: {type(close_exc).__name__}"
            ) from close_exc

    try:
        post_st = path.lstat()
    except OSError as exc:
        raise RemainderSidecarError(
            f"cannot post-lstat {path}: {type(exc).__name__}"
        ) from exc
    if (post_st.st_dev, post_st.st_ino) != (pre_st.st_dev, pre_st.st_ino):
        raise RemainderSidecarError(
            f"post-read identity mismatch for {path}"
        )
    return data


def build_bucket_inventory(root: str | os.PathLike[str]) -> dict[str, str]:
    """Build bucket_id → current_content mapping from authoritative Markdown.

    Only reads regular non-reparse-point files with valid UTF-8 frontmatter
    containing an explicit ``id`` field. Uses fd-level identity verification.
    Raises on duplicate bucket IDs or invalid files.
    """
    root_path = Path(root)
    inventory: dict[str, str] = {}
    id_sources: dict[str, Path] = {}

    for dirname in BUCKET_DIRS:
        dirpath = root_path / dirname
        if not dirpath.exists() and not dirpath.is_symlink():
            continue
        if not _is_dir_nofollow(dirpath):
            raise RemainderSidecarError(
                f"authoritative root exists but is not a regular directory: "
                f"{dirpath}"
            )
        for sub in sorted(dirpath.rglob("*")):
            if sub.is_dir() or sub.name.endswith(".md"):
                if sub.is_dir() and not _is_dir_nofollow(sub):
                    raise RemainderSidecarError(
                        f"non-regular subdirectory in bucket root: {sub}"
                    )
        for md_path in sorted(dirpath.rglob("*.md")):
            if md_path.name.startswith("."):
                continue
            if not _is_regular_nofollow(md_path):
                raise RemainderSidecarError(
                    f"non-regular file in bucket directory: {md_path}"
                )
            try:
                raw = _read_file_fd_identity(md_path)
            except RemainderSidecarError:
                raise
            except OSError as exc:
                raise RemainderSidecarError(
                    f"cannot read bucket file {md_path}: {type(exc).__name__}"
                ) from exc
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                # UnicodeDecodeError's text carries the offending byte value
                # and offset, i.e. bucket content detail — type only.
                raise RemainderSidecarError(
                    f"invalid UTF-8 in {md_path}: {type(exc).__name__}"
                ) from exc

            m = _FRONTMATTER_RE.match(text)
            if not m:
                raise RemainderSidecarError(
                    f"missing frontmatter in {md_path}"
                )
            fm = m.group(1)
            id_match = _ID_LINE_RE.search(fm)
            if not id_match:
                raise RemainderSidecarError(
                    f"missing id field in frontmatter of {md_path}"
                )
            bid = id_match.group(1).strip().strip("'\"")
            if not _BUCKET_ID_RE.match(bid):
                raise RemainderSidecarError(
                    f"invalid bucket ID in {md_path}: {bid!r}"
                )

            if bid in id_sources:
                raise RemainderSidecarError(
                    f"duplicate bucket ID {bid!r}: "
                    f"{id_sources[bid]} and {md_path}"
                )
            id_sources[bid] = md_path

            content = text[m.end():]
            inventory[bid] = content

    return inventory


# ---- startup recovery ----

def recover_remainders_before_startup(
    root: str | os.PathLike[str],
) -> dict[str, Any]:
    """Synchronous fail-closed startup recovery coordinator.

    1. Build complete bucket inventory (fails on any invalid file).
    2. Full preflight: classify all PREPARED entries WITHOUT writing.
    3. If any entry classifies as CONFLICT, fail startup immediately
       (no prior bucket modified).
    4. Apply all transitions.
    5. Archive terminal entries.

    Returns summary dict. Raises on any failure.
    """
    _set_startup("recovering")
    root_str = str(root)
    root_path = Path(root_str)
    rem_root = root_path / REMAINDER_DIR

    result: dict[str, Any] = {
        "recovered": 0,
        "committed": 0,
        "aborted": 0,
        "conflicts": 0,
        "archived": 0,
        "skipped_no_sidecar": 0,
    }

    try:
        inventory = build_bucket_inventory(root_str)
    except RemainderSidecarError:
        _set_startup("failed")
        raise
    except OSError as exc:
        _set_startup("failed")
        raise RemainderSidecarError(
            f"cannot build bucket inventory: {type(exc).__name__}"
        ) from exc

    rem_lexists = rem_root.exists() or rem_root.is_symlink() or os.path.lexists(str(rem_root))
    if not rem_lexists:
        _set_startup("complete")
        _set_wiring("active")
        clear_unresolved()
        result["skipped_no_sidecar"] = 1
        with _status.lock:
            _status.last_recovery = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _status.counts = dict(result)
        return result

    # ---- strict sidecar tree precheck ----
    if not _is_dir_nofollow(rem_root):
        _set_startup("failed")
        raise RemainderSidecarError(
            f"remainder root is not a regular directory: {rem_root}"
        )

    try:
        sidecar_children = sorted(rem_root.iterdir())
    except OSError as exc:
        _set_startup("failed")
        raise RemainderSidecarError(
            f"cannot enumerate remainder root: {rem_root}"
        ) from exc

    bucket_sidecars: list[tuple[str, Any]] = []
    _ALLOWED_HIDDEN_DIRS = frozenset({".locks"})
    _ALLOWED_DIRS = frozenset({"archive", "quarantine"})
    for child in sidecar_children:
        if child.name.startswith("."):
            if child.name not in _ALLOWED_HIDDEN_DIRS:
                _set_startup("failed")
                raise RemainderSidecarError(
                    f"unexpected hidden member in remainder root: {child.name}"
                )
            if not _is_dir_nofollow(child):
                _set_startup("failed")
                raise RemainderSidecarError(
                    f".locks must be a regular directory, got: {child}"
                )
            try:
                lock_members = sorted(child.iterdir())
            except OSError as exc:
                _set_startup("failed")
                raise RemainderSidecarError(
                    f"cannot enumerate .locks directory: {type(exc).__name__}"
                ) from exc
            for lock_member in lock_members:
                try:
                    validate_lock_member(lock_member)
                except RemainderSidecarError:
                    _set_startup("failed")
                    raise
                except OSError as exc:
                    _set_startup("failed")
                    raise RemainderSidecarError(
                        f"cannot validate lock member: {type(exc).__name__}"
                    ) from exc
            continue
        if _is_dir_nofollow(child):
            if child.name == "quarantine":
                try:
                    q_children = list(child.iterdir())
                except OSError as exc:
                    _set_startup("failed")
                    raise RemainderSidecarError(
                        f"cannot enumerate quarantine directory: "
                        f"{type(exc).__name__}"
                    ) from exc
                if q_children:
                    _set_startup("failed")
                    raise RemainderQuarantineError(
                        f"quarantine directory is not empty: "
                        f"{[c.name for c in q_children[:3]]}"
                    )
                continue
            if child.name == "archive":
                if not _is_dir_nofollow(child):
                    _set_startup("failed")
                    raise RemainderSidecarError(
                        f"archive must be a regular directory: {child}"
                    )
                try:
                    arc_members = sorted(child.iterdir())
                except OSError as exc:
                    _set_startup("failed")
                    raise RemainderSidecarError(
                        f"cannot enumerate archive directory: "
                        f"{type(exc).__name__}"
                    ) from exc
                for arc_member in arc_members:
                    try:
                        validate_archive_member(arc_member)
                    except RemainderSidecarError:
                        _set_startup("failed")
                        raise
                    except OSError as exc:
                        _set_startup("failed")
                        raise RemainderSidecarError(
                            f"cannot validate archive member: "
                            f"{type(exc).__name__}"
                        ) from exc
                continue
            _set_startup("failed")
            raise RemainderSidecarError(
                f"unexpected directory in remainder root: {child.name}"
            )
        if not _is_regular_nofollow(child):
            _set_startup("failed")
            raise RemainderSidecarError(
                f"non-regular member in remainder root: {child}"
            )
        if child.name.endswith(".lock"):
            # Real locks live only in .remainders/.locks/ and must pass the
            # public validator. A .lock at the remainder root is never emitted
            # by us — fail closed instead of skipping it silently.
            _set_startup("failed")
            raise RemainderSidecarError(
                f"unexpected lock file at remainder root: {child.name}"
            )
        if not child.name.endswith(".json"):
            _set_startup("failed")
            raise RemainderSidecarError(
                f"unexpected non-json file in remainder root: {child.name}"
            )
        try:
            sc = rs.load_sidecar(child)
        except RemainderSidecarError:
            _set_startup("failed")
            raise RemainderQuarantineError(
                f"corrupt sidecar at {child} during startup"
            )
        except Exception as exc:
            # Anything else escaping load_sidecar (UnicodeDecodeError carrying
            # byte value + offset, OSError carrying environment detail, JSON
            # errors carrying document fragments) must not reach the startup
            # fatal log, and must not leave startup_state at "recovering".
            # Type only — never trust an exception class to be self-redacting.
            _set_startup("failed")
            raise RemainderQuarantineError(
                f"corrupt sidecar at {child} during startup: "
                f"{type(exc).__name__}"
            ) from exc
        for entry in sc.entries:
            if entry.state == STATE_CONFLICT:
                _set_startup("failed")
                raise RemainderConflictError(
                    f"pre-existing CONFLICT in sidecar {child.name} "
                    f"entry {entry.entry_id}"
                )
        bucket_sidecars.append((sc.bucket_id, sc))

    # ---- sidecar health precheck ----
    try:
        health = rs.health_check(root_str)
        if not health.get("ok", True):
            # health errors are downstream-authored strings and may carry
            # bucket/entry content — report a stable code and a count only,
            # never the error text itself.
            _errs = health.get("errors", [])
            _n = len(_errs) if isinstance(_errs, (list, tuple)) else 1
            _set_startup("failed")
            raise RemainderSidecarError(
                f"sidecar health check failed: error_count={_n}"
            )
    except RemainderSidecarError:
        _set_startup("failed")
        raise
    except Exception as exc:
        # health_check itself may raise (OSError etc.). Deliberately not
        # BaseException — KeyboardInterrupt/SystemExit must stay uncaught.
        _set_startup("failed")
        raise RemainderSidecarError(
            f"sidecar health check failed: {type(exc).__name__}"
        ) from exc

    # Phase 1: preflight — classify all PREPARED without writing
    plan: list[tuple[str, rs.RemainderEntry, str]] = []
    conflicts_to_persist: list[tuple[str, rs.RemainderEntry]] = []
    for bucket_id, sc in bucket_sidecars:
        for entry in sc.entries:
            if entry.state != STATE_PREPARED:
                continue
            current = inventory.get(bucket_id)
            if current is None:
                _set_startup("failed")
                raise RemainderSidecarError(
                    f"PREPARED entry {entry.entry_id} references "
                    f"missing bucket {bucket_id}"
                )
            new_state = classify_prepared(entry, current)
            if new_state == STATE_CONFLICT:
                conflicts_to_persist.append((bucket_id, entry))
            elif new_state != STATE_PREPARED:
                plan.append((bucket_id, entry, new_state))

    if conflicts_to_persist:
        for bucket_id, entry in conflicts_to_persist:
            try:
                rs.transition_entry(
                    root_str, bucket_id, entry.entry_id,
                    rs.load_sidecar_or_none(root_str, bucket_id).generation,
                    STATE_CONFLICT,
                )
            except Exception as _persist_exc:
                # Type only: this logger feeds the startup fatal log.
                logger.error(
                    "failed to persist CONFLICT for %s/%s: %s",
                    bucket_id, entry.entry_id, type(_persist_exc).__name__,
                )
        _set_startup("failed")
        raise RemainderConflictError(
            f"{len(conflicts_to_persist)} bucket(s) classify as CONFLICT "
            f"during startup: "
            f"{[bid for bid, _ in conflicts_to_persist]}"
        )

    # Phase 2: apply all transitions
    seen_buckets: set[str] = set()
    for bucket_id, entry, new_state in plan:
        if bucket_id in seen_buckets:
            continue
        seen_buckets.add(bucket_id)
        try:
            recovered = recover_prepared_entries(
                root_str, bucket_id, inventory.get(bucket_id),
            )
            result["recovered"] += len(recovered)
            for _rec_entry, rec_state in recovered:
                if rec_state == STATE_COMMITTED:
                    result["committed"] += 1
                elif rec_state == STATE_ABORTED:
                    result["aborted"] += 1
                elif rec_state == STATE_CONFLICT:
                    result["conflicts"] += 1
                    _set_startup("failed")
                    raise RemainderConflictError(
                        f"unexpected CONFLICT during recovery apply "
                        f"for bucket {bucket_id}"
                    )
        except (RemainderConflictError, RemainderQuarantineError):
            _set_startup("failed")
            raise
        except RemainderSidecarError as exc:
            # Type only. Do NOT interpolate exc: "our own error class is
            # already redacted" is an assumption about today's implementation,
            # not an enforceable boundary — a RemainderSidecarError raised
            # anywhere downstream can carry arbitrary text.
            _set_startup("failed")
            raise RemainderSidecarError(
                f"recovery failed for bucket {bucket_id}: "
                f"{type(exc).__name__}"
            ) from exc
        except Exception as exc:
            # OSError and anything else from the durable apply/persist path.
            _set_startup("failed")
            raise RemainderSidecarError(
                f"recovery failed for bucket {bucket_id}: "
                f"{type(exc).__name__}"
            ) from exc

    # Phase 3: archive terminal entries — fail-closed
    for bucket_id, sc in bucket_sidecars:
        try:
            archived = archive_if_needed(root_str, bucket_id)
            result["archived"] += archived
        except Exception as exc:
            # Type only: this logger feeds the startup fatal log. The raise
            # below was already redacted; the log line was not.
            logger.error(
                "archive_if_needed failed for %s during startup recovery: %s",
                bucket_id, type(exc).__name__,
            )
            _set_startup("failed")
            raise RemainderSidecarError(
                f"archive failed for {bucket_id} during startup: "
                f"{type(exc).__name__}"
            ) from exc

    _set_startup("complete")
    _set_wiring("active")
    clear_unresolved()
    with _status.lock:
        _status.last_recovery = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _status.counts = dict(result)

    return result
