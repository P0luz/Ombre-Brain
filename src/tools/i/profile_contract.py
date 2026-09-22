"""Pure contract for Ombre Brain evidence-backed profile v1."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import re
from typing import Iterable


SCHEMA = "ombre-evidence-profile-v1"
VALID_ASPECTS = frozenset(
    {
        "nature",
        "values",
        "patterns",
        "limits",
        "becoming",
        "uncertainty",
        "stance",
    }
)
VALID_CALLERS = frozenset({"cheng", "huaiyin", "huaiyin_cc"})
ACTIVE_STATUS = "active"
TERMINAL_STATUSES = frozenset({"revoked", "invalidated", "superseded"})
VALID_STATUSES = frozenset({ACTIVE_STATUS, *TERMINAL_STATUSES})
MAX_CONTENT_CHARS = 4000
MAX_REFERENCE_CHARS = 512
MAX_REFERENCES = 12

_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#@+\-]{1,511}$")
_BUCKET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{2,127}$")


class ProfileContractError(ValueError):
    """The requested profile operation violates the v1 contract."""


@dataclass(frozen=True)
class ProfileDraft:
    caller: str
    content: str
    aspect: str
    confidence: float
    evidence_id: str
    source_bucket: str
    source_refs: tuple[str, ...]
    updated_at: str

    @property
    def all_evidence_refs(self) -> tuple[str, ...]:
        refs: list[str] = []
        if self.source_bucket:
            refs.append(f"bucket:{self.source_bucket}")
        if self.evidence_id:
            refs.append(self.evidence_id)
        refs.extend(self.source_refs)
        return tuple(dict.fromkeys(refs))

    @property
    def tags(self) -> tuple[str, ...]:
        return (
            "__i__",
            "profile_v1",
            f"aspect:{self.aspect}",
            f"owner:{self.caller}",
            "scope:self",
            "voice:self",
        )

    @property
    def metadata(self) -> dict:
        return {
            "profile_schema": SCHEMA,
            "status": ACTIVE_STATUS,
            "confidence": self.confidence,
            "updated_at": self.updated_at,
            "evidence_id": self.evidence_id,
            "source_bucket": self.source_bucket,
            "source_refs": list(self.source_refs),
        }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _clean_reference(value: str, *, label: str) -> str:
    ref = str(value or "").strip()
    if not ref:
        return ""
    if len(ref) > MAX_REFERENCE_CHARS or not _REFERENCE_RE.fullmatch(ref):
        raise ProfileContractError(f"{label} is not a valid evidence reference")
    return ref


def parse_source_refs(value: str | Iterable[str] | None) -> tuple[str, ...]:
    if value is None:
        raw: list[str] = []
    elif isinstance(value, str):
        raw = re.split(r"[,\r\n]+", value)
    else:
        raw = [str(item) for item in value]
    refs = [
        _clean_reference(item, label="source_refs")
        for item in raw
        if str(item).strip()
    ]
    refs = list(dict.fromkeys(refs))
    if len(refs) > MAX_REFERENCES:
        raise ProfileContractError(
            f"source_refs exceeds the {MAX_REFERENCES}-reference limit"
        )
    return tuple(refs)


def build_profile_draft(
    *,
    caller: str,
    content: str,
    aspect: str,
    confidence: float,
    evidence_id: str = "",
    source_bucket: str = "",
    source_refs: str | Iterable[str] | None = None,
    confirm_stable: bool = False,
    updated_at: str = "",
) -> ProfileDraft:
    caller = str(caller or "").strip().lower().replace("-", "_")
    if caller not in VALID_CALLERS:
        raise ProfileContractError("recognized caller is required")

    content = str(content or "").strip()
    if not content or len(content) > MAX_CONTENT_CHARS:
        raise ProfileContractError(
            f"content must contain 1-{MAX_CONTENT_CHARS} characters"
        )

    aspect = str(aspect or "").strip().lower()
    if aspect not in VALID_ASPECTS:
        raise ProfileContractError(
            "profile aspect must use the existing I-system dimensions"
        )

    try:
        confidence = float(confidence)
    except (TypeError, ValueError) as exc:
        raise ProfileContractError("confidence must be a number from 0 to 1") from exc
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise ProfileContractError("confidence must be a finite number from 0 to 1")

    evidence_id = _clean_reference(evidence_id, label="evidence_id")
    source_bucket = str(source_bucket or "").strip()
    if source_bucket and not _BUCKET_ID_RE.fullmatch(source_bucket):
        raise ProfileContractError("source_bucket is not a valid bucket id")
    parsed_refs = parse_source_refs(source_refs)
    if not evidence_id and not source_bucket and not parsed_refs:
        raise ProfileContractError(
            "at least one bucket, evidence_id, or source_refs value is required"
        )
    if not bool(confirm_stable):
        raise ProfileContractError(
            "confirm_stable=true is required; one-off impressions stay legacy/non-profile"
        )

    return ProfileDraft(
        caller=caller,
        content=content,
        aspect=aspect,
        confidence=confidence,
        evidence_id=evidence_id,
        source_bucket=source_bucket,
        source_refs=parsed_refs,
        updated_at=updated_at or _utc_now_iso(),
    )


def validate_transition(current_status: str, target_status: str, reason: str) -> str:
    current = str(current_status or ACTIVE_STATUS).strip().lower()
    target = str(target_status or "").strip().lower()
    if current not in VALID_STATUSES:
        raise ProfileContractError("stored profile status is invalid")
    if target not in TERMINAL_STATUSES:
        raise ProfileContractError(
            "profile transition target must be revoked, invalidated, or superseded"
        )
    if current != ACTIVE_STATUS and current != target:
        raise ProfileContractError("terminal profiles cannot transition again")
    if not str(reason or "").strip():
        raise ProfileContractError("a revocation/invalidation reason is required")
    return target


def is_active_profile(metadata: dict) -> bool:
    raw_updated = str(metadata.get("updated_at") or "").strip()
    try:
        parsed_updated = datetime.fromisoformat(raw_updated.replace("Z", "+00:00"))
        valid_updated = parsed_updated.tzinfo is not None
    except (TypeError, ValueError):
        valid_updated = False
    confidence = metadata.get("confidence")
    return (
        str(metadata.get("profile_schema") or "") == SCHEMA
        and str(metadata.get("status") or "").strip().lower() == ACTIVE_STATUS
        and valid_updated
        and not isinstance(confidence, bool)
        and isinstance(confidence, (int, float))
        and math.isfinite(float(confidence))
        and 0.0 <= float(confidence) <= 1.0
        and bool(
            str(metadata.get("evidence_id") or "").strip()
            or str(metadata.get("source_bucket") or "").strip()
            or [
                ref
                for ref in (metadata.get("source_refs") or [])
                if str(ref).strip()
            ]
        )
    )


def owners_of(metadata: dict) -> tuple[str, ...]:
    owners: list[str] = []
    for tag in metadata.get("tags") or []:
        value = str(tag or "").strip()
        if not value.lower().startswith("owner:"):
            continue
        owner = value.split(":", 1)[1].strip().lower().replace("-", "_")
        if owner and owner not in owners:
            owners.append(owner)
    return tuple(owners)


def validate_evidence_bucket(bucket: dict | None, caller: str) -> None:
    if not bucket:
        raise ProfileContractError("source_bucket does not exist")
    owners = owners_of(bucket.get("metadata") or {})
    if len(owners) != 1:
        raise ProfileContractError(
            "source_bucket must have exactly one owner; untagged/ambiguous is denied"
        )
    if owners[0] not in {caller, "shared", "shared_core"}:
        raise ProfileContractError("source_bucket is outside the caller's evidence scope")
