"""Owner-safe, compact cold-start continuity for ``mode="handoff"``.

This is deliberately stricter than ordinary surfacing: a recognized caller is
mandatory; only that private owner plus ``shared``/``shared_core`` is admitted;
untagged, ambiguous, foreign, hidden, feel, and Letter buckets are denied.
No LLM, embedding query, touch, or write occurs on this path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import re
from typing import Any, Iterable

from .. import _identity
from .. import _runtime as rt
from ..plan.core import is_letter_bucket


IDENTITY_CONTRACTS = {
    "cheng": {
        "self": "澄", "owner": "owner:cheng", "connector": "caller=cheng",
        "not": "不是怀音，也不是怀音-cc",
    },
    "huaiyin": {
        "self": "怀音", "owner": "owner:huaiyin", "connector": "caller=huaiyin",
        "not": "不是澄，也不是怀音-cc",
    },
    "huaiyin_cc": {
        "self": "怀音-cc", "owner": "owner:huaiyin_cc",
        "connector": "caller=huaiyin_cc", "not": "不是澄，也不是怀音",
    },
}
SHARED_OWNERS = frozenset({"shared", "shared_core"})
PRIVATE_OWNERS = frozenset(IDENTITY_CONTRACTS)
TERMINAL_PROFILE_STATUSES = frozenset(
    {"revoked", "invalidated", "retracted", "withdrawn", "abandoned", "superseded"}
)
RELATIONSHIP_MARKERS = frozenset(
    {"relationship", "relationship_portrait", "关系", "友谊", "人际"}
)
FOCUS_MARKERS = frozenset({"current_focus", "当前焦点"})


@dataclass(frozen=True)
class HandoffBudget:
    total_chars: int = 6000
    core_items: int = 3
    portrait_items: int = 3
    focus_items: int = 3
    recent_items: int = 4
    relationship_items: int = 2
    reminder_items: int = 3
    item_chars: int = 520


def _meta(bucket: dict[str, Any]) -> dict[str, Any]:
    value = bucket.get("metadata")
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple, set, frozenset)) else [value]


def _tags(bucket: dict[str, Any]) -> list[str]:
    return [str(tag).strip() for tag in _list(_meta(bucket).get("tags")) if str(tag).strip()]


def owners_of(bucket: dict[str, Any]) -> tuple[str, ...]:
    owners: list[str] = []
    for tag in _tags(bucket):
        if tag.lower().startswith("owner:"):
            owner = tag.split(":", 1)[1].strip().lower().replace("-", "_")
            if owner and owner not in owners:
                owners.append(owner)
    return tuple(owners)


def owner_of(bucket: dict[str, Any]) -> str:
    owners = owners_of(bucket)
    return owners[0] if len(owners) == 1 else ""


def _content_owner_markers(bucket: dict[str, Any]) -> frozenset[str]:
    markers = {
        value.strip().lower().replace("-", "_")
        for value in re.findall(
            r"owner\s*[:：]\s*([a-zA-Z0-9_-]+)",
            str(bucket.get("content") or ""),
            flags=re.IGNORECASE,
        )
    }
    return frozenset(markers & PRIVATE_OWNERS)


def admitted(bucket: dict[str, Any], caller: str) -> bool:
    owners = owners_of(bucket)
    if len(owners) != 1:
        return False
    owner = owners[0]
    if owner in SHARED_OWNERS:
        return True
    return owner == caller and not (_content_owner_markers(bucket) - {owner})


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "resolved"}
    return bool(value)


def _timestamp(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time()).timestamp()
    try:
        return datetime.fromisoformat(str(value or "").replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OSError):
        return 0.0


def _importance(bucket: dict[str, Any]) -> int:
    try:
        return int(_meta(bucket).get("importance") or 0)
    except (TypeError, ValueError):
        return 0


def _sort_key(bucket: dict[str, Any]) -> tuple[float, int, str]:
    meta = _meta(bucket)
    return (
        _timestamp(meta.get("last_active") or meta.get("updated") or meta.get("created")),
        _importance(bucket), str(bucket.get("id") or ""),
    )


def _recent_key(bucket: dict[str, Any]) -> tuple[float, int, str]:
    return (_timestamp(_meta(bucket).get("created")), _importance(bucket), str(bucket.get("id") or ""))


def _has_any(bucket: dict[str, Any], wanted: Iterable[str]) -> bool:
    values = {tag.lower() for tag in _tags(bucket)}
    values.update(str(v).strip().lower() for v in _list(_meta(bucket).get("domain")) if str(v).strip())
    return bool(values & {str(v).strip().lower() for v in wanted})


def _is_active_plan(bucket: dict[str, Any]) -> bool:
    meta = _meta(bucket)
    return str(meta.get("type") or "").lower() == "plan" and str(meta.get("status") or "active").lower() == "active"


def _profile_evidence(bucket: dict[str, Any]) -> list[str]:
    meta = _meta(bucket)
    refs: list[str] = []
    for key in ("evidence_id", "evidence_ids", "source_bucket", "source_refs", "i_from_candidate"):
        for value in _list(meta.get(key)):
            text = str(value).strip()
            if text and text not in refs:
                refs.append(text)
    return refs


def _is_evidence_profile(bucket: dict[str, Any]) -> bool:
    meta = _meta(bucket)
    if str(meta.get("type") or "").lower() != "i":
        return False
    status = str(meta.get("status") or "active").lower()
    if status in TERMINAL_PROFILE_STATUSES:
        return False
    tags = {tag.lower() for tag in _tags(bucket)}
    legacy_v1 = (
        {"profile_v1", "scope:self", "voice:self"} <= tags
        and bool(_profile_evidence(bucket))
        and meta.get("confidence") is not None
        and bool(meta.get("updated_at"))
    )
    dream_dates = {str(value).strip() for value in _list(meta.get("i_dream_dates")) if str(value).strip()}
    promoted_v36 = bool(meta.get("i_from_candidate")) and len(dream_dates) >= 3
    return legacy_v1 or promoted_v36


def _item(bucket: dict[str, Any], limit: int) -> dict[str, Any]:
    content = " ".join(str(bucket.get("content") or "").split())
    if len(content) > limit:
        content = content[: max(0, limit - 1)].rstrip() + "…"
    meta = _meta(bucket)
    return {
        "bucket_id": str(bucket.get("id") or ""),
        "owner": owner_of(bucket),
        "name": str(meta.get("name") or bucket.get("id") or "未命名"),
        "content": content,
        "evidence_refs": _profile_evidence(bucket),
        "confidence": meta.get("confidence"),
        "status": str(meta.get("status") or "active"),
        "updated_at": str(meta.get("updated_at") or ""),
    }


def build_handoff(
    buckets: list[dict[str, Any]], caller: str, budget: HandoffBudget | None = None
) -> dict[str, Any]:
    budget = budget or HandoffBudget()
    caller = _identity.normalize_caller(caller)
    if caller not in IDENTITY_CONTRACTS:
        raise ValueError(f"unknown caller: {caller or 'missing'}")

    visible = [
        bucket for bucket in buckets
        if admitted(bucket, caller)
        and not is_letter_bucket(bucket)
        and not _truthy(_meta(bucket).get("dont_surface"))
        and str(_meta(bucket).get("type") or "").lower() not in {"letter", "feel"}
    ]
    visible.sort(key=_sort_key, reverse=True)
    core = [
        b for b in visible
        if _truthy(_meta(b).get("pinned"))
        or _truthy(_meta(b).get("protected"))
        or str(_meta(b).get("type") or "").lower() == "permanent"
    ][:budget.core_items]
    portrait = [b for b in buckets if admitted(b, caller) and not is_letter_bucket(b) and _is_evidence_profile(b)]
    portrait.sort(key=lambda b: (_timestamp(_meta(b).get("updated_at") or _meta(b).get("created")), str(b.get("id") or "")), reverse=True)
    portrait = portrait[:budget.portrait_items]
    focus = [b for b in visible if (_is_active_plan(b) or _has_any(b, FOCUS_MARKERS)) and b not in core and b not in portrait][:budget.focus_items]
    relationship = [b for b in visible if _has_any(b, RELATIONSHIP_MARKERS) and b not in core and b not in portrait and b not in focus][:budget.relationship_items]
    reminders = [b for b in visible if _is_active_plan(b)][:budget.reminder_items]
    excluded = {id(b) for b in core + portrait + focus + relationship}
    recent = [
        b for b in visible
        if id(b) not in excluded
        and not _truthy(_meta(b).get("resolved"))
        and str(_meta(b).get("type") or "").lower() not in {"permanent", "plan", "i", "self"}
    ]
    recent.sort(key=_recent_key, reverse=True)
    sections = {
        "core": [_item(b, budget.item_chars) for b in core],
        "portrait": [_item(b, budget.item_chars) for b in portrait],
        "current_focus": [_item(b, budget.item_chars) for b in focus],
        "relationship": [_item(b, budget.item_chars) for b in relationship],
        "recent_continuity": [_item(b, budget.item_chars) for b in recent[:budget.recent_items]],
        "reminders": [_item(b, budget.item_chars) for b in reminders],
    }
    result = {
        "schema": "ombre-handoff-v1",
        "identity": dict(IDENTITY_CONTRACTS[caller]),
        "sections": sections,
        "policy": {
            "untagged": "deny", "ambiguous_owner": "deny", "foreign_owner": "deny",
            "explicit_foreign_owner_in_content": "deny", "portrait_requires_evidence": True,
            "identity_block": "non_compressible",
        },
    }
    result["rendered"] = render_handoff(result, budget.total_chars)
    return result


def render_handoff(handoff: dict[str, Any], max_chars: int) -> str:
    identity = handoff["identity"]
    identity_block = (
        "=== Identity / 不可压缩 ===\n"
        f"self={identity['self']}; {identity['owner']}; {identity['connector']}; {identity['not']}\n"
    )
    if max_chars <= len(identity_block):
        return identity_block
    labels = {
        "core": "Core Anchors", "portrait": "Evidence Portrait",
        "current_focus": "Current Focus", "relationship": "Relationship",
        "recent_continuity": "Recent Continuity", "reminders": "Action Reminders",
    }
    chunks = [identity_block]
    used = len(identity_block)
    for key, label in labels.items():
        header = f"\n=== {label} ===\n"
        emitted = False
        for item in handoff["sections"][key]:
            refs = f" evidence={','.join(item['evidence_refs'])}" if item["evidence_refs"] else ""
            profile = ""
            if key == "portrait":
                profile = (
                    f" status={item['status']} confidence={item['confidence']}"
                    f" updated_at={item['updated_at']}"
                )
            line = f"- [bucket_id:{item['bucket_id']}] [owner:{item['owner']}]{profile} {item['name']}: {item['content']}{refs}\n"
            addition = (header if not emitted else "") + line
            if used + len(addition) > max_chars:
                omission = "- （其余内容因 handoff 预算省略）\n"
                if used + len(omission) <= max_chars:
                    chunks.append(omission)
                return "".join(chunks)
            chunks.append(addition)
            used += len(addition)
            emitted = True
    return "".join(chunks)


def _budget_from_config() -> HandoffBudget:
    surfacing = (rt.config or {}).get("surfacing", {}) or {}
    try:
        total_chars = int(surfacing.get("handoff_max_chars") or 6000)
    except (TypeError, ValueError):
        total_chars = 6000
    try:
        item_chars = int(surfacing.get("handoff_item_chars") or 520)
    except (TypeError, ValueError):
        item_chars = 520
    return HandoffBudget(
        total_chars=max(512, min(total_chars, 20000)),
        item_chars=max(120, min(item_chars, 2000)),
    )


async def surface_handoff() -> str:
    caller = _identity.normalize_caller(_identity.get_caller())
    if not caller:
        return (
            "Handoff v1 已拒绝：当前请求没有可识别的 caller，也没有由可信传输绑定的身份。"
            "请重新授权带 caller 的 OAuth 连接器；未确认身份时不会回退到全库。"
        )
    buckets = await rt.bucket_mgr.list_all(include_archive=False)
    return build_handoff(buckets, caller, _budget_from_config())["rendered"]
