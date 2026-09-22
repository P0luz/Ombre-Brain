"""Spawn-safe helpers for synthetic M-03 import transactions."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import traceback

import frontmatter

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def make_markdown(bucket_id, content, *, owner="cheng", **metadata):
    values = {
        "id": bucket_id,
        "name": metadata.pop("name", bucket_id),
        "type": metadata.pop("type", "dynamic"),
        "domain": metadata.pop("domain", ["m03"]),
        "importance": metadata.pop("importance", 5),
        "tags": metadata.pop("tags", [f"owner:{owner}"] if owner else []),
        **metadata,
    }
    return frontmatter.dumps(frontmatter.Post(content, **values)).encode()


def put_markdown(root: str | Path, leaf: str, raw: bytes) -> Path:
    path = Path(root) / "dynamic" / "m03" / leaf
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


class DiskManager:
    def __init__(self, root: str):
        self.base_dir = root
        self.config = {
            "buckets_dir": root,
            "limits": {"max_bucket_bytes": 50 * 1024, "max_pinned": 50},
        }

    async def list_all(self, include_archive=False, fresh=False):
        del fresh
        root = Path(self.base_dir)
        rows = []
        for path in root.rglob("*.md"):
            if ".import-transactions" in path.parts:
                continue
            if not include_archive and "archive" in path.parts:
                continue
            post = frontmatter.load(path)
            rows.append(
                {
                    "id": str(post.get("id") or ""),
                    "content": post.content,
                    "metadata": dict(post.metadata),
                }
            )
        return rows

    def _invalidate_bm25(self):
        return None


def apply_worker(
    root: str,
    candidate_kwargs: dict,
    ready,
    start,
    results,
) -> None:
    from import_transaction import ImportCandidate, apply_import_batch
    from ombrebrain.eventsourcing.footprint import import_origin

    ready.set()
    start.wait(30)
    try:
        result = asyncio.run(
            apply_import_batch(
                buckets_dir=root,
                bucket_manager=DiskManager(root),
                job_owner=candidate_kwargs.pop("job_owner"),
                candidates=[ImportCandidate(**candidate_kwargs)],
                footprint_origin=import_origin("system", "system"),
            )
        )
        results.put(
            {
                "ok": True,
                "target": result.imported_id_map,
                "history": result.history_id_map,
                "txid": result.txid,
            }
        )
    except BaseException:
        results.put({"ok": False, "traceback": traceback.format_exc()})


def crash_apply_worker(
    root: str,
    candidate_kwargs: dict,
    point: str,
    code: int,
) -> None:
    from import_transaction import ImportCandidate, apply_import_batch
    from ombrebrain.eventsourcing.footprint import import_origin

    def injector(actual: str) -> None:
        if actual == point:
            os._exit(code)

    asyncio.run(
        apply_import_batch(
            buckets_dir=root,
            bucket_manager=DiskManager(root),
            job_owner=candidate_kwargs.pop("job_owner"),
            candidates=[ImportCandidate(**candidate_kwargs)],
            footprint_origin=import_origin("system", "system"),
            fault_injector=injector,
        )
    )
