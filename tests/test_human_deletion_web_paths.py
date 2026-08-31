from types import SimpleNamespace

import pytest

from deletion_requests import DeletionRequestStore
from web import buckets, import_api, letters


class FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(fn):
            for method in methods:
                self.routes[(method, path)] = fn
            return fn
        return decorator


class Request:
    def __init__(self, body, *, path_params=None, confirm=False):
        self.body_value = body
        self.path_params = path_params or {}
        self.query_params = {"confirm": "true"} if confirm else {}


class Manager:
    def __init__(self, records):
        self.records = records
        self.deleted = []
        self.archived = []
        self.embedding_outbox = SimpleNamespace(discard=lambda bucket_id: None)

    async def get(self, bucket_id):
        return self.records.get(bucket_id)

    async def delete(self, bucket_id):
        self.deleted.append(bucket_id)
        self.records.pop(bucket_id, None)
        return True

    async def archive(self, bucket_id):
        self.archived.append(bucket_id)
        self.records.pop(bucket_id, None)
        return True

    def _invalidate_bm25(self):
        pass


async def _exercise_routes(monkeypatch, tmp_path, records):
    manager = Manager(records)
    store = DeletionRequestStore(str(tmp_path), manager)

    async def read_json(request):
        return request.body_value

    for module in (buckets, letters, import_api):
        monkeypatch.setattr(module.sh, "_require_auth", lambda request: None)
        monkeypatch.setattr(module.sh, "_read_json_object", read_json)
        monkeypatch.setattr(module.sh, "bucket_mgr", manager)
        monkeypatch.setattr(module.sh, "deletion_requests", store)

    bucket_mcp, letter_mcp, import_mcp = FakeMCP(), FakeMCP(), FakeMCP()
    buckets.register(bucket_mcp)
    letters.register(letter_mcp)
    import_api.register(import_mcp)
    return manager, store, bucket_mcp, letter_mcp, import_mcp


@pytest.mark.asyncio
async def test_all_human_delete_paths_submit_formal_requests(monkeypatch, tmp_path):
    records = {
        "delete": {"id": "delete", "metadata": {}},
        "archive": {"id": "archive", "metadata": {}},
        "batch": {"id": "batch", "metadata": {}},
        "letter": {"id": "letter", "metadata": {"type": "letter"}},
        "review": {"id": "review", "metadata": {}},
    }
    manager, store, bmcp, lmcp, imcp = await _exercise_routes(
        monkeypatch, tmp_path, records
    )
    reason = {"reason": "no longer meaningful"}

    await bmcp.routes[("DELETE", "/api/bucket/{bucket_id}")](
        Request(reason, path_params={"bucket_id": "delete"}, confirm=True)
    )
    await bmcp.routes[("POST", "/api/bucket/{bucket_id}/archive")](
        Request(reason, path_params={"bucket_id": "archive"})
    )
    await bmcp.routes[("POST", "/api/buckets/batch")](
        Request({"ids": ["batch"], "action": "archive", **reason})
    )
    await lmcp.routes[("DELETE", "/api/letter/{letter_id}")](
        Request(reason, path_params={"letter_id": "letter"}, confirm=True)
    )
    await imcp.routes[("POST", "/api/import/review")](
        Request({"decisions": [{"bucket_id": "review", "action": "delete", **reason}]})
    )

    assert manager.deleted == []
    assert manager.archived == []
    pending = store.pending_with_buckets()
    assert {item["bucket_id"] for item in pending} == set(records)
    actions = {item["bucket_id"]: item["action"] for item in pending}
    assert actions == {"delete": "delete", "archive": "delete", "batch": "delete", "letter": "delete", "review": "delete"}


@pytest.mark.asyncio
async def test_web_delete_and_archive_keep_test_buckets_direct(monkeypatch, tmp_path):
    provenance = {"provenance": {"kind": "test", "erasable": True}}
    records = {
        "delete-test": {"id": "delete-test", "metadata": provenance},
        "archive-test": {"id": "archive-test", "metadata": provenance},
        "batch-test": {"id": "batch-test", "metadata": provenance},
    }
    manager, store, bmcp, _, _ = await _exercise_routes(monkeypatch, tmp_path, records)

    await bmcp.routes[("DELETE", "/api/bucket/{bucket_id}")](
        Request({}, path_params={"bucket_id": "delete-test"}, confirm=True)
    )
    await bmcp.routes[("POST", "/api/bucket/{bucket_id}/archive")](
        Request({}, path_params={"bucket_id": "archive-test"})
    )
    await bmcp.routes[("POST", "/api/buckets/batch")](
        Request({"ids": ["batch-test"], "action": "archive"})
    )

    assert manager.deleted == ["delete-test", "archive-test", "batch-test"]
    assert manager.archived == []
    assert store._load()["requests"] == []
