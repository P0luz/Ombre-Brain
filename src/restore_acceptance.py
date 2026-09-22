"""Read-only M-04 production acceptance preflight; never publishes or recovers."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
from typing import Any

from restore_publication import (
    TRANSACTION_ROOT_NAME,
    inspect_restore_publications_read_only,
)


class RestoreAcceptanceError(RuntimeError):
    pass


def _is_reparse(info: os.stat_result) -> bool:
    return bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _regular_directory(value: str | os.PathLike[str], label: str) -> Path:
    path = Path(value).expanduser()
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise RestoreAcceptanceError(f"{label} cannot be inspected") from exc
    if stat.S_ISLNK(info.st_mode) or _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
        raise RestoreAcceptanceError(f"{label} must be a regular directory")
    return path.resolve(strict=True)


def _tree_size(root: Path) -> int:
    total = 0
    for directory, names, files in os.walk(root, followlinks=False):
        current = Path(directory)
        info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
            raise RestoreAcceptanceError("live vault contains a reparse directory")
        for name in names + files:
            path = current / name
            item = os.lstat(path)
            if stat.S_ISLNK(item.st_mode) or _is_reparse(item):
                raise RestoreAcceptanceError("live vault contains a reparse member")
            if stat.S_ISREG(item.st_mode):
                total += item.st_size
            elif not stat.S_ISDIR(item.st_mode):
                raise RestoreAcceptanceError("live vault contains a special member")
    return total


def build_read_only_acceptance(
    *,
    live_vault: str | os.PathLike[str],
    transport: str,
    admission_status: Any,
    background_status: Any,
    quiescence_status: Any,
    minimum_free_multiplier: float = 3.0,
) -> dict[str, Any]:
    """Return evidence and blockers without creating a directory or lock file."""

    if minimum_free_multiplier < 2.0:
        raise ValueError("restore acceptance free-space multiplier must be at least 2")
    live = _regular_directory(live_vault, "live vault")
    transaction_root = live.parent / TRANSACTION_ROOT_NAME
    if transaction_root.exists() or transaction_root.is_symlink():
        journals = inspect_restore_publications_read_only(transaction_root)
    else:
        journals = {
            "transaction_root": str(transaction_root),
            "total": 0,
            "active": 0,
            "terminal": 0,
            "transactions": (),
        }
    size = _tree_size(live)
    free = shutil.disk_usage(live.parent).free
    required = max(64 * 1024 * 1024, int(size * minimum_free_multiplier))
    blockers: list[str] = []
    warnings: list[str] = []
    if str(transport) != "streamable-http":
        blockers.append("runtime restore maintenance is frozen for streamable-http only")
    if getattr(admission_status, "state", "") != "OPEN" or getattr(
        admission_status, "active", -1
    ) != 0:
        blockers.append("runtime admission is not idle/open")
    if getattr(background_status, "state", "") != "ACTIVE":
        blockers.append("background registry is not active")
    if getattr(quiescence_status, "phase", "") != "IDLE":
        blockers.append("service quiescence adapter is not idle")
    if journals["active"]:
        blockers.append("active M-04 publication journal requires startup recovery")
    if free < required:
        blockers.append("insufficient same-volume free space for candidate and rollback")
    if os.name == "nt":
        warnings.append(
            "Windows directory-rename power-loss durability is not claimed; "
            "startup shape recovery is process-crash-safe and ambiguous shapes fail closed"
        )
    return {
        "verdict": "GO" if not blockers else "NO-GO",
        "read_only": True,
        "live_vault": str(live),
        "live_device": int(os.lstat(live).st_dev),
        "vault_bytes": size,
        "free_bytes": free,
        "required_free_bytes": required,
        "journals": journals,
        "blockers": tuple(blockers),
        "warnings": tuple(warnings),
        "next_authority": "production-read-only-acceptance-only",
    }
