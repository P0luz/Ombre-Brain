import frontmatter
import pytest

from bucket_manager import BucketManager
from ombrebrain.projection.projection_mirror import TraceCatalogProjection


@pytest.mark.asyncio
async def test_bucket_manager_delete_writes_tombstone_metadata_and_ledger_payload(
    test_config,
    fake_embedding_engine,
):
    manager = BucketManager(test_config, embedding_engine=fake_embedding_engine)
    bucket_id = await manager.create_internal("remember me as a tombstone", domain=["phase4"])

    assert await manager.delete(bucket_id)

    archived_path = manager._find_bucket_file(bucket_id)
    assert archived_path is not None
    post = frontmatter.load(archived_path)

    assert post["deleted_at"]
    assert post["tombstone"] is True
    assert post["tombstoned_at"] == post["deleted_at"]
    assert post["erasure_mode"] == "tombstone_only"

    events = list(manager.ledger_mirror.iter_events())
    delete_event = events[-1]
    assert delete_event["event_type"] == "TraceDeletedToArchive"
    assert delete_event["payload"]["tombstone"] is True
    assert delete_event["payload"]["tombstoned_at"] == delete_event["payload"]["deleted_at"]
    assert delete_event["payload"]["erasure_mode"] == "tombstone_only"

    projection = TraceCatalogProjection()
    projection.rebuild(events)
    report = projection.to_report(source_latest_seq=manager.ledger_mirror.latest_seq())

    assert report["tombstone_count"] == 1
    assert projection.traces[bucket_id]["state"] == "tombstone"
    assert projection.traces[bucket_id]["deleted"] is True
    assert projection.traces[bucket_id]["tombstone"] is True
