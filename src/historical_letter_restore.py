"""Fail-closed audit and explicit restoration of historical Letter files."""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime
from typing import Any

import frontmatter

from snapshot_barrier import markdown_writer_turn
from utils import safe_path


logger = logging.getLogger("ombre_brain.historical_letter_restore")

_TERMINAL_VALUE_FIELDS = (
    "deleted_at", "tombstoned_at", "erasure_mode", "erased_at",
)
_TERMINAL_BOOL_FIELDS = ("tombstone", "deleted", "physical_erasure")
WINDOWS_SAFE_COMMIT = os.name == "nt"


class RestoreValidationError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def path_is_within(file_path: str, directory: str) -> bool:
    candidate = os.path.normcase(os.path.realpath(file_path))
    root = os.path.normcase(os.path.realpath(directory))
    try:
        return os.path.commonpath((candidate, root)) == root
    except ValueError:
        return False


def physical_bucket_sources(manager, bucket_id: str) -> tuple[list[tuple[str, Any]], bool]:
    sources: list[tuple[str, Any]] = []
    unreadable = False
    for _root, filename, file_path in manager._iter_md_files(
        list(manager._active_dirs) + [manager.archive_dir]
    ):
        stem = filename[:-3]
        filename_matches = stem == bucket_id or stem.endswith(f"_{bucket_id}")
        try:
            post = frontmatter.load(file_path)
        except Exception:
            if filename_matches:
                unreadable = True
            continue
        stored_id = str(post.get("id") or (stem if filename_matches else "")).strip()
        if stored_id == bucket_id:
            sources.append((file_path, post))
    return sources, unreadable


def physical_bucket_inventory(manager) -> tuple[dict[str, list[tuple[str, Any]]], set[str]]:
    grouped: dict[str, list[tuple[str, Any]]] = {}
    unreadable: set[str] = set()
    for _root, filename, file_path in manager._iter_md_files(
        list(manager._active_dirs) + [manager.archive_dir]
    ):
        stem = filename[:-3]
        try:
            post = frontmatter.load(file_path)
        except Exception:
            unreadable.add(stem.rsplit("_", 1)[-1])
            continue
        bucket_id = str(post.get("id") or stem).strip()
        if bucket_id:
            grouped.setdefault(bucket_id, []).append((file_path, post))
    return grouped, unreadable


def has_strong_letter_marker(post: Any) -> bool:
    if str(post.get("source_tool") or "").strip().casefold() == "letter":
        return True
    tags = post.get("tags") or []
    if isinstance(tags, str):
        tags = [part.strip() for part in tags.split(",")]
    return isinstance(tags, (list, tuple, set)) and any(
        str(tag).strip().casefold() == "__letter__" for tag in tags
    )


def has_ambiguous_letter_marker(post: Any) -> bool:
    domains = post.get("domain") or []
    if isinstance(domains, str):
        domains = [domains]
    return isinstance(domains, (list, tuple, set)) and any(
        str(domain).strip().casefold() == "letter" for domain in domains
    )


def _strict_bool(value: Any) -> bool | None:
    if value is None:
        return False
    if value is True or (type(value) is int and value == 1):
        return True
    if value is False or (type(value) is int and value == 0):
        return False
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1"}:
            return True
        if normalized in {"false", "0", ""}:
            return False
    return None


def archived_letter_rejection(post: Any) -> str:
    for field in _TERMINAL_VALUE_FIELDS:
        value = post.get(field)
        if value is None or value == "":
            continue
        if not isinstance(value, (str, date, datetime)):
            return "invalid_terminal_marker"
        return "terminal_state"
    for field in _TERMINAL_BOOL_FIELDS:
        parsed = _strict_bool(post.get(field))
        if parsed is True:
            return "terminal_state"
        if parsed is None:
            return "invalid_terminal_marker"
    if str(post.get("status") or "").strip().casefold() in {
        "deleted", "tombstone", "erased",
    }:
        return "terminal_state"
    for field in ("pinned", "protected", "anchor"):
        parsed = _strict_bool(post.get(field))
        if parsed is True:
            return "protected_state"
        if parsed is None:
            return "invalid_terminal_marker"
    if not has_strong_letter_marker(post):
        return "ambiguous_letter_marker" if has_ambiguous_letter_marker(post) else "not_letter"
    from tools.plan.core import letter_lock_state

    metadata = dict(post.metadata) if hasattr(post, "metadata") else dict(post)
    if letter_lock_state({"metadata": metadata}, None).get("invalid"):
        return "invalid_lock_state"
    return ""


def rewrite_archived_type_bytes(raw: bytes) -> bytes:
    opening = re.match(rb"\A---[ \t]*\r?\n", raw)
    if opening is None:
        raise ValueError("frontmatter opening delimiter is required")
    closing = re.search(rb"(?m)^---[ \t]*\r?$", raw[opening.end() :])
    if closing is None:
        raise ValueError("frontmatter closing delimiter is required")
    header_start = opening.end()
    header_end = header_start + closing.start()
    header = raw[header_start:header_end]
    pattern = re.compile(
        rb"(?m)^([ \t]*type[ \t]*:[ \t]*"
        rb"(?:(?:!!str|!<tag:yaml\.org,2002:str>)[ \t]+)?)"
        rb"(['\"]?)archived\2([ \t]*(?:\#.*)?\r?)$"
    )
    matches = list(pattern.finditer(header))
    if len(matches) != 1:
        raise ValueError("frontmatter archived type scalar must occur exactly once")
    match = matches[0]
    rewritten_header = (
        header[: match.start()] + match.group(1) + match.group(2) + b"letter"
        + match.group(2) + match.group(3) + header[match.end() :]
    )
    rewritten = raw[:header_start] + rewritten_header + raw[header_end:]
    parsed = frontmatter.loads(rewritten.decode("utf-8"))
    if str(parsed.get("type") or "").strip().casefold() != "letter":
        raise ValueError("rewritten frontmatter type is not letter")
    return rewritten


def require_safe_commit(source: str, target_root: str) -> None:
    if not WINDOWS_SAFE_COMMIT:
        raise OSError("Historical Letter safe commit is Windows-only")
    from windows_safe_rename import require_safe_rename_capability

    require_safe_rename_capability(source, target_root)


def _ensure_history_directory(letter_dir: str) -> str:
    history_dir = os.path.join(letter_dir, "history")
    if not WINDOWS_SAFE_COMMIT:
        raise OSError("Historical Letter safe commit is Windows-only")
    from windows_safe_rename import safe_ensure_directory

    safe_ensure_directory(letter_dir, "history")
    return history_dir


def _validated_replacement(snapshot, bucket_id: str) -> tuple[Any, bytes]:
    try:
        post = frontmatter.loads(snapshot.raw.decode("utf-8"))
    except Exception as exc:
        raise RestoreValidationError("unreadable_source") from exc
    if str(post.get("id") or "").strip() != bucket_id:
        raise RestoreValidationError("source_changed")
    if str(post.get("type") or "").strip().casefold() != "archived":
        raise RestoreValidationError("invalid_archived_type")
    rejection = archived_letter_rejection(post)
    if rejection:
        raise RestoreValidationError(rejection)
    try:
        rewritten = rewrite_archived_type_bytes(snapshot.raw)
        restored = frontmatter.loads(rewritten.decode("utf-8"))
    except Exception as exc:
        raise RestoreValidationError("invalid_archived_type") from exc
    if str(restored.get("id") or "").strip() != bucket_id:
        raise RestoreValidationError("source_changed")
    if str(restored.get("type") or "").strip().casefold() != "letter":
        raise RestoreValidationError("invalid_archived_type")
    if restored.content != post.content:
        raise RestoreValidationError("invalid_archived_type")
    return restored, rewritten


def _secure_snapshot(path: str):
    if not WINDOWS_SAFE_COMMIT:
        raise OSError("Historical Letter safe commit is Windows-only")
    from windows_safe_rename import safe_read_source_snapshot

    return safe_read_source_snapshot(path)


def commit_recovered_letter(
    source: str,
    target: str,
    bucket_id: str,
    expected_revision: str | None,
) -> tuple[str, Any]:
    if os.path.normcase(os.path.abspath(source)) == os.path.normcase(os.path.abspath(target)):
        raise ValueError("source and recovery target must differ")
    if os.path.lexists(target):
        raise FileExistsError(f"recovery target exists: {target}")
    target_parent = os.path.dirname(target)
    if not WINDOWS_SAFE_COMMIT:
        raise OSError("Historical Letter safe commit is Windows-only")
    from windows_safe_rename import (
        PostCommitRewriteError,
        SourceChangedError,
        safe_transform_rename_no_replace,
    )

    restored_post = None

    def transform(snapshot):
        nonlocal restored_post
        restored_post, rewritten = _validated_replacement(snapshot, bucket_id)
        return rewritten

    try:
        safe_transform_rename_no_replace(
            source,
            target_parent,
            os.path.basename(target),
            transform,
            expected_revision=expected_revision,
        )
    except SourceChangedError as exc:
        raise RestoreValidationError("revision_conflict") from exc
    except PostCommitRewriteError as exc:
        raise RuntimeError("ambiguous_state") from exc
    assert restored_post is not None
    return target, restored_post


def rewrite_crash_intermediate(
    source: str, bucket_id: str, expected_revision: str | None
) -> Any:
    if not WINDOWS_SAFE_COMMIT:
        raise OSError("Historical Letter safe commit is Windows-only")
    from windows_safe_rename import (
        PostCommitRewriteError,
        SourceChangedError,
        safe_transform_existing_file,
    )

    restored_post = None

    def transform(snapshot):
        nonlocal restored_post
        restored_post, rewritten = _validated_replacement(snapshot, bucket_id)
        return rewritten

    try:
        safe_transform_existing_file(
            os.path.dirname(source),
            os.path.basename(source),
            transform,
            expected_revision=expected_revision,
        )
    except SourceChangedError as exc:
        raise RestoreValidationError("revision_conflict") from exc
    except PostCommitRewriteError as exc:
        raise RuntimeError("ambiguous_state") from exc
    assert restored_post is not None
    return restored_post


async def audit_archived_letters(manager) -> dict:
    grouped, unreadable_ids = physical_bucket_inventory(manager)
    candidates: list[str] = []
    revisions: dict[str, str] = {}
    exclusions: list[dict[str, str]] = []
    for bucket_id, rows in grouped.items():
        if bucket_id in unreadable_ids:
            exclusions.append({"id": bucket_id, "reason": "unreadable_source"})
            continue
        relevant: list[tuple[str, Any]] = []
        archived_signal = False
        for path, post in rows:
            metadata = dict(post.metadata)
            if not (has_strong_letter_marker(metadata) or has_ambiguous_letter_marker(metadata)):
                continue
            relevant.append((path, post))
            if (
                str(metadata.get("type") or "").strip().casefold() == "archived"
                or path_is_within(path, manager.archive_dir)
            ):
                archived_signal = True
        if not relevant or not archived_signal:
            continue
        if len(rows) != 1:
            exclusions.append({"id": bucket_id, "reason": "duplicate_source"})
            continue
        path, post = relevant[0]
        metadata = dict(post.metadata)
        history_dir = os.path.join(manager.letter_dir, "history")
        in_archive = path_is_within(path, manager.archive_dir)
        in_history = (
            os.path.normcase(os.path.realpath(os.path.dirname(path)))
            == os.path.normcase(os.path.realpath(history_dir))
        )
        if not (in_archive or in_history):
            reason = "not_archived"
        elif str(metadata.get("type") or "").strip().casefold() != "archived":
            reason = "invalid_archived_type"
        else:
            reason = archived_letter_rejection(metadata)
        snapshot = None
        if not reason:
            try:
                snapshot = _secure_snapshot(path)
                _validated_replacement(snapshot, bucket_id)
            except RestoreValidationError as exc:
                reason = exc.reason
            except (OSError, ValueError):
                reason = "unsupported_safe_commit"
        if reason:
            exclusions.append({"id": bucket_id, "reason": reason})
        else:
            candidates.append(bucket_id)
            assert snapshot is not None
            revisions[bucket_id] = snapshot.revision
    for bucket_id in sorted(unreadable_ids):
        if bucket_id not in grouped:
            exclusions.append({"id": bucket_id, "reason": "unreadable_source"})
    candidates.sort()
    exclusions.sort(key=lambda item: item["id"])
    return {
        "candidate_count": len(candidates),
        "candidate_ids": candidates,
        "candidate_revisions": revisions,
        "excluded_count": len(exclusions),
        "exclusions": exclusions,
    }


async def recover_archived_letter(
    manager, bucket_id: str, expected_revision: str | None = None
) -> dict:
    normalized_id = str(bucket_id or "").strip()
    if not normalized_id:
        return {"ok": False, "id": "", "reason": "invalid_id"}
    committed: tuple[Any, str] | None = None
    derived_state = "applied"
    async with markdown_writer_turn(manager.base_dir):
        async with manager._bucket_turn(normalized_id):
            sources, unreadable = physical_bucket_sources(manager, normalized_id)
            if unreadable:
                return {"ok": False, "id": normalized_id, "reason": "unreadable_source"}
            if not sources:
                return {"ok": False, "id": normalized_id, "reason": "not_found"}
            if len(sources) != 1:
                return {"ok": False, "id": normalized_id, "reason": "duplicate_source"}
            source, _observed_post = sources[0]
            from windows_safe_rename import UnsafePathError

            try:
                initial = _secure_snapshot(source)
                post = frontmatter.loads(initial.raw.decode("utf-8"))
            except UnsafePathError:
                reason = (
                    "revision_conflict"
                    if expected_revision is not None
                    else "unsupported_safe_commit"
                )
                return {"ok": False, "id": normalized_id, "reason": reason}
            except (OSError, ValueError, UnicodeDecodeError):
                return {"ok": False, "id": normalized_id, "reason": "unsupported_safe_commit"}
            if str(post.get("id") or "").strip() != normalized_id:
                return {"ok": False, "id": normalized_id, "reason": "source_changed"}
            if (
                expected_revision is not None
                and initial.revision != str(expected_revision).casefold()
            ):
                return {"ok": False, "id": normalized_id, "reason": "revision_conflict"}
            pinned_revision = initial.revision
            history_dir = os.path.join(manager.letter_dir, "history")
            in_archive = path_is_within(source, manager.archive_dir)
            in_history = (
                os.path.normcase(os.path.realpath(os.path.dirname(source)))
                == os.path.normcase(os.path.realpath(history_dir))
            )
            source_type = str(post.get("type") or "").strip().casefold()
            if in_history and source_type == "letter":
                return {"ok": True, "id": normalized_id, "reason": "already_restored"}
            if not (in_archive or in_history):
                return {"ok": False, "id": normalized_id, "reason": "not_archived"}
            if source_type != "archived":
                return {"ok": False, "id": normalized_id, "reason": "invalid_archived_type"}
            rejection = archived_letter_rejection(dict(post.metadata))
            if rejection:
                return {"ok": False, "id": normalized_id, "reason": rejection}
            try:
                require_safe_commit(source, manager.letter_dir)
            except (OSError, ValueError):
                return {"ok": False, "id": normalized_id, "reason": "unsupported_safe_commit"}

            try:
                if in_history:
                    post = rewrite_crash_intermediate(
                        source, normalized_id, pinned_revision
                    )
                    target = source
                else:
                    history_dir = _ensure_history_directory(manager.letter_dir)
                    target = safe_path(history_dir, os.path.basename(source))
                    if os.path.lexists(target):
                        return {
                            "ok": False, "id": normalized_id,
                            "reason": "target_collision",
                        }
                    target, post = commit_recovered_letter(
                        source, target, normalized_id, pinned_revision
                    )
            except RestoreValidationError as exc:
                return {"ok": False, "id": normalized_id, "reason": exc.reason}
            except RuntimeError as exc:
                reason = "ambiguous_state" if str(exc) == "ambiguous_state" else "commit_failed"
                return {"ok": False, "id": normalized_id, "reason": reason}
            except FileExistsError:
                return {"ok": False, "id": normalized_id, "reason": "target_collision"}
            except (OSError, ValueError) as exc:
                logger.warning(
                    "historical Letter commit failed for %s: %s",
                    normalized_id, exc,
                )
                return {"ok": False, "id": normalized_id, "reason": "commit_failed"}

            post_sources, post_unreadable = physical_bucket_sources(
                manager, normalized_id
            )
            if (
                post_unreadable
                or len(post_sources) != 1
                or os.path.normcase(os.path.abspath(post_sources[0][0]))
                != os.path.normcase(os.path.abspath(target))
            ):
                return {
                    "ok": False, "id": normalized_id,
                    "reason": "ambiguous_state",
                }
            manager._invalidate_bm25()
            committed = (post, target)

    assert committed is not None
    post, target = committed
    manager._record_v3_bucket_event(
        "restore", normalized_id, "letter", post.content or "", dict(post.metadata)
    )
    logger.info("Recovered archived Letter: %s -> %s", normalized_id, target)
    return {
        "ok": True, "id": normalized_id, "reason": "restored",
        "derived_state": derived_state,
    }
