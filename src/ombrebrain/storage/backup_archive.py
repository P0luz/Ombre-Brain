"""Safe, verifiable local backup archives for Ombre Brain.

Markdown remains the event-memory source of truth. The embeddings database is
a rebuildable index; the optional You database is separately scoped, derived
state. Transactional snapshots keep both consistent during export. A manifest
detects incomplete/corrupted archives; it is an integrity check, not a
cryptographic signature of who created the archive.
"""

from __future__ import annotations

import hashlib
import codecs
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tempfile
from typing import Any
import zipfile

from .source_store import (
    HARD_MAX_SOURCE_BYTES,
    SOURCE_REF_RE,
    referenced_source_ids_from_markdown,
)
from ombrebrain.you.store import (
    YouStoreError,
    validate_you_snapshot_bytes,
    validate_you_snapshot_file,
)
from ombrebrain.them import (
    ThemStoreError,
    validate_them_snapshot_bytes,
    validate_them_snapshot_file,
)


MANIFEST_NAME = "backup_manifest.json"
MANIFEST_KIND = "ombre-brain-backup"
MANIFEST_SCHEMA_VERSION = 1

MIB = 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * MIB
MAX_MEMBERS = 10_000
MAX_MEMBER_BYTES = 512 * MIB
MAX_TOTAL_UNCOMPRESSED_BYTES = 1024 * MIB
MAX_COMPRESSION_RATIO = 1000.0
MAX_MANIFEST_BYTES = 8 * MIB

# The compatibility reader keeps the historical broad archive limits above.
# Production migration has a deliberately smaller attack surface: only the
# files the importer consumes are accepted, ordinary members are tightly
# bounded, and total extraction cannot fill a 512 MiB instance's filesystem.
MIGRATE_MAX_MEMBERS = 20_000
MIGRATE_MAX_TOTAL_UNCOMPRESSED_BYTES = 512 * MIB
MIGRATE_MAX_BUCKET_BYTES = 10 * MIB
MIGRATE_MAX_SOURCE_BYTES = HARD_MAX_SOURCE_BYTES
MIGRATE_MAX_EXPORT_META_BYTES = 1 * MIB
MIGRATE_MAX_EMBEDDINGS_DB_BYTES = 512 * MIB
MIGRATE_MAX_YOU_DB_BYTES = 128 * MIB
# them 与 you 同一档上限：两边存的是同构的东西，没有理由给一个更宽的口子。
MIGRATE_MAX_THEM_DB_BYTES = 128 * MIB
MIGRATE_MIN_FREE_RESERVE_BYTES = 64 * MIB


class BackupArchiveError(ValueError):
    """The backup cannot be trusted or safely processed."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_member_path(raw_name: str) -> str:
    """Normalize legacy Windows ZIP names while rejecting traversal paths."""
    if not raw_name or "\x00" in raw_name:
        raise BackupArchiveError("备份包含空路径或 NUL 字符")
    name = raw_name.replace("\\", "/")
    if name.startswith("/"):
        raise BackupArchiveError(f"备份包含绝对路径: {raw_name}")
    parts = PurePosixPath(name).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise BackupArchiveError(f"备份包含不安全路径: {raw_name}")
    if any(":" in part for part in parts):
        raise BackupArchiveError(f"备份包含盘符或非法路径: {raw_name}")
    return "/".join(parts)


def _migration_member_limit(path: str) -> int:
    """Return the production migration limit for one allow-listed member."""

    if path == MANIFEST_NAME:
        return MAX_MANIFEST_BYTES
    if path == "embeddings.db":
        return MIGRATE_MAX_EMBEDDINGS_DB_BYTES
    if path == "you/you.sqlite3":
        return MIGRATE_MAX_YOU_DB_BYTES
    if path == "them/them.sqlite3":
        return MIGRATE_MAX_THEM_DB_BYTES
    if path == "export_meta.json":
        return MIGRATE_MAX_EXPORT_META_BYTES
    parts = PurePosixPath(path).parts
    if len(parts) >= 2 and parts[0] == "buckets" and path.endswith(".md"):
        return MIGRATE_MAX_BUCKET_BYTES
    if (
        len(parts) == 2
        and parts[0] == "sources"
        and parts[1].endswith(".source")
        and SOURCE_REF_RE.fullmatch(parts[1][:-len(".source")])
    ):
        return MIGRATE_MAX_SOURCE_BYTES
    raise BackupArchiveError(f"迁移包包含不支持的成员: {path}")


def snapshot_sqlite(
    db_path: str,
    *,
    label: str = "embeddings.db",
    max_bytes: int = MIGRATE_MAX_EMBEDDINGS_DB_BYTES,
) -> bytes:
    """Return a transactionally consistent SQLite snapshot."""
    if not db_path or not os.path.isfile(db_path):
        return b""
    fd, temp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _snapshot_sqlite_to_file(
            db_path,
            temp_path,
            label=label,
            max_bytes=max_bytes,
        )
        return Path(temp_path).read_bytes()
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def _snapshot_sqlite_to_file(
    db_path: str,
    target_path: str,
    *,
    label: str = "embeddings.db",
    max_bytes: int = MIGRATE_MAX_EMBEDDINGS_DB_BYTES,
) -> bool:
    """Write a consistent SQLite snapshot to disk without retaining its bytes."""

    if not db_path or not os.path.isfile(db_path):
        return False
    source = sqlite3.connect(db_path, timeout=30)
    target = sqlite3.connect(target_path)
    try:
        page_count = int(source.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(source.execute("PRAGMA page_size").fetchone()[0])
        if page_count * page_size > max_bytes:
            raise BackupArchiveError(f"{label} 快照超过成员上限")
        source.backup(target)
        result = target.execute("PRAGMA quick_check").fetchone()
        if not result or str(result[0]).lower() != "ok":
            raise BackupArchiveError(f"{label} 快照完整性检查失败")
    finally:
        target.close()
        source.close()
    return True


def validate_sqlite_bytes(db_bytes: bytes) -> None:
    """Reject a corrupt or non-SQLite derived-index snapshot."""
    if not db_bytes:
        return
    fd, temp_path = tempfile.mkstemp(suffix=".db")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(db_bytes)
        validate_sqlite_file(temp_path)
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def _collect_markdown(buckets_dir: str) -> dict[str, bytes]:
    base = Path(buckets_dir).resolve()
    if not base.is_dir():
        raise BackupArchiveError(f"buckets_dir not found: {buckets_dir}")

    files: dict[str, bytes] = {}
    for path in sorted(base.rglob("*.md")):
        resolved = path.resolve()
        if not resolved.is_file() or not resolved.is_relative_to(base):
            raise BackupArchiveError(f"拒绝导出指向记忆目录外的文件: {path}")
        relative = resolved.relative_to(base).as_posix()
        arc_path = _normalize_member_path(f"buckets/{relative}")
        try:
            files[arc_path] = resolved.read_bytes()
        except OSError as exc:
            raise BackupArchiveError(f"无法读取记忆文件 {relative}: {exc}") from exc
    return files


def _iter_source_paths(base: Path) -> list[tuple[Path, str]]:
    """收集内容寻址的证据文件，忽略 sidecar 锁等内部文件。"""

    source_root = base / "_sources"
    if not source_root.exists():
        return []
    if source_root.is_symlink() or not source_root.is_dir():
        raise BackupArchiveError("原文证据目录不安全")

    result: list[tuple[Path, str]] = []
    for path in sorted(source_root.iterdir()):
        if not path.name.endswith(".source"):
            continue
        ref = path.name[:-len(".source")]
        if not SOURCE_REF_RE.fullmatch(ref):
            raise BackupArchiveError(f"原文证据文件名非法: {path.name}")
        if path.is_symlink():
            raise BackupArchiveError(f"拒绝导出符号链接原文证据: {path.name}")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise BackupArchiveError(f"无法解析原文证据 {path.name}: {exc}") from exc
        if not resolved.is_file() or not resolved.is_relative_to(source_root.resolve()):
            raise BackupArchiveError(f"拒绝导出指向证据目录外的文件: {path.name}")
        result.append((resolved, f"sources/{path.name}"))
    return result


def _collect_sources(base: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path, arc_path in _iter_source_paths(base):
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise BackupArchiveError(f"无法读取原文证据 {path.name}: {exc}") from exc
        if len(data) > MIGRATE_MAX_SOURCE_BYTES:
            raise BackupArchiveError(f"原文证据过大: {path.name}")
        if _sha256(data) != path.stem.removeprefix("src_"):
            raise BackupArchiveError(f"原文证据 SHA-256 校验失败: {path.name}")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BackupArchiveError(f"原文证据不是 UTF-8: {path.name}") from exc
        files[arc_path] = data
    return files


def _validate_source_reference_closure(files: dict[str, bytes]) -> None:
    """Refuse a new backup whose bucket references evidence it does not carry."""

    available = {
        PurePosixPath(path).name[:-len(".source")]
        for path in files
        if path.startswith("sources/") and path.endswith(".source")
    }
    referenced: set[str] = set()
    for path, data in files.items():
        if not path.startswith("buckets/") or not path.endswith(".md"):
            continue
        try:
            referenced.update(referenced_source_ids_from_markdown(data))
        except ValueError as exc:
            raise BackupArchiveError(f"无法验证 {path} 的原文证据引用：{exc}") from exc
    missing = sorted(referenced - available)
    if missing:
        preview = ", ".join(missing[:3])
        raise BackupArchiveError(
            f"备份存在 {len(missing)} 个悬空原文证据引用：{preview}"
        )


def _validate_archive_source_reference_closure(archive_path: str) -> None:
    """Validate the exact Markdown/source bytes written by the streaming export."""

    with zipfile.ZipFile(archive_path, "r") as archive:
        infos = [(_normalize_member_path(info.filename), info) for info in archive.infolist()]
        available = {
            PurePosixPath(path).name[:-len(".source")]
            for path, _info in infos
            if path.startswith("sources/") and path.endswith(".source")
        }
        referenced: set[str] = set()
        for path, info in infos:
            if not path.startswith("buckets/") or not path.endswith(".md"):
                continue
            if info.file_size > MIGRATE_MAX_BUCKET_BYTES:
                raise BackupArchiveError(f"备份成员过大: {path}")
            with archive.open(info, "r") as handle:
                data = handle.read(MIGRATE_MAX_BUCKET_BYTES + 1)
            if len(data) > MIGRATE_MAX_BUCKET_BYTES:
                raise BackupArchiveError(f"备份成员读取时超过上限: {path}")
            try:
                referenced.update(referenced_source_ids_from_markdown(data))
            except ValueError as exc:
                raise BackupArchiveError(
                    f"无法验证 {path} 的原文证据引用：{exc}"
                ) from exc
        missing = sorted(referenced - available)
        if missing:
            preview = ", ".join(missing[:3])
            raise BackupArchiveError(
                f"备份存在 {len(missing)} 个悬空原文证据引用：{preview}"
            )


def _build_manifest(files: dict[str, bytes], *, created_at: str, version: str) -> dict[str, Any]:
    entries = [
        {"path": path, "size": len(data), "sha256": _sha256(data)}
        for path, data in sorted(files.items())
    ]
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "kind": MANIFEST_KIND,
        "created_at": created_at,
        "version": version,
        "file_count": len(entries),
        "total_bytes": sum(item["size"] for item in entries),
        "files": entries,
    }


def build_export_archive(
    buckets_dir: str,
    embedding_db_path: str,
    export_meta: dict[str, Any],
) -> tuple[bytes, dict[str, Any]]:
    """Build a complete in-memory archive or fail without returning a partial one."""
    files = _collect_markdown(buckets_dir)
    files.update(_collect_sources(Path(buckets_dir).resolve()))
    _validate_source_reference_closure(files)
    db_bytes = snapshot_sqlite(embedding_db_path)
    if db_bytes:
        files["embeddings.db"] = db_bytes
    # 两个模块的库都进备份。任一模块没开过就没有库文件，snapshot_sqlite
    # 返回空，那一条不进归档。
    for 相对名, 目录名, 上限, 校验, 错误类型, 标签 in (
        ("you/you.sqlite3", ".you", MIGRATE_MAX_YOU_DB_BYTES,
         validate_you_snapshot_bytes, YouStoreError, "You"),
        ("them/them.sqlite3", ".them", MIGRATE_MAX_THEM_DB_BYTES,
         validate_them_snapshot_bytes, ThemStoreError, "them"),
    ):
        db_path = Path(buckets_dir).resolve() / 目录名 / 相对名.split("/", 1)[1]
        module_bytes = snapshot_sqlite(
            str(db_path), label=相对名, max_bytes=上限
        )
        if not module_bytes:
            continue
        try:
            校验(module_bytes)
        except 错误类型 as exc:
            raise BackupArchiveError(f"{标签} 快照结构校验失败") from exc
        files[相对名] = module_bytes
    meta_bytes = json.dumps(
        export_meta, ensure_ascii=False, indent=2, default=str
    ).encode("utf-8")
    files["export_meta.json"] = meta_bytes

    manifest = _build_manifest(
        files,
        created_at=str(export_meta.get("exported_at") or ""),
        version=str(export_meta.get("version") or ""),
    )
    manifest_bytes = json.dumps(
        manifest, ensure_ascii=False, indent=2
    ).encode("utf-8")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for path, data in sorted(files.items()):
            archive.writestr(path, data)
        archive.writestr(MANIFEST_NAME, manifest_bytes)
    payload = buffer.getvalue()
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise BackupArchiveError("备份压缩后超过 512 MiB 上限")
    return payload, manifest


def _stream_file_member(
    archive: zipfile.ZipFile,
    *,
    source: Path,
    arc_path: str,
    require_utf8: bool = False,
) -> dict[str, Any]:
    """Stream one source file into ZIP while hashing the exact written bytes."""

    member_limit = _migration_member_limit(arc_path)
    try:
        declared_size = source.stat().st_size
    except OSError as exc:
        raise BackupArchiveError(f"无法读取导出文件 {arc_path}: {exc}") from exc
    if declared_size < 0 or declared_size > member_limit:
        raise BackupArchiveError(f"备份成员过大: {arc_path}")

    digest = hashlib.sha256()
    utf8_decoder = codecs.getincrementaldecoder("utf-8")() if require_utf8 else None
    written = 0
    try:
        with source.open("rb") as input_handle, archive.open(
            arc_path, "w", force_zip64=True
        ) as output_handle:
            while chunk := input_handle.read(MIB):
                written += len(chunk)
                if written > member_limit:
                    raise BackupArchiveError(f"备份成员读取时超过上限: {arc_path}")
                digest.update(chunk)
                if utf8_decoder is not None:
                    try:
                        utf8_decoder.decode(chunk)
                    except UnicodeDecodeError as exc:
                        raise BackupArchiveError(
                            f"原文证据不是 UTF-8: {arc_path}"
                        ) from exc
                output_handle.write(chunk)
                archive_position = getattr(getattr(archive, "fp", None), "tell", None)
                if callable(archive_position) and archive_position() > MAX_ARCHIVE_BYTES:
                    raise BackupArchiveError("备份压缩后超过 512 MiB 上限")
    except BackupArchiveError:
        raise
    except OSError as exc:
        raise BackupArchiveError(f"无法读取导出文件 {arc_path}: {exc}") from exc
    if written != declared_size:
        raise BackupArchiveError(f"导出期间文件大小发生变化: {arc_path}")
    if utf8_decoder is not None:
        try:
            utf8_decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise BackupArchiveError(f"原文证据不是 UTF-8: {arc_path}") from exc
    return {"path": arc_path, "size": written, "sha256": digest.hexdigest()}


def build_export_archive_file(
    buckets_dir: str,
    embedding_db_path: str,
    export_meta: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Build a bounded temporary ZIP on disk and return its path plus manifest.

    Unlike :func:`build_export_archive`, this production path never holds all
    Markdown, the SQLite snapshot, the ZIP buffer, and a second ``getvalue``
    copy in RAM at the same time.  The caller owns the returned path and must
    remove it after the response finishes.
    """

    base = Path(buckets_dir).resolve()
    if not base.is_dir():
        raise BackupArchiveError(f"buckets_dir not found: {buckets_dir}")

    archive_fd, archive_path = tempfile.mkstemp(prefix="ombre-export-", suffix=".zip")
    os.close(archive_fd)
    snapshot_fd, snapshot_path = tempfile.mkstemp(prefix="ombre-embedding-", suffix=".db")
    os.close(snapshot_fd)
    you_snapshot_fd, you_snapshot_path = tempfile.mkstemp(prefix="ombre-you-", suffix=".db")
    os.close(you_snapshot_fd)
    them_snapshot_fd, them_snapshot_path = tempfile.mkstemp(prefix="ombre-them-", suffix=".db")
    os.close(them_snapshot_fd)
    entries: list[dict[str, Any]] = []
    total_uncompressed = 0

    def record(entry: dict[str, Any]) -> None:
        nonlocal total_uncompressed
        entries.append(entry)
        if len(entries) + 1 > MIGRATE_MAX_MEMBERS:  # + manifest member
            raise BackupArchiveError(
                f"备份文件项过多（上限 {MIGRATE_MAX_MEMBERS}）"
            )
        total_uncompressed += int(entry["size"])
        if total_uncompressed > MIGRATE_MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise BackupArchiveError("备份解压后超过 512 MiB 上限")

    try:
        with zipfile.ZipFile(
            archive_path,
            "w",
            zipfile.ZIP_DEFLATED,
            allowZip64=True,
        ) as archive:
            for path in sorted(base.rglob("*.md")):
                if path.is_symlink():
                    raise BackupArchiveError(f"拒绝导出符号链接文件: {path}")
                try:
                    resolved = path.resolve(strict=True)
                except OSError as exc:
                    raise BackupArchiveError(f"无法解析记忆文件 {path}: {exc}") from exc
                if not resolved.is_file() or not resolved.is_relative_to(base):
                    raise BackupArchiveError(f"拒绝导出指向记忆目录外的文件: {path}")
                relative = path.relative_to(base).as_posix()
                arc_path = _normalize_member_path(f"buckets/{relative}")
                record(
                    _stream_file_member(
                        archive,
                        source=resolved,
                        arc_path=arc_path,
                    )
                )

            for source_path, arc_path in _iter_source_paths(base):
                entry = _stream_file_member(
                    archive,
                    source=source_path,
                    arc_path=arc_path,
                    require_utf8=True,
                )
                expected_digest = source_path.stem.removeprefix("src_")
                if entry["sha256"] != expected_digest:
                    raise BackupArchiveError(
                        f"原文证据 SHA-256 校验失败: {source_path.name}"
                    )
                record(entry)

            if _snapshot_sqlite_to_file(embedding_db_path, snapshot_path):
                record(
                    _stream_file_member(
                        archive,
                        source=Path(snapshot_path),
                        arc_path="embeddings.db",
                    )
                )

            for 相对名, 目录名, 暂存, 上限, 校验, 错误类型, 标签 in (
                ("you/you.sqlite3", ".you", you_snapshot_path,
                 MIGRATE_MAX_YOU_DB_BYTES, validate_you_snapshot_file,
                 YouStoreError, "You"),
                ("them/them.sqlite3", ".them", them_snapshot_path,
                 MIGRATE_MAX_THEM_DB_BYTES, validate_them_snapshot_file,
                 ThemStoreError, "them"),
            ):
                db_path = base / 目录名 / 相对名.split("/", 1)[1]
                if not _snapshot_sqlite_to_file(
                    str(db_path), 暂存, label=相对名, max_bytes=上限
                ):
                    continue
                try:
                    校验(暂存)
                except 错误类型 as exc:
                    raise BackupArchiveError(f"{标签} 快照结构校验失败") from exc
                record(
                    _stream_file_member(
                        archive, source=Path(暂存), arc_path=相对名
                    )
                )

            meta_bytes = json.dumps(
                export_meta,
                ensure_ascii=False,
                indent=2,
                default=str,
            ).encode("utf-8")
            if len(meta_bytes) > MIGRATE_MAX_EXPORT_META_BYTES:
                raise BackupArchiveError("export_meta.json 过大")
            archive.writestr("export_meta.json", meta_bytes)
            record(
                {
                    "path": "export_meta.json",
                    "size": len(meta_bytes),
                    "sha256": _sha256(meta_bytes),
                }
            )

            entries.sort(key=lambda item: item["path"])
            manifest = {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "kind": MANIFEST_KIND,
                "created_at": str(export_meta.get("exported_at") or ""),
                "version": str(export_meta.get("version") or ""),
                "file_count": len(entries),
                "total_bytes": sum(int(item["size"]) for item in entries),
                "files": entries,
            }
            manifest_bytes = json.dumps(
                manifest,
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8")
            if len(manifest_bytes) > MAX_MANIFEST_BYTES:
                raise BackupArchiveError("backup_manifest.json 过大")
            archive.writestr(MANIFEST_NAME, manifest_bytes)

        _validate_archive_source_reference_closure(archive_path)
        if os.path.getsize(archive_path) > MAX_ARCHIVE_BYTES:
            raise BackupArchiveError("备份压缩后超过 512 MiB 上限")
        return archive_path, manifest
    except Exception:
        try:
            os.unlink(archive_path)
        except OSError:
            pass
        raise
    finally:
        try:
            os.unlink(snapshot_path)
        except OSError:
            pass
        for 暂存 in (you_snapshot_path, them_snapshot_path):
            try:
                os.unlink(暂存)
            except OSError:
                pass


def validate_sqlite_file(db_path: str) -> None:
    """Reject a corrupt/non-SQLite snapshot without materializing it in RAM."""

    if not db_path or not os.path.isfile(db_path) or os.path.getsize(db_path) == 0:
        return
    connection = sqlite3.connect(db_path, timeout=30)
    try:
        result = connection.execute("PRAGMA quick_check").fetchone()
        if not result or str(result[0]).lower() != "ok":
            raise BackupArchiveError("embeddings.db 已损坏")
    except sqlite3.DatabaseError as exc:
        raise BackupArchiveError(f"embeddings.db 无效: {exc}") from exc
    finally:
        connection.close()


def _validate_infos(
    infos: list[zipfile.ZipInfo],
    archive_size: int,
    *,
    max_members: int = MAX_MEMBERS,
    max_total_bytes: int = MAX_TOTAL_UNCOMPRESSED_BYTES,
    member_limit: Any = None,
) -> dict[str, zipfile.ZipInfo]:
    if archive_size > MAX_ARCHIVE_BYTES:
        raise BackupArchiveError("备份压缩包超过 512 MiB 上限")
    if len(infos) > max_members:
        raise BackupArchiveError(f"备份文件项过多（上限 {max_members}）")

    normalized: dict[str, zipfile.ZipInfo] = {}
    total = 0
    for info in infos:
        path = _normalize_member_path(info.filename.rstrip("/"))
        if info.is_dir():
            continue
        if path in normalized:
            raise BackupArchiveError(f"备份包含重复路径: {path}")
        if info.flag_bits & 0x1:
            raise BackupArchiveError(f"不支持加密 ZIP 成员: {path}")
        unix_mode = (info.external_attr >> 16) & 0xFFFF
        if unix_mode and stat.S_ISLNK(unix_mode):
            raise BackupArchiveError(f"不支持符号链接 ZIP 成员: {path}")
        path_limit = member_limit(path) if callable(member_limit) else MAX_MEMBER_BYTES
        if info.file_size > path_limit:
            raise BackupArchiveError(f"备份成员过大: {path}")
        total += info.file_size
        if total > max_total_bytes:
            raise BackupArchiveError(
                f"备份解压后超过 {max_total_bytes // MIB} MiB 上限"
            )
        if info.file_size and not info.compress_size:
            raise BackupArchiveError(f"备份成员压缩信息异常: {path}")
        if info.file_size >= MIB:
            ratio = info.file_size / max(1, info.compress_size)
            if ratio > MAX_COMPRESSION_RATIO:
                raise BackupArchiveError(f"备份成员压缩率异常: {path}")
        normalized[path] = info
    return normalized


def _manifest_expectations(
    manifest: Any,
    actual_sizes: dict[str, int],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Validate manifest structure against member names/sizes without payloads."""

    if not isinstance(manifest, dict):
        raise BackupArchiveError("backup_manifest.json 必须是 JSON 对象")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise BackupArchiveError("不支持的备份清单版本")
    if manifest.get("kind") != MANIFEST_KIND:
        raise BackupArchiveError("备份清单类型不正确")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise BackupArchiveError("备份清单缺少 files")

    expected: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise BackupArchiveError("备份清单文件项格式错误")
        path = _normalize_member_path(str(entry.get("path") or ""))
        if path == MANIFEST_NAME or path in expected:
            raise BackupArchiveError(f"备份清单包含重复或递归路径: {path}")
        expected[path] = entry

    if set(expected) != set(actual_sizes):
        missing = sorted(set(expected) - set(actual_sizes))
        extra = sorted(set(actual_sizes) - set(expected))
        raise BackupArchiveError(
            f"备份清单与实际文件不一致（missing={missing[:3]}, extra={extra[:3]}）"
        )
    if manifest.get("file_count") != len(actual_sizes):
        raise BackupArchiveError("备份清单 file_count 不一致")
    total = sum(actual_sizes.values())
    if manifest.get("total_bytes") != total:
        raise BackupArchiveError("备份清单 total_bytes 不一致")

    for path, size in actual_sizes.items():
        entry = expected[path]
        if entry.get("size") != size:
            raise BackupArchiveError(f"备份成员大小校验失败: {path}")
        digest = entry.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise BackupArchiveError(f"备份成员 SHA-256 格式错误: {path}")
        try:
            int(digest, 16)
        except ValueError as exc:
            raise BackupArchiveError(f"备份成员 SHA-256 格式错误: {path}") from exc
    return manifest, expected


def _verify_manifest(manifest: Any, files: dict[str, bytes]) -> dict[str, Any]:
    verified, expected = _manifest_expectations(
        manifest,
        {path: len(data) for path, data in files.items()},
    )
    for path, data in files.items():
        entry = expected[path]
        if entry.get("sha256") != _sha256(data):
            raise BackupArchiveError(f"备份成员 SHA-256 校验失败: {path}")
    return verified


def read_backup_archive(zip_bytes: bytes) -> dict[str, Any]:
    """Read a bounded archive and verify its manifest when present."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as archive:
            infos = _validate_infos(archive.infolist(), len(zip_bytes))
            files: dict[str, bytes] = {}
            for path, info in infos.items():
                try:
                    data = archive.read(info)
                except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                    raise BackupArchiveError(f"无法读取备份成员 {path}: {exc}") from exc
                if len(data) != info.file_size:
                    raise BackupArchiveError(f"备份成员读取长度不一致: {path}")
                files[path] = data
    except zipfile.BadZipFile as exc:
        raise BackupArchiveError(f"无效的 ZIP 文件: {exc}") from exc

    manifest_bytes = files.pop(MANIFEST_NAME, None)
    if manifest_bytes is None:
        return {
            "files": files,
            "manifest": None,
            "integrity_verified": False,
            "integrity_warning": "旧版备份没有完整性清单；已执行 ZIP 安全检查，但无法确认文件是否齐全",
        }
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupArchiveError(f"backup_manifest.json 无法解析: {exc}") from exc
    verified = _verify_manifest(manifest, files)
    return {
        "files": files,
        "manifest": verified,
        "integrity_verified": True,
        "integrity_warning": "",
    }


def extract_backup_archive_file(
    archive_path: str,
    destination: str,
) -> dict[str, Any]:
    """Verify and stream an archive into a private disk-backed workspace.

    The returned ``files`` mapping contains archive member names mapped to
    local temporary paths.  No member collection, SQLite snapshot, or full ZIP
    is retained in memory.  The caller owns ``destination`` and must remove it
    after apply/error.
    """

    try:
        archive_size = os.path.getsize(archive_path)
    except OSError as exc:
        raise BackupArchiveError(f"无法读取备份压缩包: {exc}") from exc

    target_root = Path(destination).resolve()
    target_root.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    files: dict[str, str] = {}

    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            infos = _validate_infos(
                archive.infolist(),
                archive_size,
                max_members=MIGRATE_MAX_MEMBERS,
                max_total_bytes=MIGRATE_MAX_TOTAL_UNCOMPRESSED_BYTES,
                member_limit=_migration_member_limit,
            )
            required_space = (
                sum(info.file_size for info in infos.values())
                + MIGRATE_MIN_FREE_RESERVE_BYTES
            )
            try:
                free_space = shutil.disk_usage(target_root).free
            except OSError as exc:
                raise BackupArchiveError(f"无法检查迁移暂存空间: {exc}") from exc
            if free_space < required_space:
                raise BackupArchiveError(
                    "迁移暂存空间不足"
                    f"（需要至少 {required_space // MIB} MiB，"
                    f"可用 {free_space // MIB} MiB）"
                )
            manifest_info = infos.pop(MANIFEST_NAME, None)
            manifest: dict[str, Any] | None = None
            expected: dict[str, dict[str, Any]] = {}

            if manifest_info is not None:
                if manifest_info.file_size > MAX_MANIFEST_BYTES:
                    raise BackupArchiveError("backup_manifest.json 过大")
                try:
                    with archive.open(manifest_info, "r") as handle:
                        manifest_bytes = handle.read(MAX_MANIFEST_BYTES + 1)
                except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                    raise BackupArchiveError(
                        f"无法读取 backup_manifest.json: {exc}"
                    ) from exc
                if len(manifest_bytes) != manifest_info.file_size:
                    raise BackupArchiveError("backup_manifest.json 读取长度不一致")
                try:
                    parsed_manifest = json.loads(manifest_bytes.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise BackupArchiveError(
                        f"backup_manifest.json 无法解析: {exc}"
                    ) from exc
                manifest, expected = _manifest_expectations(
                    parsed_manifest,
                    {path: info.file_size for path, info in infos.items()},
                )

            for index, (path, info) in enumerate(sorted(infos.items())):
                path_limit = _migration_member_limit(path)
                suffix = Path(path).suffix.lower()
                if suffix not in {".md", ".db", ".json", ".source"}:
                    suffix = ".bin"
                local_name = (
                    f"{index:05d}-"
                    f"{hashlib.sha256(path.encode('utf-8')).hexdigest()}{suffix}"
                )
                local_path = (target_root / local_name).resolve()
                if not local_path.is_relative_to(target_root):
                    raise BackupArchiveError(f"备份成员目标路径越界: {path}")

                digest = hashlib.sha256()
                written = 0
                try:
                    with archive.open(info, "r") as source, local_path.open("xb") as target:
                        while chunk := source.read(MIB):
                            written += len(chunk)
                            if written > info.file_size or written > path_limit:
                                raise BackupArchiveError(f"备份成员解压超过声明大小: {path}")
                            digest.update(chunk)
                            target.write(chunk)
                        # This is a disposable, hash-verified workspace rather
                        # than a durability boundary.  Closing the file is
                        # sufficient and avoids one fsync per imported bucket.
                except BackupArchiveError:
                    try:
                        local_path.unlink()
                    except OSError:
                        pass
                    raise
                except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                    try:
                        local_path.unlink()
                    except OSError:
                        pass
                    raise BackupArchiveError(f"无法解压备份成员 {path}: {exc}") from exc

                extracted.append(local_path)
                if written != info.file_size:
                    raise BackupArchiveError(f"备份成员读取长度不一致: {path}")
                if expected and expected[path].get("sha256") != digest.hexdigest():
                    raise BackupArchiveError(f"备份成员 SHA-256 校验失败: {path}")
                files[path] = str(local_path)
    except zipfile.BadZipFile as exc:
        raise BackupArchiveError(f"无效的 ZIP 文件: {exc}") from exc
    except Exception:
        for local_path in extracted:
            try:
                local_path.unlink()
            except OSError:
                pass
        raise

    return {
        "files": files,
        "manifest": manifest,
        "integrity_verified": manifest is not None,
        "integrity_warning": (
            "" if manifest is not None else
            "旧版备份没有完整性清单；已执行 ZIP 安全检查，但无法确认文件是否齐全"
        ),
    }
