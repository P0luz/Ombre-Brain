from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


@dataclass(frozen=True)
class LegacyCompatibilityContract:
    tool_names: tuple[str, ...]
    dashboard_routes: tuple[str, ...]
    bucket_markdown_fields: tuple[str, ...]
    protected_surfaces: tuple[str, ...]

    @classmethod
    def default(cls) -> "LegacyCompatibilityContract":
        return cls(
            tool_names=(
                "breath",
                "hold",
                "grow",
                "dream",
                "trace",
                "anchor",
                "release",
                "pulse",
                "plan",
                "letter_write",
                "letter_lock_update",
                "letter_read",
                "I",
            ),
            dashboard_routes=(
                "/",
                "/api/config",
                "/api/buckets",
                "/api/bucket/{id}",
                "/api/embedding/status",
                "/api/v3/debug/decisions",
            ),
            bucket_markdown_fields=(
                "bucket_id",
                "type",
                "content",
                "created",
                "last_active",
                "importance",
                "domain",
                "pinned",
                "anchor",
            ),
            protected_surfaces=(
                "config",
                "buckets",
                "vector database",
                "OAuth secrets",
                "deployment overrides",
                "MCP tool names",
                "Dashboard existing routes",
            ),
        )

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_names", _tuple(self.tool_names))
        object.__setattr__(self, "dashboard_routes", _tuple(self.dashboard_routes))
        object.__setattr__(self, "bucket_markdown_fields", _tuple(self.bucket_markdown_fields))
        object.__setattr__(self, "protected_surfaces", _tuple(self.protected_surfaces))

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_count": len(self.tool_names),
            "tool_names": list(self.tool_names),
            "dashboard_route_count": len(self.dashboard_routes),
            "dashboard_routes": list(self.dashboard_routes),
            "bucket_markdown_fields": list(self.bucket_markdown_fields),
            "protected_surfaces": list(self.protected_surfaces),
        }


@dataclass(frozen=True)
class CompatibilitySnapshot:
    tool_names: tuple[str, ...]
    dashboard_routes: tuple[str, ...]
    bucket_markdown_fields: tuple[str, ...]
    protected_surfaces: tuple[str, ...]

    @classmethod
    def from_contract(cls, contract: LegacyCompatibilityContract) -> "CompatibilitySnapshot":
        return cls(
            tool_names=contract.tool_names,
            dashboard_routes=contract.dashboard_routes,
            bucket_markdown_fields=contract.bucket_markdown_fields,
            protected_surfaces=contract.protected_surfaces,
        )

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_names", _tuple(self.tool_names))
        object.__setattr__(self, "dashboard_routes", _tuple(self.dashboard_routes))
        object.__setattr__(self, "bucket_markdown_fields", _tuple(self.bucket_markdown_fields))
        object.__setattr__(self, "protected_surfaces", _tuple(self.protected_surfaces))

    def without(
        self,
        *,
        tool_names: tuple[str, ...] = (),
        dashboard_routes: tuple[str, ...] = (),
        bucket_markdown_fields: tuple[str, ...] = (),
        protected_surfaces: tuple[str, ...] = (),
    ) -> "CompatibilitySnapshot":
        return CompatibilitySnapshot(
            tool_names=_minus(self.tool_names, tool_names),
            dashboard_routes=_minus(self.dashboard_routes, dashboard_routes),
            bucket_markdown_fields=_minus(self.bucket_markdown_fields, bucket_markdown_fields),
            protected_surfaces=_minus(self.protected_surfaces, protected_surfaces),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_count": len(self.tool_names),
            "tool_names": list(self.tool_names),
            "dashboard_route_count": len(self.dashboard_routes),
            "dashboard_routes": list(self.dashboard_routes),
            "bucket_markdown_fields": list(self.bucket_markdown_fields),
            "protected_surfaces": list(self.protected_surfaces),
        }


@dataclass(frozen=True)
class AcceptanceIssue:
    code: str
    message: str
    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", str(self.code))
        object.__setattr__(self, "message", str(self.message))
        object.__setattr__(self, "value", str(self.value))

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "value": self.value}


@dataclass(frozen=True)
class AcceptanceReport:
    ok: bool
    contract: LegacyCompatibilityContract
    snapshot: CompatibilitySnapshot
    issues: tuple[AcceptanceIssue, ...]

    @property
    def issue_count(self) -> int:
        return len(self.issues)

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(
            {
                "ok": self.ok,
                "issue_count": self.issue_count,
                "contract": self.contract.to_dict(),
                "snapshot": self.snapshot.to_dict(),
                "issues": [issue.to_dict() for issue in self.issues],
            }
        )


def _tuple(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(str(value) for value in values)


def _minus(values: tuple[str, ...], removed: tuple[str, ...]) -> tuple[str, ...]:
    removed_set = {str(value) for value in removed}
    return tuple(value for value in values if value not in removed_set)


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False, default=str))
