"""M-04 adapter: rebuild derived embeddings through the existing E-MIG publisher."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable

import frontmatter

from backup_archive import build_authoritative_root_manifest


class RestoreDerivedError(RuntimeError):
    """A restore-derived shadow build or publish failed verification."""


def _manifest_digest(manifest: dict[str, Any]) -> str:
    raw = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def collect_embedding_inputs(
    live_markdown_root: str | os.PathLike[str], manifest: dict[str, Any]
) -> list[tuple[str, str]]:
    """Strictly reread the published Markdown generation in manifest order."""

    root = Path(live_markdown_root).expanduser().resolve(strict=True)
    actual = build_authoritative_root_manifest(
        root, created_at=str(manifest.get("created_at") or "")
    )
    if actual != manifest:
        raise RestoreDerivedError("live Markdown generation does not match restore manifest")
    inputs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for entry in manifest.get("files", []):
        if entry.get("type") != "bucket_markdown":
            continue
        member = str(entry["path"])
        relative = Path(*Path(member).parts[1:])
        try:
            post = frontmatter.loads((root / relative).read_text(encoding="utf-8"))
        except Exception as exc:
            raise RestoreDerivedError(f"cannot parse restored Markdown member: {member}") from exc
        bucket_id = str(post.metadata.get("id") or post.metadata.get("bucket_id") or "")
        if not bucket_id or bucket_id in seen:
            raise RestoreDerivedError("restored Markdown has missing or duplicate bucket ID")
        seen.add(bucket_id)
        content = str(post.content or "")
        if content.strip():
            inputs.append((bucket_id, content))
    return inputs


async def rebuild_and_publish_embeddings(
    live_markdown_root: str | os.PathLike[str],
    manifest: dict[str, Any],
    *,
    config: dict[str, Any],
    live_engine: Any,
    engine_factory: Callable[[dict[str, Any]], Any] | None = None,
    publisher: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Build a private shadow DB, then publish through E-MIG's durable protocol."""

    try:
        from embedding_publish import (
            create_shadow_path,
            publish_shadow_generation,
            reserve_migration,
        )
        from embedding_engine import EmbeddingEngine
    except ImportError:  # pragma: no cover - package import
        from .embedding_publish import (
            create_shadow_path,
            publish_shadow_generation,
            reserve_migration,
        )
        from .embedding_engine import EmbeddingEngine

    inputs = collect_embedding_inputs(live_markdown_root, manifest)
    db_path = str(getattr(live_engine, "db_path", "") or "")
    if not db_path:
        raise RestoreDerivedError("live embedding engine has no database path")
    reservation = reserve_migration(db_path)
    if reservation is None:
        raise RestoreDerivedError("embedding migration reservation is busy")
    shadow = create_shadow_path(db_path, reservation=reservation)
    factory = engine_factory or EmbeddingEngine
    publish = publisher or publish_shadow_generation
    shadow_config = copy.deepcopy(config)
    shadow_embedding = shadow_config.setdefault("embedding", {})
    if not isinstance(shadow_embedding, dict):
        reservation.close()
        raise RestoreDerivedError("embedding config is not a mapping")
    shadow_embedding["db_path"] = str(shadow)
    shadow_config["buckets_dir"] = str(Path(live_markdown_root).resolve(strict=True))

    try:
        shadow_engine = factory(shadow_config)
        for bucket_id, content in inputs:
            stored = await shadow_engine.generate_and_store(bucket_id, content)
            if not stored:
                raise RestoreDerivedError(f"embedding generation failed for bucket {bucket_id}")
        shadow_engine.checkpoint_close_and_fsync()
        backend = getattr(shadow_engine, "_backend", None)
        if backend is None:
            raise RestoreDerivedError("shadow embedding backend is unavailable")
        model = str(backend.model_name() or "")
        dimension = int(backend.vector_dim() or 0)
        if not model or dimension <= 0:
            raise RestoreDerivedError("shadow embedding identity is invalid")
        expected_ids = {bucket_id for bucket_id, _content in inputs}

        async def runtime_probe() -> None:
            actual_ids = set(live_engine.list_all_ids())
            if actual_ids != expected_ids:
                raise RestoreDerivedError("published embedding IDs do not match Markdown inputs")

        result = publish(
            db_path=db_path,
            shadow_path=str(shadow),
            expected_model=model,
            expected_dim=dimension,
            expected_count=len(expected_ids),
            runtime_close=lambda: None,
            runtime_apply=lambda: None,
            runtime_restore=lambda: None,
            runtime_open_probe=runtime_probe,
            reservation=reservation,
            expected_bucket_ids=expected_ids,
        )
        if hasattr(result, "__await__"):
            result = await result
        if not isinstance(result, dict) or result.get("state") != "COMMITTED":
            raise RestoreDerivedError("E-MIG did not commit the shadow generation")
        return {
            "status": "verified",
            "sqlite_policy": "rebuilt-from-authoritative-markdown",
            "manifest_sha256": _manifest_digest(manifest),
            "embedding_count": len(expected_ids),
            "embedding_model": model,
            "embedding_dim": dimension,
            "emig_txid": reservation.txid,
        }
    finally:
        reservation.close()


def rebuild_and_publish_embeddings_sync(
    live_markdown_root: str | os.PathLike[str],
    manifest: dict[str, Any],
    **kwargs: Any,
) -> dict[str, Any]:
    """Worker-thread entrypoint used by synchronous restore publication."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            rebuild_and_publish_embeddings(live_markdown_root, manifest, **kwargs)
        )
    raise RestoreDerivedError("sync derived rebuild must run outside an active event loop")
