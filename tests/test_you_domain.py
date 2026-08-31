from dataclasses import replace
import sqlite3

import pytest

from ombrebrain.you import (
    EvidenceEdge,
    ReviewReceipt,
    Scope,
    YouClaim,
    YouStore,
    YouStoreError,
    contains_forbidden_subject,
    leaks_protected_text,
)
from ombrebrain.you.models import evidence_digest


def _edge(bucket_id: str = "memory-1") -> EvidenceEdge:
    return EvidenceEdge(
        bucket_id=bucket_id,
        source_id="src_" + "a" * 64,
        stance="supports",
        basis="explicit_statement",
        bucket_revision="sha256:" + "b" * 64,
    )


def _claim(scope: Scope, *, bucket_id: str = "memory-1") -> YouClaim:
    edge = _edge(bucket_id)
    return YouClaim.new(
        scope=scope,
        concept_key="preferred_address",
        concept_value="lin",
        content="对方希望日常交流时使用称呼 Lin",
        aspect="preferred_address",
        recall_policy="core",
        evidence=(edge,),
    )


def test_default_off_is_read_only_and_creates_no_storage(tmp_path):
    store = YouStore(tmp_path)

    state = store.get_state()

    assert state.enabled is False
    assert state.scope is None
    assert state.state_revision == 0
    assert not (tmp_path / ".you").exists()


def test_enable_creates_stable_scope_and_revisioned_state(tmp_path):
    store = YouStore(tmp_path)

    enabled = store.set_enabled(True, expected_revision=0)
    disabled = store.set_enabled(False, expected_revision=1)
    enabled_again = store.set_enabled(True, expected_revision=2)

    assert enabled.enabled is True
    assert enabled.scope is not None
    assert enabled.scope == disabled.scope == enabled_again.scope
    assert [enabled.state_revision, disabled.state_revision, enabled_again.state_revision] == [1, 2, 3]
    with pytest.raises(YouStoreError, match="revision conflict"):
        store.set_enabled(False, expected_revision=1)


def test_claim_reads_require_enabled_matching_scope(tmp_path):
    store = YouStore(tmp_path)
    state = store.set_enabled(True)
    assert state.scope is not None
    stored = store.put_claim(_claim(state.scope), expected_revision=0)

    assert stored.revision == 1
    assert [item.id for item in store.list_claims(state.scope)] == [stored.id]
    assert store.list_claims(Scope.new()) == []

    store.set_enabled(False, expected_revision=1)
    assert store.list_claims(state.scope) == []

    reopened = store.set_enabled(True, expected_revision=2)
    assert reopened.scope == state.scope
    assert [item.id for item in store.list_claims(state.scope)] == [stored.id]


def test_claim_revision_and_projection_staleness_are_atomic(tmp_path):
    store = YouStore(tmp_path)
    state = store.set_enabled(True)
    assert state.scope is not None
    stored = store.put_claim(_claim(state.scope), expected_revision=0)
    store.put_projection(state.scope, 1, {"claim_ids": [stored.id], "hints": []})
    assert store.get_projection(state.scope) is not None

    changed = replace(stored, content="对方偏好在交谈中被称为 Lin")
    updated = store.put_claim(changed, expected_revision=stored.revision)

    assert updated.revision == 2
    assert store.get_projection(state.scope) is None
    with pytest.raises(YouStoreError, match="revision conflict"):
        store.put_claim(changed, expected_revision=1)


def test_corrupt_state_fails_closed(tmp_path):
    root = tmp_path / ".you"
    root.mkdir()
    path = root / "you.sqlite3"
    path.write_bytes(b"not sqlite")

    with pytest.raises(YouStoreError, match="unavailable"):
        YouStore(tmp_path).get_state()


def test_snapshot_is_a_valid_independent_database(tmp_path):
    store = YouStore(tmp_path / "vault")
    state = store.set_enabled(True)
    assert state.scope is not None
    store.put_claim(_claim(state.scope))
    snapshot = tmp_path / "snapshot.sqlite3"

    assert store.snapshot_to(snapshot) is True
    connection = sqlite3.connect(snapshot)
    try:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 1
    finally:
        connection.close()


@pytest.mark.parametrize(
    "text",
    [
        "她是典型的内向人格",
        "他有抑郁症诊断",
        "她每月工资和负债情况",
        "他是否爱我以及忠诚度",
        "sexual history",
    ],
)
def test_fixed_policy_rejects_forbidden_subjects(text):
    assert contains_forbidden_subject(text) is True


def test_fixed_policy_allows_supported_non_sensitive_observations():
    assert contains_forbidden_subject("她明确说疲惫时希望先安静一会儿") is False
    assert contains_forbidden_subject("以后请叫我 Lin") is False


def test_normalized_leak_guard_blocks_source_copy_and_allows_atomic_values():
    source = "她下班以后通常希望先安静一会儿，不要连续追问。"

    assert leaks_protected_text("下班以后，通常希望先安静一会儿", [source]) is True
    assert leaks_protected_text("下 班 以 后 通 常 希 望 先 安 静 一 会 儿", [source]) is True
    assert leaks_protected_text("先减少交流压力，再根据回应继续", [source]) is False
    assert leaks_protected_text("Lin", ["以后请叫我 Lin"]) is False
    assert leaks_protected_text("2026-08-18", ["日期是 2026-08-18"]) is False


def test_supporting_buckets_are_counted_per_bucket_not_per_group():
    """闸二的计数：按 bucket 去重。

    原来按 evidence_group_id 去重，是自动抽取时代的产物——系统要自己猜"这几个
    桶算不算同一件事"。现在桶由模型自己挑，算不算独立由它自己定。
    """
    scope = Scope.new()
    edges = (_edge("memory-1"), _edge("memory-2"))
    claim = replace(_claim(scope), evidence=edges, evidence_revision=evidence_digest(edges))

    assert claim.independent_support_count == 2
    # 同一个桶写两条 edge 不能凑数
    dup = (_edge("memory-1"), replace(_edge("memory-1"), basis="observed_pattern"))
    assert replace(claim, evidence=dup, evidence_revision=evidence_digest(dup)).independent_support_count == 1


def test_confirmations_are_counted_per_distinct_calendar_day():
    """闸一的计数：同一天重申多次只算一天。"""
    scope = Scope.new()
    edges = (_edge("memory-1"), _edge("memory-2"))
    revision = evidence_digest(edges)
    receipts = (
        ReviewReceipt("2026-08-17T10:00:00+00:00", scope.observer_role_id, revision, "reaffirmed"),
        ReviewReceipt("2026-08-17T20:00:00+00:00", scope.observer_role_id, revision, "reaffirmed"),
        ReviewReceipt("2026-08-18T09:00:00+00:00", scope.observer_role_id, revision, "reaffirmed"),
    )
    claim = replace(
        _claim(scope), evidence=edges, review_receipts=receipts, evidence_revision=revision
    )

    assert claim.review_date_count == 2, "8-17 那两次只能算一天"


def test_confirmations_are_void_once_the_evidence_set_changes():
    """证据一换，先前攒的重申全部作废——改一条 you 因此天然也要重新攒三天。"""
    scope = Scope.new()
    edges = (_edge("memory-1"), _edge("memory-2"))
    old_revision = evidence_digest(edges)
    receipts = tuple(
        ReviewReceipt(f"2026-08-1{day}T10:00:00+00:00", scope.observer_role_id, old_revision, "reaffirmed")
        for day in (5, 6, 7)
    )
    claim = replace(
        _claim(scope), evidence=edges, review_receipts=receipts, evidence_revision=old_revision
    )
    assert claim.review_date_count == 3

    grown = (*edges, _edge("memory-3"))
    moved = replace(claim, evidence=grown, evidence_revision=evidence_digest(grown))
    assert moved.review_date_count == 0


def test_legacy_review_result_still_loads():
    """存量收据用的是旧名字 remains_plausible，升级后不能读不出来。"""
    scope = Scope.new()
    edges = (_edge("memory-1"),)
    revision = evidence_digest(edges)
    receipt = ReviewReceipt("2026-08-17T10:00:00+00:00", scope.observer_role_id, revision, "remains_plausible")
    claim = replace(
        _claim(scope), evidence=edges, review_receipts=(receipt,), evidence_revision=revision
    )

    assert claim.review_date_count == 1

