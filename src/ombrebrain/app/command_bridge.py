from __future__ import annotations

from dataclasses import dataclass

from ombrebrain.app.execution import ExecutionEnvelope
from ombrebrain.domain.commands import CommandKind, CommandPlan, MemoryCommand, MemoryCommandRouter


@dataclass(frozen=True)
class LegacyCommandBridge:
    router: MemoryCommandRouter

    @classmethod
    def default(cls) -> "LegacyCommandBridge":
        return cls(MemoryCommandRouter.default())

    def plan_from_envelope(self, envelope: ExecutionEnvelope) -> CommandPlan:
        command = MemoryCommand.new(
            kind=_kind_from_envelope(envelope),
            payload=envelope.sanitized_payload(),
            actor_name=envelope.actor_name,
            source=f"{envelope.module}.{envelope.operation}",
        )
        return self.router.plan(command)


def _kind_from_envelope(envelope: ExecutionEnvelope) -> CommandKind:
    module = envelope.module.lower()
    operation = envelope.operation.lower()
    joined = f"{module}.{operation}"

    if "hold" in joined or "letter_write" in joined or joined.endswith(".i"):
        return CommandKind.HOLD
    if "breath" in joined or "search" in joined:
        return CommandKind.BREATH
    if "trace" in joined or "anchor" in joined or "release" in joined or "letter_lock_update" in joined:
        return CommandKind.TRACE
    if "decay" in joined:
        return CommandKind.DECAY
    if "import" in joined:
        return CommandKind.IMPORT
    if "migrate" in joined or "migration" in joined:
        return CommandKind.MIGRATE
    if "github" in joined or "sync" in joined:
        return CommandKind.SYNC
    if module.startswith("web."):
        return CommandKind.WEB_ROUTE
    return CommandKind.UNKNOWN
