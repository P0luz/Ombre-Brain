"""Durable ordinary-write embedding intents for M-05.

This module is deliberately storage-only.  It does not call an embedding
provider, mutate ``embeddings.db``, or wire itself into hold/grow/trace.

Privacy invariant: intents contain hashes, never authoritative bucket content.
Concurrency invariant: every slot mutation holds a cross-process byte lease;
terminal transitions compare ``intent_id`` and ``generation`` so an old worker
cannot overwrite a newer intent for the same bucket.

External-lock invariant: this storage module never acquires M-04 or M-01
turns.  Integrated callers must acquire locks in this order::

    M-04 barrier -> content/quota/merge-target -> bucket turn -> outbox slot

Provider work happens after every one of those locks is released.  CONFLICT is
terminal evidence; reconciliation prepares a fresh intent and generation.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import stat
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping


SCHEMA_VERSION = 2

OP_UPSERT = "UPSERT"
OP_DELETE = "DELETE"
_OPERATIONS = {OP_UPSERT, OP_DELETE}

STATE_PREPARED = "PREPARED"
STATE_READY = "READY"
STATE_APPLIED = "APPLIED"
STATE_ABORTED = "ABORTED"
STATE_CONFLICT = "CONFLICT"
_STATES = {
    STATE_PREPARED,
    STATE_READY,
    STATE_APPLIED,
    STATE_ABORTED,
    STATE_CONFLICT,
}
_TERMINAL_STATES = {STATE_APPLIED, STATE_ABORTED, STATE_CONFLICT}
_ALLOWED_TRANSITIONS = {
    STATE_PREPARED: {STATE_READY, STATE_ABORTED, STATE_CONFLICT},
    STATE_READY: {STATE_APPLIED, STATE_CONFLICT},
}

_BUCKET_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")
_INTENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class EmbeddingOutboxError(RuntimeError):
    """The durable intent cannot be trusted or persisted."""


class StaleIntentError(EmbeddingOutboxError):
    """A worker attempted to mutate a slot that now contains a newer intent."""


@dataclass(frozen=True)
class EmbeddingIntent:
    schema: int
    intent_id: str
    generation: int
    bucket_id: str
    operation: str
    before_sha256: str | None
    target_sha256: str | None
    state: str
    created_at: str
    updated_at: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def content_sha256(content: str) -> str:
    """Hash exact Unicode content encoded as UTF-8."""

    if not isinstance(content, str):
        raise TypeError("content must be str")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _valid_hash(value: object) -> bool:
    return value is None or bool(_SHA256_RE.fullmatch(str(value)))


def _validate_intent(value: Mapping[str, Any]) -> EmbeddingIntent:
    try:
        intent = EmbeddingIntent(
            schema=int(value["schema"]),
            intent_id=str(value["intent_id"]),
            generation=int(value["generation"]),
            bucket_id=str(value["bucket_id"]),
            operation=str(value["operation"]),
            before_sha256=(
                None if value["before_sha256"] is None else str(value["before_sha256"])
            ),
            target_sha256=(
                None if value["target_sha256"] is None else str(value["target_sha256"])
            ),
            state=str(value["state"]),
            created_at=str(value["created_at"]),
            updated_at=str(value["updated_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise EmbeddingOutboxError("invalid embedding intent shape") from exc

    if intent.schema != SCHEMA_VERSION:
        raise EmbeddingOutboxError(f"unsupported embedding intent schema: {intent.schema}")
    if not _INTENT_ID_RE.fullmatch(intent.intent_id):
        raise EmbeddingOutboxError("invalid intent_id")
    if intent.generation < 1:
        raise EmbeddingOutboxError("invalid generation")
    if not _BUCKET_ID_RE.fullmatch(intent.bucket_id):
        raise EmbeddingOutboxError("invalid bucket_id")
    if intent.operation not in _OPERATIONS:
        raise EmbeddingOutboxError(f"invalid operation: {intent.operation}")
    if not _valid_hash(intent.before_sha256) or not _valid_hash(intent.target_sha256):
        raise EmbeddingOutboxError("invalid content hash")
    if intent.operation == OP_UPSERT and intent.target_sha256 is None:
        raise EmbeddingOutboxError("UPSERT requires target_sha256")
    if intent.operation == OP_DELETE and intent.target_sha256 is not None:
        raise EmbeddingOutboxError("DELETE target_sha256 must be null")
    if intent.state not in _STATES:
        raise EmbeddingOutboxError(f"invalid state: {intent.state}")
    if not intent.created_at or not intent.updated_at:
        raise EmbeddingOutboxError("intent timestamps are required")
    return intent


def _is_reparse_point(info: os.stat_result) -> bool:
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(info, "st_file_attributes", 0) & flag)


def _lstat(path: Path, label: str) -> os.stat_result:
    try:
        return os.lstat(path)
    except OSError as exc:
        raise EmbeddingOutboxError(f"cannot inspect {label}: {path}") from exc


def _assert_regular_directory(path: Path, label: str) -> None:
    info = _lstat(path, label)
    if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info) or not stat.S_ISDIR(info.st_mode):
        raise EmbeddingOutboxError(f"{label} is not a regular directory: {path}")


def _assert_regular_file(path: Path, label: str) -> None:
    info = _lstat(path, label)
    if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info) or not stat.S_ISREG(info.st_mode):
        raise EmbeddingOutboxError(f"{label} is not a regular file: {path}")


def _lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _root(root: str | os.PathLike[str], *, create: bool) -> Path:
    raw = Path(root).expanduser()
    path = Path(os.path.abspath(os.fspath(raw)))
    if _lexists(path):
        _assert_regular_directory(path, "embedding outbox root")
        return path
    if not create:
        return path
    parent = path.parent
    _assert_regular_directory(parent, "embedding outbox parent")
    try:
        path.mkdir()
    except FileExistsError:
        # A peer may have created the root after our lstat.  Trust it only after
        # the same no-link/no-reparse/type validation used for an existing root.
        pass
    except OSError as exc:
        raise EmbeddingOutboxError(f"cannot create embedding outbox root: {path}") from exc
    _assert_regular_directory(path, "embedding outbox root")
    return path


def _intent_path(
    root: str | os.PathLike[str], bucket_id: str, *, create_root: bool
) -> Path:
    if not _BUCKET_ID_RE.fullmatch(bucket_id):
        raise EmbeddingOutboxError("invalid bucket_id")
    path = _root(root, create=create_root) / f"{bucket_id}.json"
    if _lexists(path):
        _assert_regular_file(path, "embedding intent")
    return path


@contextlib.contextmanager
def _slot_turn(
    root: str | os.PathLike[str], bucket_id: str, timeout_seconds: float = 30.0
) -> Iterator[None]:
    """Blocking peer of the existing M-01 filesystem byte lease protocol."""

    base = _root(root, create=True)
    lock_dir = base / ".locks"
    if _lexists(lock_dir):
        _assert_regular_directory(lock_dir, "embedding outbox lock directory")
    else:
        try:
            lock_dir.mkdir()
        except FileExistsError:
            # Concurrent first use is expected; validation below decides whether
            # the peer created the one regular directory we permit.
            pass
        except OSError as exc:
            raise EmbeddingOutboxError(
                f"cannot create embedding outbox lock directory: {lock_dir}"
            ) from exc
        _assert_regular_directory(lock_dir, "embedding outbox lock directory")
    lock_id = hashlib.sha256(
        f"embedding-outbox-{bucket_id}".encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    lock_path = lock_dir / f"{lock_id}.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    handle = os.fdopen(descriptor, "r+b", buffering=0)
    acquired = False
    deadline = time.monotonic() + timeout_seconds
    busy = {
        errno.EACCES,
        errno.EAGAIN,
        getattr(errno, "EWOULDBLOCK", errno.EAGAIN),
    }
    try:
        while not acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:  # pragma: no cover - production host is Windows
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                if (
                    exc.errno not in busy
                    and getattr(exc, "winerror", None) not in {32, 33}
                ):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("timed out waiting for embedding outbox slot")
                time.sleep(0.01)
        # Initialize the leased byte only after acquiring it.  On Windows a
        # peer may already hold byte zero while this handle still observes an
        # empty newly-created file; writing before the lease can then raise an
        # unhandled PermissionError instead of following the retry path above.
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _durable_json(path: Path, value: Mapping[str, Any]) -> None:
    _assert_regular_directory(path.parent, "embedding outbox root")
    if _lexists(path):
        _assert_regular_file(path, "embedding intent")
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        if os.name != "nt":  # pragma: no cover
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()


def load_intent(path: str | os.PathLike[str]) -> EmbeddingIntent:
    intent_path = Path(path)
    if not _lexists(intent_path):
        raise EmbeddingOutboxError(f"embedding intent does not exist: {intent_path}")
    _assert_regular_file(intent_path, "embedding intent")
    try:
        raw = json.loads(intent_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EmbeddingOutboxError(f"cannot read embedding intent: {intent_path}") from exc
    if not isinstance(raw, Mapping):
        raise EmbeddingOutboxError("embedding intent must be a JSON object")
    return _validate_intent(raw)


def load_slot(
    root: str | os.PathLike[str], bucket_id: str
) -> EmbeddingIntent | None:
    base = _root(root, create=False)
    if not _lexists(base):
        return None
    path = _intent_path(base, bucket_id, create_root=False)
    if not _lexists(path):
        return None
    return load_intent(path)


def _next_generation(path: Path) -> int:
    if not path.exists():
        return 1
    return load_intent(path).generation + 1


def _prepare(
    root: str | os.PathLike[str],
    bucket_id: str,
    operation: str,
    before_sha256: str | None,
    target_sha256: str | None,
) -> EmbeddingIntent:
    path = _intent_path(root, bucket_id, create_root=True)
    with _slot_turn(root, bucket_id):
        now = _now_iso()
        intent = _validate_intent(
            {
                "schema": SCHEMA_VERSION,
                "intent_id": uuid.uuid4().hex,
                "generation": _next_generation(path),
                "bucket_id": bucket_id,
                "operation": operation,
                "before_sha256": before_sha256,
                "target_sha256": target_sha256,
                "state": STATE_PREPARED,
                "created_at": now,
                "updated_at": now,
            }
        )
        _durable_json(path, asdict(intent))
        if load_intent(path) != intent:
            raise EmbeddingOutboxError("durable embedding intent reread mismatch")
        return intent


def prepare_upsert(
    root: str | os.PathLike[str],
    bucket_id: str,
    before_content: str | None,
    target_content: str,
) -> EmbeddingIntent:
    """Prepare an UPSERT before the authoritative Markdown commit.

    ``before_content=None`` denotes a create where the bucket did not exist.
    Whitespace-only target content is rejected because the current embedding
    engine intentionally generates no vector for it.
    """

    if not isinstance(target_content, str) or not target_content.strip():
        raise EmbeddingOutboxError("UPSERT target content must be non-empty")
    if before_content is not None and not isinstance(before_content, str):
        raise TypeError("before_content must be str or None")
    return _prepare(
        root,
        bucket_id,
        OP_UPSERT,
        None if before_content is None else content_sha256(before_content),
        content_sha256(target_content),
    )


def prepare_delete(
    root: str | os.PathLike[str], bucket_id: str, before_content: str
) -> EmbeddingIntent:
    """Prepare a durable vector deletion before the Markdown bucket disappears."""

    if not isinstance(before_content, str):
        raise TypeError("before_content must be str")
    return _prepare(
        root,
        bucket_id,
        OP_DELETE,
        content_sha256(before_content),
        None,
    )


def classify_intent(
    intent: EmbeddingIntent, current_content: str | None
) -> str:
    """Classify a PREPARED intent from authoritative Markdown state.

    The caller must obtain ``current_content`` while holding the canonical
    bucket turn.  This pure function performs no durable transition.
    """

    checked = _validate_intent(asdict(intent))
    if checked.state != STATE_PREPARED:
        return checked.state
    current_hash = None if current_content is None else content_sha256(current_content)
    if checked.operation == OP_UPSERT:
        if current_hash == checked.target_sha256:
            return STATE_READY
        if current_hash == checked.before_sha256:
            return STATE_ABORTED
        return STATE_CONFLICT
    # DELETE has no target content hash: a missing authoritative bucket is the
    # successful post-commit state that makes the vector deletion READY.
    if current_hash is None:
        return STATE_READY
    if current_hash == checked.before_sha256:
        return STATE_ABORTED
    return STATE_CONFLICT


def transition_intent(
    root: str | os.PathLike[str],
    expected: EmbeddingIntent,
    new_state: str,
) -> EmbeddingIntent:
    """Persist one state transition using intent/generation CAS."""

    checked = _validate_intent(asdict(expected))
    allowed = _ALLOWED_TRANSITIONS.get(checked.state, set())
    if new_state not in allowed:
        raise EmbeddingOutboxError(
            f"invalid state transition: {checked.state} -> {new_state}"
        )
    path = _intent_path(root, checked.bucket_id, create_root=False)
    with _slot_turn(root, checked.bucket_id):
        current = load_intent(path)
        if (
            current.intent_id != checked.intent_id
            or current.generation != checked.generation
        ):
            raise StaleIntentError("embedding intent slot contains a newer generation")
        if current != checked:
            raise StaleIntentError("embedding intent changed since worker read it")
        updated = _validate_intent(
            asdict(replace(current, state=new_state, updated_at=_now_iso()))
        )
        _durable_json(path, asdict(updated))
        if load_intent(path) != updated:
            raise EmbeddingOutboxError("durable state transition reread mismatch")
        return updated


def recover_intent(
    root: str | os.PathLike[str],
    expected: EmbeddingIntent,
    current_content: str | None,
) -> EmbeddingIntent:
    """Classify and persist PREPARED recovery under generation CAS."""

    outcome = classify_intent(expected, current_content)
    if outcome == expected.state:
        return expected
    return transition_intent(root, expected, outcome)


def mark_applied(
    root: str | os.PathLike[str], expected_ready: EmbeddingIntent
) -> EmbeddingIntent:
    """Mark provider/SQLite work complete without clobbering a newer slot."""

    return transition_intent(root, expected_ready, STATE_APPLIED)


def is_terminal(intent: EmbeddingIntent) -> bool:
    return _validate_intent(asdict(intent)).state in _TERMINAL_STATES


def list_intents(
    root: str | os.PathLike[str], state_filter: str | None = None
) -> tuple[EmbeddingIntent, ...]:
    """Return validated direct-slot intents without creating the outbox root.

    This is an operational visibility helper only.  Corrupt, symlink, reparse,
    or non-regular ``*.json`` members fail closed instead of disappearing from
    the listing.  Lock files and temporary files are not intent slots.
    """

    if state_filter is not None and state_filter not in _STATES:
        raise EmbeddingOutboxError(f"invalid state filter: {state_filter}")
    base = _root(root, create=False)
    if not _lexists(base):
        return ()
    intents: list[EmbeddingIntent] = []
    try:
        children = sorted(base.iterdir(), key=lambda child: child.name)
    except OSError as exc:
        raise EmbeddingOutboxError(f"cannot enumerate embedding outbox: {base}") from exc
    for child in children:
        if not child.name.endswith(".json"):
            continue
        _assert_regular_file(child, "embedding intent")
        intent = load_intent(child)
        if state_filter is None or intent.state == state_filter:
            intents.append(intent)
    return tuple(intents)
