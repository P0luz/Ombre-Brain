def test_trace_catalog_projection_reports_shadow_role_for_empty_rebuild():
    from ombrebrain.projection.projection_mirror import TraceCatalogProjection

    projection = TraceCatalogProjection()

    projection.rebuild([])
    report = projection.to_report(source_latest_seq=0)

    assert report["projection_name"] == "trace_catalog"
    assert report["projection_role"] == "shadow"
    assert report["canonical"] is False
    assert report["trace_count"] == 0
    assert report["applied_seq"] == 0
    assert report["source_latest_seq"] == 0
    assert report["lag"] == 0


def test_trace_catalog_projection_rebuilds_trace_lifecycle_from_ledger(tmp_path):
    from ombrebrain.eventsourcing.ledger_mirror import LedgerMirror
    from ombrebrain.projection.projection_mirror import TraceCatalogProjection

    ledger = LedgerMirror(tmp_path / "events.jsonl")
    ledger.append_event(
        event_type="TraceCreated",
        trace_id="b1",
        trace_kind="dynamic",
        payload={"name": "first", "importance": 5},
        body="one",
    )
    ledger.append_event(
        event_type="TraceUpdated",
        trace_id="b1",
        trace_kind="dynamic",
        payload={"resolved": True, "changed_fields": ["resolved"]},
        body="one",
    )
    ledger.append_event(
        event_type="TraceTouched",
        trace_id="b1",
        trace_kind="dynamic",
        payload={"activation_count": 1},
        body="one",
    )
    ledger.append_event(
        event_type="TraceArchived",
        trace_id="b1",
        trace_kind="archived",
        payload={"type": "archived"},
        body="one",
    )
    ledger.append_event(
        event_type="TraceDeletedToArchive",
        trace_id="b2",
        trace_kind="dynamic",
        payload={"deleted_at": "2026-07-02T00:00:00"},
        body="two",
    )
    ledger.append_event(
        event_type="TraceDeletedToArchive",
        trace_id="b4",
        trace_kind="dynamic",
        payload={
            "deleted_at": "2026-07-02T01:00:00",
            "tombstone": True,
            "tombstoned_at": "2026-07-02T01:00:00",
            "erasure_mode": "tombstone_only",
        },
        body="four",
    )
    ledger.append_event(
        event_type="UnrecognizedExperimentalEvent",
        trace_id="b3",
        trace_kind="dynamic",
        payload={},
        body="three",
    )

    projection = TraceCatalogProjection()
    projection.rebuild(ledger.iter_events())
    report = projection.to_report(source_latest_seq=ledger.latest_seq())

    assert report["trace_count"] == 3
    assert report["unknown_event_count"] == 1
    assert report["applied_seq"] == 7
    assert report["lag"] == 0
    assert report["tombstone_count"] == 1

    b1 = projection.traces["b1"]
    assert b1["trace_id"] == "b1"
    assert b1["trace_kind"] == "archived"
    assert b1["state"] == "archived"
    assert b1["resolved"] is True
    assert b1["touch_count"] == 1
    assert b1["latest_event_type"] == "TraceArchived"

    b2 = projection.traces["b2"]
    assert b2["state"] == "deleted_to_archive"
    assert b2["deleted"] is True

    b4 = projection.traces["b4"]
    assert b4["state"] == "tombstone"
    assert b4["deleted"] is True
    assert b4["tombstone"] is True


def test_bucket_manager_ledger_report_includes_trace_catalog_projection(
    test_config,
    fake_embedding_engine,
):
    import asyncio
    from bucket_manager import BucketManager

    async def scenario():
        manager = BucketManager(test_config, embedding_engine=fake_embedding_engine)
        bucket_id = await manager.create_internal("projection source", domain=["测试"])
        await manager.update(bucket_id, resolved=True)
        return manager.ledger_integrity_report()

    report = asyncio.run(scenario())
    projection = report["trace_catalog_projection"]

    assert projection["projection_name"] == "trace_catalog"
    assert projection["canonical"] is False
    assert projection["trace_count"] == 1
    assert projection["applied_seq"] == report["latest_seq"]
    assert projection["source_latest_seq"] == report["latest_seq"]
    assert projection["lag"] == 0
    assert report["replay"]["ok"] is True
    assert report["replay"]["latest_seq"] == report["latest_seq"]
    assert report["replay"]["projection_trace_count"] == projection["trace_count"]


def test_bucket_manager_ledger_report_includes_sqlite_projection(
    test_config,
    fake_embedding_engine,
):
    import asyncio
    from pathlib import Path

    from bucket_manager import BucketManager

    async def scenario():
        manager = BucketManager(test_config, embedding_engine=fake_embedding_engine)
        bucket_id = await manager.create_internal("sqlite projection source", domain=["sqlite"])
        await manager.update(bucket_id, resolved=True)
        return manager, manager.ledger_integrity_report(rebuild_projections=True)

    manager, report = asyncio.run(scenario())
    projection = report["sqlite_projection"]

    assert projection["projection_name"] == "trace_catalog_sqlite"
    assert projection["projection_role"] == "shadow"
    assert projection["canonical"] is False
    assert projection["trace_count"] == 1
    assert projection["applied_seq"] == report["latest_seq"]
    assert projection["source_latest_seq"] == report["latest_seq"]
    assert projection["lag"] == 0
    assert Path(projection["path"]).exists()
    assert str(manager.base_dir) in projection["path"]


def test_bucket_manager_default_ledger_report_does_not_create_or_rewrite_sqlite_projection(
    test_config,
    fake_embedding_engine,
):
    """默认诊断路径只观测，重建投影必须显式请求。"""
    import asyncio
    import sqlite3
    from pathlib import Path

    from bucket_manager import BucketManager

    async def make_manager():
        manager = BucketManager(test_config, embedding_engine=fake_embedding_engine)
        await manager.create_internal("read-only ledger report", domain=["audit"])
        return manager

    manager = asyncio.run(make_manager())
    projection_path = (
        Path(manager.base_dir) / "_ledger" / "projections" / "trace_catalog.sqlite3"
    )

    assert not projection_path.exists()
    manager.ledger_integrity_report()
    assert not projection_path.exists(), "a default integrity report created a durable projection"

    projection_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(projection_path) as conn:
        conn.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        conn.execute("INSERT INTO sentinel(value) VALUES ('keep-me')")
    before = projection_path.read_bytes()

    manager.ledger_integrity_report()

    assert projection_path.read_bytes() == before, "a default integrity report rewrote a durable projection"


def test_bucket_manager_default_ledger_report_does_not_refresh_existing_sqlite_projection(
    test_config,
    fake_embedding_engine,
):
    import asyncio
    from pathlib import Path

    from bucket_manager import BucketManager
    from ombrebrain.projection.projection_sqlite import TraceSQLiteProjection

    async def make_manager():
        manager = BucketManager(test_config, embedding_engine=fake_embedding_engine)
        await manager.create_internal("projection snapshot one", domain=["audit"])
        return manager

    manager = asyncio.run(make_manager())
    projection_path = (
        Path(manager.base_dir) / "_ledger" / "projections" / "trace_catalog.sqlite3"
    )
    TraceSQLiteProjection(projection_path).rebuild(manager.ledger_mirror.iter_events())
    before = projection_path.read_bytes()

    asyncio.run(manager.create_internal("projection snapshot two", domain=["audit"]))
    report = manager.ledger_integrity_report()

    assert projection_path.read_bytes() == before, "a read-only report refreshed the durable projection"
    assert report["sqlite_projection"]["applied_seq"] == 1
    assert report["sqlite_projection"]["source_latest_seq"] == 2
    assert report["sqlite_projection"]["lag"] == 1
