from __future__ import annotations

from ombrebrain.architecture.contracts import ComponentDescriptor, ComponentGraph, SideEffectMode


def default_architecture() -> ComponentGraph:
    return ComponentGraph(
        (
            ComponentDescriptor(
                name="protocol.schemas",
                layer="protocol",
                side_effect_mode=SideEffectMode.AUDIT_ONLY,
                owns_surfaces=("event_schema",),
                critical=True,
            ),
            ComponentDescriptor(
                name="domain.commands",
                layer="domain",
                side_effect_mode=SideEffectMode.AUDIT_ONLY,
                dependencies=("protocol.schemas",),
                owns_surfaces=("command_plan",),
                critical=True,
            ),
            ComponentDescriptor(
                name="app.execution",
                layer="app",
                side_effect_mode=SideEffectMode.AUDIT_ONLY,
                dependencies=("protocol.schemas",),
                critical=True,
            ),
            ComponentDescriptor(
                name="projection.audit_runtime",
                layer="projection",
                side_effect_mode=SideEffectMode.AUDIT_ONLY,
                dependencies=("domain.commands",),
                owns_surfaces=("projection_journal", "projection_observations", "consistency_report"),
                critical=True,
            ),
            ComponentDescriptor(
                name="policy.engine",
                layer="policy",
                side_effect_mode=SideEffectMode.AUDIT_ONLY,
                dependencies=("domain.commands", "app.execution"),
                owns_surfaces=("policy_verdict", "capability_contract"),
                critical=True,
            ),
            ComponentDescriptor(
                name="acceptance.harness",
                layer="acceptance",
                side_effect_mode=SideEffectMode.READ_ONLY,
                critical=True,
            ),
            ComponentDescriptor(
                name="retrieval.engine",
                layer="retrieval",
                side_effect_mode=SideEffectMode.READ_ONLY,
                dependencies=("domain.commands",),
                critical=True,
            ),
        )
    )
