from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


class ToolExposure(Enum):
    NORMAL = "normal"
    RESTRICTED = "restricted"
    INTERNAL = "internal"


@dataclass(frozen=True)
class PublicToolSpec:
    name: str
    exposure: ToolExposure = ToolExposure.NORMAL
    requires_admin: bool = False
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", str(self.name))
        object.__setattr__(self, "exposure", _coerce_exposure(self.exposure))
        object.__setattr__(self, "requires_admin", bool(self.requires_admin))
        object.__setattr__(self, "metadata", dict(self.metadata))


@dataclass(frozen=True)
class PublicToolDecision:
    tool_name: str
    allowed: bool
    reason: str
    tool_class: str = ""
    exposure: ToolExposure = ToolExposure.NORMAL
    replacement: str = ""
    requires_admin: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_name", str(self.tool_name))
        object.__setattr__(self, "allowed", bool(self.allowed))
        object.__setattr__(self, "reason", str(self.reason))
        object.__setattr__(self, "tool_class", str(self.tool_class))
        object.__setattr__(self, "exposure", _coerce_exposure(self.exposure))
        object.__setattr__(self, "replacement", str(self.replacement))
        object.__setattr__(self, "requires_admin", bool(self.requires_admin))

    def to_dict(self) -> dict[str, object]:
        return {
            "tool_name": self.tool_name,
            "allowed": self.allowed,
            "reason": self.reason,
            "tool_class": self.tool_class,
            "exposure": self.exposure.value,
            "replacement": self.replacement,
            "requires_admin": self.requires_admin,
        }


@dataclass(frozen=True)
class PublicToolReport:
    decisions: tuple[PublicToolDecision, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "decisions", tuple(self.decisions))

    @property
    def ok(self) -> bool:
        return all(decision.allowed for decision in self.decisions)

    @property
    def tool_count(self) -> int:
        return len(self.decisions)

    @property
    def allowed_count(self) -> int:
        return sum(1 for decision in self.decisions if decision.allowed)

    @property
    def rejected_count(self) -> int:
        return sum(1 for decision in self.decisions if not decision.allowed)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "tool_count": self.tool_count,
            "allowed_count": self.allowed_count,
            "rejected_count": self.rejected_count,
            "decisions": [decision.to_dict() for decision in self.decisions],
        }


@dataclass(frozen=True)
class PublicToolDesignContract:
    normal_tools: frozenset[str]
    compatibility_public_names: dict[str, str]
    engineering_public_aliases: dict[str, str]
    restricted_tools: frozenset[str]
    forbidden_normal_tools: frozenset[str]

    @classmethod
    def default(cls) -> "PublicToolDesignContract":
        return cls(
            normal_tools=frozenset({"hold", "grow", "trace", "breath", "breath_search", "breath_advanced", "pulse", "dream", "anchor", "i", "you", "letter", "plan", "feel"}),
            compatibility_public_names={
                "release": "anchor",
                "letter_write": "letter",
                "letter_read": "letter",
                "letter_lock_update": "letter",
            },
            engineering_public_aliases={
                "remember": "hold/grow",
                "touch": "trace",
                "resolve": "trace",
                "suppress": "trace",
                "surface": "breath",
                "hippocampal_recall": "",
                "offline_consolidate": "",
                "update_memory_row": "",
            },
            restricted_tools=frozenset(
                {
                    "admin_erasure_request",
                    "admin_write_tombstone",
                    "rebuild_projection",
                    "verify_ledger",
                    "replay_ledger",
                }
            ),
            forbidden_normal_tools=frozenset(
                {
                    "delete",
                    "dump_all",
                    "remember",
                    "touch",
                    "resolve",
                    "suppress",
                    "surface",
                    "set_emotion",
                    "decide",
                    "update_user_profile",
                    "force_personality",
                }
            ),
        )

    def evaluate_tool(self, spec: PublicToolSpec | Mapping[str, object] | str) -> PublicToolDecision:
        tool = _coerce_spec(spec)
        key = _normalize_tool_key(tool.name)
        name = _display_tool_name(key)

        if not key:
            return self._decision(tool, name, False, "unknown public tool")

        if tool.exposure == ToolExposure.INTERNAL:
            return self._evaluate_internal(tool, key, name)

        if key in self.restricted_tools:
            if tool.exposure == ToolExposure.RESTRICTED and tool.requires_admin:
                return self._decision(tool, name, True, "allowed restricted admin tool", tool_class="restricted")
            return self._decision(tool, name, False, "restricted tool requires admin exposure")

        if tool.exposure == ToolExposure.RESTRICTED:
            return self._decision(tool, name, False, "unknown restricted tool")

        if key in self.normal_tools:
            return self._decision(tool, name, True, "allowed normal organ tool", tool_class="normal")

        if key in self.compatibility_public_names:
            return self._decision(
                tool,
                name,
                True,
                "legacy-compatible public name",
                tool_class="normal",
                replacement=self.compatibility_public_names[key],
            )

        if key in self.engineering_public_aliases:
            return self._decision(
                tool,
                name,
                False,
                "engineering name cannot be public MCP tool",
                replacement=self.engineering_public_aliases[key],
            )

        if key in self.forbidden_normal_tools:
            return self._decision(tool, name, False, "forbidden normal tool")

        return self._decision(tool, name, False, "unknown public tool")

    def evaluate_manifest(self, specs: list[PublicToolSpec] | tuple[PublicToolSpec, ...]) -> PublicToolReport:
        return PublicToolReport(tuple(self.evaluate_tool(spec) for spec in specs))

    def _evaluate_internal(self, tool: PublicToolSpec, key: str, name: str) -> PublicToolDecision:
        if key in self.engineering_public_aliases:
            return self._decision(tool, name, True, "allowed internal engineering label", tool_class="internal")
        if key in self.normal_tools:
            return self._decision(tool, name, True, "allowed normal organ tool", tool_class="normal")
        if key in self.restricted_tools and tool.requires_admin:
            return self._decision(tool, name, True, "allowed restricted admin tool", tool_class="restricted")
        if key in self.forbidden_normal_tools:
            return self._decision(tool, name, False, "forbidden normal tool")
        return self._decision(tool, name, False, "unknown internal tool label")

    def _decision(
        self,
        tool: PublicToolSpec,
        name: str,
        allowed: bool,
        reason: str,
        *,
        tool_class: str = "",
        replacement: str = "",
    ) -> PublicToolDecision:
        return PublicToolDecision(
            tool_name=name,
            allowed=allowed,
            reason=reason,
            tool_class=tool_class,
            exposure=tool.exposure,
            replacement=replacement,
            requires_admin=tool.requires_admin,
        )


def _coerce_spec(spec: PublicToolSpec | Mapping[str, object] | str) -> PublicToolSpec:
    if isinstance(spec, PublicToolSpec):
        return spec
    if isinstance(spec, Mapping):
        return PublicToolSpec(**dict(spec))
    return PublicToolSpec(name=str(spec))


def _coerce_exposure(value: object) -> ToolExposure:
    if isinstance(value, ToolExposure):
        return value
    return ToolExposure(str(value))


def _normalize_tool_key(name: str) -> str:
    normalized = str(name).strip().replace("-", "_").replace(" ", "_")
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    return normalized.lower()


def _display_tool_name(key: str) -> str:
    if key in {"i", "you"}:
        return key.title()
    return key
