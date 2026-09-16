"""Per-request caller identity primitives for owner-safe local extensions.

Transport code is responsible for setting the caller from an authenticated
credential.  Tool code may read it, but must never infer an identity from
bucket contents or silently choose a default.
"""

from __future__ import annotations

import contextvars
import logging
from contextlib import contextmanager, nullcontext
from typing import Any, Mapping, Optional

from . import _runtime as rt


_CALLER: contextvars.ContextVar[str] = contextvars.ContextVar(
    "ombre_caller", default=""
)
_TRANSPORT: contextvars.ContextVar[str] = contextvars.ContextVar(
    "ombre_transport", default="local"
)
_CANONICAL = ("cheng", "huaiyin", "huaiyin_cc")
_DEFAULTS = {
    "enabled": True,
    "untagged": "allow",
    "shared_owner_values": ["shared", "shared_core"],
    "exclude_shared_values": ["shared_context", "shared_resource"],
    "known_owners": list(_CANONICAL),
    "allow_tags": ["breath:all"],
    "allow_bucket_ids": [],
}
_ALIASES = {
    "cheng": "cheng",
    "澄": "cheng",
    "huaiyin": "huaiyin",
    "怀音": "huaiyin",
    "huaiyin_cc": "huaiyin_cc",
    "huaiyin-cc": "huaiyin_cc",
    "怀音-cc": "huaiyin_cc",
    "怀音cc": "huaiyin_cc",
}
logger = logging.getLogger("ombre_brain.identity")


def _cfg() -> dict[str, Any]:
    configured: dict[str, Any] = {}
    try:
        raw = (rt.config or {}).get("identity_filter", {})
        if isinstance(raw, dict):
            configured = raw
    except Exception:
        configured = {}
    merged = dict(_DEFAULTS)
    merged.update({key: value for key, value in configured.items() if value is not None})
    return merged


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def normalize_caller(raw: Optional[str]) -> str:
    return _ALIASES.get(str(raw or "").strip().lower(), "")


def known_callers() -> tuple[str, ...]:
    return _CANONICAL


def is_enabled() -> bool:
    return bool(_cfg().get("enabled", True))


def known_owner_values() -> tuple[str, ...]:
    values = list(_CANONICAL)
    cfg = _cfg()
    for raw in [
        *_strings(cfg.get("shared_owner_values")),
        *_strings(cfg.get("exclude_shared_values")),
    ]:
        value = str(raw).strip().lower().replace("-", "_")
        if value and value not in values:
            values.append(value)
    return tuple(values)


def set_caller(raw: Optional[str]) -> str:
    caller = normalize_caller(raw)
    _CALLER.set(caller)
    return caller


def get_caller() -> str:
    return _CALLER.get()


def origin_for_mcp(via: str, caller: Optional[str] = None) -> dict:
    """Build an immutable E1 actor receipt from the authenticated caller."""
    from ombrebrain.eventsourcing.footprint import mcp_origin

    principal = normalize_caller(caller if caller is not None else get_caller())
    if not principal:
        raise ValueError("recognized MCP caller is required for footprint origin")
    if principal not in known_callers():
        raise ValueError("recognized MCP caller is required for footprint origin")
    return mcp_origin(via, principal)


@contextmanager
def caller_context(raw: Optional[str], *, transport: str = "local"):
    """Bind one caller for the current request and restore the prior value."""
    caller_token = _CALLER.set(normalize_caller(raw))
    transport_token = _TRANSPORT.set(str(transport or "local").strip().lower())
    try:
        yield
    finally:
        _TRANSPORT.reset(transport_token)
        _CALLER.reset(caller_token)


def get_transport() -> str:
    return _TRANSPORT.get()


def owners_of(metadata: dict | None) -> tuple[str, ...]:
    tags = (metadata or {}).get("tags") or []
    if isinstance(tags, str):
        tags = [part.strip() for part in tags.split(",")]
    if not isinstance(tags, (list, tuple, set, frozenset)):
        return ()
    owners: list[str] = []
    for tag in tags:
        text = str(tag or "").strip()
        if not text.lower().startswith("owner:"):
            continue
        owner = text.split(":", 1)[1].strip().lower().replace("-", "_")
        if owner and owner not in owners:
            owners.append(owner)
    return tuple(owners)


def owner_of(metadata: dict | None) -> str:
    owners = owners_of(metadata)
    return owners[0] if len(owners) == 1 else ""


def strict_owner_of(
    metadata: dict | None, *, allow_untagged: bool = False
) -> str:
    raw_tags = (metadata or {}).get("tags")
    if raw_tags is None:
        raw_tags = []
    if not isinstance(raw_tags, list):
        raise ValueError("bucket tags must be a list")
    for tag in raw_tags:
        if not isinstance(tag, str):
            raise ValueError("bucket tags must contain strings only")
        if tag.strip().lower().startswith("owner:") and not tag.split(":", 1)[1].strip():
            raise ValueError("bucket owner cannot be empty")
    owners = owners_of({"tags": raw_tags})
    if len(owners) > 1:
        raise ValueError("bucket contains multiple owner tags")
    if not owners:
        if allow_untagged:
            return ""
        raise ValueError("bucket owner is required")
    owner = owners[0]
    if owner not in known_owner_values():
        raise ValueError(f"unknown owner value: {owner}")
    return owner


def ensure_write_owner(tags: list, caller: Optional[str] = None) -> list[str]:
    """Canonicalize one owner and bind private ownership to the caller."""
    clean = [str(tag).strip() for tag in (tags or []) if str(tag).strip()]
    if any(
        tag.lower().startswith("owner:") and not tag.split(":", 1)[1].strip()
        for tag in clean
    ):
        raise ValueError("owner 标签不能为空")
    non_owner = [tag for tag in clean if not tag.lower().startswith("owner:")]
    owners = owners_of({"tags": clean})
    if len(owners) > 1:
        raise ValueError("一次写入不能包含多个不同 owner 标签")
    if owners and owners[0] not in known_owner_values():
        raise ValueError(f"未知 owner: {owners[0]}")

    principal = normalize_caller(caller if caller is not None else get_caller())
    owner = owners[0] if owners else ""
    if principal:
        if owner in _CANONICAL and owner != principal:
            raise ValueError("不能替其他本地身份写入私有记忆")
        owner = owner or principal
    elif get_transport() != "local":
        raise ValueError("远程 MCP 写入记忆必须绑定明确的本地身份；当前凭据仅可读取。")
    elif owner in _CANONICAL:
        raise ValueError("写入私有 owner 需要已认证 caller")

    result = list(dict.fromkeys(non_owner))
    if owner:
        result.append(f"owner:{owner}")
    return result


def strip_owner_tags(tags: Any) -> list[str]:
    """Discard owner claims emitted by an untrusted metadata model."""
    return [tag for tag in _strings(tags) if not tag.lower().startswith("owner:")]


def write_owner_receipt(tags: list | None = None) -> str:
    owner = (
        owner_of({"tags": tags})
        if tags is not None
        else normalize_caller(get_caller())
    )
    return f"owner:{owner}" if owner else "owner:未声明"


def owners_compatible(existing_metadata: dict | None, incoming_tags: list) -> bool:
    incoming = owner_of({"tags": incoming_tags or []})
    if not incoming:
        return True
    return owner_of(existing_metadata or {}) == incoming


def mutation_owner(metadata: dict | None, caller: Optional[str] = None) -> str:
    """Return the owner an authenticated caller may mutate.

    Callerless local execution retains the legacy mutation contract.  Remote
    MCP requests must bind a caller; authenticated mutation then requires one
    unambiguous owner and is limited to that caller's private buckets plus the
    configured shared owners.
    """
    principal = normalize_caller(caller if caller is not None else get_caller())
    if not principal:
        if get_transport() == "local":
            return ""
        raise ValueError(
            "远程 MCP 修改记忆必须绑定明确的本地身份；当前凭据仅可读取。"
        )
    try:
        owner = strict_owner_of(metadata or {})
    except ValueError as exc:
        raise ValueError(
            "已认证 caller 只能修改带有唯一有效 owner 门牌的记忆；本次未修改。"
        ) from exc
    shared = set(_strings(_cfg().get("shared_owner_values")))
    if owner == principal or owner in shared:
        return owner
    if owner in _CANONICAL:
        raise ValueError("不能修改其他本地身份的私有记忆；本次未修改。")
    raise ValueError(f"owner:{owner} 不允许当前 caller 修改；本次未修改。")


def admitted_mutation(bucket: dict, caller: Optional[str] = None) -> bool:
    """Boolean form used by bounded derived mutations such as time ripple."""
    try:
        mutation_owner((bucket or {}).get("metadata") or {}, caller)
    except ValueError:
        return False
    return True


def mutation_predicate(
    expected_owners: Mapping[str, str] | None = None,
    caller: Optional[str] = None,
):
    """Build a stable predicate for storage-layer rechecks under bucket locks."""
    principal = normalize_caller(caller if caller is not None else get_caller())
    transport = get_transport()
    expected = dict(expected_owners or {})

    def _predicate(bucket: dict) -> bool:
        bucket_id = str((bucket or {}).get("id") or "")
        with caller_context(principal, transport=transport):
            try:
                owner = mutation_owner((bucket or {}).get("metadata") or {})
            except ValueError:
                return False
        return bucket_id not in expected or owner == expected[bucket_id]

    return _predicate


@contextmanager
def manager_mutation_guard(
    manager: Any,
    expected_owners: Mapping[str, str] | None = None,
    caller: Optional[str] = None,
):
    """Install a locked storage guard when the concrete manager supports it."""
    factory = getattr(manager, "mutation_admission", None)
    guard = (
        factory(mutation_predicate(expected_owners, caller))
        if callable(factory)
        else nullcontext()
    )
    with guard:
        yield


def preserve_mutation_owner(
    existing_metadata: dict | None,
    requested_tags: list,
    caller: Optional[str] = None,
) -> list[str]:
    """Keep an authenticated mutation's existing owner immutable."""
    principal = normalize_caller(caller if caller is not None else get_caller())
    clean = [str(tag).strip() for tag in (requested_tags or []) if str(tag).strip()]
    if not principal:
        return clean

    owner = mutation_owner(existing_metadata or {}, principal)
    if any(
        tag.lower().startswith("owner:") and not tag.split(":", 1)[1].strip()
        for tag in clean
    ):
        raise ValueError("owner 标签不能为空；本次未修改。")
    requested_owners = owners_of({"tags": clean})
    if len(requested_owners) > 1:
        raise ValueError("一次修改不能包含多个不同 owner 标签；本次未修改。")
    if requested_owners and requested_owners[0] != owner:
        raise ValueError("trace 不能改变记忆的 owner 门牌；本次未修改。")
    non_owner = [tag for tag in clean if not tag.lower().startswith("owner:")]
    return [*dict.fromkeys(non_owner), f"owner:{owner}"]


def _explicitly_allowed(bucket: dict, cfg: dict[str, Any]) -> bool:
    bucket_id = str(bucket.get("id") or "")
    if bucket_id and bucket_id in set(_strings(cfg.get("allow_bucket_ids"))):
        return True
    tags = set(_strings((bucket.get("metadata") or {}).get("tags")))
    return bool(tags & set(_strings(cfg.get("allow_tags"))))


def admitted_default(bucket: dict, caller: Optional[str] = None) -> bool:
    principal = normalize_caller(caller if caller is not None else get_caller())
    cfg = _cfg()
    if not principal or not bool(cfg.get("enabled", True)):
        return True
    if _explicitly_allowed(bucket, cfg):
        return True
    owners = owners_of(bucket.get("metadata") or {})
    if len(owners) > 1:
        return False
    if not owners:
        return str(cfg.get("untagged", "allow")).lower() != "deny"
    owner = owners[0]
    return owner == principal or owner in set(_strings(cfg.get("shared_owner_values")))


def filter_default(
    buckets: list[dict], caller: Optional[str] = None
) -> list[dict]:
    principal = normalize_caller(caller if caller is not None else get_caller())
    if not principal or not bool(_cfg().get("enabled", True)):
        return buckets
    kept = [bucket for bucket in buckets if admitted_default(bucket, principal)]
    if len(kept) != len(buckets):
        logger.info(
            "identity filter: caller=%s buckets=%d->%d",
            principal,
            len(buckets),
            len(kept),
        )
    return kept


def attribution(metadata: dict | None, caller: Optional[str] = None) -> str:
    owner = owner_of(metadata or {})
    if not owner:
        return ""
    label = f" [owner:{owner}]"
    principal = normalize_caller(caller if caller is not None else get_caller())
    if principal and owner in _CANONICAL and owner != principal:
        label += " [非本线记忆]"
    return label
