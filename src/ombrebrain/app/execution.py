from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import logging
from typing import Any

logger = logging.getLogger("ombrebrain.app.execution")


@dataclass(frozen=True)
class ExecutionEnvelope:
    module: str
    operation: str
    payload: Mapping[str, Any] | None = None
    actor_name: str = "legacy-runtime"
    source: str = "legacy"
    permissions: tuple[str, ...] = field(default_factory=tuple)
    required_permissions: tuple[str, ...] = field(default_factory=tuple)
    capability: str = ""
    writes_memory: bool = False
    protected_paths: tuple[str, ...] = field(default_factory=tuple)
    feature_flags: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "module", str(self.module).strip())
        object.__setattr__(self, "operation", str(self.operation).strip())
        object.__setattr__(self, "payload", dict(self.payload or {}))
        object.__setattr__(self, "actor_name", str(self.actor_name or "legacy-runtime"))
        object.__setattr__(self, "source", str(self.source or "legacy"))
        object.__setattr__(self, "permissions", tuple(str(item) for item in self.permissions))
        object.__setattr__(self, "required_permissions", tuple(str(item) for item in self.required_permissions))
        object.__setattr__(self, "capability", str(self.capability or ""))
        object.__setattr__(self, "protected_paths", tuple(str(item) for item in self.protected_paths))
        object.__setattr__(self, "feature_flags", tuple(str(item) for item in self.feature_flags))

    def sanitized_payload(self) -> dict[str, Any]:
        return _sanitize_payload(dict(self.payload or {}))


_SENSITIVE_PARTS = (
    "token",
    "secret",
    "password",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "session",
    "oauth",
)


def _sanitize_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            if _is_sensitive_key(key_str):
                sanitized[key_str] = "[REDACTED]"
            else:
                sanitized[key_str] = _sanitize_payload(item)
        return sanitized
    if isinstance(value, (list, tuple)):
        return [_sanitize_payload(item) for item in value]
    return _json_safe(value)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_PARTS)


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False, default=str))
