from types import SimpleNamespace

import pytest

from deletion_requests import DeletionRequestStore


class BucketManager:
    def __init__(self, bucket):
        self.bucket = bucket
        self.deleted = []
        self.archived = []
        self.discarded = []
        self.invalidated = False
        self.embedding_outbox = SimpleNamespace(discard=self.discarded.append)

    async def get(self, bucket_id):
        return self.bucket

    async def delete(self, bucket_id):
        self.deleted.append(bucket_id)
        self.bucket = None
        return True

    async def archive(self, bucket_id):
        self.archived.append(bucket_id)
        self.bucket = None
        return True

    def _invalidate_bm25(self):
        self.invalidated = True


@pytest.mark.asyncio
async def test_approved_letter_uses_full_human_letter_cleanup(tmp_path):
    manager = BucketManager({"id": "letter-1", "metadata": {"type": "letter"}})
    vectors = []
    store = DeletionRequestStore(
        str(tmp_path), manager, SimpleNamespace(delete_embedding=vectors.append)
    )
    submitted = await store.submit("letter-1", "no longer meaningful", is_letter=True)

    result = await store.decide(submitted["request"]["request_id"], "approve")

    assert result["ok"] is True
    assert manager.deleted == ["letter-1"]
    assert manager.discarded == ["letter-1"]
    assert vectors == ["letter-1"]
    assert manager.invalidated is True


@pytest.mark.asyncio
async def test_approved_ordinary_bucket_keeps_common_archive_behavior(tmp_path):
    manager = BucketManager({"id": "memory-1", "metadata": {}})
    vectors = []
    store = DeletionRequestStore(
        str(tmp_path), manager, SimpleNamespace(delete_embedding=vectors.append)
    )
    submitted = await store.submit("memory-1", "inaccurate")

    result = await store.decide(submitted["request"]["request_id"], "approve")

    assert result["ok"] is True
    assert manager.deleted == ["memory-1"]
    assert manager.discarded == []
    assert vectors == []
    assert manager.invalidated is False


@pytest.mark.asyncio
async def test_approval_preserves_requested_archive_vs_delete_action(tmp_path):
    archive_manager = BucketManager({"id": "archive-1", "metadata": {}})
    archive_store = DeletionRequestStore(str(tmp_path / "archive"), archive_manager)
    archive_request = await archive_store.submit(
        "archive-1", "put away", action="archive"
    )

    archive_result = await archive_store.decide(
        archive_request["request"]["request_id"], "approve"
    )

    assert archive_result["ok"] is True
    assert archive_manager.archived == ["archive-1"]
    assert archive_manager.deleted == []

    delete_manager = BucketManager({"id": "delete-1", "metadata": {}})
    delete_store = DeletionRequestStore(str(tmp_path / "delete"), delete_manager)
    delete_request = await delete_store.submit(
        "delete-1", "remove", action="delete"
    )

    delete_result = await delete_store.decide(
        delete_request["request"]["request_id"], "approve"
    )

    assert delete_result["ok"] is True
    assert delete_manager.deleted == ["delete-1"]
    assert delete_manager.archived == []


@pytest.mark.asyncio
async def test_generic_and_batch_submissions_detect_logical_letters(tmp_path):
    letter = {"id": "letter-1", "metadata": {"source_tool": "letter"}}
    generic_store = DeletionRequestStore(
        str(tmp_path / "generic"), BucketManager(letter)
    )
    generic = await generic_store.submit("letter-1", "remove")
    generic_record = generic_store.pending_with_buckets()[0]
    assert generic_record["is_letter"] is True
    assert generic_record["action"] == "delete"
    assert generic["pending"] is True

    class ManyBuckets(BucketManager):
        async def get(self, bucket_id):
            return {"id": bucket_id, "metadata": {"tags": ["__letter__"]}}

    batch_store = DeletionRequestStore(
        str(tmp_path / "batch"), ManyBuckets(None)
    )
    await batch_store.submit_batch(
        ["letter-2"], "put away", action="archive"
    )
    batch_record = batch_store.pending_with_buckets()[0]
    assert batch_record["is_letter"] is True
    assert batch_record["action"] == "archive"


@pytest.mark.asyncio
async def test_erasable_test_letter_remains_exempt_and_gets_letter_cleanup(tmp_path):
    manager = BucketManager({
        "id": "letter-test",
        "metadata": {
            "type": "letter",
            "provenance": {"kind": "test", "erasable": True},
        },
    })
    vectors = []
    store = DeletionRequestStore(
        str(tmp_path), manager, SimpleNamespace(delete_embedding=vectors.append)
    )

    result = await store.submit("letter-test", "cleanup", is_letter=True)

    assert result["ok"] is True
    assert result["exempt_test_data"] is True
    assert manager.deleted == ["letter-test"]
    assert manager.discarded == ["letter-test"]
    assert vectors == ["letter-test"]


@pytest.mark.asyncio
async def test_batch_submission_deduplicates_and_refuses_quota_overflow(tmp_path):
    class ManyBuckets(BucketManager):
        async def get(self, bucket_id):
            return {"id": bucket_id, "metadata": {}}

    store = DeletionRequestStore(str(tmp_path), ManyBuckets(None))
    result = await store.submit_batch(
        [*(f"memory-{index}" for index in range(11)), "memory-0"],
        "no longer meaningful",
    )

    assert [item["id"] for item in result["submitted"]] == [
        f"memory-{index}" for index in range(10)
    ]
    assert result["refused"] == [{
        "id": "memory-10",
        "code": "daily_limit",
        "error": "daily deletion request limit reached",
    }]
    assert len(store.pending_with_buckets()) == 10


@pytest.mark.asyncio
async def test_empty_reason_batch_executes_tests_and_refuses_formal_items(tmp_path):
    buckets = {
        "test-1": {
            "id": "test-1",
            "metadata": {"provenance": {"kind": "test", "erasable": True}},
        },
        "formal-1": {"id": "formal-1", "metadata": {}},
    }

    class ManyBuckets(BucketManager):
        async def get(self, bucket_id):
            return buckets.get(bucket_id)

        async def delete(self, bucket_id):
            self.deleted.append(bucket_id)
            buckets.pop(bucket_id, None)
            return True

    manager = ManyBuckets(None)
    store = DeletionRequestStore(str(tmp_path), manager)
    result = await store.submit_batch(["test-1", "formal-1"], "  ")

    assert manager.deleted == ["test-1"]
    assert result["submitted"] == [{
        "id": "test-1", "pending": False, "exempt_test_data": True
    }]
    assert result["refused"] == [{
        "id": "formal-1",
        "code": "reason_required",
        "error": "deletion reason is required",
    }]
    assert store.pending_with_buckets() == []


@pytest.mark.asyncio
async def test_empty_reason_single_test_bucket_bypasses_request_accounting(tmp_path):
    manager = BucketManager({
        "id": "test-1",
        "metadata": {"provenance": {"kind": "test", "erasable": True}},
    })
    store = DeletionRequestStore(str(tmp_path), manager)

    result = await store.submit("test-1", "  ")

    assert result["ok"] is True
    assert result["exempt_test_data"] is True
    assert manager.deleted == ["test-1"]
    assert not store.path.exists()


@pytest.mark.asyncio
async def test_decision_rejects_expected_bucket_mismatch_without_mutation(tmp_path):
    manager = BucketManager({"id": "memory-1", "metadata": {}})
    store = DeletionRequestStore(str(tmp_path), manager)
    submitted = await store.submit("memory-1", "remove")
    request_id = submitted["request"]["request_id"]

    result = await store.decide(
        request_id, "approve", expected_bucket_id="memory-2"
    )

    assert result["code"] == "bucket_mismatch"
    assert manager.deleted == []
    assert store.status("memory-1")["status"] == "pending"
