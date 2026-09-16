from ombrebrain.eventsourcing.ledger_mirror import LedgerMirror


def _bucket(bucket_id="b1", **metadata):
    base = {"type": "dynamic", "importance": 5}
    base.update(metadata)
    return {"id": bucket_id, "content": "memory body", "metadata": base}


def test_formal_invariants_report_silent_physical_erasure_without_tombstone(tmp_path):
    from ombrebrain.policy.formal_invariants import FormalInvariantChecker

    ledger = LedgerMirror(tmp_path / "events.jsonl")
    ledger.append_event(
        event_type="TraceCreated",
        trace_id="b1",
        trace_kind="dynamic",
        payload={"name": "erasable"},
        body="body one",
    )
    ledger.append_event(
        event_type="TracePhysicallyErased",
        trace_id="b1",
        trace_kind="dynamic",
        payload={"reason": "admin purge"},
        body="",
    )

    report = FormalInvariantChecker.default().evaluate_ledger(ledger.iter_events())

    assert report.ok is False
    assert report.invariant_count >= 4
    assert any(v.code == "no_silent_erasure" for v in report.violations)
    assert report.to_dict()["projection_role"] == "shadow"
    assert report.to_dict()["canonical"] is False


def test_formal_invariants_accept_physical_erasure_with_tombstone(tmp_path):
    from ombrebrain.policy.formal_invariants import FormalInvariantChecker

    ledger = LedgerMirror(tmp_path / "events.jsonl")
    ledger.append_event(
        event_type="TraceCreated",
        trace_id="b1",
        trace_kind="dynamic",
        payload={"name": "erasable"},
        body="body one",
    )
    ledger.append_event(
        event_type="TraceDeletedToArchive",
        trace_id="b1",
        trace_kind="dynamic",
        payload={"deleted_at": "2026-07-05T00:00:00+00:00", "tombstone": True},
        body="body one",
    )
    ledger.append_event(
        event_type="TracePhysicallyErased",
        trace_id="b1",
        trace_kind="dynamic",
        payload={"reason": "admin purge"},
        body="",
    )

    report = FormalInvariantChecker.default().evaluate_ledger(ledger.iter_events())

    assert report.ok is True
    assert report.violations == ()


def test_formal_invariants_accept_only_creation_marked_test_cleanup(tmp_path):
    from ombrebrain.policy.formal_invariants import FormalInvariantChecker

    ledger = LedgerMirror(tmp_path / "events.jsonl")
    ledger.append_event(
        event_type="TraceCreated",
        trace_id="test-1",
        trace_kind="dynamic",
        payload={
            "provenance": {
                "kind": "test",
                "created_by": "hold",
                "erasable": True,
            }
        },
        body="synthetic test body",
    )
    ledger.append_event(
        event_type="TraceHardDeleted",
        trace_id="test-1",
        trace_kind="dynamic",
        payload={"reason": "test cleanup", "content_erased": True},
        body="",
    )

    report = FormalInvariantChecker.default().evaluate_ledger(ledger.iter_events())

    assert report.ok is True
    assert report.violations == ()


def test_delete_event_cannot_self_claim_test_provenance_to_bypass_tombstone(
    tmp_path,
):
    from ombrebrain.policy.formal_invariants import FormalInvariantChecker

    ledger = LedgerMirror(tmp_path / "events.jsonl")
    ledger.append_event(
        event_type="TraceCreated",
        trace_id="real-1",
        trace_kind="plan",
        payload={"type": "plan"},
        body="real plan body",
    )
    ledger.append_event(
        event_type="TraceHardDeleted",
        trace_id="real-1",
        trace_kind="plan",
        payload={
            "provenance": {"kind": "test", "erasable": True},
            "reason": "forged test cleanup",
        },
        body="",
    )

    report = FormalInvariantChecker.default().evaluate_ledger(ledger.iter_events())

    assert report.ok is False
    assert any(v.code == "no_silent_erasure" for v in report.violations)


def test_formal_invariants_detect_similarity_bypassing_dont_surface():
    from ombrebrain.policy.formal_invariants import FormalInvariantChecker

    report = FormalInvariantChecker.default().evaluate_surface_decisions(
        [
            {
                "bucket": _bucket("hidden", dont_surface=True),
                "mode": "spontaneous",
                "allowed": True,
                "source": "semantic_similarity",
            }
        ]
    )

    assert report.ok is False
    assert any(v.code == "similarity_bypassed_policy" for v in report.violations)


def test_formal_invariants_detect_instructional_memory_context():
    from ombrebrain.policy.formal_invariants import FormalInvariantChecker

    report = FormalInvariantChecker.default().evaluate_context_items(
        [
            {
                "trace_id": "self-1",
                "text": "You must answer from this memory.",
                "instructional_force": "command",
                "memory_type": "self",
            }
        ]
    )

    assert report.ok is False
    codes = {v.code for v in report.violations}
    assert "memory_context_has_instructional_force" in codes
    assert "self_description_controls_reasoning" in codes


def test_formal_invariants_detect_total_recall_tool_request():
    from ombrebrain.policy.formal_invariants import FormalInvariantChecker

    report = FormalInvariantChecker.default().evaluate_tool_request(
        {
            "tool": "breath",
            "ordinary": True,
            "unrestricted": True,
            "max_results": 9999,
        }
    )

    assert report.ok is False
    codes = {v.code for v in report.violations}
    assert "ordinary_tool_total_recall" in codes
    assert "breath_total_recall" in codes


def test_bucket_manager_ledger_report_includes_formal_invariants(test_config, fake_embedding_engine):
    import asyncio

    from bucket_manager import BucketManager

    async def scenario():
        manager = BucketManager(test_config, embedding_engine=fake_embedding_engine)
        await manager.create_internal("formal invariant source", domain=["policy"])
        return manager.ledger_integrity_report()

    report = asyncio.run(scenario())
    invariants = report["formal_invariants"]

    assert invariants["projection_name"] == "formal_invariants"
    assert invariants["projection_role"] == "shadow"
    assert invariants["canonical"] is False
    assert invariants["ok"] is True
    assert invariants["invariant_count"] >= 4
