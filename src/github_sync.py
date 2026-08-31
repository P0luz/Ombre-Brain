"""
github_sync.py — GitHub 仓库同步（用于 bucket 数据云端备份）

策略：
- 同步 buckets_dir 下的 .md 记忆、_sources 原文证据和经校验的 You 事务快照
- embeddings.db 不上传（二进制，可由 /api/embedding/migrate 重算）
- 使用 GitHub Git Trees API 批量提交（一次同步 = 一个 commit）
- 支持手动触发 + 可选的定时自动同步

依赖：httpx（已在 requirements.txt）
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import tempfile
import uuid
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from typing import Any

import httpx

from ombrebrain.storage.source_store import (
    HARD_MAX_SOURCE_BYTES,
    SOURCE_REF_RE,
    SourceStore,
    referenced_source_ids_from_markdown,
)
from ombrebrain.them import (
    ThemStore,
    ThemStoreError,
    validate_them_snapshot_file,
)
from ombrebrain.you import YouStore, YouStoreError, validate_you_snapshot_file
from utils import _win_long_path

logger = logging.getLogger("ombre_brain.github_sync")

_API = "https://api.github.com"
_TIMEOUT = 60.0
_MAX_FILE_BYTES = HARD_MAX_SOURCE_BYTES  # GitHub blob 上限更高；与证据读取硬上限一致
_TREE_CHUNK = 200                  # 每个 /git/trees 请求最多内联多少文件，避免单请求过大
_TREE_CHUNK_BYTES = 2 * 1024 * 1024  # Bound decoded bodies retained by one request.
_MAX_BACKUP_FILES = 10_000
_MAX_BACKUP_PATH_BYTES = 1024
_MAX_MANIFEST_PAYLOAD_BYTES = 4 * 1024 * 1024
_MAX_MANIFEST_BASE64_BYTES = ((_MAX_MANIFEST_PAYLOAD_BYTES + 2) // 3) * 4 + 64 * 1024
_MAX_RESTORE_TOTAL_BYTES = 512 * 1024 * 1024
_MANIFEST_FILENAME = "_ombre_backup_manifest.json"
_SOURCE_REFS_COMPLETE_FIELD = "source_refs_complete"
_YOU_RELATIVE_PATH = ".you/you.sqlite3"
_THEM_RELATIVE_PATH = ".them/them.sqlite3"
# 两个模块的库都进仓库。poluz 2026-08-20：「仓库属于模型，对别人的看法也属于
# 模型」——呼应 rule.md 第 3 条。them 记的是第三方，所以这一条是她本人的决定，
# 不是照着 you 抄一遍带进来的。
_MODULE_SNAPSHOT_PATHS = (_YOU_RELATIVE_PATH, _THEM_RELATIVE_PATH)
# 每条快照路径对应的校验函数。恢复时按路径分派——用错校验函数等于没校验。
_SNAPSHOT_VALIDATORS = {
    _YOU_RELATIVE_PATH: (validate_you_snapshot_file, YouStoreError, "You"),
    _THEM_RELATIVE_PATH: (validate_them_snapshot_file, ThemStoreError, "them"),
}


class _LazyMarkdownFiles(Mapping[str, bytes]):
    """A path index whose file bodies are read only when requested.

    GitHub backup used to retain every Markdown file as ``bytes`` and then
    retain a second decoded ``str`` copy for every tree entry.  On a 512 MiB
    instance that makes the scheduled backup itself an OOM trigger.  Keeping
    only paths here lets ``_batch_commit`` hold at most one bounded chunk of
    bodies while preserving the historical mapping-shaped private API.
    """

    def __init__(self, paths: Mapping[str, str]) -> None:
        self._paths = dict(paths)

    def __len__(self) -> int:
        return len(self._paths)

    def __iter__(self) -> Iterator[str]:
        return iter(self._paths)

    def __getitem__(self, relative_path: str) -> bytes:
        full_path = self._paths[relative_path]
        if os.path.islink(full_path):
            raise RuntimeError(
                f"GitHub backup refuses symbolic-link file: {relative_path}"
            )
        size = os.path.getsize(full_path)
        if size > _MAX_FILE_BYTES:
            raise RuntimeError(
                f"GitHub backup file grew beyond {_MAX_FILE_BYTES} bytes: "
                f"{relative_path} ({size} bytes)"
            )
        # Re-check through a bounded read: a file may grow between stat() and
        # open(), and an unbounded read here would defeat the memory guarantee.
        with open(full_path, "rb") as handle:
            content = handle.read(_MAX_FILE_BYTES + 1)
        if len(content) > _MAX_FILE_BYTES:
            raise RuntimeError(
                f"GitHub backup file grew beyond {_MAX_FILE_BYTES} bytes: "
                f"{relative_path}"
            )
        if _is_source_relative_path(relative_path):
            expected = relative_path.rsplit("/", 1)[-1][:-len(".source")]
            if hashlib.sha256(content).hexdigest() != expected.removeprefix("src_"):
                raise RuntimeError(
                    f"GitHub backup source integrity mismatch: {relative_path}"
                )
            try:
                content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RuntimeError(
                    f"GitHub backup source is not UTF-8: {relative_path}"
                ) from exc
        return content


def _vetted_extra_backup_path(relative: str, full: str) -> str:
    """校验一条来自 vault 之外的额外备份路径，返回规范化后的相对名。

    You / them 的快照都是在临时目录里生成的（要事务一致，不能直接备份活动库），
    所以它们落在 buckets_dir 之外，走不了 _collect_files 那套基于 vault
    相对路径的检查，只能单独把关。
    """

    normalized = str(relative).replace("\\", "/")
    if normalized not in _MODULE_SNAPSHOT_PATHS:
        raise RuntimeError(f"unsupported extra GitHub backup path: {normalized}")
    if not os.path.isfile(full) or os.path.islink(full):
        raise RuntimeError(f"{normalized} snapshot is unavailable or unsafe")
    if os.path.getsize(full) > _MAX_FILE_BYTES:
        raise RuntimeError(
            f"GitHub backup file exceeds {_MAX_FILE_BYTES} bytes: {normalized}"
        )
    return normalized


def _is_source_relative_path(relative_path: str) -> bool:
    parts = str(relative_path).replace("\\", "/").split("/")
    return bool(
        len(parts) == 2
        and parts[0] == "_sources"
        and parts[1].endswith(".source")
        and SOURCE_REF_RE.fullmatch(parts[1][:-len(".source")])
    )


def _is_backup_relative_path(relative_path: str) -> bool:
    normalized = str(relative_path).replace("\\", "/")
    return (
        normalized.endswith(".md")
        or _is_source_relative_path(normalized)
        or normalized in _MODULE_SNAPSHOT_PATHS
    )


def _iter_backup_paths(root_dir: str) -> Iterator[str]:
    """Depth-first scandir traversal without materializing directory listings."""
    iterators: list[os.ScandirIterator] = []
    try:
        try:
            iterators.append(os.scandir(root_dir))
        except OSError as exc:
            logger.warning(f"[github_sync] cannot scan {root_dir}: {exc}")
            return

        while iterators:
            current = iterators[-1]
            try:
                entry = next(current)
            except StopIteration:
                current.close()
                iterators.pop()
                continue
            except OSError as exc:
                logger.warning(f"[github_sync] directory scan failed: {exc}")
                current.close()
                iterators.pop()
                continue

            try:
                # Match os.walk(..., followlinks=False): recurse into real
                # directories, but never follow a symlinked directory tree.
                if entry.is_dir(follow_symlinks=False):
                    try:
                        iterators.append(os.scandir(entry.path))
                    except OSError as exc:
                        logger.warning(f"[github_sync] cannot scan {entry.path}: {exc}")
                    continue
                if entry.is_file(follow_symlinks=False):
                    if entry.name.endswith(".md"):
                        yield entry.path
                    elif entry.name.endswith(".source"):
                        relative = os.path.relpath(entry.path, root_dir).replace("\\", "/")
                        if relative.startswith("_sources/"):
                            if not _is_source_relative_path(relative):
                                raise RuntimeError(
                                    f"invalid source evidence filename: {relative}"
                                )
                            yield entry.path
            except OSError as exc:
                logger.warning(f"[github_sync] skip {entry.path}: {exc}")
    finally:
        for iterator in reversed(iterators):
            iterator.close()


class GitHubSync:
    """向 GitHub 仓库批量上传记忆和原文证据。"""

    def __init__(
        self,
        token: str,
        repo: str,
        branch: str = "main",
        path_prefix: str = "ombre",
        max_source_bytes: int = 2 * 1024 * 1024,
    ):
        self.token = token
        self.repo = repo.strip()          # "owner/repo"
        self.branch = branch.strip() or "main"
        self.path_prefix = path_prefix.strip().strip("/")
        configured_source_limit = max(0, int(max_source_bytes))
        self.max_source_bytes = min(
            configured_source_limit or HARD_MAX_SOURCE_BYTES,
            HARD_MAX_SOURCE_BYTES,
        )

        self._headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        self.last_sync: str | None = None
        self.last_status: str = "idle"   # idle | ok | error
        self.last_error: str = ""
        self.last_count: int = 0
        self.is_validated: bool = False   # validate() 成功后置 True
        # A3：连续失败计数。自动备份可能连挂几次而用户毫无察觉（以为有备份其实没有）。
        # 每次成功归零、每次失败 +1，供诊断面板判断要不要升级为醒目告警。
        self.consecutive_failures: int = 0
        # Manual and scheduled backups share one instance.  Serializing them
        # prevents two bounded jobs from adding up to an unbounded peak.
        self._sync_lock = asyncio.Lock()

    # --------------------------------------------------------
    # 公开接口
    # --------------------------------------------------------

    async def sync(self, buckets_dir: str) -> dict[str, Any]:
        """同步记忆、原文证据和可用的 You 快照到 GitHub。"""
        async with self._sync_lock:
            try:
                with tempfile.TemporaryDirectory(prefix="ombre-github-modules-") as temp_dir:
                    extra_paths: dict[str, str] = {}
                    # 两个模块各导一份。任一模块没开过（库都还没建），
                    # snapshot_to 返回 False，那一条就不进这次提交。
                    for relative, store in (
                        (_YOU_RELATIVE_PATH, YouStore(buckets_dir)),
                        (_THEM_RELATIVE_PATH, ThemStore(buckets_dir)),
                    ):
                        snapshot_path = os.path.join(
                            temp_dir, relative.replace("/", "_")
                        )
                        if store.snapshot_to(snapshot_path):
                            validator, _, _ = _SNAPSHOT_VALIDATORS[relative]
                            validator(snapshot_path)
                            extra_paths[relative] = snapshot_path
                    files = self._collect_files(buckets_dir, extra_paths)
                    if not files:
                        self.last_status = "ok"
                        self.last_error = ""
                        self.last_sync = _now_iso()
                        self.last_count = 0
                        return {"ok": True, "uploaded": 0, "message": "无可同步文件"}

                    count = await self._batch_commit(files)
                self.last_sync = _now_iso()
                self.last_status = "ok"
                self.last_error = ""
                self.last_count = count
                self.consecutive_failures = 0
                return {"ok": True, "uploaded": count}
            except Exception as e:
                self.last_status = "error"
                self.last_error = str(e)
                self.consecutive_failures += 1
                logger.error(f"[github_sync] sync failed (连续 {self.consecutive_failures} 次): {e}")
                return {"ok": False, "error": str(e)}

    async def import_from_github(self, buckets_dir: str) -> dict[str, Any]:
        """从 GitHub 仓库恢复记忆和原文证据。

        这是 sync() 的逆操作。合并覆盖语义：同名（同相对路径）文件用 GitHub 上的覆盖，
        本地独有的文件保留不动。embeddings.db 不在仓库里，调用方应在导入后跑一次
        backfill 重建向量。带 path-traversal 防护（仓库内容不可信，防 ../ 逃逸）。
        """
        async with self._sync_lock:
            return await self._import_from_github_locked(buckets_dir)

    async def _import_from_github_locked(self, buckets_dir: str) -> dict[str, Any]:
        """Serialized implementation shared with the backup lock."""
        failure_context: dict[str, Any] = {}
        try:
            async with httpx.AsyncClient(headers=self._headers, timeout=_TIMEOUT) as c:
                # 取 branch HEAD → commit tree → 递归列出全部 blob
                r = await self._request(c, "GET", f"{_API}/repos/{self.repo}/git/ref/heads/{self.branch}")
                if _is_empty_repo_response(r):
                    return {
                        "ok": True,
                        "imported": 0,
                        "skipped": 0,
                        "message": f"GitHub 仓库 {self.repo} 还是空仓库，暂无可导入的记忆文件",
                    }
                if r.status_code == 404:
                    return {"ok": False, "error": f"分支 {self.branch} 不存在"}
                r.raise_for_status()
                head_sha = r.json()["object"]["sha"]
                r = await self._request(c, "GET", f"{_API}/repos/{self.repo}/git/commits/{head_sha}")
                r.raise_for_status()
                tree_sha = r.json()["tree"]["sha"]
                r = await self._request(c, "GET", f"{_API}/repos/{self.repo}/git/trees/{tree_sha}?recursive=1")
                r.raise_for_status()
                tj = r.json()
                tree = tj.get("tree", [])
                truncated = bool(tj.get("truncated"))
                if truncated:
                    raise RuntimeError(
                        "GitHub returned a truncated tree; refusing an incomplete restore"
                    )
                if not isinstance(tree, list):
                    raise RuntimeError("GitHub tree response is not a list")

                prefix = (self.path_prefix + "/") if self.path_prefix else ""
                manifest_path = f"{prefix}{_MANIFEST_FILENAME}"
                manifest_item = next(
                    (
                        t for t in tree
                        if t.get("type") == "blob" and t.get("path") == manifest_path
                    ),
                    None,
                )
                backup_manifest = await self._read_backup_manifest_summary(c, manifest_item) if manifest_item else {"present": False}
                if manifest_item and not backup_manifest.get("present"):
                    raise RuntimeError(
                        "GitHub backup manifest is unreadable; refusing an unverified restore"
                    )
                manifest_entries = backup_manifest.pop("_entries", None)
                targets = []
                for target in tree:
                    remote_path = str(target.get("path", ""))
                    if target.get("type") != "blob" or not remote_path.startswith(prefix):
                        continue
                    relative_path = remote_path[len(prefix):]
                    if _is_backup_relative_path(relative_path):
                        targets.append(target)
                if len(targets) > _MAX_BACKUP_FILES:
                    raise RuntimeError(
                        f"GitHub restore has more than {_MAX_BACKUP_FILES} files"
                    )
                target_by_rel: dict[str, dict[str, Any]] = {}
                declared_total = 0
                for target in targets:
                    rel = str(target.get("path", ""))[len(prefix):]
                    if (
                        not rel
                        or len(rel.encode("utf-8")) > _MAX_BACKUP_PATH_BYTES
                        or rel in target_by_rel
                    ):
                        raise RuntimeError(f"unsafe or duplicate GitHub restore path: {rel[:200]}")
                    try:
                        declared_size = int(target.get("size", 0) or 0)
                    except (TypeError, ValueError, OverflowError) as exc:
                        raise RuntimeError(f"invalid GitHub blob size: {rel}") from exc
                    if declared_size < 0 or declared_size > _MAX_FILE_BYTES:
                        raise RuntimeError(
                            f"GitHub restore file exceeds {_MAX_FILE_BYTES} bytes: {rel}"
                        )
                    declared_total += declared_size
                    if declared_total > _MAX_RESTORE_TOTAL_BYTES:
                        raise RuntimeError(
                            "GitHub restore exceeds the total decoded-byte limit"
                        )
                    target_by_rel[rel] = target

                expected_manifest: dict[str, dict[str, Any]] | None = None
                if backup_manifest.get("present"):
                    expected_manifest = self._validate_restore_manifest(
                        manifest_entries, target_by_rel
                    )
                    manifest_total = sum(
                        item["bytes"] for item in expected_manifest.values()
                    )
                    if (
                        backup_manifest.get("schema_version") != 1
                        or backup_manifest.get("file_count") != len(expected_manifest)
                        or backup_manifest.get("total_bytes") != manifest_total
                    ):
                        raise RuntimeError("backup manifest summary is inconsistent")
                    # Git Trees are cumulative; files deleted locally can remain
                    # in an older base tree.  A valid manifest is the canonical
                    # file set, so ignore stale remote files outside it.
                    target_by_rel = {
                        rel: target_by_rel[rel] for rel in expected_manifest
                    }
                    targets = list(target_by_rel.values())
                if not targets:
                    return {"ok": True, "imported": 0, "skipped": 0,
                            "message": f"GitHub 仓库 {self.repo} 的 {prefix or '/'} 下没有可恢复的记忆文件",
                            "backup_manifest": backup_manifest}

                failure_context = {
                    "total": len(targets),
                    "backup_manifest": backup_manifest,
                }
                base = os.path.abspath(buckets_dir)
                imported = 0
                buckets_imported = 0
                sources_imported = 0
                you_restored = False
                them_restored = False
                skipped = 0
                errors: list[str] = []
                # 先把远端不可变 blob 全部下载到临时区并完成清单/证据校验。
                # 只有所有文件都通过，才安装证据；只有所有证据都安装成功，
                # 才开始覆盖 Markdown。这样 tree 顺序不会造成“桶已恢复但证据
                # 最后才失败”的悬空 source_refs。
                with tempfile.TemporaryDirectory(
                    prefix="ombre-github-restore-"
                ) as staging_dir:
                    staged: dict[str, str] = {}
                    restored_bytes = 0
                    for index, t in enumerate(targets):
                        rel = t["path"][len(prefix):]
                        if not rel:
                            continue
                        dest = os.path.abspath(os.path.join(base, rel))
                        if dest != base and not dest.startswith(base + os.sep):
                            raise RuntimeError(f"{rel}: restore path escapes the vault")
                        self._assert_safe_restore_destination(base, rel)
                        rb = await self._request(c, "GET", f"{_API}/repos/{self.repo}/git/blobs/{t['sha']}")
                        rb.raise_for_status()
                        bj = rb.json()
                        if bj.get("encoding") == "base64":
                            encoded = "".join(str(bj.get("content", "") or "").split())
                            data = base64.b64decode(encoded, validate=True)
                        else:
                            data = (bj.get("content", "") or "").encode("utf-8")
                        if len(data) > _MAX_FILE_BYTES:
                            raise RuntimeError(
                                f"decoded file exceeds {_MAX_FILE_BYTES} bytes"
                            )
                        restored_bytes += len(data)
                        if restored_bytes > _MAX_RESTORE_TOTAL_BYTES:
                            raise RuntimeError("restore exceeds total decoded-byte limit")
                        if expected_manifest is not None:
                            expected = expected_manifest[rel]
                            if (
                                len(data) != expected["bytes"]
                                or hashlib.sha256(data).hexdigest()
                                != expected["sha256"]
                            ):
                                raise RuntimeError("backup manifest integrity mismatch")
                        if _is_source_relative_path(rel):
                            filename = rel.rsplit("/", 1)[-1]
                            ref = filename[:-len(".source")]
                            if hashlib.sha256(data).hexdigest() != ref.removeprefix("src_"):
                                raise RuntimeError("source evidence integrity mismatch")
                            try:
                                data.decode("utf-8")
                            except UnicodeDecodeError as exc:
                                raise RuntimeError("source evidence is not UTF-8") from exc

                        stage_path = os.path.join(staging_dir, f"{index:05d}.blob")
                        with open(stage_path, "wb") as stage_handle:
                            stage_handle.write(data)
                        if rel in _SNAPSHOT_VALIDATORS:
                            validator, error_type, label = _SNAPSHOT_VALIDATORS[rel]
                            try:
                                validator(stage_path)
                            except error_type as exc:
                                raise RuntimeError(
                                    f"{label} backup snapshot is invalid"
                                ) from exc
                        staged[rel] = stage_path

                    referenced_sources: set[str] = set()
                    available_sources = {
                        rel.rsplit("/", 1)[-1][:-len(".source")]
                        for rel in staged
                        if _is_source_relative_path(rel)
                    }
                    for rel in sorted(staged):
                        if not rel.endswith(".md"):
                            continue
                        with open(staged[rel], "rb") as markdown_handle:
                            markdown = markdown_handle.read(_MAX_FILE_BYTES + 1)
                        if len(markdown) > _MAX_FILE_BYTES:
                            raise RuntimeError(f"{rel}: staged Markdown exceeds limit")
                        try:
                            referenced_sources.update(
                                referenced_source_ids_from_markdown(markdown)
                            )
                        except ValueError as exc:
                            raise RuntimeError(
                                f"{rel}: invalid source evidence references: {exc}"
                            ) from exc

                    missing_sources = sorted(
                        referenced_sources - available_sources
                    )
                    integrity_warning = ""
                    if missing_sources:
                        preview = ", ".join(missing_sources[:3])
                        if backup_manifest.get(_SOURCE_REFS_COMPLETE_FIELD) is True:
                            raise RuntimeError(
                                "GitHub backup declares complete source evidence but has "
                                f"dangling references: {preview}"
                            )
                        integrity_warning = (
                            f"备份中有 {len(missing_sources)} 个原文证据引用没有对应文件；"
                            "这通常来自 v2.10.0 及更早的 GitHub 备份，事件记忆仍已恢复，"
                            "但这些原文无法核对"
                        )

                    source_store = SourceStore(
                        base, max_bytes=self.max_source_bytes
                    )
                    for rel in sorted(staged):
                        if not _is_source_relative_path(rel):
                            continue
                        with open(staged[rel], "rb") as source_handle:
                            data = source_handle.read(_MAX_FILE_BYTES + 1)
                        if len(data) > _MAX_FILE_BYTES:
                            raise RuntimeError(f"{rel}: staged source exceeds limit")
                        ref = rel.rsplit("/", 1)[-1][:-len(".source")]
                        installed_ref = source_store.put(data.decode("utf-8"))
                        if installed_ref != ref:
                            raise RuntimeError(f"{rel}: source evidence reference mismatch")
                        imported += 1
                        sources_imported += 1

                    for rel in sorted(staged):
                        if _is_source_relative_path(rel):
                            continue
                        dest = os.path.abspath(os.path.join(base, rel))
                        try:
                            self._assert_safe_restore_destination(base, rel)
                            with open(staged[rel], "rb") as stage_handle:
                                data = stage_handle.read(_MAX_FILE_BYTES + 1)
                            if len(data) > _MAX_FILE_BYTES:
                                raise RuntimeError("staged file exceeds limit")
                            # _win_long_path 前缀绕开 Windows 260 字符 MAX_PATH。
                            dest_long = _win_long_path(dest)
                            os.makedirs(
                                _win_long_path(os.path.dirname(dest)), exist_ok=True
                            )
                            # 单个 Markdown 原子覆盖；证据已经全部安装成功。
                            temp_path = f"{dest}.{uuid.uuid4().hex}.tmp"
                            temp_long = _win_long_path(temp_path)
                            try:
                                with open(temp_long, "wb") as output_handle:
                                    output_handle.write(data)
                                    output_handle.flush()
                                    os.fsync(output_handle.fileno())
                                os.replace(temp_long, dest_long)
                            except Exception:
                                if os.path.exists(temp_long):
                                    try:
                                        os.remove(temp_long)
                                    except OSError:
                                        pass
                                raise
                            imported += 1
                            if rel == _YOU_RELATIVE_PATH:
                                you_restored = True
                            elif rel == _THEM_RELATIVE_PATH:
                                them_restored = True
                            elif rel.endswith(".md"):
                                buckets_imported += 1
                        except Exception as exc:
                            skipped += 1
                            errors.append(f"{rel}: {exc}")

                self.last_sync = _now_iso()
                restore_ok = skipped == 0
                self.last_status = "ok" if restore_ok else "error"
                result = {
                    "ok": restore_ok,
                    "imported": imported,
                    "buckets_imported": buckets_imported,
                    "sources_imported": sources_imported,
                    "skipped": skipped,
                    "total": len(targets),
                    "truncated": False,
                    "error": errors[0] if errors else "",
                    "errors": errors[:10],
                    "backup_manifest": backup_manifest,
                    "integrity_warning": integrity_warning,
                }
                if you_restored:
                    result["you_restored"] = True
                if them_restored:
                    result["them_restored"] = True
                return result
        except Exception as e:
            logger.error(f"[github_sync] import failed: {e}")
            self.last_status = "error"
            self.last_error = str(e)
            if failure_context:
                total = int(failure_context["total"])
                return {
                    "ok": False,
                    "imported": 0,
                    "buckets_imported": 0,
                    "sources_imported": 0,
                    "skipped": total,
                    "total": total,
                    "truncated": False,
                    "error": str(e)[:500],
                    "errors": [str(e)[:500]],
                    "backup_manifest": failure_context["backup_manifest"],
                }
            return {"ok": False, "error": str(e)}

    async def validate(self) -> dict[str, Any]:
        """验证 token + repo 可访问，且具有写权限（contents: write）。"""
        try:
            async with httpx.AsyncClient(headers=self._headers, timeout=15.0) as c:
                r = await c.get(f"{_API}/repos/{self.repo}")
                if r.status_code == 404:
                    return {"ok": False, "error": f"仓库 {self.repo} 不存在或无权限访问"}
                if r.status_code == 401:
                    return {"ok": False, "error": "Token 无效或已过期"}
                r.raise_for_status()
                data = r.json()

                # Check write permission via `permissions.push` field
                # (GitHub returns this field when authenticated)
                perms = data.get("permissions", {})
                can_push = perms.get("push", False) or perms.get("admin", False)
                if perms and not can_push:
                    return {
                        "ok": False,
                        "error": "Token 只有读权限，无法上传文件。请在 GitHub → Settings → Developer settings → Fine-grained tokens 中将 Contents 权限设为 Read and write",
                    }

                self.is_validated = True
                return {
                    "ok": True,
                    "repo_full_name": data.get("full_name", self.repo),
                    "private": data.get("private", False),
                    "default_branch": data.get("default_branch", "main"),
                    "can_push": can_push,
                }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def status(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.token and self.repo),
            "repo": self.repo,
            "branch": self.branch,
            "path_prefix": self.path_prefix,
            "last_sync": self.last_sync,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "last_count": self.last_count,
            "is_validated": self.is_validated,
            "consecutive_failures": self.consecutive_failures,
        }

    # --------------------------------------------------------
    # 内部实现
    # --------------------------------------------------------

    def _collect_files(
        self,
        buckets_dir: str,
        extra_paths: Mapping[str, str] | None = None,
    ) -> Mapping[str, bytes]:
        """Index eligible memory/evidence paths without retaining their bodies.

        extra_paths 是 vault 之外的、已单独把过关的路径（目前只有 You 的事务
        快照）。它们直接并进同一份索引，而不是再套一层 Mapping：叠加层的
        __len__ 每次都要把两边的键拼成 set 再 union，而 `if not files` 这类
        判断会踩到它。一份字典就够了。
        """
        paths: dict[str, str] = {}
        for relative, full in (extra_paths or {}).items():
            paths[_vetted_extra_backup_path(relative, full)] = full
        # 事务快照必须盖过 vault 里可能存在的同名活动文件。当前
        # _iter_backup_paths 只产 .md 和 .source，撞不上；这道防线是留给
        # 以后放宽白名单的人的——备份到写了一半的活动库是静默的坏。
        extra_relatives = frozenset(paths)
        if not os.path.isdir(buckets_dir):
            return _LazyMarkdownFiles(paths)
        base_real = os.path.realpath(buckets_dir)
        for full in _iter_backup_paths(buckets_dir):
            try:
                full_real = os.path.realpath(full)
                if os.path.commonpath((base_real, full_real)) != base_real:
                    logger.warning(
                        "[github_sync] skip path outside vault: %s", full
                    )
                    continue
                size = os.path.getsize(full)
                if size > _MAX_FILE_BYTES:
                    relative = os.path.relpath(full, buckets_dir).replace("\\", "/")
                    if _is_source_relative_path(relative):
                        raise RuntimeError(
                            f"GitHub backup source exceeds {_MAX_FILE_BYTES} bytes: {relative}"
                        )
                    logger.warning(
                        f"[github_sync] skip {os.path.basename(full)}: "
                        f"too large ({size} bytes)"
                    )
                    continue
                relative = os.path.relpath(full, buckets_dir).replace("\\", "/")
                if len(relative.encode("utf-8")) > _MAX_BACKUP_PATH_BYTES:
                    raise RuntimeError(
                        f"GitHub backup path exceeds {_MAX_BACKUP_PATH_BYTES} bytes: "
                        f"{relative[:200]}"
                    )
                if relative in extra_relatives:
                    continue
                if relative not in paths and len(paths) >= _MAX_BACKUP_FILES:
                    raise RuntimeError(
                        f"GitHub backup has more than {_MAX_BACKUP_FILES} files; "
                        "split or archive the vault before retrying"
                    )
                paths[relative] = full
            except OSError as e:
                logger.warning(f"[github_sync] skip {os.path.basename(full)}: {e}")
        return _LazyMarkdownFiles(paths)

    @staticmethod
    def _assert_safe_restore_destination(base: str, relative_path: str) -> None:
        """Reject symlink/junction parents before a restore write."""
        base_real = os.path.realpath(base)
        current = base
        parts = relative_path.replace("\\", "/").split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise RuntimeError("unsafe restore path components")
        for part in parts:
            current = os.path.join(current, part)
            if os.path.lexists(current):
                if os.path.islink(current):
                    raise RuntimeError("restore path contains a symbolic link")
                current_real = os.path.realpath(current)
                if os.path.commonpath((base_real, current_real)) != base_real:
                    raise RuntimeError("restore path resolves outside the vault")

    @staticmethod
    def _validate_restore_manifest(
        raw_entries: object,
        targets: Mapping[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        if not isinstance(raw_entries, list) or len(raw_entries) > len(targets):
            raise RuntimeError("backup manifest file set does not match GitHub tree")
        expected: dict[str, dict[str, Any]] = {}
        total = 0
        for item in raw_entries:
            if not isinstance(item, dict):
                raise RuntimeError("backup manifest entry must be an object")
            path = item.get("path")
            digest = item.get("sha256")
            try:
                size = int(item.get("bytes"))
            except (TypeError, ValueError, OverflowError) as exc:
                raise RuntimeError("backup manifest contains an invalid size") from exc
            if (
                not isinstance(path, str)
                or path not in targets
                or path in expected
                or len(path.encode("utf-8")) > _MAX_BACKUP_PATH_BYTES
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(ch not in "0123456789abcdefABCDEF" for ch in digest)
                or size < 0
                or size > _MAX_FILE_BYTES
            ):
                raise RuntimeError("backup manifest contains an invalid entry")
            total += size
            if total > _MAX_RESTORE_TOTAL_BYTES:
                raise RuntimeError("backup manifest exceeds total decoded-byte limit")
            expected[path] = {"bytes": size, "sha256": digest.lower()}
        return expected

    def _build_backup_manifest(self, files: Mapping[str, bytes]) -> dict[str, Any]:
        """Build a JSON-safe manifest for the markdown files in one sync."""
        if len(files) > _MAX_BACKUP_FILES:
            raise RuntimeError(
                f"GitHub backup has more than {_MAX_BACKUP_FILES} files; "
                "split or archive the vault before retrying"
            )
        entries = []
        total_bytes = 0
        available_sources = {
            str(path).rsplit("/", 1)[-1][:-len(".source")]
            for path in files
            if _is_source_relative_path(str(path))
        }
        referenced_sources: set[str] = set()
        # Sorting keys is cheap.  Sorting ``files.items()`` would force a lazy
        # mapping to materialize every body at once and reintroduce the OOM.
        for rel_path in sorted(files):
            if len(str(rel_path).encode("utf-8")) > _MAX_BACKUP_PATH_BYTES:
                raise RuntimeError(
                    f"GitHub backup path exceeds {_MAX_BACKUP_PATH_BYTES} bytes: "
                    f"{str(rel_path)[:200]}"
                )
            content = files[rel_path]
            size = len(content)
            if str(rel_path).endswith(".md"):
                try:
                    referenced_sources.update(
                        referenced_source_ids_from_markdown(content)
                    )
                except ValueError as exc:
                    raise RuntimeError(
                        f"GitHub backup cannot validate {rel_path}: {exc}"
                    ) from exc
            total_bytes += size
            entries.append({
                "path": rel_path,
                "bytes": size,
                "sha256": hashlib.sha256(content).hexdigest(),
            })
        missing_sources = sorted(referenced_sources - available_sources)
        if missing_sources:
            preview = ", ".join(missing_sources[:3])
            raise RuntimeError(
                "GitHub backup has dangling source evidence references: "
                f"{preview}"
            )
        return {
            "schema_version": 1,
            "source": "ombre-brain",
            _SOURCE_REFS_COMPLETE_FIELD: True,
            "generated_at": _now_iso(),
            "repo": self.repo,
            "branch": self.branch,
            "path_prefix": self.path_prefix,
            "file_count": len(entries),
            "total_bytes": total_bytes,
            "files": entries,
        }

    def _iter_file_chunks(
        self,
        files: Mapping[str, bytes],
        manifest: Mapping[str, Any],
    ) -> Iterator[list[tuple[str, bytes]]]:
        """Yield verified bodies bounded by both count and decoded byte size."""
        expected_items = manifest.get("files", [])
        if not isinstance(expected_items, list) or len(expected_items) != len(files):
            raise RuntimeError("GitHub backup manifest does not match the file index")
        chunk: list[tuple[str, bytes]] = []
        chunk_bytes = 0
        # The manifest is already sorted.  Iterating its entries directly avoids
        # materializing a second O(N) path→metadata dictionary.
        for expected_item in expected_items:
            if not isinstance(expected_item, Mapping) or not expected_item.get("path"):
                raise RuntimeError("GitHub backup manifest contains an invalid file entry")
            relative_path = str(expected_item["path"])
            try:
                content = files[relative_path]
            except KeyError as exc:
                raise RuntimeError(
                    f"GitHub backup source changed while being indexed: {relative_path}; retry"
                ) from exc
            if not isinstance(content, bytes):
                content = bytes(content)
            expected_size = int(expected_item.get("bytes", -1))
            if (
                expected_size != len(content)
                or str(expected_item.get("sha256") or "")
                != hashlib.sha256(content).hexdigest()
            ):
                raise RuntimeError(
                    f"GitHub backup source changed while being read: {relative_path}; retry"
                )
            if chunk and (
                len(chunk) >= _TREE_CHUNK
                or chunk_bytes + len(content) > _TREE_CHUNK_BYTES
            ):
                yield chunk
                chunk = []
                chunk_bytes = 0
            chunk.append((relative_path, content))
            chunk_bytes += len(content)
        if chunk:
            yield chunk

    async def _batch_commit(self, files: Mapping[str, bytes]) -> int:
        """用 Git Trees API 一次性提交所有文件，返回上传文件数。

        关键点：tree entry 直接内联 `content`（UTF-8 文本），由 GitHub 在建
        tree 时顺带创建 blob —— 几百个文件只需 1~N 个 /git/trees 请求，而不是
        每个文件一个 /git/blobs 请求。后者会瞬间打满 GitHub 的 *secondary rate
        limit*（返回 403），正是之前同步莫名 403 的根因。

        大批量时分块提交（每块 _TREE_CHUNK 个），块与块之间用 base_tree 串联，
        最后只打一个 commit。所有请求都带指数退避重试以应对偶发的二级限流。
        """
        async with httpx.AsyncClient(headers=self._headers, timeout=_TIMEOUT) as c:
            # 1. 获取 branch HEAD commit SHA。GitHub 空仓库没有任何 ref，会在这里返回 409。
            r = await self._request(c, "GET", f"{_API}/repos/{self.repo}/git/ref/heads/{self.branch}")
            bootstrap_branch = _is_empty_repo_response(r)
            head_sha: str | None = None
            base_tree_sha: str | None = None
            if r.status_code == 404:
                raise RuntimeError(f"分支 {self.branch} 不存在，请先在 GitHub 上创建该分支")
            if not bootstrap_branch:
                r.raise_for_status()
                head_sha = r.json()["object"]["sha"]

                # 2. 获取 HEAD commit 对应的 tree SHA
                r = await self._request(c, "GET", f"{_API}/repos/{self.repo}/git/commits/{head_sha}")
                r.raise_for_status()
                base_tree_sha = r.json()["tree"]["sha"]

            # 3. Build a hard-bounded manifest first.  Lazy production mappings
            # read one body at a time during this pass and retain metadata only.
            manifest_path = f"{self.path_prefix}/{_MANIFEST_FILENAME}" if self.path_prefix else _MANIFEST_FILENAME
            manifest = self._build_backup_manifest(files)
            manifest_content = json.dumps(
                manifest, ensure_ascii=False, indent=2, sort_keys=True
            )
            manifest_entry = {
                "path": manifest_path,
                "mode": "100644",
                "type": "blob",
                "content": manifest_content,
            }
            # Account for the outer Git Trees JSON escaping too.  Count/path
            # limits bound construction; this payload limit bounds the request.
            manifest_payload_bytes = len(json.dumps(
                {"tree": [manifest_entry]},
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8"))
            if manifest_payload_bytes > _MAX_MANIFEST_PAYLOAD_BYTES:
                raise RuntimeError(
                    "GitHub backup manifest exceeds the bounded request limit "
                    f"({_MAX_MANIFEST_PAYLOAD_BYTES} bytes); split or archive the vault"
                )

            # 4. Read/decode/upload one bounded file chunk at a time.
            cur_base = base_tree_sha
            for file_chunk in self._iter_file_chunks(files, manifest):
                tree_entries: list[dict[str, Any]] = []
                for rel_path, content in file_chunk:
                    gh_path = f"{self.path_prefix}/{rel_path}" if self.path_prefix else rel_path
                    try:
                        text = content.decode("utf-8")
                        entry = {"path": gh_path, "mode": "100644", "type": "blob", "content": text}
                    except UnicodeDecodeError:
                        rb = await self._request(
                            c, "POST", f"{_API}/repos/{self.repo}/git/blobs",
                            json={"content": base64.b64encode(content).decode(), "encoding": "base64"},
                        )
                        rb.raise_for_status()
                        blob_sha = rb.json()["sha"]
                        del rb
                        entry = {"path": gh_path, "mode": "100644", "type": "blob", "sha": blob_sha}
                    tree_entries.append(entry)
                tree_payload: dict[str, Any] = {"tree": tree_entries}
                if cur_base:
                    tree_payload["base_tree"] = cur_base
                r = await self._request(
                    c, "POST", f"{_API}/repos/{self.repo}/git/trees",
                    json=tree_payload,
                )
                r.raise_for_status()
                cur_base = r.json()["sha"]
                # httpx responses retain their originating request (including
                # its serialized JSON body).  Drop it before reading the next
                # chunk so two request bodies cannot overlap in memory.
                del r
                del tree_payload, tree_entries, file_chunk

            # Keep the manifest in its own bounded request.  Otherwise a large
            # manifest silently sits on top of the file-chunk byte budget.
            manifest_payload: dict[str, Any] = {"tree": [manifest_entry]}
            if cur_base:
                manifest_payload["base_tree"] = cur_base
            r = await self._request(
                c,
                "POST",
                f"{_API}/repos/{self.repo}/git/trees",
                json=manifest_payload,
            )
            r.raise_for_status()
            cur_base = r.json()["sha"]
            del r, manifest_payload, manifest_entry, manifest_content
            new_tree_sha = cur_base

            # 5. 创建 commit
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            r = await self._request(
                c, "POST", f"{_API}/repos/{self.repo}/git/commits",
                json={
                    "message": f"Ombre Brain sync — {now_str} ({len(files)} files)",
                    "tree": new_tree_sha,
                    "parents": [head_sha] if head_sha else [],
                },
            )
            r.raise_for_status()
            commit_sha: str = r.json()["sha"]

            # 6. 更新已有 branch ref；空仓库首次提交则创建 branch ref
            if bootstrap_branch:
                r = await self._request(
                    c, "POST", f"{_API}/repos/{self.repo}/git/refs",
                    json={"ref": f"refs/heads/{self.branch}", "sha": commit_sha},
                )
            else:
                r = await self._request(
                    c, "PATCH", f"{_API}/repos/{self.repo}/git/refs/heads/{self.branch}",
                    json={"sha": commit_sha, "force": False},
                )
            r.raise_for_status()

        return len(files)

    async def _read_backup_manifest_summary(
        self,
        client: httpx.AsyncClient,
        manifest_item: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            declared_size = int(manifest_item.get("size", -1))
            if declared_size < 0 or declared_size > _MAX_MANIFEST_PAYLOAD_BYTES:
                raise ValueError("backup manifest exceeds the decoded-byte limit")
            sha = manifest_item.get("sha", "")
            if not sha:
                return {"present": False}
            rb = await self._request(client, "GET", f"{_API}/repos/{self.repo}/git/blobs/{sha}")
            rb.raise_for_status()
            bj = rb.json()
            if bj.get("encoding") == "base64":
                encoded = bj.get("content", "")
                if not isinstance(encoded, str) or len(encoded) > _MAX_MANIFEST_BASE64_BYTES:
                    raise ValueError("backup manifest base64 payload is too large")
                compact = "".join(encoded.split())
                decoded = base64.b64decode(compact, validate=True)
                if len(decoded) > _MAX_MANIFEST_PAYLOAD_BYTES:
                    raise ValueError("backup manifest exceeds the decoded-byte limit")
                raw = decoded.decode("utf-8")
            else:
                raw = str(bj.get("content", "") or "")
                if (
                    len(raw) > _MAX_MANIFEST_PAYLOAD_BYTES
                    or len(raw.encode("utf-8")) > _MAX_MANIFEST_PAYLOAD_BYTES
                ):
                    raise ValueError("backup manifest exceeds the decoded-byte limit")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("backup manifest root must be an object")
            summary = {
                "present": True,
                "schema_version": data.get("schema_version"),
                "generated_at": data.get("generated_at", ""),
                "file_count": int(data.get("file_count") or 0),
                "total_bytes": int(data.get("total_bytes") or 0),
                "_entries": data.get("files"),
            }
            if data.get(_SOURCE_REFS_COMPLETE_FIELD) is True:
                summary[_SOURCE_REFS_COMPLETE_FIELD] = True
            return summary
        except Exception as e:
            return {"present": False, "error": str(e)[:200]}

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        json: dict | None = None,
        _max_retries: int = 4,
    ) -> httpx.Response:
        """带退避重试的请求。专治 GitHub 二级限流（403/429 + Retry-After）。

        普通 4xx（权限/404 等）直接返回交由上层 raise_for_status 处理，不重试。
        """
        for attempt in range(_max_retries + 1):
            resp = await client.request(method, url, json=json)
            if resp.status_code not in (403, 429):
                return resp
            # 判断是否二级限流（而非真正的权限 403）
            body_l = resp.text.lower()
            is_rate = (
                "rate limit" in body_l
                or "retry-after" in {k.lower() for k in resp.headers}
                or resp.headers.get("x-ratelimit-remaining") == "0"
            )
            if not is_rate or attempt == _max_retries:
                return resp
            # 计算等待时长：优先 Retry-After，其次指数退避
            retry_after = resp.headers.get("retry-after")
            if retry_after and retry_after.isdigit():
                wait = int(retry_after)
            else:
                wait = min(2 ** attempt, 30)
            logger.warning(f"[github_sync] secondary rate limit, retry in {wait}s (attempt {attempt + 1})")
            await asyncio.sleep(wait)
        return resp


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_empty_repo_response(resp: httpx.Response) -> bool:
    """GitHub returns 409 when refs are requested from a zero-commit repo."""
    if resp.status_code != 409:
        return False
    try:
        message = str(resp.json().get("message", ""))
    except Exception:
        message = resp.text
    return "empty" in message.lower()
