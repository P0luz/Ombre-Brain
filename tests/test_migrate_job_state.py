"""Red/blue regressions for complete-backup migration job ownership."""

import asyncio
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import migrate_engine as migrate_mod
from migrate_engine import MigrateEngine, _ParsedBucket
from web import import_api as import_web


class _BucketManager:
    async def get(self, _bucket_id):
        return None


class _Embedding:
    model = ""
    enabled = False


class _MCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(handler):
            for method in methods:
                self.routes[(method, path)] = handler
            return handler

        return decorator


class _Request:
    def __init__(self, payload):
        self.payload = payload
        self.headers = {}
        self.query_params = {}


def _parsed_payload(bucket_id: str):
    return {
        "buckets": [
            _ParsedBucket(
                bucket_id=bucket_id,
                arc_path=f"buckets/dynamic/{bucket_id}.md",
                md_bytes=b"---\nid: placeholder\n---\nbody\n",
                name=bucket_id,
                bucket_type="dynamic",
                domain=["test"],
                created="",
            )
        ],
        "import_model": "",
        "import_model_dim": 0,
        "import_backend": "",
        "has_embeddings": False,
        "db_bytes": None,
        "integrity_verified": True,
        "integrity_warning": "",
        "manifest": None,
    }


@pytest.mark.parametrize(
    ("id_map", "package_ids", "expected_target", "expected_status"),
    [
        ({"B": "B-new"}, frozenset({"A", "B"}), "B-new", "active"),
        ({"A": "A"}, frozenset({"A", "B"}), "B", "detached"),
        ({"A": "A"}, frozenset({"A"}), "B", "active"),
    ],
)
def test_relation_target_remap_fails_closed_for_unimported_package_bucket(
    tmp_path, id_map, package_ids, expected_target, expected_status
):
    import frontmatter

    path = tmp_path / "a.md"
    post = frontmatter.Post("body", relation_links=[{
        "target_bucket_id": "B", "type": "causes", "label": "", "status": "active",
    }])
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    engine = MigrateEngine({"buckets_dir": str(tmp_path)}, _BucketManager(), _Embedding())

    engine._remap_imported_relation_targets({"A": str(path)}, id_map, package_ids)

    link = frontmatter.load(path).metadata["relation_links"][0]
    assert link["target_bucket_id"] == expected_target
    assert link["status"] == expected_status


def test_parse_reservation_is_atomic_across_threads(tmp_path):
    engine = MigrateEngine(
        {"buckets_dir": str(tmp_path)},
        _BucketManager(),
        _Embedding(),
    )
    barrier = threading.Barrier(16)

    def reserve():
        barrier.wait(timeout=5)
        return engine.reserve_parse()

    with ThreadPoolExecutor(max_workers=16) as pool:
        reservations = list(pool.map(lambda _index: reserve(), range(16)))

    winners = [reservation for reservation in reservations if reservation]
    assert len(winners) == 1
    assert engine.job_id == winners[0]


@pytest.mark.asyncio
async def test_concurrent_zip_parse_cannot_replace_inflight_generation(monkeypatch, tmp_path):
    engine = MigrateEngine(
        {"buckets_dir": str(tmp_path)},
        _BucketManager(),
        _Embedding(),
    )
    entered = threading.Event()
    release = threading.Event()

    def blocking_parse(raw: bytes):
        entered.set()
        assert release.wait(timeout=5)
        return _parsed_payload(raw.decode("ascii"))

    monkeypatch.setattr(engine, "_parse_zip_sync", blocking_parse)
    first_task = asyncio.create_task(engine.parse_zip(b"first-job"))
    for _ in range(100):
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    assert entered.is_set()
    assert engine.phase == "parsing"

    second = await engine.parse_zip(b"second-job")
    assert second["ok"] is False
    assert second["busy"] is True
    assert engine.phase == "parsing"

    release.set()
    first = await first_task
    assert first["ok"] is True
    assert first["job_id"]
    assert engine.phase == "parsed"
    assert engine._parsed_buckets[0].bucket_id == "first-job"


@pytest.mark.asyncio
async def test_apply_reservation_is_generation_bound_and_single_use(monkeypatch, tmp_path):
    engine = MigrateEngine(
        {"buckets_dir": str(tmp_path)},
        _BucketManager(),
        _Embedding(),
    )
    monkeypatch.setattr(engine, "_parse_zip_sync", lambda _raw: _parsed_payload("one"))
    parsed = await engine.parse_zip(b"archive")

    assert engine.reserve_apply("stale-job") is None
    reservation = engine.reserve_apply(parsed["job_id"])
    assert reservation
    assert engine.phase == "applying"
    assert engine.reserve_apply(parsed["job_id"]) is None


@pytest.mark.asyncio
async def test_parse_conflicts_use_one_vault_snapshot_instead_of_per_id_get(
    monkeypatch,
    tmp_path,
):
    class SnapshotManager:
        list_calls = 0

        async def list_all(self, *, include_archive):
            assert include_archive is True
            self.list_calls += 1
            return [{"id": "one", "metadata": {"name": "existing"}}]

        async def get(self, _bucket_id):
            pytest.fail("production conflict detection must not scan once per ID")

    manager = SnapshotManager()
    engine = MigrateEngine(
        {"buckets_dir": str(tmp_path)},
        manager,
        _Embedding(),
    )
    monkeypatch.setattr(engine, "_parse_zip_sync", lambda _raw: _parsed_payload("one"))

    parsed = await engine.parse_zip(b"archive")

    assert parsed["ok"] is True
    assert parsed["conflicts_count"] == 1
    assert manager.list_calls == 1
    assert engine._conflict_ids_at_parse == frozenset({"one"})


@pytest.mark.asyncio
async def test_apply_prewarms_bucket_path_index_once(monkeypatch, tmp_path):
    class IndexedManager(_BucketManager):
        ensure_calls = 0

        def _ensure_bucket_path_index(self):
            self.ensure_calls += 1

        @staticmethod
        def _find_bucket_file(_bucket_id):
            return None

    manager = IndexedManager()
    engine = MigrateEngine(
        {"buckets_dir": str(tmp_path / "vault")},
        manager,
        _Embedding(),
    )
    monkeypatch.setattr(engine, "_parse_zip_sync", lambda _raw: _parsed_payload("one"))
    monkeypatch.setattr(
        engine,
        "_write_bucket_file",
        lambda _bucket, target_id, _buckets_dir: (
            target_id,
            str(tmp_path / "published.md"),
        ),
    )
    parsed = await engine.parse_zip(b"archive")

    await engine.apply(
        {},
        reservation_id=engine.reserve_apply(parsed["job_id"]),
    )

    assert manager.ensure_calls == 1


@pytest.mark.asyncio
async def test_apply_route_returns_one_202_and_one_409_before_background_runs(
    monkeypatch,
):
    release = asyncio.Event()

    class Engine:
        phase = "parsed"
        job_id = "parsed-job"
        reservations = 0
        applied = 0

        def reserve_apply(self, expected_job_id):
            if self.phase != "parsed" or expected_job_id != self.job_id:
                return None
            self.phase = "applying"
            self.reservations += 1
            return "reservation-1"

        async def apply(self, _decisions, *, reservation_id):
            assert reservation_id == "reservation-1"
            self.applied += 1
            await release.wait()
            self.phase = "done"

    engine = Engine()

    async def read_json(request):
        return request.payload

    monkeypatch.setattr(import_web.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(import_web.sh, "_read_json_object", read_json)
    monkeypatch.setattr(import_web.sh, "migrate_engine", engine, raising=False)
    mcp = _MCP()
    import_web.register(mcp)
    handler = mcp.routes[("POST", "/api/migrate/apply")]
    request = _Request({"job_id": "parsed-job", "decisions": {}})

    first = await handler(request)
    second = await handler(request)

    assert first.status_code == 202
    assert json.loads(first.body)["job_id"] == "parsed-job"
    assert second.status_code == 409
    assert engine.reservations == 1
    await asyncio.sleep(0)
    assert engine.applied == 1
    release.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_apply_route_rejects_missing_or_stale_job_id(monkeypatch):
    class Engine:
        phase = "parsed"
        job_id = "current-job"

        def reserve_apply(self, _expected_job_id):
            pytest.fail("stale requests must not reserve the parsed package")

    async def read_json(request):
        return request.payload

    monkeypatch.setattr(import_web.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(import_web.sh, "_read_json_object", read_json)
    monkeypatch.setattr(import_web.sh, "migrate_engine", Engine(), raising=False)
    mcp = _MCP()
    import_web.register(mcp)
    handler = mcp.routes[("POST", "/api/migrate/apply")]

    missing = await handler(_Request({"decisions": {}}))
    stale = await handler(_Request({"job_id": "old-job", "decisions": {}}))

    assert missing.status_code == 409
    assert stale.status_code == 409


@pytest.mark.asyncio
async def test_apply_route_schedule_failure_abandons_reservation(monkeypatch):
    class Engine:
        phase = "parsed"
        job_id = "current-job"

        def __init__(self):
            self.abandoned = []

        def reserve_apply(self, expected_job_id):
            assert expected_job_id == self.job_id
            self.phase = "applying"
            return "apply-reservation"

        def abandon_apply(self, reservation_id, message):
            self.abandoned.append((reservation_id, message))
            self.phase = "error"
            return True

        async def apply(self, *_args, **_kwargs):
            pytest.fail("unscheduled apply coroutine must never run")

    async def read_json(request):
        return request.payload

    engine = Engine()
    monkeypatch.setattr(import_web.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(import_web.sh, "_read_json_object", read_json)
    monkeypatch.setattr(import_web.sh, "migrate_engine", engine, raising=False)
    monkeypatch.setattr(
        import_web.asyncio,
        "create_task",
        lambda _coro: (_ for _ in ()).throw(RuntimeError("scheduler down")),
    )
    mcp = _MCP()
    import_web.register(mcp)

    response = await mcp.routes[("POST", "/api/migrate/apply")](
        _Request({"job_id": "current-job", "decisions": {}})
    )

    assert response.status_code == 503
    assert engine.phase == "error"
    assert engine.abandoned == [
        ("apply-reservation", "task scheduling failed: scheduler down")
    ]


@pytest.mark.asyncio
async def test_cancelled_parse_reaps_worker_before_releasing_slot(monkeypatch, tmp_path):
    engine = MigrateEngine(
        {"buckets_dir": str(tmp_path)},
        _BucketManager(),
        _Embedding(),
    )
    entered = threading.Event()
    release = threading.Event()

    def blocking_parse(_raw):
        entered.set()
        assert release.wait(timeout=5)
        return _parsed_payload("cancelled")

    monkeypatch.setattr(engine, "_parse_zip_sync", blocking_parse)
    task = asyncio.create_task(engine.parse_zip(b"archive"))
    for _ in range(100):
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.05)

    assert not task.done()
    assert engine.phase == "parsing"
    busy = await engine.parse_zip(b"attacker-retry")
    assert busy["busy"] is True

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert engine.phase == "error"


async def _wait_for_thread(event: threading.Event) -> None:
    for _ in range(200):
        if event.is_set():
            return
        await asyncio.sleep(0.01)
    pytest.fail("worker thread did not start")


@pytest.mark.asyncio
async def test_cancelled_apply_reaps_create_worker_before_workspace_cleanup(
    monkeypatch,
    tmp_path,
):
    engine = MigrateEngine(
        {"buckets_dir": str(tmp_path / "vault")},
        _BucketManager(),
        _Embedding(),
    )
    monkeypatch.setattr(engine, "_parse_zip_sync", lambda _raw: _parsed_payload("one"))
    parsed = await engine.parse_zip(b"archive")
    workspace = tmp_path / "workspace-create"
    workspace.mkdir()
    engine._parse_temp_dir = str(workspace)
    entered = threading.Event()
    release = threading.Event()

    def blocking_create(_bucket, target_id, _buckets_dir):
        entered.set()
        assert release.wait(timeout=5)
        return target_id, str(tmp_path / "published.md")

    monkeypatch.setattr(engine, "_write_bucket_file", blocking_create)
    reservation = engine.reserve_apply(parsed["job_id"])
    task = asyncio.create_task(engine.apply({}, reservation_id=reservation))
    await _wait_for_thread(entered)
    for _ in range(3):
        task.cancel()
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)

    assert not task.done()
    assert workspace.is_dir()
    assert engine.phase == "applying"
    assert engine._apply_reservation == reservation

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not workspace.exists()
    assert engine.phase == "error"
    assert engine._apply_reservation == ""


@pytest.mark.asyncio
async def test_cancelled_apply_reaps_overwrite_worker_before_workspace_cleanup(
    monkeypatch,
    tmp_path,
):
    existing_path = tmp_path / "existing.md"
    existing_path.write_text("existing", encoding="utf-8")

    class ConflictManager(_BucketManager):
        async def get(self, bucket_id):
            return {"id": bucket_id, "metadata": {"name": bucket_id}}

        @staticmethod
        def _find_bucket_file(_bucket_id):
            return str(existing_path)

    engine = MigrateEngine(
        {"buckets_dir": str(tmp_path / "vault")},
        ConflictManager(),
        _Embedding(),
    )
    monkeypatch.setattr(engine, "_parse_zip_sync", lambda _raw: _parsed_payload("one"))
    parsed = await engine.parse_zip(b"archive")
    workspace = tmp_path / "workspace-overwrite"
    workspace.mkdir()
    engine._parse_temp_dir = str(workspace)
    entered = threading.Event()
    release = threading.Event()

    def blocking_overwrite(_bucket, target_id, _buckets_dir, _existing_path):
        entered.set()
        assert release.wait(timeout=5)
        return target_id, str(tmp_path / "published.md")

    monkeypatch.setattr(engine, "_overwrite_bucket_transaction", blocking_overwrite)
    reservation = engine.reserve_apply(parsed["job_id"])
    task = asyncio.create_task(
        engine.apply({"one": "overwrite"}, reservation_id=reservation)
    )
    await _wait_for_thread(entered)
    task.cancel()
    await asyncio.sleep(0.05)

    assert not task.done()
    assert workspace.is_dir()
    assert engine._apply_reservation == reservation

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not workspace.exists()
    assert engine.phase == "error"
    assert engine._apply_reservation == ""


@pytest.mark.asyncio
async def test_cancelled_apply_reaps_embedding_merge_before_workspace_cleanup(
    monkeypatch,
    tmp_path,
):
    engine = MigrateEngine(
        {"buckets_dir": str(tmp_path / "vault")},
        _BucketManager(),
        _Embedding(),
    )
    monkeypatch.setattr(engine, "_parse_zip_sync", lambda _raw: _parsed_payload("one"))
    parsed = await engine.parse_zip(b"archive")
    workspace = tmp_path / "workspace-merge"
    workspace.mkdir()
    snapshot = workspace / "embeddings.db"
    snapshot.write_bytes(b"placeholder")
    engine._parse_temp_dir = str(workspace)
    engine._zip_db_path = str(snapshot)
    engine._has_embeddings = True
    monkeypatch.setattr(engine, "_embedding_match", lambda: True)
    monkeypatch.setattr(
        engine,
        "_write_bucket_file",
        lambda _bucket, target_id, _buckets_dir: (
            target_id,
            str(tmp_path / "published.md"),
        ),
    )
    monkeypatch.setattr(engine, "_remap_imported_relation_targets", lambda *_args: None)
    entered = threading.Event()
    release = threading.Event()

    def blocking_merge(_source_db, _id_map):
        entered.set()
        assert release.wait(timeout=5)
        return set()

    monkeypatch.setattr(engine, "_merge_embeddings_path", blocking_merge)
    reservation = engine.reserve_apply(parsed["job_id"])
    task = asyncio.create_task(engine.apply({}, reservation_id=reservation))
    await _wait_for_thread(entered)
    task.cancel()
    await asyncio.sleep(0.05)

    assert not task.done()
    assert workspace.is_dir()
    assert snapshot.exists()
    assert engine._apply_reservation == reservation

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not workspace.exists()
    assert engine.phase == "error"
    assert engine._apply_reservation == ""


@pytest.mark.asyncio
async def test_abandon_apply_releases_unscheduled_generation(monkeypatch, tmp_path):
    engine = MigrateEngine(
        {"buckets_dir": str(tmp_path / "vault")},
        _BucketManager(),
        _Embedding(),
    )
    monkeypatch.setattr(engine, "_parse_zip_sync", lambda _raw: _parsed_payload("one"))
    parsed = await engine.parse_zip(b"archive")
    workspace = tmp_path / "workspace-unscheduled"
    workspace.mkdir()
    engine._parse_temp_dir = str(workspace)
    reservation = engine.reserve_apply(parsed["job_id"])

    assert engine.abandon_apply(reservation, "task scheduling failed") is True
    assert engine.phase == "error"
    assert engine._apply_reservation == ""
    assert not workspace.exists()
    assert engine._parsed_buckets == []


@pytest.mark.asyncio
async def test_parsed_workspace_expires_generation_bound(monkeypatch, tmp_path):
    engine = MigrateEngine(
        {"buckets_dir": str(tmp_path / "vault")},
        _BucketManager(),
        _Embedding(),
    )
    monkeypatch.setattr(engine, "_parse_zip_sync", lambda _raw: _parsed_payload("one"))
    parsed = await engine.parse_zip(b"archive")
    workspace = tmp_path / "workspace-expired"
    workspace.mkdir()
    (workspace / "member.md").write_text("payload", encoding="utf-8")
    engine._parse_temp_dir = str(workspace)
    parsed_at = engine._parsed_at_monotonic
    monkeypatch.setattr(migrate_mod, "_PARSED_WORKSPACE_TTL_SECONDS", 10.0)

    assert engine._expire_parsed_workspace(parsed_at + 11.0) is True

    assert engine.phase == "error"
    assert "有效期" in engine.get_status()["error"]
    assert engine._apply_reservation == ""
    assert engine._parsed_buckets == []
    assert not workspace.exists()
    assert engine.reserve_apply(parsed["job_id"]) is None


@pytest.mark.asyncio
async def test_abandon_parsed_rejects_stale_generation_and_cleans_current(
    monkeypatch,
    tmp_path,
):
    engine = MigrateEngine(
        {"buckets_dir": str(tmp_path / "vault")},
        _BucketManager(),
        _Embedding(),
    )
    monkeypatch.setattr(engine, "_parse_zip_sync", lambda _raw: _parsed_payload("one"))
    parsed = await engine.parse_zip(b"archive")
    workspace = tmp_path / "workspace-abandon"
    workspace.mkdir()
    engine._parse_temp_dir = str(workspace)

    assert engine.abandon_parsed("stale-job") is False
    assert workspace.exists()
    assert engine.phase == "parsed"

    assert engine.abandon_parsed(parsed["job_id"], "用户取消迁移") is True
    assert not workspace.exists()
    assert engine.phase == "error"
    assert engine.get_status()["error"] == "用户取消迁移"


@pytest.mark.asyncio
async def test_migrate_upload_reserves_before_reading_and_spools_to_disk(monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    second_stream_read = False

    class Engine:
        def __init__(self):
            self.reserved = False
            self.path_seen = ""

        def reserve_parse(self):
            if self.reserved:
                return None
            self.reserved = True
            return "upload-job"

        def abandon_parse(self, reservation_id, _message):
            assert reservation_id == "upload-job"
            self.reserved = False

        async def parse_zip_file(self, path, *, reservation_id):
            assert reservation_id == "upload-job"
            self.path_seen = path
            with open(path, "rb") as handle:
                assert handle.read() == b"fake-zip"
            self.reserved = False
            return {"ok": True, "job_id": reservation_id}

    class FirstRequest:
        headers = {"content-type": "application/zip"}

        async def stream(self):
            entered.set()
            await release.wait()
            yield b"fake-zip"

    class SecondRequest:
        headers = {"content-type": "application/zip"}

        async def stream(self):
            nonlocal second_stream_read
            second_stream_read = True
            yield b"must-not-be-read"

    engine = Engine()
    monkeypatch.setattr(import_web.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(import_web.sh, "migrate_engine", engine, raising=False)
    mcp = _MCP()
    import_web.register(mcp)
    handler = mcp.routes[("POST", "/api/migrate/upload")]

    first_task = asyncio.create_task(handler(FirstRequest()))
    await entered.wait()
    second = await handler(SecondRequest())
    assert second.status_code == 409
    assert second_stream_read is False

    release.set()
    first = await first_task
    assert first.status_code == 200
    assert engine.path_seen
    assert not os.path.exists(engine.path_seen)


@pytest.mark.asyncio
async def test_cancelled_migrate_upload_cleans_spool_and_releases_reservation(
    monkeypatch,
    tmp_path,
):
    entered = asyncio.Event()
    never = asyncio.Event()
    created = []

    class Engine:
        abandoned = False

        @staticmethod
        def reserve_parse():
            return "cancel-upload"

        def abandon_parse(self, reservation_id, message):
            assert reservation_id == "cancel-upload"
            assert "取消" in message
            self.abandoned = True

    class Request:
        headers = {"content-type": "application/zip"}

        async def stream(self):
            yield b"partial"
            entered.set()
            await never.wait()

    original_mkstemp = import_web.tempfile.mkstemp

    def tracked_mkstemp(*args, **kwargs):
        kwargs["dir"] = tmp_path
        fd, path = original_mkstemp(*args, **kwargs)
        created.append(path)
        return fd, path

    engine = Engine()
    monkeypatch.setattr(import_web.tempfile, "mkstemp", tracked_mkstemp)
    monkeypatch.setattr(import_web.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(import_web.sh, "migrate_engine", engine, raising=False)
    mcp = _MCP()
    import_web.register(mcp)
    task = asyncio.create_task(
        mcp.routes[("POST", "/api/migrate/upload")](Request())
    )
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert engine.abandoned is True
    assert created and all(not os.path.exists(path) for path in created)
