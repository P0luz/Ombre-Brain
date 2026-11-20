"""Bounded structured projection of automatic recall candidates."""

import json
import math

from .search import search_eligible_matches

MAX_QUERY_CHARS = 2000
MAX_CANDIDATES = 6
MAX_CONTENT_CHARS = 1000
# Reserve space in the 24 KiB response budget for the MCP/RPC envelope.
MAX_PAYLOAD_BYTES = 23 * 1024


def _text(value, limit):
    return value[:limit] if isinstance(value, str) else None


def _labels(value):
    if not isinstance(value, list):
        return []
    return [item[:64] for item in value[:12] if isinstance(item, str)]


def _number(value):
    if type(value) in (float, int) and abs(value) <= 1e12 and math.isfinite(value):
        return value
    return None


async def recall_candidates(query: str) -> dict:
    query = query.strip()[:MAX_QUERY_CHARS]
    payload = {"schema_version": 1, "candidates": []}
    if not query or "\x00" in query:
        return payload

    matches, _, _, _ = await search_eligible_matches(
        query, MAX_CANDIDATES, "automatic", include_archive=False, diagnostics=False
    )
    for bucket in matches:
        bucket_id = bucket.get("id")
        content = _text(bucket.get("content"), MAX_CONTENT_CHARS)
        if not isinstance(bucket_id, str) or not 0 < len(bucket_id) <= 128:
            continue
        if not content or not content.strip():
            continue
        metadata = bucket.get("metadata") or {}
        candidate = {
            "bucket_id": bucket_id,
            "content": content,
            "score": _number(bucket.get("score")),
            "created_at": _text(metadata.get("created"), 64),
            "updated_at": None,
            "domain": _labels(metadata.get("domain")),
            "tags": _labels(metadata.get("tags")),
            "importance": _number(metadata.get("importance")),
            "valence": _number(metadata.get("valence")),
            "arousal": _number(metadata.get("arousal")),
            "policy": {"silent_context": True, "speakable": False},
            "relations": {"corrects": [], "supersedes": []},
        }
        payload["candidates"].append(candidate)
        if len(json.dumps(payload, ensure_ascii=True)) > MAX_PAYLOAD_BYTES:
            payload["candidates"].pop()
            break
        if len(payload["candidates"]) == MAX_CANDIDATES:
            break
    return payload
