import json
from pathlib import Path

import pytest


def test_append_event_writes_jsonl_with_hash_and_sequence(tmp_path):
    from ombrebrain.eventsourcing.ledger_mirror import LedgerMirror

    ledger = LedgerMirror(tmp_path / "events.jsonl")

    event = ledger.append_event(
        event_type="TraceCreated",
        trace_id="b1",
        trace_kind="dynamic",
        payload={"name": "hello"},
        body="memory body",
    )

    rows = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    data = json.loads(rows[0])

    assert event["seq"] == 1
    assert data["seq"] == 1
    assert data["event_type"] == "TraceCreated"
    assert data["schema_version"] == 1
    assert data["ledger_role"] == "mirror"
    assert data["canonical"] is False
    assert data["trace_id"] == "b1"
    assert data["trace_kind"] == "dynamic"
    assert data["body_hash"].startswith("sha256:")
    assert data["payload"] == {"name": "hello"}


def _ledger_events(buckets_dir: str):
    path = Path(buckets_dir) / "_ledger" / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.asyncio
async def test_bucket_manager_create_appends_trace_created_event(test_config, fake_embedding_engine):
    from bucket_manager import BucketManager

    manager = BucketManager(test_config, embedding_engine=fake_embedding_engine)

    bucket_id = await manager.create_internal(
        "memory body should not be copied into the ledger",
        tags=["ledger"],
        domain=["测试"],
        source_tool="hold",
    )

    events = _ledger_events(test_config["buckets_dir"])

    assert [event["event_type"] for event in events] == ["TraceCreated"]
    assert events[0]["trace_id"] == bucket_id
    assert events[0]["trace_kind"] == "dynamic"
    assert events[0]["payload"]["source_tool"] == "hold"
    assert "memory body should not be copied" not in json.dumps(events[0], ensure_ascii=False)


@pytest.mark.asyncio
async def test_bucket_manager_update_delete_and_archive_append_lifecycle_events(
    test_config,
    fake_embedding_engine,
):
    from bucket_manager import BucketManager

    manager = BucketManager(test_config, embedding_engine=fake_embedding_engine)
    updated_id = await manager.create_internal("one", domain=["测试"])
    archived_id = await manager.create_internal("two", domain=["测试"])

    assert await manager.update(updated_id, resolved=True)
    assert await manager.delete(updated_id)
    assert await manager.archive(archived_id)

    events = _ledger_events(test_config["buckets_dir"])

    assert [event["event_type"] for event in events] == [
        "TraceCreated",
        "TraceCreated",
        "TraceUpdated",
        "TraceDeletedToArchive",
        "TraceArchived",
    ]
    assert events[2]["trace_id"] == updated_id
    assert events[2]["payload"]["changed_fields"] == ["resolved"]
    assert events[3]["trace_id"] == updated_id
    assert events[4]["trace_id"] == archived_id


@pytest.mark.asyncio
async def test_bucket_manager_touch_appends_trace_touched_event(test_config, fake_embedding_engine):
    from bucket_manager import BucketManager

    manager = BucketManager(test_config, embedding_engine=fake_embedding_engine)
    bucket_id = await manager.create_internal("touch me", domain=["测试"])

    await manager.touch(bucket_id)

    events = _ledger_events(test_config["buckets_dir"])

    assert [event["event_type"] for event in events] == ["TraceCreated", "TraceTouched"]
    assert events[1]["trace_id"] == bucket_id
    assert events[1]["payload"]["activation_count"] == 1


@pytest.mark.asyncio
async def test_bucket_manager_exposes_read_only_ledger_integrity_report(
    test_config,
    fake_embedding_engine,
):
    from bucket_manager import BucketManager

    manager = BucketManager(test_config, embedding_engine=fake_embedding_engine)
    await manager.create_internal("diagnose me", domain=["测试"])

    report = manager.ledger_integrity_report()

    assert report["ok"] is True
    assert report["canonical"] is False
    assert report["ledger_role"] == "mirror"
    assert report["valid_events"] == 1
    assert report["latest_seq"] == 1
    assert report["replay"]["ok"] is True
    assert report["replay"]["event_count"] == 1
    assert report["replay"]["projection_trace_count"] == 1
    assert report["replay"]["violations"] == []


def test_iter_events_skips_corrupt_partial_lines_and_integrity_reports_them(tmp_path):
    from ombrebrain.eventsourcing.ledger_mirror import LedgerMirror

    ledger = LedgerMirror(tmp_path / "events.jsonl")
    ledger.append_event(
        event_type="TraceCreated",
        trace_id="b1",
        trace_kind="dynamic",
        payload={},
        body="one",
    )
    with (tmp_path / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write('{"seq": 2, "event_type": "TraceUpdated"')

    events_before_append = list(ledger.iter_events())
    report = ledger.verify_integrity()

    assert [event["seq"] for event in events_before_append] == [1]
    assert report["ok"] is False
    assert report["valid_events"] == 1
    assert report["invalid_lines"] == [2]

    appended = ledger.append_event(
        event_type="TraceTouched",
        trace_id="b1",
        trace_kind="dynamic",
        payload={},
        body="one",
    )

    assert appended["seq"] == 2
    assert [event["event_type"] for event in ledger.iter_events()] == [
        "TraceCreated",
        "TraceTouched",
    ]
