from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace as dataclass_replace
from enum import Enum
import json
from typing import Any


class BoundaryStage(Enum):
    COMMAND = "command"
    POLICY_PREFLIGHT = "policy_preflight"
    EVENT_DERIVATION = "event_derivation"
    EVENT_POLICY_VALIDATION = "event_policy_validation"
    LEDGER_APPEND = "ledger_append"
    RECEIPT = "receipt"


@dataclass(frozen=True)
class CommandBoundaryReceipt:
    command_id: str
    command_kind: str
    stages: tuple[BoundaryStage, ...] = ()
    events: tuple[dict[str, Any], ...] = ()
    ledger_appended: bool = False
    policy_preflight_allowed: bool = True
    event_validation_allowed: bool = True
    adapter_direct_write: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "command_id", str(self.command_id))
        object.__setattr__(self, "command_kind", _normalize_command_kind(self.command_kind))
        object.__setattr__(self, "stages", tuple(_coerce_stage(stage) for stage in self.stages))
        object.__setattr__(self, "events", tuple(_json_safe_dict(event) for event in self.events))
        object.__setattr__(self, "ledger_appended", bool(self.ledger_appended))
        object.__setattr__(self, "policy_preflight_allowed", bool(self.policy_preflight_allowed))
        object.__setattr__(self, "event_validation_allowed", bool(self.event_validation_allowed))
        object.__setattr__(self, "adapter_direct_write", bool(self.adapter_direct_write))
        object.__setattr__(self, "metadata", _json_safe_dict(self.metadata))

    def replace(self, **changes: Any) -> "CommandBoundaryReceipt":
        return dataclass_replace(self, **changes)

    def with_stages(self, stages: tuple[BoundaryStage, ...]) -> "CommandBoundaryReceipt":
        return self.replace(stages=stages)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_kind": self.command_kind,
            "stages": [stage.value for stage in self.stages],
            "events": [dict(event) for event in self.events],
            "ledger_appended": self.ledger_appended,
            "policy_preflight_allowed": self.policy_preflight_allowed,
            "event_validation_allowed": self.event_validation_allowed,
            "adapter_direct_write": self.adapter_direct_write,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class CommandBoundaryIssue:
    code: str
    message: str
    command_id: str = ""
    command_kind: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", str(self.code))
        object.__setattr__(self, "message", str(self.message))
        object.__setattr__(self, "command_id", str(self.command_id))
        object.__setattr__(self, "command_kind", str(self.command_kind))
        object.__setattr__(self, "metadata", _json_safe_dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "command_id": self.command_id,
            "command_kind": self.command_kind,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class CommandBoundaryReport:
    receipts: tuple[str, ...]
    issues: tuple[CommandBoundaryIssue, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "receipts", tuple(str(receipt_id) for receipt_id in self.receipts))
        object.__setattr__(self, "issues", tuple(self.issues))

    @property
    def ok(self) -> bool:
        return not self.issues

    @property
    def receipt_count(self) -> int:
        return len(self.receipts)

    @property
    def issue_count(self) -> int:
        return len(self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "receipt_count": self.receipt_count,
            "issue_count": self.issue_count,
            "receipts": list(self.receipts),
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class AdvancedCommandBoundaryContract:
    mutating_command_kinds: frozenset[str]
    mutation_stages: tuple[BoundaryStage, ...]
    read_only_stages: tuple[BoundaryStage, ...]

    @classmethod
    def default(cls) -> "AdvancedCommandBoundaryContract":
        return cls(
            mutating_command_kinds=frozenset(
                {
                    "hold",
                    "grow",
                    "trace",
                    "decay",
                    "import",
                    "migrate",
                    "anchor",
                    "release",
                    "plan",
                    "letter",
                    "letter_write",
                    "letter_lock_update",
                    "i",
                    "create_trace",
                    "touch_trace",
                    "resolve_trace",
                    "suppress_trace",
                    "archive_trace",
                    "request_admin_erasure",
                    "admin_erasure_request",
                }
            ),
            mutation_stages=(
                BoundaryStage.COMMAND,
                BoundaryStage.POLICY_PREFLIGHT,
                BoundaryStage.EVENT_DERIVATION,
                BoundaryStage.EVENT_POLICY_VALIDATION,
                BoundaryStage.LEDGER_APPEND,
                BoundaryStage.RECEIPT,
            ),
            read_only_stages=(
                BoundaryStage.COMMAND,
                BoundaryStage.POLICY_PREFLIGHT,
                BoundaryStage.RECEIPT,
            ),
        )

    def evaluate_receipt(self, receipt: CommandBoundaryReceipt | Mapping[str, Any]) -> CommandBoundaryReport:
        normalized = _coerce_receipt(receipt)
        return CommandBoundaryReport(
            receipts=(normalized.command_id,),
            issues=tuple(self._evaluate(normalized)),
        )

    def evaluate_manifest(
        self,
        receipts: list[CommandBoundaryReceipt] | tuple[CommandBoundaryReceipt, ...],
    ) -> CommandBoundaryReport:
        receipt_ids: list[str] = []
        issues: list[CommandBoundaryIssue] = []
        for raw_receipt in receipts:
            receipt = _coerce_receipt(raw_receipt)
            receipt_ids.append(receipt.command_id)
            issues.extend(self._evaluate(receipt))
        return CommandBoundaryReport(receipts=tuple(receipt_ids), issues=tuple(issues))

    def _evaluate(self, receipt: CommandBoundaryReceipt) -> tuple[CommandBoundaryIssue, ...]:
        issues: list[CommandBoundaryIssue] = []
        mutating = self._is_mutation(receipt)
        required = self.mutation_stages if mutating else self.read_only_stages
        positions = _stage_positions(receipt.stages)

        if receipt.adapter_direct_write:
            issues.append(
                _issue(
                    "adapter_direct_memory_write",
                    "adapters must not directly mutate memory outside the command boundary",
                    receipt,
                )
            )

        for stage in required:
            if stage not in positions:
                issues.append(
                    _issue(
                        "missing_boundary_stage",
                        "command boundary receipt is missing a required stage",
                        receipt,
                        stage=stage.value,
                    )
                )

        if _order_invalid(required, positions):
            issues.append(
                _issue(
                    "boundary_stage_order_invalid",
                    "command boundary stages must follow command -> policy -> event -> ledger -> receipt order",
                    receipt,
                    expected=[stage.value for stage in required],
                    actual=[stage.value for stage in receipt.stages],
                )
            )

        if receipt.ledger_appended and not receipt.policy_preflight_allowed:
            issues.append(
                _issue(
                    "ledger_append_after_policy_denial",
                    "ledger append must not happen after policy preflight denial",
                    receipt,
                )
            )

        if mutating and not receipt.events:
            issues.append(
                _issue(
                    "mutation_without_events",
                    "mutating commands must derive explicit events",
                    receipt,
                )
            )

        if mutating and not receipt.ledger_appended:
            issues.append(
                _issue(
                    "mutation_without_ledger_append",
                    "mutating commands must append derived events to the ledger",
                    receipt,
                )
            )

        if receipt.ledger_appended and not _event_validation_before_ledger(receipt, positions):
            issues.append(
                _issue(
                    "ledger_append_without_event_policy_validation",
                    "ledger append must be preceded by successful event policy validation",
                    receipt,
                )
            )

        return tuple(issues)

    def _is_mutation(self, receipt: CommandBoundaryReceipt) -> bool:
        if receipt.command_kind in self.mutating_command_kinds:
            return True
        return bool(receipt.events or receipt.ledger_appended)


def _coerce_receipt(receipt: CommandBoundaryReceipt | Mapping[str, Any]) -> CommandBoundaryReceipt:
    if isinstance(receipt, CommandBoundaryReceipt):
        return receipt
    return CommandBoundaryReceipt(**dict(receipt))


def _coerce_stage(stage: BoundaryStage | str) -> BoundaryStage:
    if isinstance(stage, BoundaryStage):
        return stage
    return BoundaryStage(str(stage))


def _normalize_command_kind(value: object) -> str:
    if isinstance(value, Enum):
        value = value.value
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def _stage_positions(stages: tuple[BoundaryStage, ...]) -> dict[BoundaryStage, int]:
    positions: dict[BoundaryStage, int] = {}
    for index, stage in enumerate(stages):
        positions.setdefault(stage, index)
    return positions


def _order_invalid(required: tuple[BoundaryStage, ...], positions: dict[BoundaryStage, int]) -> bool:
    present = [stage for stage in required if stage in positions]
    return any(positions[left] > positions[right] for left, right in zip(present, present[1:]))


def _event_validation_before_ledger(
    receipt: CommandBoundaryReceipt,
    positions: dict[BoundaryStage, int],
) -> bool:
    validation = positions.get(BoundaryStage.EVENT_POLICY_VALIDATION)
    append = positions.get(BoundaryStage.LEDGER_APPEND)
    if validation is None or append is None:
        return False
    return receipt.event_validation_allowed and validation < append


def _issue(code: str, message: str, receipt: CommandBoundaryReceipt, **metadata: Any) -> CommandBoundaryIssue:
    return CommandBoundaryIssue(
        code=code,
        message=message,
        command_id=receipt.command_id,
        command_kind=receipt.command_kind,
        metadata=metadata,
    )


def _json_safe_dict(value: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(dict(value), ensure_ascii=False, allow_nan=False, default=str))
