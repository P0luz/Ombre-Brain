"""Create-time local-origin receipts for Ombre Brain buckets.

The receipt answers only how a bucket first entered this local vault.  It is
not a lifecycle audit log and it never authorizes an operation.  Validation is
kept pure so every publication path can fail before creating Markdown.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ombrebrain.protocol.schemas import ActorKind


SCHEMA_VERSION = 1
FIELD_NAMES = frozenset(
    {"schema", "via", "actor_kind", "actor_principal", "surface"}
)
KNOWN_CALLERS = frozenset({"cheng", "huaiyin", "huaiyin_cc"})
MCP_VIA = frozenset({"hold", "grow", "import", "plan", "letter", "i"})
ALL_VIA = MCP_VIA | {"direct"}
ALL_SURFACES = frozenset(
    {"mcp", "web_dashboard", "import_transaction", "cli", "system"}
)
ALL_ACTOR_KINDS = frozenset(member.value for member in ActorKind)
ORIGIN_VIA_LABELS = {
    "hold": "经 hold 留下",
    "grow": "经 grow 整理留下",
    "import": "经 import 导入",
    "plan": "经 plan 登记",
    "letter": "经 letter 写下",
    "i": "经 I 留下",
    "direct": "由本地直接写入",
}


class FootprintOriginError(ValueError):
    """The supplied local-origin receipt is absent, malformed, or untrusted."""


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise FootprintOriginError(f"footprint_origin.{field} must be a string")
    if not value:
        raise FootprintOriginError(f"footprint_origin.{field} must not be empty")
    return value


def validate_origin(value: Any) -> dict[str, Any]:
    """Return a canonical receipt or fail closed.

    Extra keys and tuple combinations outside the frozen E1 allowlist are
    rejected.  ``bool`` is explicitly not accepted as schema ``1``.
    """

    if not isinstance(value, Mapping):
        raise FootprintOriginError("an explicit footprint_origin object is required")
    keys = frozenset(value.keys())
    if keys != FIELD_NAMES:
        missing = sorted(FIELD_NAMES - keys, key=str)
        extra = sorted(keys - FIELD_NAMES, key=str)
        detail = []
        if missing:
            detail.append("missing=" + ",".join(missing))
        if extra:
            detail.append("extra=" + ",".join(str(item) for item in extra))
        raise FootprintOriginError("invalid footprint_origin keys: " + " ".join(detail))

    schema = value["schema"]
    if isinstance(schema, bool) or not isinstance(schema, int) or schema != SCHEMA_VERSION:
        raise FootprintOriginError("footprint_origin.schema must be integer 1")
    via = _text(value["via"], "via")
    actor_kind = _text(value["actor_kind"], "actor_kind")
    principal = _text(value["actor_principal"], "actor_principal")
    surface = _text(value["surface"], "surface")
    if via not in ALL_VIA:
        raise FootprintOriginError(f"unknown footprint_origin.via: {via}")
    if actor_kind not in ALL_ACTOR_KINDS:
        raise FootprintOriginError(
            f"unknown footprint_origin.actor_kind: {actor_kind}"
        )
    if surface not in ALL_SURFACES:
        raise FootprintOriginError(f"unknown footprint_origin.surface: {surface}")

    allowed = False
    if surface == "mcp":
        allowed = (
            via in MCP_VIA
            and actor_kind == ActorKind.MCP_TOOL.value
            and principal in KNOWN_CALLERS
        )
    elif surface == "web_dashboard":
        allowed = (
            via == "letter"
            and actor_kind == ActorKind.WEB_DASHBOARD.value
            and principal == "human"
        )
    elif surface == "import_transaction":
        allowed = via == "import" and (
            (actor_kind == ActorKind.MCP_TOOL.value and principal in KNOWN_CALLERS)
            or (actor_kind == ActorKind.WEB_DASHBOARD.value and principal == "human")
            or (actor_kind == ActorKind.SYSTEM.value and principal == "system")
        )
    elif surface == "cli":
        allowed = (
            via == "direct"
            and actor_kind == ActorKind.USER.value
            and principal == "local_operator"
        )
    elif surface == "system":
        allowed = (
            via == "direct"
            and actor_kind == ActorKind.SYSTEM.value
            and principal == "system"
        )
    if not allowed:
        raise FootprintOriginError("footprint_origin tuple is not allowed")

    return {
        "schema": SCHEMA_VERSION,
        "via": via,
        "actor_kind": actor_kind,
        "actor_principal": principal,
        "surface": surface,
    }


def render_origin_line(value: Any) -> str:
    """Render one compact E2 line from a validated E1 receipt.

    The first projection deliberately exposes only the creation channel.  Actor
    principal and target owner stay out of ordinary breath/query text.
    """

    origin = validate_origin(value)
    return f"👣 来源：{ORIGIN_VIA_LABELS[origin['via']]}"


def mcp_origin(via: str, caller: str) -> dict[str, Any]:
    return validate_origin(
        {
            "schema": SCHEMA_VERSION,
            "via": str(via).strip().lower(),
            "actor_kind": ActorKind.MCP_TOOL.value,
            "actor_principal": str(caller).strip().lower().replace("-", "_"),
            "surface": "mcp",
        }
    )


def dashboard_letter_origin() -> dict[str, Any]:
    return validate_origin(
        {
            "schema": SCHEMA_VERSION,
            "via": "letter",
            "actor_kind": ActorKind.WEB_DASHBOARD.value,
            "actor_principal": "human",
            "surface": "web_dashboard",
        }
    )


def import_origin(actor_kind: str, actor_principal: str) -> dict[str, Any]:
    return validate_origin(
        {
            "schema": SCHEMA_VERSION,
            "via": "import",
            "actor_kind": actor_kind,
            "actor_principal": actor_principal,
            "surface": "import_transaction",
        }
    )


def cli_origin() -> dict[str, Any]:
    return validate_origin(
        {
            "schema": SCHEMA_VERSION,
            "via": "direct",
            "actor_kind": ActorKind.USER.value,
            "actor_principal": "local_operator",
            "surface": "cli",
        }
    )


def system_origin() -> dict[str, Any]:
    return validate_origin(
        {
            "schema": SCHEMA_VERSION,
            "via": "direct",
            "actor_kind": ActorKind.SYSTEM.value,
            "actor_principal": "system",
            "surface": "system",
        }
    )
