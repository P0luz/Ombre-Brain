"""M-04 isolated restore publication transaction.

This module is deliberately not connected to the server or a production vault.
It publishes only an already verified M-04 restore whose ``buckets`` directory
is on the same volume as an existing live Markdown vault.  The caller must
provide and hold a service-quiescence context for publication or recovery.

The old vault is retained inside the transaction directory.  No path is ever
deleted automatically; cleanup and SQLite rebuilding remain separate,
explicitly unauthorized operations.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid
from typing import Any, Callable

try:
    from backup_archive import (
        build_authoritative_root_manifest,
        verify_isolated_roundtrip_restore,
    )
except ImportError:  # pragma: no cover - package import
    from .backup_archive import (
        build_authoritative_root_manifest,
        verify_isolated_roundtrip_restore,
    )


JOURNAL_KIND = "ombre-brain-m04-restore-publication"
JOURNAL_SCHEMA_VERSION = 1
JOURNAL_NAME = "publication.json"
STATE_PREPARED = "PREPARED"
STATE_OLD_STAGED = "OLD_STAGED"
STATE_NEW_PUBLISHED = "NEW_PUBLISHED"
STATE_MARKDOWN_PUBLISHED = "MARKDOWN_PUBLISHED"
STATE_DERIVED_REBUILT = "DERIVED_REBUILT"
STATE_COMMITTED = "COMMITTED"
STATE_ROLLED_BACK = "ROLLED_BACK"
_ACTIVE_STATES = {
    STATE_PREPARED,
    STATE_OLD_STAGED,
    STATE_NEW_PUBLISHED,
    STATE_MARKDOWN_PUBLISHED,
    STATE_DERIVED_REBUILT,
}
_TERMINAL_STATES = {STATE_COMMITTED, STATE_ROLLED_BACK}
_TXID_RE = re.compile(r"^[0-9a-f]{32}$")
TRANSACTION_ROOT_NAME = ".m04-restore-transactions"

FaultInjector = Callable[[str], None]
QuiescenceFactory = Callable[[], AbstractContextManager[Any]]
DerivedRebuilder = Callable[[Path, dict[str, Any]], dict[str, Any]]


class RestorePublicationError(RuntimeError):
    """Fail-closed publication or recovery error."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
        raise RestorePublicationError(f"{label} cannot be inspected") from exc
    if stat.S_ISLNK(info.st_mode) or _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
        raise RestorePublicationError(f"{label} must be a regular directory")
    return path.resolve(strict=True)


def _directory_identity(path: Path) -> tuple[int, int, int, int, int]:
    info = os.lstat(path)
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)


def _assert_identity(path: Path, expected: tuple[int, int, int, int, int], label: str) -> None:
    try:
        actual = _directory_identity(path)
    except OSError as exc:
        raise RestorePublicationError(f"{label} disappeared before publication") from exc
    if actual != expected or stat.S_ISLNK(actual[2]) or not stat.S_ISDIR(actual[2]):
        raise RestorePublicationError(f"{label} changed before publication")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(str(path), flags)
    except OSError:
        # Windows does not reliably permit opening directories for fsync.  Each
        # journal file is still flushed; directory rename durability requires an
        # operator-approved Windows integration batch.
        if os.name == "nt":
            return
        raise
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_journal(txdir: Path, payload: dict[str, Any]) -> None:
    payload = dict(payload)
    payload["updated_at"] = _now_iso()
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    temp = txdir / f".{JOURNAL_NAME}.tmp"
    target = txdir / JOURNAL_NAME
    try:
        with temp.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
        _fsync_directory(txdir)
    except OSError as exc:
        try:
            temp.unlink()
        except OSError:
            pass
        raise RestorePublicationError("cannot durably write restore publication journal") from exc


def _read_journal(txdir: Path) -> dict[str, Any]:
    target = txdir / JOURNAL_NAME
    try:
        info = os.lstat(target)
        if stat.S_ISLNK(info.st_mode) or _is_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_size > 8 * 1024 * 1024:
            raise RestorePublicationError("restore publication journal is unsafe")
        payload = json.loads(target.read_text(encoding="utf-8"))
    except RestorePublicationError:
        raise
    except Exception as exc:
        raise RestorePublicationError("restore publication journal is unreadable") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("kind") != JOURNAL_KIND
        or payload.get("schema_version") != JOURNAL_SCHEMA_VERSION
        or payload.get("state") not in _ACTIVE_STATES | _TERMINAL_STATES
        or not _TXID_RE.fullmatch(str(payload.get("txid", "")))
    ):
        raise RestorePublicationError("restore publication journal is invalid")
    manifest = payload.get("backup_manifest")
    candidate_path = payload.get("candidate_path")
    rollback_path = payload.get("rollback_path")
    if (
        not isinstance(manifest, dict)
        or not isinstance(candidate_path, str)
        or not Path(candidate_path).is_absolute()
        or not isinstance(rollback_path, str)
        or not Path(rollback_path).is_absolute()
        or payload.get("sqlite_policy") != "derived:not-captured:rebuild-required"
        or payload.get("cleanup_policy") != "manual:no-automatic-delete"
    ):
        raise RestorePublicationError("restore publication journal contract is incomplete")
    _safe_live_name(payload.get("live_name"))
    if payload["state"] in {STATE_DERIVED_REBUILT, STATE_COMMITTED}:
        _validated_rebuild_receipt(payload.get("derived_rebuild"), manifest)
    return payload


def _inject(injector: FaultInjector | None, point: str) -> None:
    if injector is not None:
        injector(point)


def _manifest_without_time(manifest: dict[str, Any]) -> dict[str, Any]:
    out = dict(manifest)
    out.pop("created_at", None)
    return out


def _manifest_digest(manifest: dict[str, Any]) -> str:
    raw = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _validated_rebuild_receipt(
    receipt: object, manifest: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(receipt, dict):
        raise RestorePublicationError("derived rebuild did not return a receipt")
    expected = _manifest_digest(manifest)
    if (
        receipt.get("status") != "verified"
        or receipt.get("sqlite_policy") != "rebuilt-from-authoritative-markdown"
        or receipt.get("manifest_sha256") != expected
    ):
        raise RestorePublicationError("derived rebuild receipt is incomplete or mismatched")
    try:
        raw = json.dumps(receipt, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RestorePublicationError("derived rebuild receipt is not JSON-safe") from exc
    if len(raw.encode("utf-8")) > 64 * 1024:
        raise RestorePublicationError("derived rebuild receipt exceeds size limit")
    return dict(receipt)


def _verify_root(root: Path, expected: dict[str, Any]) -> None:
    actual = build_authoritative_root_manifest(root, created_at=str(expected["created_at"]))
    if _manifest_without_time(actual) != _manifest_without_time(expected):
        raise RestorePublicationError("published Markdown root does not match restore manifest")


def _new_transaction(transaction_root: Path, txid: str) -> Path:
    if not _TXID_RE.fullmatch(txid):
        raise RestorePublicationError("invalid restore publication transaction id")
    txdir = transaction_root / txid
    try:
        txdir.mkdir(mode=0o700)
    except OSError as exc:
        raise RestorePublicationError("restore publication transaction already exists or cannot be created") from exc
    return _regular_directory(txdir, "restore publication transaction")


def _safe_live_name(value: object) -> str:
    name = str(value or "")
    if not name or name in {".", ".."} or Path(name).name != name or "/" in name or "\\" in name:
        raise RestorePublicationError("restore publication live name is unsafe")
    return name


def publish_verified_restore(
    restored_root: str | os.PathLike[str],
    live_vault: str | os.PathLike[str],
    transaction_root: str | os.PathLike[str],
    *,
    quiescence: QuiescenceFactory,
    rebuild_derived: DerivedRebuilder,
    txid: str | None = None,
    fault_injector: FaultInjector | None = None,
) -> dict[str, Any]:
    """Publish an isolated restored ``buckets`` root with rollback retained."""

    restored = verify_isolated_roundtrip_restore(restored_root)
    restored_dir = _regular_directory(restored["restored_root"], "verified restore root")
    candidate = _regular_directory(restored_dir / "buckets", "verified restore buckets")
    live = _regular_directory(live_vault, "live Markdown vault")
    txroot = _regular_directory(transaction_root, "restore transaction root")
    if txroot.name != TRANSACTION_ROOT_NAME or txroot.parent != live.parent:
        raise RestorePublicationError(
            f"restore transaction root must be live sibling {TRANSACTION_ROOT_NAME}"
        )
    if live == candidate or live.is_relative_to(candidate) or candidate.is_relative_to(live):
        raise RestorePublicationError("live and restored Markdown roots must be disjoint")
    devices = {os.lstat(path).st_dev for path in (candidate, live, txroot)}
    if len(devices) != 1:
        raise RestorePublicationError("restore publication requires candidate, live, and journal on one volume")

    manifest = restored["backup_manifest"]
    _verify_root(candidate, manifest)
    candidate_identity = _directory_identity(candidate)
    live_identity = _directory_identity(live)
    txid = txid or uuid.uuid4().hex
    txdir = _new_transaction(txroot, txid)
    rollback = txdir / "rollback-vault"
    payload: dict[str, Any] = {
        "kind": JOURNAL_KIND,
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "txid": txid,
        "state": STATE_PREPARED,
        "live_name": _safe_live_name(live.name),
        "candidate_path": str(candidate),
        "rollback_path": str(rollback),
        "backup_manifest": manifest,
        "sqlite_policy": "derived:not-captured:rebuild-required",
        "cleanup_policy": "manual:no-automatic-delete",
    }
    _write_journal(txdir, payload)
    _inject(fault_injector, "publication.prepared")

    with quiescence():
        _assert_identity(live, live_identity, "live Markdown vault")
        _assert_identity(candidate, candidate_identity, "restore candidate")
        _verify_root(candidate, manifest)
        _inject(fault_injector, "publication.before_old_staged")
        try:
            os.replace(live, rollback)
            _fsync_directory(live.parent)
        except OSError as exc:
            raise RestorePublicationError("cannot stage old live Markdown vault") from exc
        _inject(fault_injector, "publication.old_renamed")
        payload["state"] = STATE_OLD_STAGED
        _write_journal(txdir, payload)
        _inject(fault_injector, "publication.old_staged")
        try:
            os.replace(candidate, live)
            _fsync_directory(live.parent)
        except OSError as exc:
            raise RestorePublicationError("cannot publish restored Markdown vault") from exc
        _inject(fault_injector, "publication.new_renamed")
        payload["state"] = STATE_NEW_PUBLISHED
        _write_journal(txdir, payload)
        _inject(fault_injector, "publication.new_published")
        _verify_root(live, manifest)
        payload["state"] = STATE_MARKDOWN_PUBLISHED
        _write_journal(txdir, payload)
        _inject(fault_injector, "publication.markdown_published")
        receipt = _validated_rebuild_receipt(rebuild_derived(live, manifest), manifest)
        payload["derived_rebuild"] = receipt
        payload["state"] = STATE_DERIVED_REBUILT
        _write_journal(txdir, payload)
        _inject(fault_injector, "publication.derived_rebuilt")
        payload["state"] = STATE_COMMITTED
        _write_journal(txdir, payload)
        _inject(fault_injector, "publication.committed")
    return {"txid": txid, "state": payload["state"], "live_vault": str(live), "rollback_vault": str(rollback), "journal": str(txdir / JOURNAL_NAME)}


def _load_transaction(transaction_dir: str | os.PathLike[str]) -> tuple[Path, dict[str, Any]]:
    txdir = _regular_directory(transaction_dir, "restore publication transaction")
    payload = _read_journal(txdir)
    if txdir.name != payload["txid"] or txdir.parent.name != TRANSACTION_ROOT_NAME:
        raise RestorePublicationError("restore publication journal is outside its fixed transaction root")
    return txdir, payload


def _recover_loaded_transaction(
    txdir: Path,
    payload: dict[str, Any],
    *,
    fault_injector: FaultInjector | None = None,
) -> dict[str, Any]:
    state = payload["state"]
    if state in _TERMINAL_STATES:
        return payload
    live = txdir.parent.parent / _safe_live_name(payload.get("live_name"))
    rollback = txdir / "rollback-vault"
    if Path(payload["rollback_path"]) != rollback:
        raise RestorePublicationError("rollback path is outside its transaction")

    _inject(fault_injector, "recovery.begin")
    shape = _validate_recovery_shape(txdir, payload)
    if shape == (True, True, False):
        payload["state"] = STATE_ROLLED_BACK
    elif shape == (False, True, True):
        os.replace(rollback, live)
        _fsync_directory(live.parent)
        payload["state"] = STATE_ROLLED_BACK
    elif shape == (True, False, True):
        failed_new = txdir / "failed-new-vault"
        os.replace(live, failed_new)
        os.replace(rollback, live)
        _fsync_directory(live.parent)
        payload["failed_new_path"] = str(failed_new)
        payload["state"] = STATE_ROLLED_BACK
    else:  # pragma: no cover - validation closes this branch
        raise RestorePublicationError("unsupported validated recovery shape")
    _inject(fault_injector, "recovery.before_terminal")
    _write_journal(txdir, payload)
    return payload


def _validate_recovery_shape(
    txdir: Path, payload: dict[str, Any]
) -> tuple[bool, bool, bool]:
    """Validate one active transaction without mutating any path."""

    live = txdir.parent.parent / _safe_live_name(payload.get("live_name"))
    candidate = Path(payload["candidate_path"])
    rollback = txdir / "rollback-vault"
    manifest = payload["backup_manifest"]
    if Path(payload["rollback_path"]) != rollback:
        raise RestorePublicationError("rollback path is outside its transaction")
    live_exists = live.exists() and not live.is_symlink()
    candidate_exists = candidate.exists() and not candidate.is_symlink()
    rollback_exists = rollback.exists() and not rollback.is_symlink()
    shape = (live_exists, candidate_exists, rollback_exists)
    if shape == (True, True, False):
        _regular_directory(live, "prepared live Markdown vault")
        _verify_root(_regular_directory(candidate, "prepared restore candidate"), manifest)
    elif shape == (False, True, True):
        _verify_root(_regular_directory(candidate, "prepared restore candidate"), manifest)
        _regular_directory(rollback, "staged rollback Markdown vault")
    elif shape == (True, False, True):
        _verify_root(_regular_directory(live, "newly published Markdown vault"), manifest)
        _regular_directory(rollback, "staged rollback Markdown vault")
        failed_new = txdir / "failed-new-vault"
        if failed_new.exists() or failed_new.is_symlink():
            raise RestorePublicationError("failed-new quarantine path already exists")
    else:
        raise RestorePublicationError(
            f"restore publication filesystem shape is unsafe for recovery: {shape}"
        )
    return shape


def recover_restore_publication(
    transaction_dir: str | os.PathLike[str],
    *,
    quiescence: QuiescenceFactory,
    fault_injector: FaultInjector | None = None,
) -> dict[str, Any]:
    """Fail closed or roll back one incomplete publication transaction."""

    txdir, payload = _load_transaction(transaction_dir)
    with quiescence():
        return _recover_loaded_transaction(txdir, payload, fault_injector=fault_injector)


def recover_restore_publications_before_startup(
    transaction_root: str | os.PathLike[str],
    *,
    quiescence: QuiescenceFactory,
    fault_injector: FaultInjector | None = None,
) -> dict[str, Any]:
    """Validate every journal, then recover all active transactions under one hold.

    The caller must run this before constructing any live reader/writer or derived
    component.  Enumeration and validation complete before the first mutation.
    """

    txroot = _regular_directory(transaction_root, "restore transaction root")
    if txroot.name != TRANSACTION_ROOT_NAME:
        raise RestorePublicationError("startup recovery requires the fixed transaction root")
    loaded: list[tuple[Path, dict[str, Any]]] = []
    active_live_names: set[str] = set()
    try:
        children = sorted(txroot.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise RestorePublicationError("cannot enumerate restore transactions") from exc
    for child in children:
        txdir, payload = _load_transaction(child)
        if payload["state"] in _ACTIVE_STATES:
            live_name = _safe_live_name(payload.get("live_name"))
            folded = os.path.normcase(live_name)
            if folded in active_live_names:
                raise RestorePublicationError("multiple active restore transactions target one live vault")
            active_live_names.add(folded)
        loaded.append((txdir, payload))

    results: list[dict[str, Any]] = []
    active_count = sum(1 for _txdir, payload in loaded if payload["state"] in _ACTIVE_STATES)
    with quiescence():
        for txdir, payload in loaded:
            if payload["state"] in _ACTIVE_STATES:
                _validate_recovery_shape(txdir, payload)
        for txdir, payload in loaded:
            _inject(fault_injector, "startup.before_transaction")
            results.append(
                _recover_loaded_transaction(txdir, payload, fault_injector=fault_injector)
            )
            _inject(fault_injector, "startup.after_transaction")
    return {
        "transaction_root": str(txroot),
        "transactions": results,
        "active_recovered": active_count,
        "total": len(results),
    }


def inspect_restore_publications_read_only(
    transaction_root: str | os.PathLike[str],
) -> dict[str, Any]:
    """Validate journals and active filesystem shapes without moving anything."""

    txroot = _regular_directory(transaction_root, "restore transaction root")
    if txroot.name != TRANSACTION_ROOT_NAME:
        raise RestorePublicationError("inspection requires the fixed transaction root")
    loaded: list[tuple[Path, dict[str, Any]]] = []
    active_live_names: set[str] = set()
    for child in sorted(txroot.iterdir(), key=lambda path: path.name):
        txdir, payload = _load_transaction(child)
        if payload["state"] in _ACTIVE_STATES:
            folded = os.path.normcase(_safe_live_name(payload.get("live_name")))
            if folded in active_live_names:
                raise RestorePublicationError(
                    "multiple active restore transactions target one live vault"
                )
            active_live_names.add(folded)
            _validate_recovery_shape(txdir, payload)
        loaded.append((txdir, payload))
    return {
        "transaction_root": str(txroot),
        "total": len(loaded),
        "active": sum(1 for _txdir, payload in loaded if payload["state"] in _ACTIVE_STATES),
        "terminal": sum(1 for _txdir, payload in loaded if payload["state"] in _TERMINAL_STATES),
        "transactions": tuple(
            {"txid": payload["txid"], "state": payload["state"]}
            for _txdir, payload in loaded
        ),
    }
