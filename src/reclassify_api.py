#!/usr/bin/env python3
"""Reclassify legacy ``dynamic/unclassified`` buckets.

The command changes metadata and location only. Stored memory content is read
and written verbatim. Configuration and the analyzer are resolved when the
command runs, not while the module is imported, so the workflow is testable and
safe to embed in a future desktop maintenance process.
"""

from __future__ import annotations

import asyncio
import math
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

import frontmatter

from dehydrator import Dehydrator
from snapshot_barrier import markdown_writer_turn
from utils import atomic_write_text, load_config


class Analyzer(Protocol):
    async def analyze(self, content: str) -> Mapping[str, Any]: ...


Emit = Callable[[str], Any]


def sanitize(name: object, *, fallback: str = "unnamed", limit: int = 80) -> str:
    cleaned = re.sub(r'[^\w\s\u4e00-\u9fff-]', "", str(name or ""), flags=re.UNICODE)
    return cleaned.strip()[:limit] or fallback


def _bucket_id(value: object, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", str(value or ""))[:64]
    return cleaned or sanitize(fallback, fallback="bucket", limit=64)


def _unit_float(value: object, default: float) -> float:
    try:
        numeric = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(numeric):
        return default
    return max(0.0, min(1.0, numeric))


def _string_list(value: object, *, limit: int, fallback: list[str]) -> list[str]:
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, (list, tuple)):
        candidates = list(value)
    else:
        candidates = []
    normalized = []
    for item in candidates:
        text = str(item or "").strip()
        if text and text not in normalized:
            normalized.append(text[:80])
        if len(normalized) >= limit:
            break
    return normalized or list(fallback)


def _target_path(
    dynamic_dir: Path,
    post: frontmatter.Post,
    source: Path,
    analysis: Mapping[str, Any],
) -> tuple[Path, dict[str, Any]]:
    old_name = str(post.metadata.get("name") or "")
    domains = _string_list(
        analysis.get("domain"),
        limit=3,
        fallback=["unclassified"],
    )
    tags = _string_list(analysis.get("tags"), limit=5, fallback=[])
    name = sanitize(
        analysis.get("suggested_name") or old_name,
        fallback=sanitize(old_name, fallback="unnamed", limit=80),
        limit=80,
    )
    identifier = _bucket_id(post.metadata.get("id"), source.stem)
    primary = sanitize(domains[0], fallback="unclassified", limit=80)
    filename = f"{name}_{identifier}.md" if name != identifier else f"{identifier}.md"
    metadata = {
        "domain": domains,
        "tags": tags,
        "name": name,
        "valence": _unit_float(analysis.get("valence"), 0.5),
        "arousal": _unit_float(analysis.get("arousal"), 0.3),
    }
    return dynamic_dir / primary / filename, metadata


async def reclassify(
    config: Mapping[str, Any] | None = None,
    *,
    analyzer: Analyzer | None = None,
    emit: Emit = print,
) -> dict[str, int]:
    """Reclassify every legacy bucket and return machine-readable counters."""

    resolved_config = dict(config or load_config())
    dynamic_dir = Path(str(resolved_config.get("buckets_dir") or "buckets")) / "dynamic"
    unclassified_dir = dynamic_dir / "unclassified"
    legacy_cn_dir = dynamic_dir / "未分类"
    source_dirs = [path for path in (unclassified_dir, legacy_cn_dir) if path.is_dir()]
    files = sorted(
        path
        for source_dir in source_dirs
        for path in source_dir.glob("*.md")
        if path.is_file()
    )
    summary = {"found": len(files), "updated": 0, "failed": 0, "skipped": 0}
    emit(f"Found {len(files)} unclassified bucket(s).")
    if not files:
        return summary

    active_analyzer = analyzer or Dehydrator(resolved_config)
    for source in files:
        try:
            post = frontmatter.load(source)
            original_content = post.content
            old_name = str(post.metadata.get("name") or "")
            analysis = await active_analyzer.analyze(
                f"{old_name}\n{original_content}"[:2000]
                if old_name
                else original_content[:2000]
            )
            if not isinstance(analysis, Mapping):
                raise ValueError("analyzer result must be an object")
            destination, metadata = _target_path(
                dynamic_dir,
                post,
                source,
                analysis,
            )
            for key, value in metadata.items():
                post.metadata[key] = value
            if post.content != original_content:
                raise RuntimeError("stored content changed before serialization")

            async with markdown_writer_turn(dynamic_dir.parent):
                if destination != source and destination.exists():
                    raise FileExistsError(
                        "destination already exists; source was left untouched: "
                        f"{destination}"
                    )
                destination.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(str(destination), frontmatter.dumps(post))
                if destination != source:
                    source.unlink()
            summary["updated"] += 1
            emit(f"OK {source.name} -> {destination.relative_to(dynamic_dir)}")
        except Exception as exc:
            summary["failed"] += 1
            emit(f"ERROR {source.name}: {exc}")

    for source_dir in source_dirs:
        try:
            source_dir.rmdir()
        except OSError:
            pass
    return summary


if __name__ == "__main__":
    asyncio.run(reclassify())
