"""Crash-recoverable Markdown import transactions.

M-03 deliberately treats Markdown as the only authoritative commit boundary.
Untrusted input is rendered into a same-volume workspace first.  Publication is
then serialized across processes using the M-01 filesystem leases in this order:

    import-apply -> quota-pinned -> quota-high_importance -> sorted bucket IDs

No provider, embedding, network, LLM, config, or backup work belongs here.
"""

from __future__ import annotations

import hashlib
import errno
import json
import math
import os
import re
import shutil
import stat
import time
import uuid
from contextlib import AsyncExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import frontmatter

try:
    from bucket_manager import _filesystem_turn
    from snapshot_barrier import markdown_writer_turn, markdown_writer_turn_sync
    from tools import _identity
    from tools.plan.core import (
        LETTER_LOCK_PRINCIPALS,
        is_letter_bucket,
        letter_lock_state,
    )
    from utils import sanitize_name
    from ombrebrain.eventsourcing.footprint import validate_origin
except ImportError:  # pragma: no cover - package import
    from .bucket_manager import _filesystem_turn
    from .snapshot_barrier import markdown_writer_turn, markdown_writer_turn_sync
    from .tools import _identity
    from .tools.plan.core import (
        LETTER_LOCK_PRINCIPALS,
        is_letter_bucket,
        letter_lock_state,
    )
    from .utils import sanitize_name
    from .ombrebrain.eventsourcing.footprint import validate_origin


SCHEMA_VERSION = 1
TRANSACTION_DIRNAME = ".import-transactions"
ADMIN_RESTORE_SCOPE = "admin_restore"
STATE_STAGING = "STAGING"
STATE_VALIDATED = "VALIDATED"
STATE_PREPARED = "PREPARED"
STATE_PUBLISHING = "PUBLISHING"
STATE_MARKDOWN_COMMITTED = "MARKDOWN_COMMITTED"
STATE_DERIVED_PENDING = "DERIVED_PENDING"
STATE_COMMITTED = "COMMITTED"
STATE_ROLLED_BACK = "ROLLED_BACK"

_PRECOMMIT_STATES = {
    STATE_STAGING,
    STATE_VALIDATED,
    STATE_PREPARED,
    STATE_PUBLISHING,
}
_POSTCOMMIT_STATES = {
    STATE_MARKDOWN_COMMITTED,
    STATE_DERIVED_PENDING,
    STATE_COMMITTED,
}
_TYPE_SUBDIR = {
    "permanent": "permanent",
    "dynamic": "dynamic",
    "archive": "archive",
    "archived": "archive",
    "feel": "feel",
    "plan": "plans",
    "letter": "letters",
}
_MAX_BUCKET_ID = 200
_DEFAULT_MAX_BUCKET_BYTES = 50 * 1024
_DEFAULT_MAX_PINNED = 20
_HIGH_IMPORTANCE_THRESHOLD = 9
_HIGH_IMPORTANCE_CAP = 24
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL", "CLOCK$",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}

FaultInjector = Callable[[str], None]


class ImportTransactionError(RuntimeError):
    """A fail-closed import validation, publication, or recovery failure."""


class ImportRecoveryError(ImportTransactionError):
    """One or more incomplete transactions could not be recovered exactly."""


@dataclass(frozen=True)
class ImportCandidate:
    source_id: str
    markdown: bytes
    decision: str = "import"
    expected_live_sha256: str = ""
    expected_live_relative_path: str = ""
    conflicted_at_review: bool = False
    assigned_owner: str = ""

    @classmethod
    def from_path(
        cls,
        path: str | os.PathLike[str],
        **kwargs: Any,
    ) -> "ImportCandidate":
        """Compatibility constructor; the external path is never journaled."""

        return cls(markdown=Path(path).read_bytes(), **kwargs)


@dataclass(frozen=True)
class BatchApplyResult:
    txid: str
    imported_id_map: dict[str, str]
    history_id_map: dict[str, str]
    markdown_committed: bool
    derived_ids: tuple[str, ...]
    errors: tuple[str, ...] = field(default_factory=tuple)


def _inject(injector: FaultInjector | None, point: str) -> None:
    if injector is not None:
        injector(point)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_id(value: str, label: str = "bucket_id") -> str:
    candidate = str(value or "")
    if (
        not candidate
        or candidate in {".", ".."}
        or len(candidate) > _MAX_BUCKET_ID
        or "/" in candidate
        or "\\" in candidate
        or ":" in candidate
        or candidate.endswith((" ", "."))
        or any(ord(char) < 32 for char in candidate)
    ):
        raise ImportTransactionError(f"unsafe {label}")
    if candidate.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        raise ImportTransactionError(f"unsafe {label}")
    return candidate


def validate_job_owner(owner: str) -> str:
    value = str(owner or "").strip().lower().replace("-", "_")
    if value == ADMIN_RESTORE_SCOPE:
        return value
    if not value or value not in _identity.known_owner_values():
        raise ImportTransactionError("an explicit known import owner is required")
    return value


def strict_owner_tags(metadata: dict) -> tuple[str, ...]:
    """Thin strict wrapper over the one owner policy implementation."""

    owner = _identity.strict_owner_of(metadata, allow_untagged=True)
    return (owner,) if owner else ()


def _assign_owner(metadata: dict, assigned_owner: str) -> dict:
    tags = metadata.get("tags")
    if tags is None:
        tags = []
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise ImportTransactionError("bucket tags must be a list of strings")
    clean = [
        tag for tag in tags
        if not tag.strip().lower().startswith("owner:")
    ]
    clean.append(f"owner:{assigned_owner}")
    out = dict(metadata)
    out["tags"] = clean
    return out


def _validate_and_canonicalize_letter(metadata: dict, source_id: str) -> dict:
    """Reject corrupt imported Letter locks before any staging/publication.

    Logical Letter markers are deliberately broader than ``type``.  Once any
    marker identifies a Letter, canonicalize its storage type so changing only
    type/source_tool/tags cannot route it through a generic bucket surface.
    Historical Letters with no lock tuple remain compatible; any present tuple
    must be complete and valid and name one of the four trusted principals.
    """
    probe = {"id": source_id, "metadata": metadata, "content": ""}
    if not is_letter_bucket(probe):
        return metadata
    out = dict(metadata)
    out["type"] = "letter"
    tuple_fields = ("lock_type", "unlock_date", "locked_by_principal")
    if not any(field in out and str(out.get(field) or "").strip() for field in tuple_fields):
        return out
    principal = str(out.get("locked_by_principal") or "").strip().casefold()
    if principal not in LETTER_LOCK_PRINCIPALS:
        raise ImportTransactionError(
            f"invalid Letter lock principal for {source_id}"
        )
    state = letter_lock_state({"id": source_id, "metadata": out}, principal)
    if state.get("invalid"):
        raise ImportTransactionError(
            f"invalid or partial Letter lock tuple for {source_id}"
        )
    return out


def _parse_markdown(raw: bytes) -> tuple[dict, str]:
    try:
        text = raw.decode("utf-8")
        post = frontmatter.loads(text)
    except Exception as exc:
        raise ImportTransactionError(
            f"invalid UTF-8/frontmatter ({type(exc).__name__})"
        ) from exc
    metadata = dict(post.metadata)
    try:
        json.dumps(metadata, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ImportTransactionError("metadata must be finite and JSON-safe") from exc
    return metadata, post.content


def _normalize_candidate(
    candidate: ImportCandidate,
    *,
    job_owner: str,
    max_bucket_bytes: int,
) -> tuple[dict, str, str]:
    source_id = _validate_id(candidate.source_id, "source_id")
    if candidate.decision not in {"import", "overwrite", "keep_both", "skip"}:
        raise ImportTransactionError(f"invalid import decision for {source_id}")
    metadata, content = _parse_markdown(candidate.markdown)
    # Imported provenance is not authoritative for this local vault.  New and
    # keep-both entries receive a fresh local import receipt; overwrite handles
    # preservation of the live receipt under the publication locks.
    metadata.pop("footprint_origin", None)
    embedded_id = str(metadata.get("id") or metadata.get("bucket_id") or "")
    if embedded_id and embedded_id != source_id:
        raise ImportTransactionError(f"frontmatter ID mismatch for {source_id}")
    if len(content.encode("utf-8")) > max_bucket_bytes:
        raise ImportTransactionError(f"bucket content exceeds configured limit: {source_id}")

    incoming = strict_owner_tags(metadata)
    assigned = str(candidate.assigned_owner or "").strip().lower().replace("-", "_")
    if assigned:
        assigned = validate_job_owner(assigned)
    if incoming:
        owner = incoming[0]
        if assigned and assigned != owner:
            raise ImportTransactionError(f"owner assignment conflicts with {source_id}")
    else:
        if not assigned:
            raise ImportTransactionError(
                f"untagged import requires explicit owner assignment: {source_id}"
            )
        metadata = _assign_owner(metadata, assigned)
        owner = assigned
    if job_owner != ADMIN_RESTORE_SCOPE and owner != job_owner:
        raise ImportTransactionError(f"import owner is outside the job scope: {source_id}")

    importance = metadata.get("importance", 5)
    if isinstance(importance, bool):
        raise ImportTransactionError(f"invalid importance for {source_id}")
    try:
        importance_number = float(importance)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ImportTransactionError(f"invalid importance for {source_id}") from exc
    if not math.isfinite(importance_number) or not importance_number.is_integer():
        raise ImportTransactionError(f"invalid importance for {source_id}")
    importance_int = int(importance_number)
    if not 1 <= importance_int <= 10:
        raise ImportTransactionError(f"importance out of range for {source_id}")
    metadata["importance"] = importance_int
    if metadata.get("pinned"):
        metadata["importance"] = 10

    domains = metadata.get("domain") or []
    if isinstance(domains, str):
        domains = [domains]
    if not isinstance(domains, list) or any(not isinstance(item, str) for item in domains):
        raise ImportTransactionError(f"invalid domain for {source_id}")
    metadata["domain"] = domains
    metadata = _validate_and_canonicalize_letter(metadata, source_id)
    metadata["id"] = source_id
    metadata.pop("bucket_id", None)
    return metadata, content, owner


def _target_relative(metadata: dict, bucket_id: str) -> str:
    bucket_id = _validate_id(bucket_id)
    raw_type = metadata.get("type", "dynamic")
    if not isinstance(raw_type, str):
        raise ImportTransactionError("bucket type must be a string")
    bucket_type = raw_type.strip().lower()
    if bucket_type not in _TYPE_SUBDIR:
        raise ImportTransactionError(f"unsupported bucket type: {bucket_type}")
    subdir = _TYPE_SUBDIR[bucket_type]
    domains = metadata.get("domain") or []
    if bucket_type == "feel":
        primary = "沉淀物"
    elif bucket_type == "plan":
        primary = str(metadata.get("status") or "active")
    elif bucket_type == "letter":
        primary = "history"
    elif isinstance(domains, list) and domains:
        primary = str(domains[0])
    else:
        primary = "general"
    primary = sanitize_name(primary)
    safe_name = sanitize_name(str(metadata.get("name") or bucket_id))[:40]
    safe_id = re.sub(r"[^\w.-]", "_", bucket_id, flags=re.UNICODE)[:_MAX_BUCKET_ID]
    if not safe_id:
        raise ImportTransactionError("bucket ID cannot form a safe filename")
    return Path(subdir, primary, f"{safe_name}_{safe_id}.md").as_posix()


def _history_relative(history_id: str, metadata: dict) -> str:
    safe_name = sanitize_name(str(metadata.get("name") or "memory"))[:40]
    safe_id = re.sub(r"[^\w.-]", "_", history_id, flags=re.UNICODE)
    return Path("archive", "history", f"{safe_name}_{safe_id}.md").as_posix()


def _resolve_relative(root: Path, relative: str) -> Path:
    rel = Path(str(relative))
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise ImportTransactionError("unsafe transaction relative path")
    for component in rel.parts:
        if component in {"", ".", ".."}:
            raise ImportTransactionError("unsafe transaction path component")
        if component.endswith((" ", ".")):
            raise ImportTransactionError("Windows-ambiguous path component")
        device = component.split(".", 1)[0].upper()
        if device in _WINDOWS_RESERVED:
            raise ImportTransactionError("Windows reserved path component")
    root_abs = root.resolve()
    unresolved = root_abs / rel
    current = root_abs
    for component in rel.parts:
        current = current / component
        if current.exists() or current.is_symlink():
            try:
                info = os.lstat(current)
            except OSError as exc:
                raise ImportTransactionError("cannot inspect transaction path") from exc
            is_reparse = bool(
                getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            )
            is_junction = bool(
                getattr(current, "is_junction", lambda: False)()
            )
            if stat.S_ISLNK(info.st_mode) or is_reparse or is_junction:
                raise ImportTransactionError("symlink/reparse transaction path rejected")
            if current != unresolved and not stat.S_ISDIR(info.st_mode):
                raise ImportTransactionError("non-directory transaction path component")
            if current == unresolved and not (
                stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
            ):
                raise ImportTransactionError("special transaction target rejected")
    resolved = unresolved.resolve()
    try:
        resolved.relative_to(root_abs)
    except ValueError as exc:
        raise ImportTransactionError("transaction path escapes vault") from exc
    return resolved


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ImportTransactionError("live path escapes vault") from exc


def _fsync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_through_replace(source: Path, target: Path) -> None:
    if source.resolve(strict=False).anchor.lower() != target.resolve(
        strict=False
    ).anchor.lower():
        raise ImportTransactionError("transaction replace requires one volume")
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move.restype = wintypes.BOOL
        if not move(str(source), str(target), 0x1 | 0x8):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(source, target)
        _fsync_parent(target)


def _write_bytes(path: Path, data: bytes, *, replace: bool = True) -> None:
    # Validate every existing ancestor before and after mkdir; import inputs may
    # not redirect publication through symlinks, junctions, or device files.
    root = path.anchor and Path(path.anchor) or Path(".")
    current = root
    for component in path.parts[1:] if path.anchor else path.parts[:-1]:
        current = current / component
        if current.exists() and current.is_symlink():
            raise ImportTransactionError("symlink write path rejected")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not path.is_file():
        raise ImportTransactionError("non-regular write target rejected")
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temp, "xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            _write_through_replace(temp, path)
        else:
            os.link(temp, path)
            os.unlink(temp)
        _fsync_parent(path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


@contextmanager
def _sync_filesystem_turn(
    base_dir: str,
    key: str,
    timeout_seconds: float = 30.0,
):
    """Blocking peer of M-01's async lease for startup recovery."""

    lock_dir = Path(base_dir) / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_id = hashlib.sha256(
        str(key).encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    lock_path = lock_dir / f"{lock_id}.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    handle = os.fdopen(descriptor, "r+b", buffering=0)
    deadline = time.monotonic() + timeout_seconds
    busy = {
        errno.EACCES,
        errno.EAGAIN,
        getattr(errno, "EWOULDBLOCK", errno.EAGAIN),
    }
    acquired = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
        while not acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(
                        handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                    )
                acquired = True
            except OSError as exc:
                if (
                    exc.errno not in busy
                    and getattr(exc, "winerror", None) not in {32, 33}
                ):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("timed out waiting for import recovery lease")
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


def _write_manifest(txdir: Path, manifest: dict) -> None:
    raw = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    path = txdir / "manifest.json"
    _write_bytes(path, raw)
    reread = json.loads(path.read_text(encoding="utf-8"))
    if reread != manifest:
        raise ImportTransactionError("transaction manifest reread mismatch")


def _cleanup_transaction_payload(txdir: Path) -> None:
    """Remove body-bearing transient files while retaining the terminal journal."""
    for name in ("stage", "old", "history"):
        path = txdir / name
        if not path.exists():
            continue
        info = os.lstat(path)
        is_reparse = bool(
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )
        if stat.S_ISLNK(info.st_mode) or is_reparse or not stat.S_ISDIR(info.st_mode):
            raise ImportRecoveryError(
                f"transaction payload path is unsafe: {name}"
            )
        shutil.rmtree(path)
        _fsync_parent(path)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _iter_bucket_files(root: Path) -> Iterable[Path]:
    for dirname in ("permanent", "dynamic", "archive", "feel", "plans", "letters"):
        base = root / dirname
        if base.is_dir():
            for path in base.rglob("*.md"):
                try:
                    info = os.lstat(path)
                except OSError as exc:
                    raise ImportTransactionError(
                        "cannot inspect bucket namespace"
                    ) from exc
                is_reparse = bool(
                    getattr(info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                )
                if (
                    stat.S_ISLNK(info.st_mode)
                    or is_reparse
                    or not stat.S_ISREG(info.st_mode)
                ):
                    raise ImportTransactionError(
                        "non-regular Markdown in bucket namespace"
                    )
                yield path


def _find_id_paths(root: Path, bucket_id: str) -> list[Path]:
    found: list[Path] = []
    for path in _iter_bucket_files(root):
        metadata, _content = _parse_markdown(path.read_bytes())
        if str(metadata.get("id") or metadata.get("bucket_id") or "") == bucket_id:
            found.append(path)
    return sorted(found, key=lambda item: _relative(root, item))


def review_bucket_snapshot(buckets_dir: str, bucket_id: str) -> dict[str, str] | None:
    """Return one fresh ID/path/hash/owner snapshot without exposing the body."""

    root = Path(buckets_dir).resolve()
    paths = _find_id_paths(root, _validate_id(bucket_id))
    if len(paths) > 1:
        raise ImportTransactionError(f"duplicate live bucket ID: {bucket_id}")
    if not paths:
        return None
    path = paths[0]
    metadata, _content = _parse_markdown(path.read_bytes())
    owner = _identity.strict_owner_of(metadata, allow_untagged=True)
    return {
        "bucket_id": bucket_id,
        "relative_path": _relative(root, path),
        "sha256": _hash_file(path),
        "owner": owner,
    }


def _render(metadata: dict, content: str) -> bytes:
    return frontmatter.dumps(frontmatter.Post(content, **metadata)).encode("utf-8")


def _quota_flags(metadata: dict) -> tuple[bool, bool]:
    pinned = bool(metadata.get("pinned"))
    high = (
        int(metadata.get("importance") or 5) >= _HIGH_IMPORTANCE_THRESHOLD
        and not pinned
        and not bool(metadata.get("protected"))
    )
    return pinned, high


async def _validate_quotas(
    root: Path,
    bucket_manager: Any,
    entries: list[dict],
) -> None:
    try:
        active = await bucket_manager.list_all(include_archive=False, fresh=True)
    except TypeError:
        active = await bucket_manager.list_all(include_archive=False)
    pinned = sum(bool(item.get("metadata", {}).get("pinned")) for item in active)
    high = sum(
        int(item.get("metadata", {}).get("importance") or 0)
        >= _HIGH_IMPORTANCE_THRESHOLD
        and not item.get("metadata", {}).get("pinned")
        and not item.get("metadata", {}).get("protected")
        for item in active
    )
    by_id = {str(item.get("id") or ""): item for item in active}
    for entry in entries:
        if entry["decision"] == "overwrite":
            old = by_id.get(entry["source_id"])
            if old:
                old_pinned, old_high = _quota_flags(old.get("metadata", {}) or {})
                pinned -= int(old_pinned)
                high -= int(old_high)
        pinned += int(entry["new_pinned"])
        high += int(entry["new_high"])
    config = getattr(bucket_manager, "config", {}) or {}
    limits = config.get("limits", {}) or {}
    pinned_cap = int(limits.get("max_pinned") or _DEFAULT_MAX_PINNED)
    if pinned_cap > 0 and pinned > pinned_cap:
        raise ImportTransactionError(
            f"import would exceed pinned quota ({pinned}/{pinned_cap})"
        )
    if high > _HIGH_IMPORTANCE_CAP:
        raise ImportTransactionError(
            f"import would exceed high-importance quota ({high}/{_HIGH_IMPORTANCE_CAP})"
        )


def _entry_paths(root: Path, txdir: Path, entry: dict) -> dict[str, Path]:
    paths = {
        "stage": _resolve_relative(txdir, entry["stage_path"]),
        "target": _resolve_relative(root, entry["target_path"]),
    }
    if entry.get("old_path"):
        paths["old"] = _resolve_relative(txdir, entry["old_path"])
    if entry.get("old_live_path"):
        paths["old_live"] = _resolve_relative(root, entry["old_live_path"])
    if entry.get("history_stage_path"):
        paths["history_stage"] = _resolve_relative(txdir, entry["history_stage_path"])
    if entry.get("history_path"):
        paths["history"] = _resolve_relative(root, entry["history_path"])
    return paths


def _rollback(root: Path, txdir: Path, manifest: dict, injector: FaultInjector | None) -> None:
    _inject(injector, "rollback.before")
    if manifest.get("state") in {STATE_STAGING, STATE_VALIDATED}:
        manifest["state"] = STATE_ROLLED_BACK
        manifest["updated_at"] = _now_iso()
        _write_manifest(txdir, manifest)
        _cleanup_transaction_payload(txdir)
        _inject(injector, "rollback.after")
        return
    failures: list[str] = []
    for entry in reversed(manifest.get("entries", [])):
        try:
            paths = _entry_paths(root, txdir, entry)
            history = paths.get("history")
            if history and history.exists():
                if _hash_file(history) != entry.get("history_sha256"):
                    raise ImportRecoveryError("history hash changed during rollback")
                history.unlink()
                _fsync_parent(history)

            target = paths["target"]
            decision = entry["decision"]
            if decision == "overwrite":
                old = paths["old"]
                old_live = paths["old_live"]
                if not old.exists() or _hash_file(old) != entry["old_sha256"]:
                    raise ImportRecoveryError("old snapshot is missing or corrupt")
                if target.exists():
                    target_hash = _hash_file(target)
                    if target_hash == entry["new_sha256"]:
                        target.unlink()
                        _fsync_parent(target)
                    elif target.resolve() != old_live.resolve() or target_hash != entry["old_sha256"]:
                        raise ImportRecoveryError("target contains an unknown generation")
                if old_live.exists():
                    if _hash_file(old_live) != entry["old_sha256"]:
                        raise ImportRecoveryError("old live path contains an unknown generation")
                else:
                    _write_bytes(old_live, old.read_bytes(), replace=False)
            else:
                if target.exists():
                    if _hash_file(target) != entry["new_sha256"]:
                        raise ImportRecoveryError("new target contains an unknown generation")
                    target.unlink()
                    _fsync_parent(target)
        except Exception as exc:
            failures.append(
                f"{entry.get('source_id', '?')}:{type(exc).__name__}:{exc}"
            )
    if failures:
        raise ImportRecoveryError("rollback failed: " + " | ".join(failures))
    manifest["state"] = STATE_ROLLED_BACK
    manifest["updated_at"] = _now_iso()
    _write_manifest(txdir, manifest)
    _cleanup_transaction_payload(txdir)
    _inject(injector, "rollback.after")


async def apply_import_batch(
    *,
    buckets_dir: str,
    bucket_manager: Any,
    job_owner: str,
    candidates: Iterable[ImportCandidate],
    footprint_origin: dict,
    fault_injector: FaultInjector | None = None,
) -> BatchApplyResult:
    """Stage, validate, and atomically publish one Markdown batch."""

    canonical_origin = validate_origin(footprint_origin)
    if canonical_origin["via"] != "import" or canonical_origin["surface"] != "import_transaction":
        raise ImportTransactionError(
            "import transaction requires an explicit import_transaction origin"
        )
    root = Path(buckets_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    owner = validate_job_owner(job_owner)
    selected = [item for item in candidates if item.decision != "skip"]
    source_ids = [item.source_id for item in selected]
    if len(source_ids) != len(set(source_ids)):
        raise ImportTransactionError("an import batch contains duplicate source IDs")
    txid = uuid.uuid4().hex
    txdir = root / TRANSACTION_DIRNAME / txid
    _inject(fault_injector, "stage.mkdir")
    (txdir / "stage").mkdir(parents=True, exist_ok=False)
    (txdir / "old").mkdir()
    (txdir / "history").mkdir()
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "txid": txid,
        "state": STATE_STAGING,
        "job_owner": owner,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "derived_state": "not_started",
        "entries": [],
    }
    _write_manifest(txdir, manifest)

    config = getattr(bucket_manager, "config", {}) or {}
    max_bytes = int(
        ((config.get("limits", {}) or {}).get("max_bucket_bytes"))
        or _DEFAULT_MAX_BUCKET_BYTES
    )
    prepared: list[dict] = []
    target_ids: set[str] = set()
    for ordinal, candidate in enumerate(selected):
        metadata, content, candidate_owner = _normalize_candidate(
            candidate,
            job_owner=owner,
            max_bucket_bytes=max_bytes,
        )
        if candidate.decision in {"overwrite", "keep_both"} and not candidate.conflicted_at_review:
            raise ImportTransactionError(
                f"{candidate.decision} was not authorized by a conflict review"
            )
        target_id = (
            str(uuid.uuid4())
            if candidate.decision == "keep_both"
            else candidate.source_id
        )
        while target_id in target_ids:
            target_id = str(uuid.uuid4())
        target_ids.add(target_id)
        metadata["id"] = target_id
        metadata["footprint_origin"] = dict(canonical_origin)
        rendered = _render(metadata, content)
        target_relative = _target_relative(metadata, target_id)
        stage_relative = Path("stage", f"{ordinal:06d}.md").as_posix()
        _inject(fault_injector, "stage.write.before")
        _write_bytes(_resolve_relative(txdir, stage_relative), rendered, replace=False)
        _inject(fault_injector, "stage.write.after")
        new_pinned, new_high = _quota_flags(metadata)
        history_id = (
            f"{candidate.source_id[:160]}-superseded-{uuid.uuid4().hex[:12]}"
            if candidate.decision == "overwrite"
            else ""
        )
        prepared.append(
            {
                "ordinal": ordinal,
                "source_id": candidate.source_id,
                "target_id": target_id,
                "history_id": history_id,
                "decision": candidate.decision,
                "owner": candidate_owner,
                "expected_live_sha256": candidate.expected_live_sha256,
                "expected_live_path": candidate.expected_live_relative_path,
                "target_path": target_relative,
                "stage_path": stage_relative,
                "new_sha256": _sha256(rendered),
                "new_pinned": new_pinned,
                "new_high": new_high,
                "old_path": "",
                "old_live_path": "",
                "old_sha256": "",
                "history_stage_path": "",
                "history_path": "",
                "history_sha256": "",
                "published_target": False,
                "published_history": False,
                "removed_old": False,
            }
        )
    manifest["entries"] = prepared
    manifest["state"] = STATE_VALIDATED
    manifest["updated_at"] = _now_iso()
    _write_manifest(txdir, manifest)
    _inject(fault_injector, "journal.validated")

    lock_ids: set[str] = set()
    for entry in prepared:
        lock_ids.update((entry["source_id"], entry["target_id"]))
        if entry["history_id"]:
            lock_ids.add(entry["history_id"])

    try:
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(markdown_writer_turn(str(root)))
            await stack.enter_async_context(_filesystem_turn(str(root), "import-apply"))
            # M-01's logical quota turns rendezvous on these exact kernel keys:
            # _quota_turn("pinned") -> content-quota-pinned.
            await stack.enter_async_context(
                _filesystem_turn(str(root), "content-quota-pinned")
            )
            await stack.enter_async_context(
                _filesystem_turn(str(root), "content-quota-high_importance")
            )
            for bucket_id in sorted(lock_ids):
                factory = getattr(bucket_manager, "_bucket_turn", None)
                if callable(factory):
                    await stack.enter_async_context(factory(bucket_id))
                else:
                    await stack.enter_async_context(
                        _filesystem_turn(str(root), f"bucket-{bucket_id}")
                    )
            _inject(fault_injector, "locks.acquired")

            for entry, candidate in zip(prepared, selected):
                current = review_bucket_snapshot(str(root), entry["source_id"])
                if entry["decision"] == "import":
                    if current is not None:
                        raise ImportTransactionError(
                            f"new conflict appeared for {entry['source_id']}; review again"
                        )
                else:
                    if current is None:
                        raise ImportTransactionError(
                            f"reviewed conflict disappeared for {entry['source_id']}"
                        )
                    if (
                        current["sha256"] != candidate.expected_live_sha256
                        or current["relative_path"]
                        != candidate.expected_live_relative_path
                    ):
                        raise ImportTransactionError(
                            f"reviewed conflict changed for {entry['source_id']}; review again"
                        )
                    if entry["decision"] == "overwrite":
                        current_owner = current["owner"]
                        if not current_owner or current_owner != entry["owner"]:
                            raise ImportTransactionError(
                                f"overwrite owner mismatch for {entry['source_id']}"
                            )
                    if entry["decision"] == "keep_both":
                        target_collision = review_bucket_snapshot(
                            str(root), entry["target_id"]
                        )
                        if target_collision is not None:
                            raise ImportTransactionError("keep_both target ID collision")

                target = _resolve_relative(root, entry["target_path"])
                if target.exists() and (
                    current is None
                    or target.resolve()
                    != _resolve_relative(root, current["relative_path"]).resolve()
                ):
                    raise ImportTransactionError(
                        f"target path collision for {entry['source_id']}"
                    )
                if entry["history_id"]:
                    # The final name is recalculated from the exact old
                    # metadata below; the ID itself must already be globally
                    # collision-free while its sorted lease is held.
                    if review_bucket_snapshot(
                        str(root), entry["history_id"]
                    ) is not None:
                        raise ImportTransactionError("history ID collision")
            _inject(fault_injector, "fresh.verify")
            await _validate_quotas(root, bucket_manager, prepared)

            for entry in prepared:
                if entry["decision"] != "overwrite":
                    continue
                current = review_bucket_snapshot(str(root), entry["source_id"])
                if current is None:
                    raise ImportTransactionError("overwrite source disappeared")
                old_live = _resolve_relative(root, current["relative_path"])
                old_bytes = old_live.read_bytes()
                old_relative = Path("old", f"{entry['ordinal']:06d}.md").as_posix()
                _write_bytes(_resolve_relative(txdir, old_relative), old_bytes, replace=False)
                old_meta, old_content = _parse_markdown(old_bytes)
                # Overwrite preserves only an already-valid local origin.  A
                # missing or malformed legacy value remains unrecorded rather
                # than being replaced by a retroactive import claim.
                stage_meta, stage_content = _parse_markdown(
                    _resolve_relative(txdir, entry["stage_path"]).read_bytes()
                )
                try:
                    live_origin = validate_origin(old_meta.get("footprint_origin"))
                except (TypeError, ValueError):
                    stage_meta.pop("footprint_origin", None)
                else:
                    stage_meta["footprint_origin"] = live_origin
                corrected_stage = _render(stage_meta, stage_content)
                _write_bytes(
                    _resolve_relative(txdir, entry["stage_path"]),
                    corrected_stage,
                    replace=True,
                )
                entry["new_sha256"] = _sha256(corrected_stage)
                history_id = entry["history_id"]
                _validate_id(history_id, "history_id")
                old_meta["id"] = history_id
                old_meta["type"] = "archived"
                old_meta["superseded_by"] = entry["target_id"]
                old_meta["archived_at"] = _now_iso()
                history_bytes = _render(old_meta, old_content)
                history_stage = Path(
                    "history", f"{entry['ordinal']:06d}.md"
                ).as_posix()
                _write_bytes(
                    _resolve_relative(txdir, history_stage),
                    history_bytes,
                    replace=False,
                )
                entry.update(
                    {
                        "history_id": history_id,
                        "old_path": old_relative,
                        "old_live_path": current["relative_path"],
                        "old_sha256": _sha256(old_bytes),
                        "history_stage_path": history_stage,
                        "history_path": _history_relative(history_id, old_meta),
                        "history_sha256": _sha256(history_bytes),
                    }
                )
                _inject(fault_injector, "old.snapshot")

            manifest["state"] = STATE_PREPARED
            manifest["updated_at"] = _now_iso()
            _write_manifest(txdir, manifest)
            _inject(fault_injector, "journal.prepared")
            manifest["state"] = STATE_PUBLISHING
            manifest["updated_at"] = _now_iso()
            _write_manifest(txdir, manifest)

            for entry in prepared:
                paths = _entry_paths(root, txdir, entry)
                if entry["decision"] == "overwrite":
                    history = paths["history"]
                    _inject(fault_injector, "publish.history.before")
                    _write_bytes(
                        history,
                        paths["history_stage"].read_bytes(),
                        replace=False,
                    )
                    entry["published_history"] = True
                    _write_manifest(txdir, manifest)
                    _inject(fault_injector, "publish.history.after")

                target = paths["target"]
                _inject(fault_injector, "publish.target.before")
                if entry["decision"] == "overwrite":
                    old_live = paths["old_live"]
                    if target.resolve() == old_live.resolve():
                        _write_bytes(target, paths["stage"].read_bytes(), replace=True)
                    else:
                        _write_bytes(target, paths["stage"].read_bytes(), replace=False)
                        entry["published_target"] = True
                        _write_manifest(txdir, manifest)
                        if _hash_file(old_live) != entry["old_sha256"]:
                            raise ImportTransactionError("overwrite source changed during publish")
                        old_live.unlink()
                        _fsync_parent(old_live)
                        entry["removed_old"] = True
                else:
                    _write_bytes(target, paths["stage"].read_bytes(), replace=False)
                entry["published_target"] = True
                manifest["updated_at"] = _now_iso()
                _write_manifest(txdir, manifest)
                _inject(fault_injector, "publish.target.after")

            manifest["state"] = STATE_MARKDOWN_COMMITTED
            manifest["derived_state"] = "pending"
            manifest["updated_at"] = _now_iso()
            _write_manifest(txdir, manifest)
            _inject(fault_injector, "journal.markdown_committed")
    except BaseException as exc:
        durable_manifest = manifest
        try:
            durable_manifest = _load_manifest(txdir)
        except Exception as rollback_exc:
            raise ImportRecoveryError(
                f"import failed ({type(exc).__name__}: {exc}); "
                f"journal reread also failed "
                f"({type(rollback_exc).__name__}: {rollback_exc})"
            ) from exc
        if durable_manifest.get("state") in _POSTCOMMIT_STATES:
            _verify_committed(root, txdir, durable_manifest)
            raise
        try:
            if durable_manifest.get("state") in {STATE_PREPARED, STATE_PUBLISHING}:
                async with markdown_writer_turn(str(root)):
                    _rollback(root, txdir, durable_manifest, fault_injector)
            else:
                _rollback(root, txdir, durable_manifest, fault_injector)
        except Exception as rollback_exc:
            raise ImportRecoveryError(
                f"import failed ({type(exc).__name__}: {exc}); "
                f"rollback also failed ({type(rollback_exc).__name__}: {rollback_exc})"
            ) from exc
        raise

    invalidate = getattr(bucket_manager, "_invalidate_bm25", None)
    if callable(invalidate):
        invalidate()
    manifest["state"] = STATE_DERIVED_PENDING
    manifest["updated_at"] = _now_iso()
    _write_manifest(txdir, manifest)
    return BatchApplyResult(
        txid=txid,
        imported_id_map={
            entry["source_id"]: entry["target_id"] for entry in prepared
        },
        history_id_map={
            entry["source_id"]: entry["history_id"]
            for entry in prepared
            if entry["history_id"]
        },
        markdown_committed=True,
        derived_ids=tuple(entry["target_id"] for entry in prepared),
    )


def mark_import_transaction_committed(
    buckets_dir: str,
    txid: str,
    *,
    derived_error: str = "",
) -> None:
    root = Path(buckets_dir).resolve()
    if not re.fullmatch(r"[0-9a-f]{32}", str(txid)):
        raise ImportTransactionError("invalid transaction ID")
    txdir = root / TRANSACTION_DIRNAME / txid
    manifest = _load_manifest(txdir)
    if manifest["state"] not in {
        STATE_MARKDOWN_COMMITTED,
        STATE_DERIVED_PENDING,
        STATE_COMMITTED,
    }:
        raise ImportTransactionError("cannot finalize an uncommitted transaction")
    _verify_committed(root, txdir, manifest)
    manifest["state"] = STATE_COMMITTED
    manifest["derived_state"] = "error" if derived_error else "complete"
    manifest["derived_error"] = str(derived_error)[:500] if derived_error else ""
    manifest["updated_at"] = _now_iso()
    _write_manifest(txdir, manifest)
    _cleanup_transaction_payload(txdir)


def _load_manifest(txdir: Path) -> dict:
    try:
        txinfo = os.lstat(txdir)
    except OSError as exc:
        raise ImportRecoveryError(
            f"cannot inspect import transaction: {txdir.name}"
        ) from exc
    if (
        stat.S_ISLNK(txinfo.st_mode)
        or not stat.S_ISDIR(txinfo.st_mode)
        or bool(
            getattr(txinfo, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )
    ):
        raise ImportRecoveryError("transaction workspace is a symlink/reparse point")
    path = txdir / "manifest.json"
    try:
        path_info = os.lstat(path)
    except OSError as exc:
        raise ImportRecoveryError(
            f"missing import manifest: {txdir.name}"
        ) from exc
    if (
        stat.S_ISLNK(path_info.st_mode)
        or not stat.S_ISREG(path_info.st_mode)
        or bool(
            getattr(path_info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )
    ):
        raise ImportRecoveryError("manifest is not a regular file")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ImportRecoveryError(
            f"unreadable import manifest: {txdir.name}"
        ) from exc
    if (
        manifest.get("schema_version") != SCHEMA_VERSION
        or manifest.get("txid") != txdir.name
        or not isinstance(manifest.get("entries"), list)
    ):
        raise ImportRecoveryError(f"invalid import manifest: {txdir.name}")
    validate_job_owner(str(manifest.get("job_owner") or ""))
    return manifest


def _verify_committed(root: Path, txdir: Path, manifest: dict) -> None:
    failures: list[str] = []
    for entry in manifest["entries"]:
        try:
            paths = _entry_paths(root, txdir, entry)
            if not paths["target"].is_file():
                raise ImportRecoveryError("committed target is missing")
            if _hash_file(paths["target"]) != entry["new_sha256"]:
                raise ImportRecoveryError("committed target hash mismatch")
            history = paths.get("history")
            if history is not None and (
                not history.is_file()
                or _hash_file(history) != entry["history_sha256"]
            ):
                raise ImportRecoveryError("committed history hash mismatch")
            if entry["decision"] == "overwrite":
                old_live = paths["old_live"]
                if old_live.resolve() != paths["target"].resolve() and old_live.exists():
                    raise ImportRecoveryError("old path survived committed overwrite")
        except Exception as exc:
            failures.append(
                f"{entry.get('source_id', '?')}:{type(exc).__name__}:{exc}"
            )
    if failures:
        raise ImportRecoveryError(
            "committed import verification failed: " + " | ".join(failures)
        )


def recover_import_transactions(
    buckets_dir: str,
    *,
    fault_injector: FaultInjector | None = None,
) -> list[str]:
    """Recover every durable journal before any live component is constructed."""

    root = Path(buckets_dir).resolve()
    base = root / TRANSACTION_DIRNAME
    if not base.exists():
        return []
    if not base.is_dir():
        raise ImportRecoveryError("import transaction root is not a directory")
    recovered: list[str] = []
    failures: list[str] = []
    with markdown_writer_turn_sync(str(root)):
        with _sync_filesystem_turn(str(root), "import-apply"):
            for txdir in sorted(base.iterdir(), key=lambda path: path.name):
                if not txdir.is_dir():
                    continue
                try:
                    manifest = _load_manifest(txdir)
                    state = str(manifest.get("state") or "")
                    if state in _PRECOMMIT_STATES:
                        _rollback(root, txdir, manifest, fault_injector)
                        recovered.append(txdir.name)
                    elif state in _POSTCOMMIT_STATES:
                        _verify_committed(root, txdir, manifest)
                        if state == STATE_COMMITTED:
                            _cleanup_transaction_payload(txdir)
                        recovered.append(txdir.name)
                    elif state == STATE_ROLLED_BACK:
                        _cleanup_transaction_payload(txdir)
                        recovered.append(txdir.name)
                    else:
                        raise ImportRecoveryError(f"unknown import state: {state}")
                except Exception as exc:
                    failures.append(f"{txdir.name}:{type(exc).__name__}:{exc}")
    if failures:
        raise ImportRecoveryError(
            "import recovery failed closed: " + " | ".join(failures)
        )
    return recovered
