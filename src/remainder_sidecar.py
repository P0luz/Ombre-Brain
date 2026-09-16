"""Remainder sidecar storage for merge audit trail (R-01).

Pure isolation layer — records per-bucket audit entries tracking what was
lost or changed during content merges.  Does not import from or wire into
the existing OB tool chain (merge, M-04, startup, Dashboard).

External-lock invariant: this module never acquires M-04 or M-01 turns.
Integrated callers must acquire locks in this order::

    M-04 barrier -> content/quota/merge-target -> bucket turn -> sidecar slot

Privacy: sidecar files do not enter breath / BM25 / embedding / API read
chains.  Three owners share one local vault; isolation comes from owner
provenance tags and controlled local access, not filesystem-level separation.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import logging
import os
import re
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

logger = logging.getLogger("remainder_sidecar")

SCHEMA_VERSION = 2
REMAINDER_DIR = ".remainders"
ARCHIVE_SUBDIR = "archive"
QUARANTINE_SUBDIR = "quarantine"

MAX_ACTIVE_ENTRIES = 50
MAX_ACTIVE_BYTES = 512 * 1024
MAX_ARCHIVE_ENTRIES = 200

STATE_PREPARED = "PREPARED"
STATE_COMMITTED = "COMMITTED"
STATE_ABORTED = "ABORTED"
STATE_CONFLICT = "CONFLICT"
_STATES = frozenset({STATE_PREPARED, STATE_COMMITTED, STATE_ABORTED, STATE_CONFLICT})
_TERMINAL_STATES = frozenset({STATE_COMMITTED, STATE_ABORTED, STATE_CONFLICT})
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    STATE_PREPARED: frozenset({STATE_COMMITTED, STATE_ABORTED, STATE_CONFLICT}),
}
_ARCHIVABLE_STATES = frozenset({STATE_COMMITTED, STATE_ABORTED})

MERGE_METHODS = frozenset({"llm", "raw_concat", "exact_dup"})

_BUCKET_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")
_ENTRY_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_OWNER_RE = re.compile(r"^[a-z0-9_]{1,80}$")
_Q_FILENAME_RE = re.compile(
    r"^(.+?)_\d{8}T\d{6}Z_[0-9a-f]{12}\.json\.corrupt$"
)

_KNOWN_OWNERS: frozenset[str] = frozenset({
    "cheng", "huaiyin", "huaiyin_cc",
    "shared", "shared_core", "shared_context", "shared_resource",
})


class RemainderSidecarError(RuntimeError):
    """Base error for remainder sidecar operations."""


class StaleSidecarError(RemainderSidecarError):
    """Sidecar generation changed since the caller's last read."""


class RemainderQuarantineError(RemainderSidecarError):
    """Bucket is quarantined due to a corrupt sidecar."""


class RemainderConflictError(RemainderSidecarError):
    """Bucket has an unresolved CONFLICT entry."""


@dataclass(frozen=True)
class RemainderEntry:
    entry_id: str
    state: str
    bucket_id: str
    old_sha256: str
    new_sha256: str
    merged_sha256: str
    unmatched_verbatim_lines: tuple[dict[str, Any], ...]
    merge_method: str
    owner: str
    created_at: str
    committed_at: str | None


@dataclass(frozen=True)
class SidecarFile:
    schema: int
    bucket_id: str
    generation: int
    entries: tuple[RemainderEntry, ...]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _now_filename() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def content_sha256(content: str) -> str:
    if not isinstance(content, str):
        raise TypeError("content must be str")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---- path safety (mirrors embedding_outbox patterns) ----

def _is_reparse_point(info: os.stat_result) -> bool:
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(info, "st_file_attributes", 0) & flag)


def _lstat(path: Path, label: str) -> os.stat_result:
    try:
        return os.lstat(path)
    except OSError as exc:
        raise RemainderSidecarError(f"cannot inspect {label}: {path}") from exc


def _assert_regular_directory(path: Path, label: str) -> None:
    info = _lstat(path, label)
    if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info) or not stat.S_ISDIR(info.st_mode):
        raise RemainderSidecarError(f"{label} is not a regular directory: {path}")


def _assert_regular_file(path: Path, label: str) -> None:
    info = _lstat(path, label)
    if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info) or not stat.S_ISREG(info.st_mode):
        raise RemainderSidecarError(f"{label} is not a regular file: {path}")


def _assert_regular_file_if_exists(path: Path, label: str) -> None:
    if not os.path.lexists(os.fspath(path)):
        return
    _assert_regular_file(path, label)


def _file_identity(info: os.stat_result) -> tuple[int, int]:
    return (info.st_dev, info.st_ino)


def _lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _ensure_dir(path: Path, label: str) -> Path:
    if _lexists(path):
        _assert_regular_directory(path, label)
        return path
    parent = path.parent
    _assert_regular_directory(parent, f"{label} parent")
    try:
        path.mkdir()
    except FileExistsError:
        pass
    except OSError as exc:
        raise RemainderSidecarError(f"cannot create {label}: {path}") from exc
    _assert_regular_directory(path, label)
    return path


def _remainder_root(root: str | os.PathLike[str], *, create: bool) -> Path:
    raw = Path(root).expanduser()
    path = Path(os.path.abspath(os.fspath(raw))) / REMAINDER_DIR
    if _lexists(path):
        _assert_regular_directory(path, "remainder root")
        return path
    if not create:
        return path
    _assert_regular_directory(path.parent, "vault root")
    return _ensure_dir(path, "remainder root")


def _sidecar_path(root: str | os.PathLike[str], bucket_id: str, *, create: bool) -> Path:
    if not _BUCKET_ID_RE.fullmatch(bucket_id):
        raise RemainderSidecarError(f"invalid bucket_id: {bucket_id}")
    rem = _remainder_root(root, create=create)
    path = rem / f"{bucket_id}.json"
    if _lexists(path):
        _assert_regular_file(path, "sidecar file")
    return path


# ---- cross-process byte lease ----

_LOCK_INIT_WAIT_SECONDS = 0.5
_LOCK_INIT_POLL_INTERVAL = 0.005


def _validate_lock_stat(lock_path: Path) -> os.stat_result:
    info = _lstat(lock_path, "lock file")
    if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
        raise RemainderSidecarError(
            f"lock file is a symlink or reparse point: {lock_path}"
        )
    if not stat.S_ISREG(info.st_mode):
        raise RemainderSidecarError(
            f"lock file is not a regular file: {lock_path}"
        )
    if info.st_size > 1:
        raise RemainderSidecarError(
            f"lock file has invalid size {info.st_size} (expected 1): {lock_path}"
        )
    if info.st_size == 0:
        ident = _file_identity(info)
        deadline = time.monotonic() + _LOCK_INIT_WAIT_SECONDS
        while True:
            time.sleep(_LOCK_INIT_POLL_INTERVAL)
            info = _lstat(lock_path, "lock file")
            if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
                raise RemainderSidecarError(
                    f"lock file is a symlink or reparse point: {lock_path}"
                )
            if not stat.S_ISREG(info.st_mode):
                raise RemainderSidecarError(
                    f"lock file is not a regular file: {lock_path}"
                )
            if _file_identity(info) != ident:
                raise RemainderSidecarError(
                    f"lock file identity changed during init wait: {lock_path}"
                )
            if info.st_size == 1:
                return info
            if info.st_size > 1:
                raise RemainderSidecarError(
                    f"lock file has invalid size {info.st_size} (expected 1): {lock_path}"
                )
            if time.monotonic() >= deadline:
                raise RemainderSidecarError(
                    f"lock file stuck at size 0 (crashed initializer?): {lock_path}"
                )
    return info


@contextlib.contextmanager
def _slot_turn(
    root: str | os.PathLike[str], bucket_id: str, timeout_seconds: float = 30.0
) -> Iterator[None]:
    rem = _remainder_root(root, create=True)
    lock_dir = _ensure_dir(rem / ".locks", "sidecar lock directory")
    lock_id = hashlib.sha256(
        f"remainder-sidecar-{bucket_id}".encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    lock_path = lock_dir / f"{lock_id}.lock"

    init_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        init_flags |= os.O_CLOEXEC
    try:
        init_fd = os.open(lock_path, init_flags, 0o600)
        try:
            os.write(init_fd, b"\0")
        finally:
            os.close(init_fd)
    except FileExistsError:
        pass

    pre_stat = _validate_lock_stat(lock_path)

    flags = os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        fd_stat = os.fstat(descriptor)
        if not stat.S_ISREG(fd_stat.st_mode) or _is_reparse_point(fd_stat):
            raise RemainderSidecarError(f"lock file fd is not a regular file: {lock_path}")
        pre_ident = _file_identity(pre_stat)
        fd_ident = _file_identity(fd_stat)
        if pre_ident != fd_ident:
            raise RemainderSidecarError(
                f"lock file identity changed between lstat and open: {lock_path}"
            )
        post_stat = _lstat(lock_path, "lock file post-open")
        post_ident = _file_identity(post_stat)
        if post_ident != fd_ident:
            raise RemainderSidecarError(
                f"lock file identity changed after open: {lock_path}"
            )
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    handle = os.fdopen(descriptor, "r+b", buffering=0)
    acquired = False
    deadline = time.monotonic() + timeout_seconds
    busy = {errno.EACCES, errno.EAGAIN, getattr(errno, "EWOULDBLOCK", errno.EAGAIN)}
    try:
        while not acquired:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                if exc.errno not in busy and getattr(exc, "winerror", None) not in {32, 33}:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "timed out waiting for remainder sidecar slot"
                    )
                time.sleep(0.01)
        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


# ---- durable JSON write ----

def _durable_json(path: Path, value: Any) -> None:
    _assert_regular_directory(path.parent, "sidecar directory")
    if _lexists(path):
        _assert_regular_file(path, "sidecar file")
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        if os.name != "nt":
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()


# ---- validation ----

def _validate_unmatched_line(line: Mapping[str, Any]) -> dict[str, Any]:
    source = str(line.get("source", ""))
    if source not in ("old", "new"):
        raise RemainderSidecarError(f"invalid unmatched line source: {source}")
    line_index = line.get("line_index")
    if not isinstance(line_index, int) or line_index < 0:
        raise RemainderSidecarError("invalid unmatched line_index")
    text = line.get("text")
    if not isinstance(text, str):
        raise RemainderSidecarError("invalid unmatched line text")
    return {"source": source, "line_index": line_index, "text": text}


def _validate_entry(value: Mapping[str, Any]) -> RemainderEntry:
    try:
        entry_id = str(value["entry_id"])
        entry_state = str(value["state"])
        bucket_id = str(value["bucket_id"])
        old_sha256 = str(value["old_sha256"])
        new_sha256 = str(value["new_sha256"])
        merged_sha256 = str(value["merged_sha256"])
        raw_lines = value["unmatched_verbatim_lines"]
        merge_method = str(value["merge_method"])
        owner = str(value["owner"])
        created_at = str(value["created_at"])
        committed_at = value["committed_at"]
        if committed_at is not None:
            committed_at = str(committed_at)
    except (KeyError, TypeError) as exc:
        raise RemainderSidecarError("invalid remainder entry shape") from exc

    if not _ENTRY_ID_RE.fullmatch(entry_id):
        raise RemainderSidecarError("invalid entry_id")
    if entry_state not in _STATES:
        raise RemainderSidecarError(f"invalid state: {entry_state}")
    if not _BUCKET_ID_RE.fullmatch(bucket_id):
        raise RemainderSidecarError(f"invalid bucket_id: {bucket_id}")
    for h, name in [
        (old_sha256, "old_sha256"),
        (new_sha256, "new_sha256"),
        (merged_sha256, "merged_sha256"),
    ]:
        if not _SHA256_RE.fullmatch(h):
            raise RemainderSidecarError(f"invalid {name}")
    if not isinstance(raw_lines, list):
        raise RemainderSidecarError("unmatched_verbatim_lines must be a list")
    lines = tuple(_validate_unmatched_line(line) for line in raw_lines)
    if merge_method not in MERGE_METHODS:
        raise RemainderSidecarError(f"invalid merge_method: {merge_method}")
    if owner and not _OWNER_RE.fullmatch(owner):
        raise RemainderSidecarError(f"invalid owner: {owner}")
    if not created_at:
        raise RemainderSidecarError("created_at is required")

    if entry_state == STATE_COMMITTED and committed_at is None:
        raise RemainderSidecarError("COMMITTED entry must have committed_at")
    if entry_state != STATE_COMMITTED and committed_at is not None:
        raise RemainderSidecarError(
            f"{entry_state} entry must not have committed_at"
        )

    return RemainderEntry(
        entry_id=entry_id,
        state=entry_state,
        bucket_id=bucket_id,
        old_sha256=old_sha256,
        new_sha256=new_sha256,
        merged_sha256=merged_sha256,
        unmatched_verbatim_lines=lines,
        merge_method=merge_method,
        owner=owner,
        created_at=created_at,
        committed_at=committed_at,
    )


def _validate_sidecar(value: Mapping[str, Any]) -> SidecarFile:
    try:
        schema = int(value["schema"])
        bucket_id = str(value["bucket_id"])
        generation = int(value["generation"])
        raw_entries = value["entries"]
    except (KeyError, TypeError, ValueError) as exc:
        raise RemainderSidecarError("invalid sidecar shape") from exc

    if schema != SCHEMA_VERSION:
        raise RemainderSidecarError(f"unsupported sidecar schema: {schema}")
    if not _BUCKET_ID_RE.fullmatch(bucket_id):
        raise RemainderSidecarError(f"invalid bucket_id: {bucket_id}")
    if generation < 1:
        raise RemainderSidecarError("invalid generation")
    if not isinstance(raw_entries, list):
        raise RemainderSidecarError("entries must be a list")

    entries = tuple(_validate_entry(e) for e in raw_entries)
    for e in entries:
        if e.bucket_id != bucket_id:
            raise RemainderSidecarError(
                f"entry bucket_id mismatch: {e.bucket_id} != {bucket_id}"
            )

    return SidecarFile(
        schema=schema, bucket_id=bucket_id,
        generation=generation, entries=entries,
    )


def _sidecar_to_dict(sc: SidecarFile) -> dict[str, Any]:
    return {
        "schema": sc.schema,
        "bucket_id": sc.bucket_id,
        "generation": sc.generation,
        "entries": [_entry_to_dict(e) for e in sc.entries],
    }


def _entry_to_dict(e: RemainderEntry) -> dict[str, Any]:
    return {
        "entry_id": e.entry_id,
        "state": e.state,
        "bucket_id": e.bucket_id,
        "old_sha256": e.old_sha256,
        "new_sha256": e.new_sha256,
        "merged_sha256": e.merged_sha256,
        "unmatched_verbatim_lines": list(e.unmatched_verbatim_lines),
        "merge_method": e.merge_method,
        "owner": e.owner,
        "created_at": e.created_at,
        "committed_at": e.committed_at,
    }


def _replace_entry(e: RemainderEntry, **kwargs: Any) -> RemainderEntry:
    d = _entry_to_dict(e)
    d.update(kwargs)
    return _validate_entry(d)


# ---- core algorithm ----

def extract_unmatched(
    old_text: str, new_text: str, merged_text: str
) -> list[dict[str, Any]]:
    """Combined provenance queue — each merged line consumes at most one input."""
    input_entries: list[dict[str, Any]] = []
    for i, line in enumerate(old_text.splitlines()):
        stripped = line.strip()
        if stripped:
            input_entries.append({
                "source": "old", "line_index": i,
                "text": stripped, "_consumed": False,
            })
    for i, line in enumerate(new_text.splitlines()):
        stripped = line.strip()
        if stripped:
            input_entries.append({
                "source": "new", "line_index": i,
                "text": stripped, "_consumed": False,
            })

    for line in merged_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for entry in input_entries:
            if not entry["_consumed"] and entry["text"] == stripped:
                entry["_consumed"] = True
                break

    return [
        {"source": e["source"], "line_index": e["line_index"], "text": e["text"]}
        for e in input_entries if not e["_consumed"]
    ]


# ---- load ----

def load_sidecar(path: str | os.PathLike[str]) -> SidecarFile:
    p = Path(path)
    if not _lexists(p):
        raise RemainderSidecarError(f"sidecar does not exist: {p}")
    _assert_regular_file(p, "sidecar file")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RemainderSidecarError(f"cannot read sidecar: {p}") from exc
    if not isinstance(raw, Mapping):
        raise RemainderSidecarError("sidecar must be a JSON object")
    return _validate_sidecar(raw)


def load_sidecar_or_none(
    root: str | os.PathLike[str], bucket_id: str
) -> SidecarFile | None:
    path = _sidecar_path(root, bucket_id, create=False)
    if not _lexists(path):
        return None
    return load_sidecar(path)


# ---- quarantine (internal unlocked + public locked) ----

def _quarantine_raw(
    sidecar_path: Path, rem_root: Path, bucket_id: str
) -> Path:
    q_dir = _ensure_dir(rem_root / QUARANTINE_SUBDIR, "quarantine directory")
    ts = _now_filename()
    uid = uuid.uuid4().hex[:12]
    dest = q_dir / f"{bucket_id}_{ts}_{uid}.json.corrupt"
    _assert_regular_directory(dest.parent, "quarantine directory")
    if _lexists(dest):
        raise RemainderSidecarError(f"quarantine destination exists: {dest}")
    try:
        os.replace(sidecar_path, dest)
    except OSError as exc:
        raise RemainderSidecarError(
            f"cannot quarantine sidecar: {sidecar_path}"
        ) from exc
    logger.warning("quarantined corrupt sidecar %s -> %s", sidecar_path, dest)
    return dest


def quarantine_sidecar(
    root: str | os.PathLike[str], bucket_id: str
) -> Path:
    path = _sidecar_path(root, bucket_id, create=False)
    if not _lexists(path):
        raise RemainderSidecarError(f"sidecar does not exist: {path}")
    rem = _remainder_root(root, create=True)
    with _slot_turn(root, bucket_id):
        return _quarantine_raw(path, rem, bucket_id)


def _check_quarantine(root: str | os.PathLike[str], bucket_id: str) -> None:
    rem = _remainder_root(root, create=False)
    if not _lexists(rem):
        return
    q_dir = rem / QUARANTINE_SUBDIR
    if not _lexists(q_dir):
        return
    _assert_regular_directory(q_dir, "quarantine directory")
    prefix = f"{bucket_id}_"
    for child in q_dir.iterdir():
        if not child.name.startswith(prefix) or not child.name.endswith(".json.corrupt"):
            continue
        _assert_regular_file(child, f"quarantine member {child.name}")
        raise RemainderQuarantineError(
            f"bucket {bucket_id} is quarantined: {child}"
        )


# ---- classify ----

def classify_prepared(
    entry: RemainderEntry, current_content: str | None
) -> str:
    if entry.state != STATE_PREPARED:
        return entry.state
    if current_content is None:
        return STATE_CONFLICT
    current_hash = content_sha256(current_content)
    if current_hash == entry.merged_sha256:
        return STATE_COMMITTED
    if current_hash == entry.old_sha256:
        return STATE_ABORTED
    return STATE_CONFLICT


# ---- owner adapter ----

def strict_owner_from_metadata(
    metadata: Mapping[str, Any],
    *,
    allow_untagged: bool = True,
    extra_known: frozenset[str] | None = None,
) -> str:
    """Extract owner from metadata tags with strict validation.

    Mirrors ``_identity.strict_owner_of`` semantics: reads ``owner:``
    prefixed strings from ``metadata["tags"]``, normalises to lowercase
    with underscores, rejects multi-owner / unknown / non-list tags.
    """
    tags = (metadata or {}).get("tags")
    if tags is None:
        tags = []
    if not isinstance(tags, list):
        raise RemainderSidecarError(
            f"bucket tags must be a list, got {type(tags).__name__}"
        )

    known = _KNOWN_OWNERS | (extra_known or frozenset())

    owners: list[str] = []
    for tag in tags:
        if not isinstance(tag, str):
            raise RemainderSidecarError("bucket tags must contain strings only")
        text = tag.strip()
        if not text.lower().startswith("owner:"):
            continue
        value = text[6:].strip().lower().replace("-", "_")
        if not value:
            raise RemainderSidecarError("owner tag must not be empty")
        if value not in known:
            raise RemainderSidecarError(f"unknown owner value: {value}")
        if value not in owners:
            owners.append(value)

    if len(owners) > 1:
        raise RemainderSidecarError(
            "bucket contains multiple owner tags: "
            + ", ".join(f"owner:{v}" for v in owners)
        )
    if not owners:
        if allow_untagged:
            return ""
        raise RemainderSidecarError("bucket owner is required")
    return owners[0]


# ---- prepare ----

def prepare_remainder(
    root: str | os.PathLike[str],
    bucket_id: str,
    old_text: str,
    new_text: str,
    merged_text: str,
    merge_method: str,
    metadata: Mapping[str, Any],
    current_content: str | None = None,
    *,
    allow_untagged: bool = True,
    extra_known: frozenset[str] | None = None,
) -> tuple[RemainderEntry, int]:
    """Write a PREPARED entry. Recovers stale PREPARED entries first.

    Returns ``(entry, sidecar_generation)`` — pass generation to
    ``commit_remainder`` / ``transition_entry`` for CAS.

    Owner is extracted from ``metadata`` via ``strict_owner_from_metadata``;
    raw owner strings are not accepted.
    """
    if not isinstance(old_text, str):
        raise TypeError("old_text must be str")
    if not isinstance(new_text, str):
        raise TypeError("new_text must be str")
    if not isinstance(merged_text, str):
        raise TypeError("merged_text must be str")
    if merge_method not in MERGE_METHODS:
        raise RemainderSidecarError(f"invalid merge_method: {merge_method}")

    owner = strict_owner_from_metadata(
        metadata, allow_untagged=allow_untagged, extra_known=extra_known,
    )

    _check_quarantine(root, bucket_id)

    path = _sidecar_path(root, bucket_id, create=True)
    unmatched = extract_unmatched(old_text, new_text, merged_text)

    with _slot_turn(root, bucket_id):
        if _lexists(path):
            try:
                sc = load_sidecar(path)
            except RemainderSidecarError:
                rem = _remainder_root(root, create=True)
                _quarantine_raw(path, rem, bucket_id)
                raise RemainderQuarantineError(
                    f"corrupt sidecar for {bucket_id} quarantined during prepare"
                )

            entries: list[RemainderEntry] = list(sc.entries)
            for i, e in enumerate(entries):
                if e.state != STATE_PREPARED:
                    continue
                if current_content is None:
                    raise RemainderSidecarError(
                        f"cannot recover PREPARED entry {e.entry_id} "
                        f"without current_content"
                    )
                new_state = classify_prepared(e, current_content)
                if new_state != STATE_PREPARED:
                    committed_at = (
                        _now_iso() if new_state == STATE_COMMITTED else None
                    )
                    entries[i] = _replace_entry(
                        e, state=new_state, committed_at=committed_at,
                    )

            _check_conflict_entries(entries, bucket_id)
            generation = sc.generation + 1
        else:
            entries = []
            generation = 1

        now = _now_iso()
        entry = _validate_entry({
            "entry_id": uuid.uuid4().hex,
            "state": STATE_PREPARED,
            "bucket_id": bucket_id,
            "old_sha256": content_sha256(old_text),
            "new_sha256": content_sha256(new_text),
            "merged_sha256": content_sha256(merged_text),
            "unmatched_verbatim_lines": unmatched,
            "merge_method": merge_method,
            "owner": owner,
            "created_at": now,
            "committed_at": None,
        })
        entries.append(entry)

        sc_new = SidecarFile(
            schema=SCHEMA_VERSION, bucket_id=bucket_id,
            generation=generation, entries=tuple(entries),
        )
        _durable_json(path, _sidecar_to_dict(sc_new))

        reread = load_sidecar(path)
        if reread.generation != generation:
            raise RemainderSidecarError("durable sidecar reread mismatch")

        return entry, generation


def _check_conflict_entries(
    entries: list[RemainderEntry] | tuple[RemainderEntry, ...],
    bucket_id: str,
) -> None:
    for e in entries:
        if e.state == STATE_CONFLICT:
            raise RemainderConflictError(
                f"bucket {bucket_id} has unresolved CONFLICT entry {e.entry_id}"
            )


# ---- transition ----

def transition_entry(
    root: str | os.PathLike[str],
    bucket_id: str,
    entry_id: str,
    expected_generation: int,
    new_state: str,
) -> RemainderEntry:
    """Persist one state transition using generation CAS."""
    if new_state not in _STATES:
        raise RemainderSidecarError(f"invalid target state: {new_state}")

    path = _sidecar_path(root, bucket_id, create=False)

    with _slot_turn(root, bucket_id):
        sc = load_sidecar(path)
        if sc.generation != expected_generation:
            raise StaleSidecarError(
                f"sidecar generation {sc.generation} != expected {expected_generation}"
            )

        new_entries = list(sc.entries)
        result: RemainderEntry | None = None
        for i, e in enumerate(new_entries):
            if e.entry_id != entry_id:
                continue
            allowed = _ALLOWED_TRANSITIONS.get(e.state, frozenset())
            if new_state not in allowed:
                raise RemainderSidecarError(
                    f"invalid state transition: {e.state} -> {new_state}"
                )
            committed_at = (
                _now_iso() if new_state == STATE_COMMITTED else e.committed_at
            )
            new_entries[i] = _replace_entry(
                e, state=new_state, committed_at=committed_at,
            )
            result = new_entries[i]
            break

        if result is None:
            raise RemainderSidecarError(
                f"entry {entry_id} not found in sidecar"
            )

        sc_new = SidecarFile(
            schema=SCHEMA_VERSION, bucket_id=bucket_id,
            generation=sc.generation + 1, entries=tuple(new_entries),
        )
        _durable_json(path, _sidecar_to_dict(sc_new))
        return result


def commit_remainder(
    root: str | os.PathLike[str],
    bucket_id: str,
    entry_id: str,
    expected_generation: int,
) -> RemainderEntry:
    return transition_entry(
        root, bucket_id, entry_id, expected_generation, STATE_COMMITTED,
    )


def abort_remainder(
    root: str | os.PathLike[str],
    bucket_id: str,
    entry_id: str,
    expected_generation: int,
) -> RemainderEntry:
    return transition_entry(
        root, bucket_id, entry_id, expected_generation, STATE_ABORTED,
    )


# ---- recovery ----

def recover_prepared_entries(
    root: str | os.PathLike[str],
    bucket_id: str,
    current_content: str | None,
) -> list[tuple[RemainderEntry, str]]:
    """Recover all PREPARED entries.  Returns ``(updated_entry, new_state)``."""
    path = _sidecar_path(root, bucket_id, create=False)
    if not _lexists(path):
        return []

    with _slot_turn(root, bucket_id):
        try:
            sc = load_sidecar(path)
        except RemainderSidecarError:
            rem = _remainder_root(root, create=True)
            _quarantine_raw(path, rem, bucket_id)
            raise RemainderQuarantineError(
                f"corrupt sidecar for {bucket_id} quarantined during recovery"
            )

        transitions: list[tuple[RemainderEntry, str]] = []
        new_entries = list(sc.entries)
        changed = False

        for i, e in enumerate(new_entries):
            if e.state != STATE_PREPARED:
                continue
            new_state = classify_prepared(e, current_content)
            if new_state == STATE_PREPARED:
                continue
            committed_at = (
                _now_iso() if new_state == STATE_COMMITTED else None
            )
            new_entries[i] = _replace_entry(
                e, state=new_state, committed_at=committed_at,
            )
            transitions.append((new_entries[i], new_state))
            changed = True

        if changed:
            sc_new = SidecarFile(
                schema=SCHEMA_VERSION, bucket_id=bucket_id,
                generation=sc.generation + 1, entries=tuple(new_entries),
            )
            _durable_json(path, _sidecar_to_dict(sc_new))

        return transitions


def is_terminal(entry: RemainderEntry) -> bool:
    return entry.state in _TERMINAL_STATES


# ---- rotation / archive ----

def archive_if_needed(
    root: str | os.PathLike[str],
    bucket_id: str,
    max_active: int = MAX_ACTIVE_ENTRIES,
) -> int:
    """Rotate COMMITTED/ABORTED entries to archive.  Returns count moved.

    PREPARED and CONFLICT entries always stay in the active sidecar.
    Archive is written before active is updated; if archive write fails
    the active sidecar is unchanged and any partial archive files from
    this batch are cleaned up.
    """
    path = _sidecar_path(root, bucket_id, create=False)
    if not _lexists(path):
        return 0

    with _slot_turn(root, bucket_id):
        sc = load_sidecar(path)

        archivable = [e for e in sc.entries if e.state in _ARCHIVABLE_STATES]
        non_archivable = [e for e in sc.entries if e.state not in _ARCHIVABLE_STATES]

        current_bytes = len(
            json.dumps(_sidecar_to_dict(sc), ensure_ascii=False).encode("utf-8")
        )
        total_entries = len(sc.entries)

        needs_rotation = (
            total_entries > max_active or current_bytes > MAX_ACTIVE_BYTES
        )
        if not needs_rotation or not archivable:
            return 0

        keep_count = max(0, max_active - len(non_archivable))
        if current_bytes > MAX_ACTIVE_BYTES and keep_count >= len(archivable):
            keep_count = len(archivable) // 2
        to_archive = archivable[:-keep_count] if keep_count > 0 else archivable

        if not to_archive:
            return 0

        rem = _remainder_root(root, create=True)
        archive_dir = _ensure_dir(rem / ARCHIVE_SUBDIR, "archive directory")
        ts = _now_filename()
        uid = uuid.uuid4().hex[:12]

        created_archive_files: list[Path] = []
        try:
            for batch_start in range(0, len(to_archive), MAX_ARCHIVE_ENTRIES):
                batch = to_archive[batch_start:batch_start + MAX_ARCHIVE_ENTRIES]
                suffix = f"_{batch_start}" if batch_start > 0 else ""
                archive_path = archive_dir / f"{bucket_id}_{ts}_{uid}{suffix}.json"
                _assert_regular_directory(archive_path.parent, "archive directory")
                if _lexists(archive_path):
                    raise RemainderSidecarError(
                        f"archive destination exists: {archive_path}"
                    )
                created_archive_files.append(archive_path)
                archive_data = {
                    "schema": SCHEMA_VERSION,
                    "bucket_id": bucket_id,
                    "archived_at": _now_iso(),
                    "entries": [_entry_to_dict(e) for e in batch],
                }
                _durable_json(archive_path, archive_data)
        except Exception as archive_exc:
            cleanup_errors: list[str] = []
            for f in created_archive_files:
                try:
                    if _lexists(f):
                        f.unlink()
                except OSError as ce:
                    cleanup_errors.append(f"{f.name}: {ce}")
            if cleanup_errors:
                raise RemainderSidecarError(
                    "archive failed and cleanup also failed: "
                    + "; ".join(cleanup_errors)
                ) from archive_exc
            raise

        archived_ids = frozenset(e.entry_id for e in to_archive)
        remaining = tuple(e for e in sc.entries if e.entry_id not in archived_ids)

        sc_new = SidecarFile(
            schema=SCHEMA_VERSION, bucket_id=bucket_id,
            generation=sc.generation + 1, entries=remaining,
        )
        _durable_json(path, _sidecar_to_dict(sc_new))

        return len(to_archive)


# ---- read-only visibility ----

def list_entries(
    root: str | os.PathLike[str],
    state_filter: str | None = None,
) -> list[RemainderEntry]:
    """Read-only listing across all sidecars.  Does not create remainder root."""
    if state_filter is not None and state_filter not in _STATES:
        raise RemainderSidecarError(f"invalid state filter: {state_filter}")
    rem = _remainder_root(root, create=False)
    if not _lexists(rem):
        return []
    result: list[RemainderEntry] = []
    try:
        children = sorted(rem.iterdir(), key=lambda c: c.name)
    except OSError as exc:
        raise RemainderSidecarError(
            f"cannot enumerate remainder root: {rem}"
        ) from exc
    for child in children:
        if not child.name.endswith(".json") or child.name.startswith("."):
            continue
        _assert_regular_file(child, f"sidecar member {child.name}")
        sc = load_sidecar(child)
        for e in sc.entries:
            if state_filter is None or e.state == state_filter:
                result.append(e)
    return result


def health_check(root: str | os.PathLike[str]) -> dict[str, Any]:
    """Read-only health summary."""
    rem = _remainder_root(root, create=False)
    report: dict[str, Any] = {
        "ok": True,
        "errors": [],
        "root_exists": _lexists(rem),
        "sidecar_count": 0,
        "total_entries": 0,
        "total_active_bytes": 0,
        "prepared_count": 0,
        "conflict_count": 0,
        "quarantined_buckets": [],
        "oversized_unresolvable": False,
    }
    if not report["root_exists"]:
        return report

    for child in rem.iterdir():
        if not child.name.endswith(".json") or child.name.startswith("."):
            continue
        try:
            info = os.lstat(child)
            if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
                report["ok"] = False
                report["errors"].append(f"{child.name}: symlink or reparse point")
                continue
            if not stat.S_ISREG(info.st_mode):
                report["ok"] = False
                report["errors"].append(f"{child.name}: not a regular file")
                continue
            sc = load_sidecar(child)
            report["sidecar_count"] += 1
            report["total_entries"] += len(sc.entries)
            sc_bytes = len(
                json.dumps(
                    _sidecar_to_dict(sc), ensure_ascii=False
                ).encode("utf-8")
            )
            report["total_active_bytes"] += sc_bytes
            prepared = sum(1 for e in sc.entries if e.state == STATE_PREPARED)
            conflicts = sum(1 for e in sc.entries if e.state == STATE_CONFLICT)
            report["prepared_count"] += prepared
            report["conflict_count"] += conflicts
            archivable = sum(
                1 for e in sc.entries if e.state in _ARCHIVABLE_STATES
            )
            if sc_bytes > MAX_ACTIVE_BYTES and archivable == 0:
                report["oversized_unresolvable"] = True
                report["ok"] = False
                report["errors"].append(
                    f"{child.name}: oversized ({sc_bytes} bytes) "
                    f"with no archivable entries"
                )
        except Exception as exc:
            report["ok"] = False
            report["errors"].append(f"{child.name}: {exc}")

    q_dir = rem / QUARANTINE_SUBDIR
    if _lexists(q_dir):
        try:
            _assert_regular_directory(q_dir, "quarantine directory")
            for child in q_dir.iterdir():
                if not child.name.endswith(".json.corrupt"):
                    continue
                _assert_regular_file_if_exists(
                    child, f"quarantine member {child.name}"
                )
                m = _Q_FILENAME_RE.match(child.name)
                if m:
                    report["quarantined_buckets"].append(m.group(1))
        except RemainderSidecarError as exc:
            report["ok"] = False
            report["errors"].append(f"quarantine: {exc}")

    return report


# ---- public read-only validators (R-01A) ----

_LOCK_FILENAME_RE = re.compile(r"^[0-9a-f]{64}\.lock$")
_ARCHIVE_FILENAME_RE = re.compile(
    r"^(.+)_(\d{8}T\d{6}Z)_([0-9a-f]{12})(?:_(\d+))?\.json$"
)
_ARCHIVE_ALLOWED_KEYS = frozenset({"schema", "bucket_id", "archived_at", "entries"})
_ARCHIVE_MAX_BYTES = 10 * 1024 * 1024


def _safe_read_bytes(path: Path, label: str, max_bytes: int) -> bytes:
    """fd-level identity-verified read: pre-lstat, O_RDONLY|O_NOFOLLOW, fstat, loop-read, post-lstat."""
    try:
        pre = _lstat(path, label)
    except RemainderSidecarError:
        raise
    except OSError as exc:
        raise RemainderSidecarError(
            f"cannot lstat {label}: {type(exc).__name__}"
        ) from exc
    if stat.S_ISLNK(pre.st_mode) or _is_reparse_point(pre) or not stat.S_ISREG(pre.st_mode):
        raise RemainderSidecarError(f"{label} is not a regular file: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RemainderSidecarError(
            f"cannot open {label}: {type(exc).__name__}"
        ) from exc
    main_exc: BaseException | None = None
    try:
        try:
            fd_stat = os.fstat(fd)
        except OSError as exc:
            raise RemainderSidecarError(
                f"cannot fstat {label}: {type(exc).__name__}"
            ) from exc
        if not stat.S_ISREG(fd_stat.st_mode) or _is_reparse_point(fd_stat):
            raise RemainderSidecarError(f"{label} fd is not a regular file: {path}")
        if _file_identity(pre) != _file_identity(fd_stat):
            raise RemainderSidecarError(
                f"{label} identity changed between lstat and open: {path}"
            )
        chunks: list[bytes] = []
        total = 0
        limit = max_bytes + 1
        while total < limit:
            try:
                chunk = os.read(fd, limit - total)
            except OSError as exc:
                if getattr(exc, "errno", None) == errno.EINTR:
                    continue
                raise RemainderSidecarError(
                    f"cannot read {label}: {type(exc).__name__}"
                ) from exc
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        content = b"".join(chunks)
    except BaseException as _main:
        main_exc = _main
        raise
    finally:
        try:
            os.close(fd)
        except OSError as close_exc:
            if main_exc is not None:
                raise RemainderSidecarError(
                    f"cannot close {label}: {type(close_exc).__name__}"
                    f" (during {type(main_exc).__name__})"
                ) from main_exc
            raise RemainderSidecarError(
                f"cannot close {label}: {type(close_exc).__name__}"
            ) from close_exc
    try:
        post = _lstat(path, f"{label} post-read")
    except RemainderSidecarError:
        raise
    except OSError as exc:
        raise RemainderSidecarError(
            f"cannot post-lstat {label}: {type(exc).__name__}"
        ) from exc
    if _file_identity(post) != _file_identity(fd_stat):
        raise RemainderSidecarError(
            f"{label} identity changed after read: {path}"
        )
    return content


def validate_lock_member(path: str | os.PathLike[str]) -> None:
    """Validate a single lock file against R-01 contract. Completely read-only."""
    p = Path(path)
    if not _lexists(p):
        raise RemainderSidecarError(f"lock member does not exist: {p}")
    if not _LOCK_FILENAME_RE.fullmatch(p.name):
        raise RemainderSidecarError("lock member has invalid filename")
    content = _safe_read_bytes(p, "lock member", 1)
    if content != b"\0":
        raise RemainderSidecarError(
            f"lock member has invalid content (expected 1 null byte, got {len(content)} bytes)"
        )


def validate_archive_member(path: str | os.PathLike[str]) -> None:
    """Validate a single archive JSON file against R-01 contract. Completely read-only."""
    p = Path(path)
    if not _lexists(p):
        raise RemainderSidecarError(f"archive member does not exist: {p}")
    m = _ARCHIVE_FILENAME_RE.fullmatch(p.name)
    if not m:
        raise RemainderSidecarError("archive member has invalid filename")
    fn_bucket_id = m.group(1)
    fn_ts = m.group(2)
    fn_offset = m.group(4)
    if not _BUCKET_ID_RE.fullmatch(fn_bucket_id):
        raise RemainderSidecarError("archive filename contains invalid bucket_id")
    try:
        parsed_ts = datetime.strptime(fn_ts, "%Y%m%dT%H%M%SZ")
        if parsed_ts.strftime("%Y%m%dT%H%M%SZ") != fn_ts:
            raise ValueError("round-trip mismatch")
    except ValueError:
        raise RemainderSidecarError("archive filename has invalid timestamp")
    if fn_offset is not None:
        if fn_offset != str(int(fn_offset)):
            raise RemainderSidecarError("archive filename has invalid batch offset")
        offset_val = int(fn_offset)
        if offset_val == 0 or offset_val % MAX_ARCHIVE_ENTRIES != 0:
            raise RemainderSidecarError("archive filename has invalid batch offset")

    raw_bytes = _safe_read_bytes(p, "archive member", _ARCHIVE_MAX_BYTES)
    if len(raw_bytes) > _ARCHIVE_MAX_BYTES:
        raise RemainderSidecarError("archive member exceeds maximum size")

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise RemainderSidecarError("archive member contains invalid UTF-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise RemainderSidecarError("archive member contains invalid JSON")
    if not isinstance(data, dict):
        raise RemainderSidecarError("archive member must be a JSON object")

    unknown = set(data.keys()) - _ARCHIVE_ALLOWED_KEYS
    if unknown:
        raise RemainderSidecarError("archive member has unknown top-level keys")

    try:
        schema_val = data["schema"]
    except KeyError:
        raise RemainderSidecarError("archive member missing schema")
    if not isinstance(schema_val, int) or schema_val != SCHEMA_VERSION:
        raise RemainderSidecarError("archive member has unsupported schema")

    try:
        bucket_id = str(data["bucket_id"])
    except (KeyError, TypeError):
        raise RemainderSidecarError("archive member missing bucket_id")
    if not _BUCKET_ID_RE.fullmatch(bucket_id):
        raise RemainderSidecarError("archive member has invalid bucket_id")
    if bucket_id != fn_bucket_id:
        raise RemainderSidecarError(
            "archive member bucket_id does not match filename"
        )

    try:
        archived_at = str(data["archived_at"])
    except (KeyError, TypeError):
        raise RemainderSidecarError("archive member missing archived_at")
    if not archived_at:
        raise RemainderSidecarError("archive member has empty archived_at")
    try:
        dt = datetime.fromisoformat(archived_at)
    except (ValueError, TypeError):
        raise RemainderSidecarError("archive member has invalid archived_at")
    if dt.tzinfo is None:
        raise RemainderSidecarError("archive member archived_at must be UTC")
    utc_offset = dt.utcoffset()
    if utc_offset is None or utc_offset.total_seconds() != 0:
        raise RemainderSidecarError("archive member archived_at must be UTC")

    try:
        entries_raw = data["entries"]
    except KeyError:
        raise RemainderSidecarError("archive member missing entries")
    if not isinstance(entries_raw, list):
        raise RemainderSidecarError("archive member entries must be a list")
    if not entries_raw:
        raise RemainderSidecarError("archive member entries must not be empty")

    seen_ids: set[str] = set()
    for entry_data in entries_raw:
        entry = _validate_entry(entry_data)
        if entry.bucket_id != bucket_id:
            raise RemainderSidecarError(
                "archive entry bucket_id does not match archive bucket_id"
            )
        if entry.state not in _ARCHIVABLE_STATES:
            raise RemainderSidecarError("archive entry has non-archivable state")
        if entry.entry_id in seen_ids:
            raise RemainderSidecarError("archive member has duplicate entry_id")
        seen_ids.add(entry.entry_id)
