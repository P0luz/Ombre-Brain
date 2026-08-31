from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from typing import Any, Mapping

from ombrebrain.domain.commands import CommandKind


class OrganTool(str, Enum):
    HOLD = "hold"
    GROW = "grow"
    BREATH = "breath"
    PULSE = "pulse"
    DREAM = "dream"
    TRACE = "trace"
    ANCHOR = "anchor"
    RELEASE = "release"
    I = "I"
    LETTER_WRITE = "letter_write"
    LETTER_LOCK_UPDATE = "letter_lock_update"
    LETTER_READ = "letter_read"
    PLAN = "plan"


class NeuralSubsystem(str, Enum):
    ENGRAM_ENCODING = "engram_encoding"
    CUE_DRIVEN_SURFACING = "cue_driven_surfacing"
    HOMEOSTATIC_MONITORING = "homeostatic_monitoring"
    OFFLINE_REPLAY = "offline_replay"
    RECONSOLIDATION = "reconsolidation"
    LANDMARK_NETWORK = "landmark_network"
    SELF_DESCRIPTION_MEMORY = "self_description_memory"
    ARTIFACT_TRACE = "artifact_trace"
    UNRESOLVED_TENSION_MEMORY = "unresolved_tension_memory"


class ToolRouteError(ValueError):
    pass


@dataclass(frozen=True)
class ToolScope:
    actor_name: str = "legacy-runtime"
    source: str = "mcp"
    permissions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "actor_name", str(self.actor_name or "legacy-runtime"))
        object.__setattr__(self, "source", str(self.source or "mcp"))
        object.__setattr__(self, "permissions", tuple(str(item) for item in self.permissions))

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor_name": self.actor_name,
            "source": self.source,
            "permissions": list(self.permissions),
        }


@dataclass(frozen=True)
class NeuralToolRoute:
    public_tool: str
    organ_tool: OrganTool
    subsystem: NeuralSubsystem
    command_kind: CommandKind
    writes_memory: bool
    may_drive_action: bool = False
    surface_budget: str = "none"
    policy_boundaries: tuple[str, ...] = ()
    capability_tags: tuple[str, ...] = ()
    scope: ToolScope = ToolScope()

    def __post_init__(self) -> None:
        object.__setattr__(self, "organ_tool", _coerce_enum(OrganTool, self.organ_tool))
        object.__setattr__(self, "subsystem", _coerce_enum(NeuralSubsystem, self.subsystem))
        object.__setattr__(self, "command_kind", _coerce_enum(CommandKind, self.command_kind))
        object.__setattr__(self, "policy_boundaries", tuple(str(item) for item in self.policy_boundaries))
        object.__setattr__(self, "capability_tags", tuple(str(item) for item in self.capability_tags))

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(
            {
                "public_tool": self.public_tool,
                "organ_tool": self.organ_tool.value,
                "subsystem": self.subsystem.value,
                "command_kind": self.command_kind.value,
                "writes_memory": self.writes_memory,
                "may_drive_action": self.may_drive_action,
                "surface_budget": self.surface_budget,
                "policy_boundaries": list(self.policy_boundaries),
                "capability_tags": list(self.capability_tags),
                "scope": self.scope.to_dict(),
            }
        )


@dataclass(frozen=True)
class NeuralToolRouter:
    forbidden_tools: tuple[str, ...] = ("total_recall", "dump_memory")

    @classmethod
    def default(cls) -> "NeuralToolRouter":
        return cls()

    def route(
        self,
        tool: str | OrganTool,
        *,
        scope: ToolScope | Mapping[str, Any] | None = None,
    ) -> NeuralToolRoute:
        normalized = _normalize_tool(tool)
        if normalized in self.forbidden_tools:
            raise ToolRouteError(f"forbidden neural tool route: {normalized}")
        try:
            public_tool, organ_tool = _PUBLIC_TO_ORGAN[normalized]
        except KeyError as exc:
            raise ToolRouteError(f"unknown public organ tool: {normalized}") from exc
        route_spec = _ROUTE_TABLE[organ_tool]
        return NeuralToolRoute(
            public_tool=public_tool,
            organ_tool=organ_tool,
            subsystem=route_spec["subsystem"],
            command_kind=route_spec["command_kind"],
            writes_memory=bool(route_spec["writes_memory"]),
            surface_budget=str(route_spec.get("surface_budget", "none")),
            policy_boundaries=tuple(route_spec["policy_boundaries"]),
            capability_tags=tuple(route_spec["capability_tags"]),
            scope=_coerce_scope(scope),
        )


_PUBLIC_TO_ORGAN: dict[str, tuple[str, OrganTool]] = {
    "hold": ("hold", OrganTool.HOLD),
    "grow": ("grow", OrganTool.GROW),
    "breath": ("breath", OrganTool.BREATH),
    "pulse": ("pulse", OrganTool.PULSE),
    "dream": ("dream", OrganTool.DREAM),
    "trace": ("trace", OrganTool.TRACE),
    "anchor": ("anchor", OrganTool.ANCHOR),
    "release": ("release", OrganTool.RELEASE),
    "i": ("I", OrganTool.I),
    "letter": ("letter_write", OrganTool.LETTER_WRITE),
    "letter_write": ("letter_write", OrganTool.LETTER_WRITE),
    "letter_lock_update": ("letter_lock_update", OrganTool.LETTER_LOCK_UPDATE),
    "letter_read": ("letter_read", OrganTool.LETTER_READ),
    "plan": ("plan", OrganTool.PLAN),
}

_ROUTE_TABLE: dict[OrganTool, dict[str, object]] = {
    OrganTool.HOLD: {
        "subsystem": NeuralSubsystem.ENGRAM_ENCODING,
        "command_kind": CommandKind.HOLD,
        "writes_memory": True,
        "policy_boundaries": ("encoding-boundary", "non-cognition-boundary"),
        "capability_tags": ("memory:write", "tools:hold"),
    },
    OrganTool.GROW: {
        "subsystem": NeuralSubsystem.ENGRAM_ENCODING,
        "command_kind": CommandKind.HOLD,
        "writes_memory": True,
        "policy_boundaries": ("encoding-boundary", "non-cognition-boundary"),
        "capability_tags": ("memory:write", "tools:grow"),
    },
    OrganTool.BREATH: {
        "subsystem": NeuralSubsystem.CUE_DRIVEN_SURFACING,
        "command_kind": CommandKind.BREATH,
        "writes_memory": False,
        "surface_budget": "normal",
        "policy_boundaries": ("surface-policy", "total-recall-denied"),
        "capability_tags": ("memory:read", "tools:breath"),
    },
    OrganTool.PULSE: {
        "subsystem": NeuralSubsystem.HOMEOSTATIC_MONITORING,
        "command_kind": CommandKind.BREATH,
        "writes_memory": False,
        "policy_boundaries": ("memory-system-state-only", "not-current-emotion"),
        "capability_tags": ("memory:read", "tools:pulse"),
    },
    OrganTool.DREAM: {
        "subsystem": NeuralSubsystem.OFFLINE_REPLAY,
        "command_kind": CommandKind.BREATH,
        "writes_memory": False,
        "policy_boundaries": ("sedimentation-only", "no-autonomous-goal"),
        "capability_tags": ("memory:read", "tools:dream"),
    },
    OrganTool.TRACE: {
        "subsystem": NeuralSubsystem.RECONSOLIDATION,
        "command_kind": CommandKind.TRACE,
        "writes_memory": True,
        "policy_boundaries": ("append-only-reconstruction", "original-trace-preserved"),
        "capability_tags": ("memory:write", "tools:trace"),
    },
    OrganTool.ANCHOR: {
        "subsystem": NeuralSubsystem.LANDMARK_NETWORK,
        "command_kind": CommandKind.TRACE,
        "writes_memory": True,
        "policy_boundaries": ("non-cognition-boundary", "landmark-limit"),
        "capability_tags": ("memory:write", "tools:anchor"),
    },
    OrganTool.RELEASE: {
        "subsystem": NeuralSubsystem.LANDMARK_NETWORK,
        "command_kind": CommandKind.TRACE,
        "writes_memory": True,
        "policy_boundaries": ("non-cognition-boundary", "landmark-release"),
        "capability_tags": ("memory:write", "tools:release"),
    },
    OrganTool.I: {
        "subsystem": NeuralSubsystem.SELF_DESCRIPTION_MEMORY,
        "command_kind": CommandKind.HOLD,
        "writes_memory": True,
        "policy_boundaries": ("non-cognition-boundary", "self-description-not-control"),
        "capability_tags": ("memory:write", "tools:i"),
    },
    OrganTool.LETTER_WRITE: {
        "subsystem": NeuralSubsystem.ARTIFACT_TRACE,
        "command_kind": CommandKind.HOLD,
        "writes_memory": True,
        "policy_boundaries": ("non-cognition-boundary", "raw-artifact-preserved"),
        "capability_tags": ("memory:write", "tools:letter_write"),
    },
    OrganTool.LETTER_READ: {
        "subsystem": NeuralSubsystem.ARTIFACT_TRACE,
        "command_kind": CommandKind.BREATH,
        "writes_memory": False,
        "policy_boundaries": ("non-cognition-boundary", "raw-artifact-preserved"),
        "capability_tags": ("memory:read", "tools:letter_read"),
    },
    OrganTool.LETTER_LOCK_UPDATE: {
        "subsystem": NeuralSubsystem.ARTIFACT_TRACE,
        "command_kind": CommandKind.TRACE,
        "writes_memory": True,
        "policy_boundaries": ("letter-lock-metadata-only", "raw-artifact-preserved"),
        "capability_tags": ("memory:write", "tools:letter_lock_update"),
    },
    OrganTool.PLAN: {
        "subsystem": NeuralSubsystem.UNRESOLVED_TENSION_MEMORY,
        "command_kind": CommandKind.HOLD,
        "writes_memory": True,
        "policy_boundaries": ("no-agency-boundary", "may-drive-action-false"),
        "capability_tags": ("memory:write", "tools:plan"),
    },
}


def _normalize_tool(tool: str | OrganTool) -> str:
    if isinstance(tool, OrganTool):
        return tool.value.lower()
    return str(tool or "").strip().lower().replace("-", "_")


def _coerce_scope(value: ToolScope | Mapping[str, Any] | None) -> ToolScope:
    if isinstance(value, ToolScope):
        return value
    if isinstance(value, Mapping):
        return ToolScope(
            actor_name=str(value.get("actor_name") or "legacy-runtime"),
            source=str(value.get("source") or "mcp"),
            permissions=tuple(value.get("permissions") or ()),
        )
    return ToolScope()


def _coerce_enum(enum_type: type[Enum], value: object) -> Enum:
    if isinstance(value, enum_type):
        return value
    return enum_type(str(value))


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False, default=str))
