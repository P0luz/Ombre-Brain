"""M-04 isolated Markdown staging and archive/manifest primitives.

This module deliberately has no server, scheduler, or production-vault entry
point.  ``build_isolated_archive`` accepts an already-quiescent *copy* of a
bucket vault; it does not acquire M-04's future cross-store snapshot locks.

Markdown remains authoritative.  SQLite, configuration, secrets, raw evidence,
and runtime journals are intentionally outside this first-batch format.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
from typing import Any, Callable
import zipfile

import frontmatter

from snapshot_barrier import SnapshotCoordination, authoritative_markdown_snapshot_turn


MANIFEST_NAME = "backup_manifest.json"
MANIFEST_KIND = "ombre-brain-m04-isolated-bucket-backup"
MANIFEST_SCHEMA_VERSION = 2

BUCKET_DIRS = frozenset({"permanent", "dynamic", "feel", "plans", "letters", "archive"})
REMAINDER_DIR = ".remainders"
REMAINDER_ARCHIVE_SUBDIR = "archive"
REMAINDER_QUARANTINE_SUBDIR = "quarantine"
REMAINDER_LOCKS_SUBDIR = ".locks"
_REMAINDER_MEMBER_TYPES = frozenset({
    "remainder_sidecar",
    "remainder_archive",
    "remainder_quarantine",
})
_ALL_MEMBER_TYPES = frozenset({"bucket_markdown"}) | _REMAINDER_MEMBER_TYPES
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_MEMBERS = 20_000
MAX_BUCKET_BYTES = 10 * 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_COMPRESSION_RATIO = 1_000.0
STAGING_MARKER_NAME = "m04_staging_manifest.json"
STAGING_KIND = "ombre-brain-m04-authoritative-markdown-staging"
STAGING_SCHEMA_VERSION = 1
RESTORE_MARKER_NAME = "m04_restored_manifest.json"
RESTORE_KIND = "ombre-brain-m04-isolated-roundtrip-restore"
RESTORE_SCHEMA_VERSION = 1


class BackupArchiveError(ValueError):
    """The archive or its source snapshot is unsafe or incomplete."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _is_reparse_point(info: os.stat_result) -> bool:
    return bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _normalize_member_path(raw_name: str) -> str:
    """Return a portable ZIP path or reject traversal and Windows aliases."""

    raw_name = str(raw_name or "")
    if not raw_name or "\x00" in raw_name:
        raise BackupArchiveError("archive member has an empty or NUL path")
    name = raw_name.replace("\\", "/")
    if name.startswith("/"):
        raise BackupArchiveError("archive member has an absolute path")
    parts = PurePosixPath(name).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise BackupArchiveError("archive member has unsafe path components")
    reserved = {"CON", "PRN", "AUX", "NUL", "CLOCK$"}
    reserved.update({f"COM{i}" for i in range(1, 10)})
    reserved.update({f"LPT{i}" for i in range(1, 10)})
    for part in parts:
        if ":" in part or part.endswith((" ", ".")):
            raise BackupArchiveError("archive member has a non-portable path")
        if part.split(".", 1)[0].upper() in reserved:
            raise BackupArchiveError("archive member uses a Windows device name")
    return "/".join(parts)


def _assert_regular_file(path: Path, root: Path) -> Path:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise BackupArchiveError(f"cannot inspect source member: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info) or not stat.S_ISREG(info.st_mode):
        raise BackupArchiveError(f"source member is not a regular file: {path}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise BackupArchiveError(f"cannot resolve source member: {path}") from exc
    if not resolved.is_relative_to(root):
        raise BackupArchiveError(f"source member escapes snapshot root: {path}")
    return resolved


def _bucket_id(metadata: dict[str, Any], member_path: str) -> str:
    value = str(metadata.get("id") or metadata.get("bucket_id") or "").strip()
    if not value or len(value) > 200 or any(char in value for char in "/\\\x00"):
        raise BackupArchiveError(f"bucket has no safe explicit ID: {member_path}")
    return value


def _as_string_refs(value: Any, member_path: str) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise BackupArchiveError(
            f"source_refs must remain list[str] in batch-1 archives: {member_path}"
        )
    return list(value)


def _metadata_from_markdown(data: bytes, member_path: str) -> dict[str, Any]:
    try:
        post = frontmatter.loads(data.decode("utf-8"))
    except Exception as exc:
        raise BackupArchiveError(f"bucket markdown is not strict UTF-8/frontmatter: {member_path}") from exc
    if not isinstance(post.metadata, dict):
        raise BackupArchiveError(f"bucket metadata is not a mapping: {member_path}")
    return dict(post.metadata)


def _closure_from_files(files: dict[str, bytes]) -> dict[str, Any]:
    """Validate internal bucket links and inventory external evidence pointers.

    ``source_bucket`` and explicit ``bucket:<id>`` values are internal closure
    edges.  ``evidence_id`` and ordinary ``source_refs`` are stable external
    pointers only: their raw bodies are intentionally not copied by batch 1.
    """

    ids: dict[str, str] = {}
    metadata_by_path: dict[str, dict[str, Any]] = {}
    for path, data in sorted(files.items()):
        metadata = _metadata_from_markdown(data, path)
        bucket_id = _bucket_id(metadata, path)
        if bucket_id in ids:
            raise BackupArchiveError(f"duplicate bucket ID in snapshot: {bucket_id}")
        ids[bucket_id] = path
        metadata_by_path[path] = metadata

    edges: list[dict[str, str]] = []
    external_refs: set[str] = set()
    for path, metadata in sorted(metadata_by_path.items()):
        bucket_id = _bucket_id(metadata, path)
        source_bucket = str(metadata.get("source_bucket") or "").strip()
        if source_bucket:
            edges.append({"from": bucket_id, "field": "source_bucket", "to": source_bucket})
        evidence_id = str(metadata.get("evidence_id") or "").strip()
        if evidence_id:
            external_refs.add(evidence_id)
        for ref in _as_string_refs(metadata.get("source_refs"), path):
            cleaned = ref.strip()
            if not cleaned:
                continue
            if cleaned.startswith("bucket:"):
                target = cleaned.split(":", 1)[1].strip()
                if not target:
                    raise BackupArchiveError(f"empty bucket reference: {path}")
                edges.append({"from": bucket_id, "field": "source_refs", "to": target})
            else:
                external_refs.add(cleaned)

    missing = sorted({edge["to"] for edge in edges} - set(ids))
    if missing:
        raise BackupArchiveError(
            "snapshot has dangling internal bucket references: " + ", ".join(missing[:3])
        )
    return {
        "bucket_ids": sorted(ids),
        "internal_edges": sorted(edges, key=lambda item: (item["from"], item["field"], item["to"])),
        "external_evidence_refs": sorted(external_refs),
        "raw_evidence_included": False,
    }


def _collect_bucket_files(snapshot_root: str | os.PathLike[str]) -> dict[str, bytes]:
    root = Path(snapshot_root).resolve()
    if not root.is_dir():
        raise BackupArchiveError(f"snapshot root does not exist: {root}")
    files: dict[str, bytes] = {}
    casefold_paths: set[str] = set()
    for directory in sorted(BUCKET_DIRS):
        base = root / directory
        if not base.exists():
            continue
        try:
            base_info = os.lstat(base)
        except OSError as exc:
            raise BackupArchiveError(f"cannot inspect bucket directory: {directory}") from exc
        if stat.S_ISLNK(base_info.st_mode) or _is_reparse_point(base_info) or not stat.S_ISDIR(base_info.st_mode):
            raise BackupArchiveError(f"bucket directory is unsafe: {directory}")
        for candidate in sorted(base.rglob("*.md"), key=lambda item: item.as_posix()):
            source = _assert_regular_file(candidate, root)
            relative = source.relative_to(root).as_posix()
            archive_path = _normalize_member_path(f"buckets/{relative}")
            if archive_path.casefold() in casefold_paths:
                raise BackupArchiveError(f"case-insensitive duplicate source path: {archive_path}")
            casefold_paths.add(archive_path.casefold())
            try:
                data = source.read_bytes()
            except OSError as exc:
                raise BackupArchiveError(f"cannot read source member: {relative}") from exc
            if len(data) > MAX_BUCKET_BYTES:
                raise BackupArchiveError(f"bucket exceeds batch-1 size limit: {archive_path}")
            files[archive_path] = data
    if not files:
        raise BackupArchiveError("snapshot has no bucket markdown files")
    return files


def _build_manifest(
    files: dict[str, bytes],
    *,
    created_at: str,
    remainder_files: dict[str, tuple[bytes, str]] | None = None,
) -> dict[str, Any]:
    closure = _closure_from_files(files)
    entries = [
        {
            "path": path,
            "type": "bucket_markdown",
            "size": len(data),
            "sha256": _sha256(data),
        }
        for path, data in sorted(files.items())
    ]
    if remainder_files:
        for path, (data, member_type) in sorted(remainder_files.items()):
            entries.append({
                "path": path,
                "type": member_type,
                "size": len(data),
                "sha256": _sha256(data),
            })
    entries.sort(key=lambda e: e["path"])
    return {
        "kind": MANIFEST_KIND,
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "created_at": created_at,
        "file_count": len(entries),
        "total_bytes": sum(entry["size"] for entry in entries),
        "files": entries,
        "reference_closure": closure,
        "policy": {
            "contains_markdown_authority": True,
            "contains_embeddings_db": False,
            "contains_runtime_journals": False,
            "contains_secrets": False,
            "contains_raw_evidence": False,
        },
    }


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    """Stable identity fields used to detect a path swap during the freeze."""

    return (
        stat.S_IFMT(info.st_mode),
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_size),
        int(info.st_mtime_ns),
    )


def _assert_regular_directory(path: Path, label: str) -> os.stat_result:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise BackupArchiveError(f"cannot inspect {label}: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info) or not stat.S_ISDIR(info.st_mode):
        raise BackupArchiveError(f"{label} is not a regular directory: {path}")
    return info


def _canonical_regular_directory(value: str | os.PathLike[str], label: str) -> Path:
    path = Path(value).expanduser()
    _assert_regular_directory(path, label)
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise BackupArchiveError(f"cannot resolve {label}: {path}") from exc


def _checked_source_stat(path: Path, expected: tuple[int, int, int, int, int]) -> None:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise BackupArchiveError(f"source path changed during snapshot: {path}") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or _is_reparse_point(info)
        or not stat.S_ISREG(info.st_mode)
        or _identity(info) != expected
    ):
        raise BackupArchiveError(f"source path identity changed during snapshot: {path}")


def _claim_member_path(member: str, folded: set[str]) -> None:
    """Reject duplicate and Windows case-fold-colliding member paths."""

    normalized = _normalize_member_path(member)
    key = normalized.casefold()
    if key in folded:
        raise BackupArchiveError(f"case-insensitive duplicate source path: {normalized}")
    folded.add(key)


def _enumerate_authoritative_markdown(root: Path) -> list[tuple[Path, str, tuple[int, int, int, int, int]]]:
    """Strictly enumerate only the six authoritative bucket roots.

    ``Path.rglob`` is intentionally not used: each descendant is inspected with
    lstat and every non-directory/non-regular-file entry is rejected before a
    copy is attempted.
    """

    _assert_regular_directory(root, "snapshot root")
    found: list[tuple[Path, str, tuple[int, int, int, int, int]]] = []
    folded: set[str] = set()
    total = 0

    def walk(directory: Path, relative_parts: tuple[str, ...]) -> None:
        nonlocal total
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise BackupArchiveError(f"cannot enumerate authoritative directory: {directory}") from exc
        for child in children:
            path = Path(child.path)
            try:
                # DirEntry.stat() reports dev/inode as zero on this Windows
                # runtime, so use lstat for the identity later compared with
                # the opened descriptor.
                info = os.lstat(path)
            except OSError as exc:
                raise BackupArchiveError(f"cannot inspect authoritative path: {path}") from exc
            if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
                raise BackupArchiveError(f"authoritative path is a symlink/reparse point: {path}")
            parts = (*relative_parts, child.name)
            if stat.S_ISDIR(info.st_mode):
                if child.name.endswith(".md"):
                    raise BackupArchiveError(f"authoritative path has an unexpected Markdown directory: {path}")
                walk(path, parts)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise BackupArchiveError(f"authoritative path is not a regular file: {path}")
            if not child.name.endswith(".md"):
                raise BackupArchiveError(f"unexpected non-Markdown authoritative file: {path}")
            relative = "/".join(parts)
            member = _normalize_member_path(f"buckets/{relative}")
            if member.casefold() in folded:
                raise BackupArchiveError(f"case-insensitive duplicate source path: {member}")
            if info.st_size < 0 or info.st_size > MAX_BUCKET_BYTES:
                raise BackupArchiveError(f"bucket exceeds staging size limit: {member}")
            _claim_member_path(member, folded)
            total += int(info.st_size)
            if len(found) >= MAX_MEMBERS or total > MAX_TOTAL_UNCOMPRESSED_BYTES:
                raise BackupArchiveError("staging source exceeds member or total-size limit")
            found.append((path, member, _identity(info)))

    for bucket_dir in sorted(BUCKET_DIRS):
        base = root / bucket_dir
        try:
            info = os.lstat(base)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise BackupArchiveError(f"cannot inspect bucket directory: {bucket_dir}") from exc
        if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info) or not stat.S_ISDIR(info.st_mode):
            raise BackupArchiveError(f"bucket directory is unsafe: {bucket_dir}")
        walk(base, (bucket_dir,))
    if not found:
        raise BackupArchiveError("snapshot has no authoritative bucket markdown files")
    return found


def _enumerate_remainder_files(
    root: Path,
    folded: set[str],
    *,
    prior_count: int = 0,
    prior_bytes: int = 0,
) -> list[tuple[Path, str, str, tuple[int, int, int, int, int]]]:
    """Enumerate .remainders authority members for manifest/staging.

    Returns ``(abs_path, archive_member_path, member_type, identity)``.
    Does NOT enumerate ``.locks`` contents (runtime, not authoritative).
    Validates ``.locks`` members if the directory exists.
    """

    rem = root / REMAINDER_DIR
    try:
        rem_info = os.lstat(rem)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise BackupArchiveError(f"cannot inspect remainder root: {rem}") from exc
    if stat.S_ISLNK(rem_info.st_mode) or _is_reparse_point(rem_info):
        raise BackupArchiveError(f"remainder root is a symlink/reparse point: {rem}")
    if not stat.S_ISDIR(rem_info.st_mode):
        raise BackupArchiveError(f"remainder root is not a regular directory: {rem}")

    found: list[tuple[Path, str, str, tuple[int, int, int, int, int]]] = []
    total = prior_bytes
    combined_count = prior_count

    def _check_file(path: Path, member: str, member_type: str) -> None:
        nonlocal total, combined_count
        info = _lstat_safe(path)
        if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
            raise BackupArchiveError(f"remainder member is a symlink/reparse point: {path}")
        if not stat.S_ISREG(info.st_mode):
            raise BackupArchiveError(f"remainder member is not a regular file: {path}")
        if info.st_size < 0 or info.st_size > MAX_BUCKET_BYTES:
            raise BackupArchiveError(f"remainder member exceeds size limit: {member}")
        _claim_member_path(member, folded)
        total += int(info.st_size)
        combined_count += 1
        if combined_count >= MAX_MEMBERS or total > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise BackupArchiveError("staging source exceeds member or total-size limit")
        found.append((path, member, member_type, _identity(info)))

    try:
        children = sorted(os.scandir(rem), key=lambda e: e.name)
    except OSError as exc:
        raise BackupArchiveError(f"cannot enumerate remainder root: {rem}") from exc

    for child in children:
        path = Path(child.path)
        info = _lstat_safe(path)

        if child.name == REMAINDER_LOCKS_SUBDIR:
            if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
                raise BackupArchiveError(f".locks is a symlink/reparse point: {path}")
            if not stat.S_ISDIR(info.st_mode):
                raise BackupArchiveError(f".locks is not a regular directory: {path}")
            _validate_locks_dir(path)
            continue

        if child.name == REMAINDER_ARCHIVE_SUBDIR:
            if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
                raise BackupArchiveError(f"remainder archive dir is a symlink/reparse point: {path}")
            if not stat.S_ISDIR(info.st_mode):
                raise BackupArchiveError(f"remainder archive dir is not a regular directory: {path}")
            _enumerate_remainder_subdir(
                path, ".remainders/archive", "remainder_archive",
                ".json", found, folded, _check_file,
            )
            continue

        if child.name == REMAINDER_QUARANTINE_SUBDIR:
            if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
                raise BackupArchiveError(f"remainder quarantine dir is a symlink/reparse point: {path}")
            if not stat.S_ISDIR(info.st_mode):
                raise BackupArchiveError(f"remainder quarantine dir is not a regular directory: {path}")
            _enumerate_remainder_subdir(
                path, ".remainders/quarantine", "remainder_quarantine",
                ".json.corrupt", found, folded, _check_file,
            )
            continue

        if stat.S_ISDIR(info.st_mode):
            raise BackupArchiveError(f"unexpected directory in remainder root: {path}")

        if not child.name.endswith(".json"):
            raise BackupArchiveError(f"unexpected non-JSON file in remainder root: {path}")

        member = _normalize_member_path(f".remainders/{child.name}")
        _check_file(path, member, "remainder_sidecar")

    return found


def _lstat_safe(path: Path) -> os.stat_result:
    try:
        return os.lstat(path)
    except OSError as exc:
        raise BackupArchiveError(f"cannot inspect remainder path: {path}") from exc


def _enumerate_remainder_subdir(
    directory: Path,
    prefix: str,
    member_type: str,
    suffix: str,
    found: list,
    folded: set[str],
    check_fn: Any,
) -> None:
    try:
        children = sorted(os.scandir(directory), key=lambda e: e.name)
    except OSError as exc:
        raise BackupArchiveError(f"cannot enumerate remainder subdir: {directory}") from exc
    for child in children:
        path = Path(child.path)
        info = _lstat_safe(path)
        if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
            raise BackupArchiveError(f"remainder subdir member is a symlink/reparse point: {path}")
        if stat.S_ISDIR(info.st_mode):
            raise BackupArchiveError(f"unexpected directory in remainder subdir: {path}")
        if not stat.S_ISREG(info.st_mode):
            raise BackupArchiveError(f"remainder subdir member is not a regular file: {path}")
        if not child.name.endswith(suffix):
            raise BackupArchiveError(f"unexpected file suffix in remainder subdir: {path}")
        member = _normalize_member_path(f"{prefix}/{child.name}")
        check_fn(path, member, member_type)


def _is_valid_remainder_archive_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if len(parts) < 2 or parts[0] != ".remainders":
        return False
    if len(parts) == 2:
        return parts[1].endswith(".json")
    if len(parts) == 3:
        if parts[1] == "archive":
            return parts[2].endswith(".json")
        if parts[1] == "quarantine":
            return parts[2].endswith(".json.corrupt")
    return False


def _validate_locks_dir(locks_dir: Path) -> None:
    try:
        children = sorted(os.scandir(locks_dir), key=lambda e: e.name)
    except OSError as exc:
        raise BackupArchiveError(f"cannot enumerate .locks directory: {locks_dir}") from exc
    for child in children:
        path = Path(child.path)
        info = _lstat_safe(path)
        if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
            raise BackupArchiveError(f".locks member is a symlink/reparse point: {path}")
        if not stat.S_ISREG(info.st_mode):
            raise BackupArchiveError(f".locks member is not a regular file: {path}")
        if not child.name.endswith(".lock"):
            raise BackupArchiveError(f".locks member has unexpected suffix: {path}")
        if info.st_size != 1:
            raise BackupArchiveError(f".locks member has invalid size {info.st_size}: {path}")


def _read_source_bytes(
    source: Path,
    expected: tuple[int, int, int, int, int],
) -> bytes:
    """Read bytes with fd-level identity verification to defeat ABA swaps."""

    _checked_source_stat(source, expected)
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(source, flags)
    except OSError as exc:
        raise BackupArchiveError(f"cannot open source member: {source}") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != expected:
            raise BackupArchiveError(f"source descriptor identity changed during snapshot: {source}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) != expected[3]:
        raise BackupArchiveError(f"source size changed during snapshot: {source}")
    _checked_source_stat(source, expected)
    return data


def _copy_source_file(
    source: Path,
    target: Path,
    expected: tuple[int, int, int, int, int],
) -> bytes:
    """Copy one pre-enumerated member and reject observed source path races."""

    _checked_source_stat(source, expected)
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        source_fd = os.open(source, flags)
    except OSError as exc:
        raise BackupArchiveError(f"cannot open source member: {source}") from exc
    temporary: Path | None = None
    try:
        opened = os.fstat(source_fd)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != expected:
            raise BackupArchiveError(f"source descriptor identity changed during snapshot: {source}")
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".m04-copy-", suffix=".tmp", dir=target.parent
        )
        temporary = Path(temporary_name)
        chunks: list[bytes] = []
        with os.fdopen(source_fd, "rb", closefd=False) as reader, os.fdopen(
            descriptor, "wb"
        ) as writer:
            while True:
                chunk = reader.read(1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        data = b"".join(chunks)
        if len(data) != expected[3]:
            raise BackupArchiveError(f"source size changed during snapshot: {source}")
        os.replace(temporary, target)
        temporary = None
    except OSError as exc:
        raise BackupArchiveError(f"cannot copy source member: {source}") from exc
    finally:
        try:
            os.close(source_fd)
        except OSError:
            pass
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
    _checked_source_stat(source, expected)
    return data


def _write_marker(stage: Path, marker_name: str, payload: dict[str, Any]) -> None:
    """Atomically replace a marker after flushing its contents."""

    target = stage / marker_name
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    if len(data) > MAX_MANIFEST_BYTES:
        raise BackupArchiveError("staging marker exceeds manifest size limit")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".m04-marker-", suffix=".tmp", dir=stage)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError as exc:
        raise BackupArchiveError("cannot durably write staging marker") from exc
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _write_staging_marker(stage: Path, payload: dict[str, Any]) -> None:
    _write_marker(stage, STAGING_MARKER_NAME, payload)


def _new_staging_directory(root: Path, staging_parent: str | os.PathLike[str]) -> Path:
    parent = Path(staging_parent).expanduser()
    parent_info = _assert_regular_directory(parent, "staging parent")
    parent = parent.resolve(strict=True)
    if parent == root or parent.is_relative_to(root):
        raise BackupArchiveError("staging parent must be outside the authoritative vault")
    if int(parent_info.st_dev) != int(os.stat(root).st_dev):
        raise BackupArchiveError("staging parent must be on the same volume as the vault")
    stage = Path(tempfile.mkdtemp(prefix=".m04-staging-", dir=parent))
    stage_info = _assert_regular_directory(stage, "fresh staging directory")
    if int(stage_info.st_dev) != int(os.stat(root).st_dev):
        raise BackupArchiveError("fresh staging directory is not on the vault volume")
    return stage


def _staging_payload(
    *,
    state: str,
    manifest: dict[str, Any] | None,
    coordination: SnapshotCoordination | None,
    failure: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": STAGING_KIND,
        "schema_version": STAGING_SCHEMA_VERSION,
        "state": state,
        "updated_at": _now_iso(),
        "sqlite_policy": "derived:not-captured:rebuild-required",
    }
    if manifest is not None:
        payload["backup_manifest"] = manifest
    if coordination is not None:
        payload["terminal_m03_transactions"] = list(coordination.terminal_m03_transactions)
        payload["emig_state"] = coordination.emig_state
    if failure:
        payload["failure"] = failure[:200]
    return payload


def _read_marker(stage: Path, marker_name: str, label: str) -> dict[str, Any]:
    marker = stage / marker_name
    try:
        info = os.lstat(marker)
    except OSError as exc:
        raise BackupArchiveError(f"{label} has no readable completion marker") from exc
    if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info) or not stat.S_ISREG(info.st_mode):
        raise BackupArchiveError(f"{label} completion marker is unsafe")
    if info.st_size < 0 or info.st_size > MAX_MANIFEST_BYTES:
        raise BackupArchiveError(f"{label} completion marker exceeds size limit")
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupArchiveError(f"{label} completion marker is invalid") from exc
    if not isinstance(payload, dict):
        raise BackupArchiveError(f"{label} completion marker must be an object")
    return payload


def _read_staging_marker(stage: Path) -> dict[str, Any]:
    return _read_marker(stage, STAGING_MARKER_NAME, "staging directory")


def _read_authoritative_files(root: Path) -> dict[str, bytes]:
    """Rehash a strict vault-shaped copy using the frozen member paths."""

    files: dict[str, bytes] = {}
    for source, member, identity in _enumerate_authoritative_markdown(root):
        _checked_source_stat(source, identity)
        try:
            files[member] = source.read_bytes()
        except OSError as exc:
            raise BackupArchiveError(f"cannot rehash authoritative member: {source}") from exc
        _checked_source_stat(source, identity)
    return files


def _read_authoritative_files_v2(root: Path) -> tuple[dict[str, bytes], dict[str, tuple[bytes, str]]]:
    """Read both Markdown and remainder files for a v2 manifest."""

    markdown_files: dict[str, bytes] = {}
    for source, member, identity in _enumerate_authoritative_markdown(root):
        _checked_source_stat(source, identity)
        try:
            markdown_files[member] = source.read_bytes()
        except OSError as exc:
            raise BackupArchiveError(f"cannot rehash authoritative member: {source}") from exc
        _checked_source_stat(source, identity)

    remainder_files: dict[str, tuple[bytes, str]] = {}
    folded: set[str] = set()
    md_total = sum(len(v) for v in markdown_files.values())
    for m in markdown_files:
        folded.add(m.casefold())
    for source, member, member_type, identity in _enumerate_remainder_files(
        root, folded, prior_count=len(markdown_files), prior_bytes=md_total,
    ):
        data = _read_source_bytes(source, identity)
        remainder_files[member] = (data, member_type)

    return markdown_files, remainder_files


def verify_authoritative_markdown_staging(staging_root: str | os.PathLike[str]) -> dict[str, Any]:
    """Fully rehash and validate a completed staging copy outside the freeze."""

    root = _canonical_regular_directory(staging_root, "staging root")
    payload = _read_staging_marker(root)
    if (
        payload.get("kind") != STAGING_KIND
        or payload.get("schema_version") != STAGING_SCHEMA_VERSION
        or payload.get("state") != "complete"
        or payload.get("sqlite_policy") != "derived:not-captured:rebuild-required"
    ):
        raise BackupArchiveError("staging directory is incomplete or uses an unsupported marker")
    manifest = payload.get("backup_manifest")
    schema_ver = manifest.get("schema_version") if isinstance(manifest, dict) else None
    allowed_top = BUCKET_DIRS | {STAGING_MARKER_NAME}
    if schema_ver == 2:
        allowed_top = allowed_top | {REMAINDER_DIR}
    for child in root.iterdir():
        if child.name not in allowed_top:
            raise BackupArchiveError(f"staging directory has unexpected top-level member: {child.name}")
    if schema_ver == 2:
        md_files, rem_files = _read_authoritative_files_v2(root)
        all_files = dict(md_files)
        for k, (data, _t) in rem_files.items():
            all_files[k] = data
        _validate_manifest(manifest, all_files)
    else:
        files = _read_authoritative_files(root)
        _validate_manifest(manifest, files)
    return {
        "staging_root": str(root),
        "backup_manifest": manifest,
        "terminal_m03_transactions": tuple(payload.get("terminal_m03_transactions") or ()),
        "emig_state": payload.get("emig_state"),
        "sqlite_policy": payload["sqlite_policy"],
    }


async def create_authoritative_markdown_staging(
    buckets_dir: str | os.PathLike[str],
    staging_parent: str | os.PathLike[str],
    *,
    created_at: str | None = None,
    embedding_db_path: str | os.PathLike[str] | None = None,
    timeout_seconds: float = 30.0,
    lock_root: str | os.PathLike[str] | None = None,
    fault_injector: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Create and verify a same-volume Markdown staging copy.

    The exclusive barrier covers only preflight, strict enumeration, byte copy,
    source hashing/closure construction, and the durable ``complete`` marker.
    Full staging verification deliberately occurs after that barrier is released.
    Failed/cancelled attempts retain an explicitly ``incomplete`` quarantine
    directory; callers receive no usable snapshot result.
    """

    stage: Path | None = None
    coordination: SnapshotCoordination | None = None
    try:
        async with authoritative_markdown_snapshot_turn(
            buckets_dir,
            embedding_db_path=embedding_db_path,
            timeout_seconds=timeout_seconds,
            lock_root=lock_root,
        ) as coordination:
            root = Path(coordination.buckets_dir)
            stage = _new_staging_directory(root, staging_parent)
            _write_staging_marker(
                stage,
                _staging_payload(state="incomplete", manifest=None, coordination=coordination),
            )
            if fault_injector:
                fault_injector("staging.created")
            files: dict[str, bytes] = {}
            for source, member, identity in _enumerate_authoritative_markdown(root):
                if fault_injector:
                    fault_injector("copy.before")
                relative = PurePosixPath(member).relative_to("buckets")
                files[member] = _copy_source_file(source, stage / relative, identity)
                if fault_injector:
                    fault_injector("copy.after")
                await asyncio.sleep(0)
            folded: set[str] = set()
            md_total = sum(len(v) for v in files.values())
            for m in files:
                folded.add(m.casefold())
            remainder_files: dict[str, tuple[bytes, str]] = {}
            for source, member, member_type, identity in _enumerate_remainder_files(
                root, folded, prior_count=len(files), prior_bytes=md_total,
            ):
                if fault_injector:
                    fault_injector("copy.before")
                target = stage / PurePosixPath(member)
                remainder_files[member] = (
                    _copy_source_file(source, target, identity),
                    member_type,
                )
                if fault_injector:
                    fault_injector("copy.after")
                await asyncio.sleep(0)
            manifest = _build_manifest(
                files, created_at=created_at or _now_iso(),
                remainder_files=remainder_files or None,
            )
            if fault_injector:
                fault_injector("marker.complete.before")
            _write_staging_marker(
                stage,
                _staging_payload(state="complete", manifest=manifest, coordination=coordination),
            )
    except BaseException as exc:
        if stage is not None:
            try:
                _write_staging_marker(
                    stage,
                    _staging_payload(
                        state="incomplete",
                        manifest=None,
                        coordination=coordination,
                        failure=type(exc).__name__,
                    ),
                )
            except Exception:
                pass
        raise
    # This full rehash/reference validation is intentionally outside the lease.
    return await asyncio.to_thread(verify_authoritative_markdown_staging, stage)


def _read_verified_archive(archive_path: str | os.PathLike[str]) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Read every ZIP member only after structural validation, then validate v1/v2."""

    source = Path(archive_path)
    try:
        source_info = os.lstat(source)
    except OSError as exc:
        raise BackupArchiveError(f"cannot inspect archive: {source}") from exc
    if stat.S_ISLNK(source_info.st_mode) or _is_reparse_point(source_info) or not stat.S_ISREG(source_info.st_mode):
        raise BackupArchiveError("archive source is not a regular file")
    try:
        with zipfile.ZipFile(source, "r") as archive:
            infos = _validated_infos(archive, source_info.st_size)
            manifest_bytes = _read_member(archive, infos.pop(MANIFEST_NAME), MANIFEST_NAME)
            try:
                manifest = json.loads(manifest_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BackupArchiveError("manifest is not valid UTF-8 JSON") from exc
            files = {
                path: _read_member(archive, info, path)
                for path, info in sorted(infos.items())
            }
    except zipfile.BadZipFile as exc:
        raise BackupArchiveError("archive is not a valid ZIP file") from exc
    return _validate_manifest(manifest, files), files


def build_isolated_archive(
    snapshot_root: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Write a verified archive from an already-isolated bucket snapshot.

    This is intentionally not a live backup API.  The caller must supply a
    quiescent staging copy and a previously unused destination filename.
    """

    files = _collect_bucket_files(snapshot_root)
    root = Path(snapshot_root).resolve()
    folded: set[str] = set()
    for m in files:
        folded.add(m.casefold())
    md_total = sum(len(v) for v in files.values())
    remainder_files: dict[str, tuple[bytes, str]] = {}
    for source, member, member_type, identity in _enumerate_remainder_files(
        root, folded, prior_count=len(files), prior_bytes=md_total,
    ):
        data = _read_source_bytes(source, identity)
        remainder_files[member] = (data, member_type)
    manifest = _build_manifest(
        files,
        created_at=created_at or _now_iso(),
        remainder_files=remainder_files or None,
    )
    target = Path(destination).resolve()
    if target.exists():
        raise BackupArchiveError(f"archive destination already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".m04-backup-", suffix=".zip", dir=target.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise BackupArchiveError("manifest exceeds batch-1 size limit")
        with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for path, data in sorted(files.items()):
                archive.writestr(path, data)
            for path, (data, _t) in sorted(remainder_files.items()):
                archive.writestr(path, data)
            archive.writestr(MANIFEST_NAME, manifest_bytes)
        if temp_path.stat().st_size > MAX_ARCHIVE_BYTES:
            raise BackupArchiveError("archive exceeds batch-1 compressed size limit")
        os.replace(temp_path, target)
    except Exception:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise
    verify_isolated_archive(target)
    return manifest


def _validated_infos(archive: zipfile.ZipFile, archive_size: int) -> dict[str, zipfile.ZipInfo]:
    if archive_size > MAX_ARCHIVE_BYTES:
        raise BackupArchiveError("archive exceeds batch-1 compressed size limit")
    infos = archive.infolist()
    if len(infos) > MAX_MEMBERS:
        raise BackupArchiveError("archive has too many members")
    result: dict[str, zipfile.ZipInfo] = {}
    folded: set[str] = set()
    total = 0
    for info in infos:
        if info.is_dir():
            raise BackupArchiveError("directory members are not allowed")
        path = _normalize_member_path(info.filename)
        if path in result or path.casefold() in folded:
            raise BackupArchiveError(f"archive has a duplicate member path: {path}")
        folded.add(path.casefold())
        if info.flag_bits & 0x1:
            raise BackupArchiveError("encrypted archive members are not supported")
        mode = (info.external_attr >> 16) & 0xFFFF
        kind = stat.S_IFMT(mode)
        if kind and kind != stat.S_IFREG:
            raise BackupArchiveError("symlink or special archive members are not supported")
        if info.file_size < 0 or info.file_size > (MAX_MANIFEST_BYTES if path == MANIFEST_NAME else MAX_BUCKET_BYTES):
            raise BackupArchiveError(f"archive member exceeds size limit: {path}")
        if info.file_size and not info.compress_size:
            raise BackupArchiveError(f"archive member has invalid compression metadata: {path}")
        if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
            raise BackupArchiveError(f"archive member compression ratio is unsafe: {path}")
        total += info.file_size
        if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise BackupArchiveError("archive uncompressed size exceeds batch-1 limit")
        if path != MANIFEST_NAME:
            parts = PurePosixPath(path).parts
            is_bucket = (
                len(parts) >= 3 and parts[0] == "buckets"
                and parts[1] in BUCKET_DIRS and path.endswith(".md")
            )
            is_remainder = _is_valid_remainder_archive_path(path)
            if not is_bucket and not is_remainder:
                raise BackupArchiveError(f"archive contains a non-bucket/non-remainder unexpected member: {path}")
        result[path] = info
    if MANIFEST_NAME not in result:
        raise BackupArchiveError("archive has no manifest")
    return result


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, path: str) -> bytes:
    try:
        data = archive.read(info)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise BackupArchiveError(f"cannot read archive member: {path}") from exc
    if len(data) != info.file_size:
        raise BackupArchiveError(f"archive member length mismatch: {path}")
    return data


def _expected_type_for_path(path: str) -> str | None:
    parts = PurePosixPath(path).parts
    if len(parts) >= 3 and parts[0] == "buckets" and parts[1] in BUCKET_DIRS and path.endswith(".md"):
        return "bucket_markdown"
    if len(parts) >= 2 and parts[0] == ".remainders":
        if len(parts) == 2 and parts[1].endswith(".json"):
            return "remainder_sidecar"
        if len(parts) == 3 and parts[1] == "archive" and parts[2].endswith(".json"):
            return "remainder_archive"
        if len(parts) == 3 and parts[1] == "quarantine" and parts[2].endswith(".json.corrupt"):
            return "remainder_quarantine"
    return None


def _validate_manifest(manifest: Any, files: dict[str, bytes]) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise BackupArchiveError("manifest must be an object")
    if manifest.get("kind") != MANIFEST_KIND:
        raise BackupArchiveError("unsupported manifest kind")
    schema_ver = manifest.get("schema_version")
    if schema_ver not in (1, 2):
        raise BackupArchiveError(f"unsupported batch-1 manifest schema version: {schema_ver}")
    if not isinstance(manifest.get("created_at"), str) or not manifest["created_at"].strip():
        raise BackupArchiveError("manifest has no creation timestamp")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise BackupArchiveError("manifest files must be a list")
    expected: dict[str, dict[str, Any]] = {}
    markdown_files: dict[str, bytes] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise BackupArchiveError("manifest entry must be an object")
        path = _normalize_member_path(entry.get("path", ""))
        entry_type = entry.get("type")
        if path in expected:
            raise BackupArchiveError(f"manifest has duplicate entry: {path}")
        expected_type = _expected_type_for_path(path)
        if schema_ver == 1:
            if entry_type != "bucket_markdown":
                raise BackupArchiveError("v1 manifest must only contain bucket_markdown entries")
            if expected_type != "bucket_markdown":
                raise BackupArchiveError(f"v1 manifest has non-bucket path: {path}")
        else:
            if entry_type not in _ALL_MEMBER_TYPES:
                raise BackupArchiveError(f"manifest has unsupported entry type: {entry_type}")
            if expected_type is None:
                raise BackupArchiveError(f"manifest entry has unrecognised path pattern: {path}")
            if entry_type != expected_type:
                raise BackupArchiveError(f"manifest entry type {entry_type} does not match path {path} (expected {expected_type})")
        expected[path] = entry
    entry_paths = [_normalize_member_path(e.get("path", "")) for e in entries]
    if entry_paths != sorted(entry_paths):
        raise BackupArchiveError("manifest files are not sorted by path")
    if set(expected) != set(files):
        raise BackupArchiveError("manifest member set does not match archive")
    all_files_total = sum(len(v) for v in files.values())
    if manifest.get("file_count") != len(files) or manifest.get("total_bytes") != all_files_total:
        raise BackupArchiveError("manifest count or byte total does not match archive")
    for path, data in files.items():
        entry = expected[path]
        if entry.get("size") != len(data) or entry.get("sha256") != _sha256(data):
            raise BackupArchiveError(f"manifest hash mismatch: {path}")
    for path, data in files.items():
        if expected[path].get("type") == "bucket_markdown":
            markdown_files[path] = data
    if manifest.get("reference_closure") != _closure_from_files(markdown_files):
        raise BackupArchiveError("manifest reference closure does not match archive")
    policy = manifest.get("policy")
    if not isinstance(policy, dict) or any(policy.get(key) is not False for key in ("contains_embeddings_db", "contains_runtime_journals", "contains_secrets", "contains_raw_evidence")):
        raise BackupArchiveError("manifest violates batch-1 exclusion policy")
    return manifest


def verify_isolated_archive(archive_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Verify an archive in place without extracting it into a live vault."""

    manifest, _files = _read_verified_archive(archive_path)
    return manifest


def build_authoritative_root_manifest(
    root: str | os.PathLike[str], *, created_at: str
) -> dict[str, Any]:
    """Build the schema-1 manifest for a regular authoritative Markdown root.

    This is a read-only seam for the isolated restore-publication protocol.  It
    applies the same path, byte, frontmatter, and reference-closure validation as
    archive creation without writing an archive or touching runtime state.
    """

    canonical = _canonical_regular_directory(root, "authoritative Markdown root")
    allowed_top = BUCKET_DIRS | {REMAINDER_DIR}
    for child in canonical.iterdir():
        if child.name not in allowed_top:
            raise BackupArchiveError(
                f"authoritative Markdown root has unexpected top-level member: {child.name}"
            )
    md_files, rem_files = _read_authoritative_files_v2(canonical)
    return _build_manifest(md_files, created_at=created_at, remainder_files=rem_files or None)


def _new_restore_quarantine(restore_parent: str | os.PathLike[str]) -> Path:
    """Create an exact fresh restore child under an empty caller-owned parent."""

    parent = Path(restore_parent).expanduser()
    _assert_regular_directory(parent, "restore parent")
    parent = parent.resolve(strict=True)
    try:
        if any(parent.iterdir()):
            raise BackupArchiveError("restore parent must be an exact empty directory")
    except OSError as exc:
        raise BackupArchiveError("cannot inspect restore parent contents") from exc
    restore = Path(tempfile.mkdtemp(prefix=".m04-restore-", dir=parent))
    _assert_regular_directory(restore, "fresh restore quarantine")
    return restore


def _restore_payload(*, state: str, manifest: dict[str, Any] | None, failure: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": RESTORE_KIND,
        "schema_version": RESTORE_SCHEMA_VERSION,
        "state": state,
        "updated_at": _now_iso(),
        "sqlite_policy": "derived:not-captured:rebuild-required",
    }
    if manifest is not None:
        payload["backup_manifest"] = manifest
    if failure:
        payload["failure"] = failure[:200]
    return payload


def _write_restore_member(root: Path, member: str, data: bytes) -> None:
    relative = PurePosixPath(_normalize_member_path(member))
    target = (root / relative).resolve(strict=False)
    if not target.is_relative_to(root):
        raise BackupArchiveError("restore target escapes fresh quarantine")
    target.parent.mkdir(parents=True, exist_ok=True)
    _assert_regular_directory(target.parent, "restore target parent")
    try:
        with target.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise BackupArchiveError(f"cannot write restored archive member: {member}") from exc


def verify_isolated_roundtrip_restore(restored_root: str | os.PathLike[str]) -> dict[str, Any]:
    """Verify only a completed isolated restore child; never publish it."""

    root = _canonical_regular_directory(restored_root, "restored vault")
    payload = _read_marker(root, RESTORE_MARKER_NAME, "restored vault")
    if (
        payload.get("kind") != RESTORE_KIND
        or payload.get("schema_version") != RESTORE_SCHEMA_VERSION
        or payload.get("state") != "complete"
        or payload.get("sqlite_policy") != "derived:not-captured:rebuild-required"
    ):
        raise BackupArchiveError("restored vault is incomplete or uses an unsupported marker")
    manifest = payload.get("backup_manifest")
    schema_ver = manifest.get("schema_version") if isinstance(manifest, dict) else None
    allowed_top = {"buckets", RESTORE_MARKER_NAME}
    if schema_ver == 2:
        allowed_top.add(REMAINDER_DIR)
    for child in root.iterdir():
        if child.name not in allowed_top:
            raise BackupArchiveError(f"restored vault has unexpected top-level member: {child.name}")
    buckets_root = root / "buckets"
    _assert_regular_directory(buckets_root, "restored buckets root")
    if schema_ver == 2:
        md_files = _read_authoritative_files(buckets_root)
        folded: set[str] = set()
        for m in md_files:
            folded.add(m.casefold())
        rem_files: dict[str, tuple[bytes, str]] = {}
        for source, member, member_type, identity in _enumerate_remainder_files(
            root, folded, prior_count=len(md_files), prior_bytes=sum(len(v) for v in md_files.values()),
        ):
            data = _read_source_bytes(source, identity)
            rem_files[member] = (data, member_type)
        files: dict[str, bytes] = dict(md_files)
        for k, (rdata, _t) in rem_files.items():
            files[k] = rdata
        _validate_manifest(manifest, files)
    else:
        files = _read_authoritative_files(buckets_root)
        _validate_manifest(manifest, files)
    return {
        "restored_root": str(root),
        "backup_manifest": manifest,
        "sqlite_policy": payload["sqlite_policy"],
    }


def restore_verified_isolated_archive(
    archive_path: str | os.PathLike[str],
    restore_parent: str | os.PathLike[str],
    *,
    fault_injector: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Extract a verified archive into a fresh, marked quarantine child.

    ``restore_parent`` must exist, be regular, and be empty.  It is never
    replaced or deleted.  A fresh child is returned only after a full rehash and
    closure validation; errors leave at most that child's ``incomplete`` marker.
    """

    root: Path | None = None
    try:
        manifest, files = _read_verified_archive(archive_path)
        root = _new_restore_quarantine(restore_parent)
        _write_marker(root, RESTORE_MARKER_NAME, _restore_payload(state="incomplete", manifest=None))
        for entry in manifest["files"]:
            member = str(entry["path"])
            if fault_injector:
                fault_injector("restore.before_member")
            _write_restore_member(root, member, files[member])
            if fault_injector:
                fault_injector("restore.after_member")
        if fault_injector:
            fault_injector("restore.before_complete_marker")
        _write_marker(root, RESTORE_MARKER_NAME, _restore_payload(state="complete", manifest=manifest))
        return verify_isolated_roundtrip_restore(root)
    except BaseException as exc:
        if root is not None:
            try:
                _write_marker(
                    root,
                    RESTORE_MARKER_NAME,
                    _restore_payload(state="incomplete", manifest=None, failure=type(exc).__name__),
                )
            except Exception:
                pass
        raise


def verify_isolated_round_trip(
    staging_root: str | os.PathLike[str],
    archive_path: str | os.PathLike[str],
    restored_root: str | os.PathLike[str],
) -> dict[str, Any]:
    """Compare staging, archive, and isolated restore with no publish action."""

    staging = verify_authoritative_markdown_staging(staging_root)
    archive_manifest, archive_files = _read_verified_archive(archive_path)
    restored = verify_isolated_roundtrip_restore(restored_root)
    schema_ver = archive_manifest.get("schema_version", 1)
    if schema_ver == 2:
        s_md, s_rem = _read_authoritative_files_v2(Path(staging["staging_root"]))
        staging_files: dict[str, bytes] = dict(s_md)
        for k, (data, _t) in s_rem.items():
            staging_files[k] = data
        r_root = Path(restored["restored_root"])
        r_md = _read_authoritative_files(r_root / "buckets")
        r_folded: set[str] = set()
        for m in r_md:
            r_folded.add(m.casefold())
        r_rem: dict[str, tuple[bytes, str]] = {}
        for source, member, member_type, identity in _enumerate_remainder_files(
            r_root, r_folded, prior_count=len(r_md), prior_bytes=sum(len(v) for v in r_md.values()),
        ):
            data = _read_source_bytes(source, identity)
            r_rem[member] = (data, member_type)
        restored_files: dict[str, bytes] = dict(r_md)
        for k, (data, _t) in r_rem.items():
            restored_files[k] = data
    else:
        staging_files = _read_authoritative_files(Path(staging["staging_root"]))
        restored_files = _read_authoritative_files(Path(restored["restored_root"]) / "buckets")
    if staging_files != archive_files or archive_files != restored_files:
        raise BackupArchiveError("round-trip Markdown bytes or paths do not match staging")
    if not (
        staging["backup_manifest"]
        == archive_manifest
        == restored["backup_manifest"]
    ):
        raise BackupArchiveError("round-trip manifest or reference closure does not match staging")
    return {
        "staging": staging,
        "archive_manifest": archive_manifest,
        "restore": restored,
        "file_count": len(staging_files),
    }


def package_verified_authoritative_staging(
    staging_root: str | os.PathLike[str],
    archive_destination: str | os.PathLike[str],
) -> dict[str, Any]:
    """Package only a completed, fully verified M-04 staging directory."""

    staging = verify_authoritative_markdown_staging(staging_root)
    manifest = build_isolated_archive(
        staging["staging_root"],
        archive_destination,
        created_at=staging["backup_manifest"]["created_at"],
    )
    if manifest != staging["backup_manifest"]:
        raise BackupArchiveError("archive manifest drifted from verified staging manifest")
    return verify_isolated_archive(archive_destination)


async def perform_isolated_markdown_round_trip(
    source_vault: str | os.PathLike[str],
    staging_parent: str | os.PathLike[str],
    archive_destination: str | os.PathLike[str],
    restore_parent: str | os.PathLike[str],
    *,
    created_at: str | None = None,
    embedding_db_path: str | os.PathLike[str] | None = None,
    timeout_seconds: float = 30.0,
    lock_root: str | os.PathLike[str] | None = None,
    staging_fault_injector: Callable[[str], None] | None = None,
    restore_fault_injector: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run the isolated staging -> archive -> empty-vault proof, never publish."""

    staging = await create_authoritative_markdown_staging(
        source_vault,
        staging_parent,
        created_at=created_at,
        embedding_db_path=embedding_db_path,
        timeout_seconds=timeout_seconds,
        lock_root=lock_root,
        fault_injector=staging_fault_injector,
    )
    # Packaging starts only after the staging coordinator's full verify returned.
    verified_staging = await asyncio.to_thread(
        verify_authoritative_markdown_staging, staging["staging_root"]
    )
    archive_manifest = await asyncio.to_thread(
        package_verified_authoritative_staging,
        verified_staging["staging_root"],
        archive_destination,
    )
    restored = await asyncio.to_thread(
        restore_verified_isolated_archive,
        archive_destination,
        restore_parent,
        fault_injector=restore_fault_injector,
    )
    result = await asyncio.to_thread(
        verify_isolated_round_trip,
        verified_staging["staging_root"],
        archive_destination,
        restored["restored_root"],
    )
    if result["archive_manifest"] != archive_manifest:
        raise BackupArchiveError("archive builder returned a manifest that did not verify")
    return result


def extract_verified_isolated_archive(
    archive_path: str | os.PathLike[str], destination: str | os.PathLike[str]
) -> dict[str, Any]:
    """Extract a verified archive into an empty, caller-owned staging directory."""

    manifest = verify_isolated_archive(archive_path)
    root = Path(destination).resolve()
    if root.exists() and any(root.iterdir()):
        raise BackupArchiveError("extraction destination must be empty")
    root.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            for entry in manifest["files"]:
                path = str(entry["path"])
                target = (root / PurePosixPath(path)).resolve()
                if not target.is_relative_to(root):
                    raise BackupArchiveError("extraction target escapes destination")
                target.parent.mkdir(parents=True, exist_ok=True)
                data = _read_member(archive, archive.getinfo(path), path)
                with target.open("xb") as handle:
                    handle.write(data)
                extracted.append(target)
    except Exception:
        for path in reversed(extracted):
            try:
                path.unlink()
            except OSError:
                pass
        raise
    return manifest


__all__ = [
    "BackupArchiveError",
    "MANIFEST_KIND",
    "MANIFEST_SCHEMA_VERSION",
    "STAGING_MARKER_NAME",
    "STAGING_KIND",
    "STAGING_SCHEMA_VERSION",
    "RESTORE_MARKER_NAME",
    "RESTORE_KIND",
    "RESTORE_SCHEMA_VERSION",
    "build_isolated_archive",
    "create_authoritative_markdown_staging",
    "extract_verified_isolated_archive",
    "package_verified_authoritative_staging",
    "perform_isolated_markdown_round_trip",
    "restore_verified_isolated_archive",
    "verify_authoritative_markdown_staging",
    "verify_isolated_archive",
    "verify_isolated_round_trip",
    "verify_isolated_roundtrip_restore",
]
