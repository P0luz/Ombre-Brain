from pathlib import Path

import pytest

from deletion_requests import DeletionRequestStore


@pytest.mark.asyncio
async def test_formal_request_is_pending_required_unique_withdrawable_and_persistent(bucket_mgr, test_config):
    bid = await bucket_mgr.create("keep me")
    store = DeletionRequestStore(test_config["buckets_dir"], bucket_mgr)
    assert (await store.submit(bid, "   "))["code"] == "reason_required"
    submitted = await store.submit(bid, "no longer useful")
    assert submitted["pending"] is True
    assert (await bucket_mgr.get(bid))["content"] == "keep me"
    assert (await store.submit(bid, "again"))["code"] == "pending_exists"

    restarted = DeletionRequestStore(test_config["buckets_dir"], bucket_mgr)
    assert restarted.status(bid)["status"] == "pending"
    assert (await restarted.withdraw(bid))["withdrawn"] is True
    assert restarted.status(bid)["status"] == "withdrawn"
    assert (await bucket_mgr.get(bid))["content"] == "keep me"


@pytest.mark.asyncio
async def test_daily_limit_counts_successful_submissions(bucket_mgr, test_config):
    store = DeletionRequestStore(test_config["buckets_dir"], bucket_mgr)
    for index in range(10):
        bid = await bucket_mgr.create(f"memory {index}")
        assert (await store.submit(bid, "reason"))["ok"] is True
        assert (await store.withdraw(bid))["ok"] is True
    extra = await bucket_mgr.create("extra")
    assert (await store.submit(extra, "reason"))["code"] == "daily_limit"


@pytest.mark.asyncio
async def test_lifetime_limit_counts_rejections_and_withdrawals(bucket_mgr, test_config):
    bid = await bucket_mgr.create("one life")
    store = DeletionRequestStore(test_config["buckets_dir"], bucket_mgr)
    for index in range(5):
        submitted = await store.submit(bid, f"reason {index}")
        assert submitted["ok"] is True
        request_id = submitted["request"]["request_id"]
        if index % 2:
            assert (await store.withdraw(bid))["ok"] is True
        else:
            assert (await store.decide(request_id, "reject", "still meaningful"))["ok"] is True
    assert (await store.submit(bid, "sixth"))["code"] == "lifetime_limit"
    assert store.status(bid)["lifetime_count"] == 5


@pytest.mark.asyncio
async def test_test_bucket_exempt_letter_included_and_direct_delete_unchanged(bucket_mgr, test_config):
    store = DeletionRequestStore(test_config["buckets_dir"], bucket_mgr)
    test_id = await bucket_mgr.create("fixture", test_data=True)
    result = await store.submit(test_id, "cleanup")
    assert result["deleted"] is True and result["exempt_test_data"] is True
    assert await bucket_mgr.get(test_id) is None

    letter_id = await bucket_mgr.create("sealed words", bucket_type="letter")
    pending = await store.submit(letter_id, "obsolete")
    assert pending["pending"] is True
    assert await bucket_mgr.get(letter_id) is not None

    ai_id = await bucket_mgr.create("AI may delete directly")
    assert await bucket_mgr.delete(ai_id) is True
    assert store.status(ai_id) is None


@pytest.mark.asyncio
async def test_breath_batch_approve_reject_and_undecided(bucket_mgr, test_config):
    store = DeletionRequestStore(test_config["buckets_dir"], bucket_mgr)
    first = await bucket_mgr.create("first body")
    second = await bucket_mgr.create("second body")
    one = await store.submit(first, "first reason")
    two = await store.submit(second, "second reason")

    batch = await store.render_pending_batch()
    assert batch.count("=== Pending human deletion requests") == 1
    assert one["request"]["request_id"] in batch and two["request"]["request_id"] in batch
    assert "first body" in batch and "second reason" in batch
    assert "Do not approve merely because a human asked" in batch
    assert "deletion_request_id" in batch

    assert (await store.decide(one["request"]["request_id"], "reject", "still needed"))["ok"]
    assert store.status(first)["ai_reason"] == "still needed"
    assert await bucket_mgr.get(first) is not None
    assert store.status(second)["status"] == "pending"

    assert (await store.decide(two["request"]["request_id"], "approve", "not meaningful"))["ok"]
    assert await bucket_mgr.get(second) is None
    archived = await bucket_mgr.get_including_archive(second)
    assert archived["metadata"]["deleted_at"]


@pytest.mark.asyncio
async def test_breath_batch_does_not_reveal_ai_locked_letter_body(bucket_mgr, test_config):
    store = DeletionRequestStore(test_config["buckets_dir"], bucket_mgr)
    secret = "sealed letter body must never enter deletion breath"
    letter_id = await bucket_mgr.create(
        secret,
        bucket_type="letter",
        lock_type="permanent",
        locked_by="human",
        unlock_date="9999-12-31",
    )
    submitted = await store.submit(letter_id, "please remove this letter", is_letter=True)

    batch = await store.render_pending_batch()

    assert submitted["request"]["request_id"] in batch
    assert letter_id in batch
    assert "please remove this letter" in batch
    assert secret not in batch
    assert "LOCKED" in batch
    assert "locked=true" in batch
    assert "lock_type=permanent" in batch


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_action", ["delete", "archive"])
async def test_missing_pending_target_is_durably_superseded_before_breath(
    bucket_mgr, test_config, terminal_action
):
    store = DeletionRequestStore(test_config["buckets_dir"], bucket_mgr)
    bucket_id = await bucket_mgr.create("independently removed")
    submitted = await store.submit(bucket_id, "remove this")

    assert await getattr(bucket_mgr, terminal_action)(bucket_id) is True
    batch = await store.render_pending_batch()

    assert submitted["request"]["request_id"] not in batch
    assert store.status(bucket_id)["status"] == "superseded"
    stored = store._load()["requests"][-1]
    assert stored["status"] == "superseded"
    assert stored["superseded_reason"] == "target is no longer active"


def test_state_file_keeps_audit_history(test_config, bucket_mgr):
    store = DeletionRequestStore(test_config["buckets_dir"], bucket_mgr)
    assert store._load() == {"version": 1, "requests": []}
    assert store.path.name == ".human_deletion_requests.json"


def test_trace_binds_deletion_decision_to_required_bucket_id():
    source = (Path(__file__).parents[1] / "src" / "server.py").read_text(encoding="utf-8")
    assert "expected_bucket_id=bucket_id" in source


def test_dashboard_skips_reason_prompt_for_known_erasable_test_buckets():
    source = (Path(__file__).parents[1] / "frontend" / "dashboard.html").read_text(encoding="utf-8")
    assert "function isKnownErasableTestBucket(id)" in source
    assert "if (!isKnownErasableTestBucket(id))" in source
    assert "var allKnownTests = ids.every(isKnownErasableTestBucket);" in source
