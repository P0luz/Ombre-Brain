from __future__ import annotations

from ombrebrain.acceptance import FormalAcceptanceHarness, LegacyCompatibilityContract, CompatibilitySnapshot


def test_default_compatibility_contract_pins_legacy_surfaces() -> None:
    contract = LegacyCompatibilityContract.default()

    assert contract.tool_names == (
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
    )
    assert "bucket_id" in contract.bucket_markdown_fields
    assert "content" in contract.bucket_markdown_fields
    assert "config" in contract.protected_surfaces
    assert "vector database" in contract.protected_surfaces
    assert "/api/config" in contract.dashboard_routes


def test_formal_acceptance_harness_accepts_matching_snapshot() -> None:
    harness = FormalAcceptanceHarness.default()
    snapshot = CompatibilitySnapshot.from_contract(harness.contract)

    report = harness.evaluate(snapshot)

    assert report.ok is True
    assert report.issue_count == 0
    assert report.to_dict()["contract"]["tool_count"] == 13
    assert report.to_dict()["snapshot"]["dashboard_route_count"] >= 3


def test_formal_acceptance_harness_reports_missing_tool_and_bucket_field() -> None:
    contract = LegacyCompatibilityContract.default()
    snapshot = CompatibilitySnapshot.from_contract(contract).without(
        tool_names=("breath",),
        bucket_markdown_fields=("content",),
    )

    report = FormalAcceptanceHarness(contract).evaluate(snapshot)

    assert report.ok is False
    assert {issue.code for issue in report.issues} == {"missing_tool_name", "missing_bucket_markdown_field"}
    assert any(issue.value == "breath" for issue in report.issues)
    assert any(issue.value == "content" for issue in report.issues)


def test_formal_acceptance_harness_is_read_only_for_runtime_state(tmp_path) -> None:
    from ombrebrain.app.legacy_runtime import LegacyRuntime

    runtime = LegacyRuntime.from_config({"buckets_dir": str(tmp_path / "buckets")})
    before = runtime.fabric.next_index()
    snapshot = CompatibilitySnapshot.from_contract(LegacyCompatibilityContract.default())

    report = FormalAcceptanceHarness.default().evaluate(snapshot)

    assert report.ok is True
    assert runtime.fabric.next_index() == before
