"""Storage service for the evidence-backed I profile prototype."""

from __future__ import annotations

from datetime import datetime, timezone

from .profile_contract import (
    ProfileContractError,
    SCHEMA,
    build_profile_draft,
    is_active_profile,
    owners_of,
    validate_evidence_bucket,
    validate_transition,
)
from ..plan.core import is_letter_bucket
from .. import _identity


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def create_profile(
    bucket_mgr,
    *,
    caller: str,
    content: str,
    aspect: str,
    confidence: float,
    evidence_id: str = "",
    source_bucket: str = "",
    source_refs: str = "",
    confirm_stable: bool = False,
) -> dict:
    draft = build_profile_draft(
        caller=caller,
        content=content,
        aspect=aspect,
        confidence=confidence,
        evidence_id=evidence_id,
        source_bucket=source_bucket,
        source_refs=source_refs,
        confirm_stable=confirm_stable,
    )
    if draft.source_bucket:
        validate_evidence_bucket(
            await bucket_mgr.get(draft.source_bucket),
            draft.caller,
        )

    bucket_id = await bucket_mgr.create(
        content=draft.content,
        tags=list(draft.tags),
        importance=6,
        domain=["self"],
        valence=0.5,
        arousal=0.3,
        name=None,
        bucket_type="i",
        why_remembered="evidence-backed stable self-profile",
        weight=0.8,
        source_tool="I",
        footprint_origin=_identity.origin_for_mcp("i", draft.caller),
    )
    update_fields = {
        **draft.metadata,
        "dont_surface": True,
    }
    try:
        updated = await bucket_mgr.update(bucket_id, **update_fields)
    except Exception:
        await bucket_mgr.delete(bucket_id)
        raise
    if not updated:
        await bucket_mgr.delete(bucket_id)
        raise ProfileContractError(
            "profile metadata could not be committed; partial bucket rolled back"
        )
    return {
        "schema": SCHEMA,
        "bucket_id": bucket_id,
        "owner": draft.caller,
        "aspect": draft.aspect,
        "status": "active",
        "confidence": draft.confidence,
        "updated_at": draft.updated_at,
        "evidence_refs": list(draft.all_evidence_refs),
    }


async def transition_profile(
    bucket_mgr,
    *,
    caller: str,
    bucket_id: str,
    target_status: str,
    reason: str,
) -> dict:
    bucket = await bucket_mgr.get(str(bucket_id or "").strip())
    if not bucket:
        raise ProfileContractError("profile bucket does not exist")
    metadata = bucket.get("metadata") or {}
    owners = owners_of(metadata)
    if owners != (caller,):
        raise ProfileContractError("profile owner does not match caller")
    if str(metadata.get("profile_schema") or "") != SCHEMA:
        raise ProfileContractError("legacy I entries are not mutated by profile v1")
    status = validate_transition(metadata.get("status"), target_status, reason)
    updated_at = _utc_now_iso()
    updated = await bucket_mgr.update(
        bucket_id,
        status=status,
        updated_at=updated_at,
        revocation_reason=str(reason).strip(),
    )
    if not updated:
        raise ProfileContractError("profile status update failed")
    return {
        "schema": SCHEMA,
        "bucket_id": bucket_id,
        "owner": caller,
        "status": status,
        "updated_at": updated_at,
    }


async def list_profiles(
    bucket_mgr,
    *,
    caller: str,
    limit: int = 20,
    include_inactive: bool = False,
) -> list[dict]:
    if caller not in {"cheng", "huaiyin", "huaiyin_cc"}:
        raise ProfileContractError("recognized caller is required")
    limit = int(limit)
    if not 1 <= limit <= 50:
        raise ProfileContractError("limit must be between 1 and 50")
    buckets = await bucket_mgr.list_all(include_archive=False)
    profiles: list[dict] = []
    for bucket in buckets:
        # Letter identity wins over profile/type/tag disguises.
        if is_letter_bucket(bucket):
            continue
        metadata = bucket.get("metadata") or {}
        if str(metadata.get("profile_schema") or "") != SCHEMA:
            continue
        if owners_of(metadata) != (caller,):
            continue
        status = str(metadata.get("status") or "").strip().lower()
        if status == "active" and not is_active_profile(metadata):
            continue
        if status != "active" and not include_inactive:
            continue
        profiles.append(
            {
                "schema": SCHEMA,
                "bucket_id": str(bucket.get("id") or ""),
                "owner": caller,
                "aspect": next(
                    (
                        str(tag).split(":", 1)[1]
                        for tag in metadata.get("tags") or []
                        if str(tag).startswith("aspect:")
                    ),
                    "",
                ),
                "content": str(bucket.get("content") or ""),
                "status": status,
                "confidence": metadata.get("confidence"),
                "updated_at": metadata.get("updated_at"),
                "evidence_refs": [
                    ref
                    for ref in (
                        [f"bucket:{metadata.get('source_bucket')}"]
                        if metadata.get("source_bucket")
                        else []
                    )
                    + (
                        [str(metadata.get("evidence_id"))]
                        if metadata.get("evidence_id")
                        else []
                    )
                    + [
                        str(ref)
                        for ref in (metadata.get("source_refs") or [])
                        if str(ref).strip()
                    ]
                ],
            }
        )
    profiles.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return profiles[:limit]
