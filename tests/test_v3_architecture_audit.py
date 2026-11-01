from ombrebrain.architecture import (
    ArchitectureAuditor,
    ComponentDescriptor,
    ComponentGraph,
    SideEffectMode,
    default_architecture,
)


def test_default_v3_architecture_contract_passes() -> None:
    report = ArchitectureAuditor.default().audit(default_architecture())

    assert report.ok is True
    assert report.issue_count == 0


def test_architecture_audit_detects_duplicate_protected_write_owners() -> None:
    graph = ComponentGraph(
        (
            ComponentDescriptor(
                name="legacy.bucket_manager",
                layer="legacy",
                side_effect_mode=SideEffectMode.WRITE_LEGACY_STATE,
                owns_surfaces=("buckets",),
            ),
            ComponentDescriptor(
                name="rogue.writer",
                layer="legacy",
                side_effect_mode=SideEffectMode.WRITE_LEGACY_STATE,
                owns_surfaces=("buckets",),
            ),
        )
    )

    report = ArchitectureAuditor.default().audit(graph)

    assert report.ok is False
    assert any(issue.code == "duplicate_write_owner" for issue in report.issues)


def test_architecture_audit_detects_read_only_surface_ownership() -> None:
    graph = ComponentGraph(
        (
            ComponentDescriptor(
                name="decision.debug",
                layer="decision",
                side_effect_mode=SideEffectMode.READ_ONLY,
                owns_surfaces=("memory_fabric",),
            ),
        )
    )

    report = ArchitectureAuditor.default().audit(graph)

    assert report.ok is False
    assert any(issue.code == "read_only_owns_surface" for issue in report.issues)


def test_architecture_audit_detects_unknown_dependency_and_cycles() -> None:
    graph = ComponentGraph(
        (
            ComponentDescriptor(
                name="a",
                layer="x",
                side_effect_mode=SideEffectMode.AUDIT_ONLY,
                dependencies=("b", "missing"),
            ),
            ComponentDescriptor(
                name="b",
                layer="x",
                side_effect_mode=SideEffectMode.AUDIT_ONLY,
                dependencies=("a",),
            ),
        )
    )

    report = ArchitectureAuditor.default().audit(graph)

    assert report.ok is False
    assert any(issue.code == "unknown_dependency" for issue in report.issues)
    assert any(issue.code == "dependency_cycle" for issue in report.issues)


def test_architecture_audit_detects_missing_critical_components() -> None:
    graph = ComponentGraph(
        (
            ComponentDescriptor(
                name="protocol.schemas",
                layer="protocol",
                side_effect_mode=SideEffectMode.AUDIT_ONLY,
                critical=True,
            ),
        )
    )

    report = ArchitectureAuditor.default().audit(graph)

    assert report.ok is False
    assert any(issue.code == "missing_critical_component" for issue in report.issues)
