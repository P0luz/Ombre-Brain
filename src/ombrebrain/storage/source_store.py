"""不可变原文证据存储。

原文按 UTF-8 内容哈希写入 ``<vault>/_sources/src_<sha256>.source``。
文件不参与普通 Markdown 扫描、浮现或语义索引；从 2.10.1 起会随完整
本地/GitHub 备份迁移。

**谁会读它**（3.0.0 起）：

- **模型没有任何回读入口。** 3.0.0 删掉了 `source_read` / `source_attach` /
  `source_detach` / `source_restore`，原文层对 MCP 只写不读。ADR-0001 里
  「`source_read` 是唯一公开读取入口」那句已被取代，当前行为以
  `docs/INTERNALS.md` §3.3.1 为准。
- **进程内部仍有两个读者**，而且都不把原文交出去：`you/service.py` 与
  `them/service.py` 在 `_build_edges` 里读出依据桶的原文当 `protected_texts`，
  用来挡住「把依据的原文照抄成一条认识」。读进来只用于比对，不进任何返回值。

这一段写清楚是因为「没有公开读取入口」和「没有读者」是两件事：把后者当成
前者，会让人以为动这个文件不影响任何人。
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import threading
import weakref
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import frontmatter


SOURCE_REF_RE = re.compile(r"^src_[0-9a-f]{64}$")
MAX_SOURCE_REFS = 32
MAX_SOURCE_LINKS = 128
MAX_SOURCE_RANGES = 128
HARD_MAX_SOURCE_BYTES = 10 * 1024 * 1024


_PATH_LOCKS: weakref.WeakValueDictionary[str, threading.RLock] = (
    weakref.WeakValueDictionary()
)
_PATH_LOCKS_GUARD = threading.Lock()


def _path_lock(path: Path) -> threading.RLock:
    key = str(path.resolve(strict=False))
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _exclusive_publish_lock(path: Path) -> Iterator[None]:
    """Serialize the rare no-hardlink publish path across processes."""

    lock_path = path.with_name(f".{path.name}.lock")
    with _path_lock(path):
        with lock_path.open("a+b") as handle:
            if os.name == "nt":  # pragma: no branch - platform-specific
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - exercised in Linux CI/Docker
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def normalize_source_ranges(value: Any) -> list[list[int]]:
    """规范化为 1-based、闭区间、互不重叠的行范围。"""
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("source_ranges 必须是 [[起始行, 结束行], ...]")
    if len(value) > MAX_SOURCE_RANGES:
        raise ValueError(f"source_ranges 过多（{len(value)} > {MAX_SOURCE_RANGES}）")
    ranges: list[tuple[int, int]] = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError("source_ranges 每项必须包含起始行和结束行")
        if any(isinstance(value, bool) or not isinstance(value, int) for value in item):
            raise ValueError("source_ranges 行号必须是整数")
        start, end = item
        if start < 1 or end < start:
            raise ValueError("source_ranges 必须使用 1-based 闭区间，且结束行不小于起始行")
        ranges.append((start, end))
    ranges.sort()
    merged: list[list[int]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def normalize_source_refs(value: Any) -> list[dict[str, Any]]:
    """校验并去重桶 frontmatter 中的原文引用。"""
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("source_refs 必须是列表")
    if len(value) > MAX_SOURCE_REFS:
        raise ValueError(f"source_refs 过多（{len(value)} > {MAX_SOURCE_REFS}）")
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[tuple[int, int], ...]]] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("source_refs 每项必须是对象")
        ref = str(item.get("ref") or "").strip()
        if not SOURCE_REF_RE.fullmatch(ref):
            raise ValueError("source_refs 包含非法 ref")
        ranges = normalize_source_ranges(item.get("ranges"))
        key = (ref, tuple((pair[0], pair[1]) for pair in ranges))
        if key in seen:
            continue
        seen.add(key)
        normalized.append({"ref": ref, "ranges": ranges})
    return normalized


def normalize_source_links(value: Any) -> list[dict[str, Any]]:
    """Validate the persistent, stable-order source binding ledger."""
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("source_links 必须是列表")
    if len(value) > MAX_SOURCE_LINKS:
        raise ValueError(f"source_links 过多（{len(value)} > {MAX_SOURCE_LINKS}）")
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[tuple[int, int], ...]]] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("source_links 每项必须是对象")
        ref = str(item.get("ref") or "").strip()
        if not SOURCE_REF_RE.fullmatch(ref):
            raise ValueError("source_links 包含非法 ref")
        status = str(item.get("status") or "").strip().lower()
        if status not in {"active", "detached"}:
            raise ValueError("source_links status 必须是 active 或 detached")
        ranges = normalize_source_ranges(item.get("ranges"))
        key = (ref, tuple((pair[0], pair[1]) for pair in ranges))
        if key in seen:
            raise ValueError("source_links 包含重复绑定")
        seen.add(key)
        normalized.append({"ref": ref, "ranges": ranges, "status": status})
    if len([item for item in normalized if item["status"] == "active"]) > MAX_SOURCE_REFS:
        raise ValueError(f"活动 source_links 过多（>{MAX_SOURCE_REFS}）")
    return normalized


def source_links_from_metadata(metadata: Any) -> list[dict[str, Any]]:
    """Return links without mutating legacy metadata lacking ``source_links``."""
    metadata = metadata or {}
    if "source_links" in metadata and metadata.get("source_links") is not None:
        return normalize_source_links(metadata.get("source_links"))
    return [dict(item, status="active") for item in normalize_source_refs(metadata.get("source_refs") or [])]


def referenced_source_ids_from_metadata(metadata: Any) -> set[str]:
    """Validate and union both evidence metadata projections.

    ``source_links`` is the stable ledger while ``source_refs`` is its active
    compatibility projection, but old exports and hand-edited migrations can
    contain useful claims in either field.  Evidence closure must validate both
    claimed fields instead of silently trusting whichever one is preferred.
    """

    metadata = metadata or {}
    refs = normalize_source_refs(metadata.get("source_refs"))
    links = normalize_source_links(metadata.get("source_links"))
    return {item["ref"] for item in refs} | {item["ref"] for item in links}


def active_source_refs_from_links(links: Any) -> list[dict[str, Any]]:
    return [
        {"ref": item["ref"], "ranges": item["ranges"]}
        for item in normalize_source_links(links)
        if item["status"] == "active"
    ]


def append_source_links(metadata: Any, refs: Any) -> list[dict[str, Any]]:
    """Append ordinary hold/grow refs without reviving a detached binding."""
    links = source_links_from_metadata(metadata)
    for ref in normalize_source_refs(refs):
        key = (ref["ref"], tuple(tuple(pair) for pair in ref["ranges"]))
        if any((item["ref"], tuple(tuple(pair) for pair in item["ranges"])) == key for item in links):
            continue
        if len(links) >= MAX_SOURCE_LINKS:
            raise ValueError(f"source_links 过多（{len(links) + 1} > {MAX_SOURCE_LINKS}）")
        if len(active_source_refs_from_links(links)) >= MAX_SOURCE_REFS:
            raise ValueError(f"source_refs 过多（>{MAX_SOURCE_REFS}）")
        links.append({**ref, "status": "active"})
    return links


def referenced_source_ids_from_markdown(content: bytes | str) -> set[str]:
    """Return validated source IDs declared by one bucket Markdown document."""

    if isinstance(content, bytes) and b"source_refs" not in content and b"source_links" not in content:
        return set()
    if not isinstance(content, bytes) and "source_refs" not in str(content) and "source_links" not in str(content):
        return set()
    try:
        text = content.decode("utf-8") if isinstance(content, bytes) else str(content)
    except UnicodeDecodeError as exc:
        raise ValueError("包含 source_refs 的桶不是 UTF-8") from exc
    # Preserve legacy export compatibility for unrelated malformed Markdown;
    # only documents that claim evidence references need this stricter parse.
    try:
        post = frontmatter.loads(text)
    except Exception as exc:
        raise ValueError("无法解析包含 source_refs 的桶 frontmatter") from exc
    try:
        referenced = referenced_source_ids_from_metadata(post.metadata or {})
    except ValueError as exc:
        raise ValueError(f"桶包含非法 source_refs：{exc}") from exc
    return referenced


class SourceStore:
    """内容寻址的只增不改原文层。"""

    def __init__(self, vault_dir: str | Path, max_bytes: int = 2 * 1024 * 1024):
        self.root = Path(vault_dir).resolve() / "_sources"
        self.max_bytes = max(0, int(max_bytes))

    @property
    def effective_max_bytes(self) -> int:
        configured = self.max_bytes or HARD_MAX_SOURCE_BYTES
        return min(configured, HARD_MAX_SOURCE_BYTES)

    @staticmethod
    def _read_bounded(path: Path, limit: int) -> bytes:
        if path.is_symlink():
            raise OSError("原文证据文件不允许为符号链接")
        declared_size = path.stat().st_size
        if declared_size > limit:
            raise OSError("原文证据文件超过配置的读取上限")
        with path.open("rb") as handle:
            raw = handle.read(limit + 1)
        if len(raw) > limit or len(raw) != declared_size:
            raise OSError("原文证据文件在读取期间发生变化")
        return raw

    def put(self, content: str) -> str:
        raw = str(content).encode("utf-8")
        if not raw:
            raise ValueError("原文为空")
        limit = self.effective_max_bytes
        if len(raw) > limit:
            raise ValueError(
                f"原文过大（{len(raw) / 1024:.1f} KB > 上限 {limit / 1024:.0f} KB）"
            )
        digest = hashlib.sha256(raw).hexdigest()
        ref = f"src_{digest}"
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f"{ref}.source"
        if target.exists():
            if self._read_bounded(target, limit) != raw:
                raise OSError("原文哈希冲突或现有证据文件已损坏")
            return ref

        fd, temp_name = tempfile.mkstemp(prefix=".source-", dir=str(self.root))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temp_name, target)
            except FileExistsError:
                if self._read_bounded(target, limit) != raw:
                    raise OSError("原文哈希冲突或并发写入结果不一致")
            except OSError:
                # Some NAS/SMB/FUSE volumes do not implement hard links.  The
                # sidecar lease keeps ``os.replace`` from overwriting a
                # concurrently published immutable source.
                with _exclusive_publish_lock(target):
                    if target.exists():
                        if self._read_bounded(target, limit) != raw:
                            raise OSError("原文哈希冲突或现有证据文件已损坏")
                    else:
                        os.replace(temp_name, target)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
        return ref

    def read(self, ref: str) -> str:
        ref = str(ref).strip()
        if not SOURCE_REF_RE.fullmatch(ref):
            raise ValueError("非法 source_ref")
        target = self.root / f"{ref}.source"
        raw = self._read_bounded(target, self.effective_max_bytes)
        expected = ref.removeprefix("src_")
        if hashlib.sha256(raw).hexdigest() != expected:
            raise OSError("原文证据完整性校验失败")
        return raw.decode("utf-8")

    @staticmethod
    def select_ranges(content: str, ranges: list[list[int]]) -> str:
        normalized = normalize_source_ranges(ranges)
        if not normalized:
            return content
        lines = content.splitlines(keepends=True)
        selected: list[str] = []
        for start, end in normalized:
            if end > len(lines):
                raise ValueError("原文证据行范围超出实际内容")
            selected.extend(lines[start - 1 : end])
        return "".join(selected)
