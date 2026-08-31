import asyncio
import json

import pytest

from tools import _runtime as rt
from web import _shared as sh
from web import buckets as buckets_web
from web import import_api


class FakeMcp:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(handler):
            self.routes[path] = handler
            return handler

        return decorator


class FakeRequest:
    def __init__(self, *, path_params=None, body=None):
        self.path_params = path_params or {}
        self._body = body or {}
        self.headers = {}
        self.query_params = {}

    async def json(self):
        return self._body


class FakeBucketManager:
    def __init__(self):
        self.rows = {
            "already-pinned": self._row("already-pinned", pinned=True, bucket_type="permanent"),
            "plain": self._row("plain", pinned=False, bucket_type="dynamic"),
        }
        self.updates = []

    @staticmethod
    def _row(bucket_id, *, pinned, bucket_type):
        return {
            "id": bucket_id,
            "content": f"content for {bucket_id}",
            "metadata": {
                "id": bucket_id,
                "name": bucket_id,
                "pinned": pinned,
                "type": bucket_type,
                "importance": 10 if pinned else 5,
            },
        }

    async def list_all(self, include_archive=False):
        return list(self.rows.values())

    async def get(self, bucket_id):
        return self.rows.get(bucket_id)

    async def update(self, bucket_id, **updates):
        updates.pop("event_actor", None)
        self.updates.append((bucket_id, updates))
        self.rows[bucket_id]["metadata"].update(updates)
        return True


class DummyLogger:
    def warning(self, *_args, **_kwargs):
        pass

    def info(self, *_args, **_kwargs):
        pass


@pytest.fixture
def pinned_quota_runtime(monkeypatch):
    bucket_mgr = FakeBucketManager()
    monkeypatch.setattr(sh, "bucket_mgr", bucket_mgr, raising=False)
    monkeypatch.setattr(sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(rt, "bucket_mgr", bucket_mgr, raising=False)
    monkeypatch.setattr(rt, "config", {"limits": {"max_pinned": 1}}, raising=False)
    monkeypatch.setattr(rt, "logger", DummyLogger(), raising=False)
    return bucket_mgr


def _json(response):
    return json.loads(response.body.decode("utf-8"))


@pytest.mark.asyncio
async def test_bucket_pin_route_rejects_new_pin_when_quota_is_full(pinned_quota_runtime):
    mcp = FakeMcp()
    buckets_web.register(mcp)

    response = await mcp.routes["/api/bucket/{bucket_id}/pin"](
        FakeRequest(path_params={"bucket_id": "plain"})
    )

    assert response.status_code == 400
    assert "error" in _json(response)
    assert pinned_quota_runtime.rows["plain"]["metadata"]["pinned"] is False
    assert pinned_quota_runtime.updates == []


@pytest.mark.asyncio
async def test_bucket_pin_route_rejects_archived_bucket(pinned_quota_runtime):
    metadata = pinned_quota_runtime.rows["plain"]["metadata"]
    metadata["type"] = "archived"
    mcp = FakeMcp()
    buckets_web.register(mcp)

    response = await mcp.routes["/api/bucket/{bucket_id}/pin"](
        FakeRequest(path_params={"bucket_id": "plain"})
    )

    assert response.status_code == 409
    assert metadata["type"] == "archived"
    assert metadata["pinned"] is False
    assert pinned_quota_runtime.updates == []


@pytest.mark.asyncio
async def test_bucket_pin_route_rejects_protected_bucket_before_quota_check(
    pinned_quota_runtime,
):
    metadata = pinned_quota_runtime.rows["plain"]["metadata"]
    metadata["protected"] = True
    mcp = FakeMcp()
    buckets_web.register(mcp)

    response = await mcp.routes["/api/bucket/{bucket_id}/pin"](
        FakeRequest(path_params={"bucket_id": "plain"})
    )

    payload = _json(response)
    assert response.status_code == 409
    assert payload["conflict"] == "pinned_protected_mutually_exclusive"
    assert metadata["pinned"] is False
    assert metadata["protected"] is True
    assert pinned_quota_runtime.updates == []


@pytest.mark.asyncio
async def test_bucket_pin_route_can_unpin_historical_pinned_protected_state(
    pinned_quota_runtime,
):
    metadata = pinned_quota_runtime.rows["already-pinned"]["metadata"]
    metadata["protected"] = True
    mcp = FakeMcp()
    buckets_web.register(mcp)

    response = await mcp.routes["/api/bucket/{bucket_id}/pin"](
        FakeRequest(path_params={"bucket_id": "already-pinned"})
    )

    assert response.status_code == 200
    assert _json(response)["pinned"] is False
    assert metadata["pinned"] is False
    assert metadata["protected"] is True
    assert pinned_quota_runtime.updates == [
        ("already-pinned", {"pinned": False})
    ]


@pytest.mark.asyncio
async def test_import_review_pin_action_respects_pinned_quota(pinned_quota_runtime):
    mcp = FakeMcp()
    import_api.register(mcp)

    response = await mcp.routes["/api/import/review"](
        FakeRequest(body={"decisions": [{"bucket_id": "plain", "action": "pin"}]})
    )

    assert response.status_code == 200
    assert _json(response)["applied"] == 0
    assert _json(response)["errors"] == 1
    assert pinned_quota_runtime.rows["plain"]["metadata"]["pinned"] is False
    assert pinned_quota_runtime.updates == []


@pytest.mark.asyncio
async def test_import_review_pin_rejects_archived_bucket(pinned_quota_runtime):
    metadata = pinned_quota_runtime.rows["plain"]["metadata"]
    metadata["type"] = "archived"
    mcp = FakeMcp()
    import_api.register(mcp)

    response = await mcp.routes["/api/import/review"](
        FakeRequest(body={"decisions": [{"bucket_id": "plain", "action": "pin"}]})
    )

    assert response.status_code == 200
    assert _json(response) == {"applied": 0, "errors": 1}
    assert metadata["type"] == "archived"
    assert metadata["pinned"] is False
    assert pinned_quota_runtime.updates == []


@pytest.mark.asyncio
async def test_bucket_unpin_requires_same_request_importance(pinned_quota_runtime):
    mcp = FakeMcp()
    buckets_web.register(mcp)

    response = await mcp.routes["/api/bucket/{bucket_id}/pin"](
        FakeRequest(path_params={"bucket_id": "already-pinned"})
    )

    assert response.status_code == 400
    assert "importance" in _json(response)["error"]
    assert pinned_quota_runtime.rows["already-pinned"]["metadata"]["pinned"] is True
    assert pinned_quota_runtime.updates == []


@pytest.mark.asyncio
async def test_concurrent_import_review_pins_share_check_and_write_lock(
    pinned_quota_runtime,
    monkeypatch,
):
    first = pinned_quota_runtime.rows["already-pinned"]["metadata"]
    first.update({"pinned": False, "type": "dynamic", "importance": 5})
    monkeypatch.setattr(rt, "config", {"limits": {"max_pinned": 1}})
    mcp = FakeMcp()
    import_api.register(mcp)

    responses = await asyncio.gather(
        mcp.routes["/api/import/review"](
            FakeRequest(
                body={
                    "decisions": [
                        {"bucket_id": "already-pinned", "action": "pin"}
                    ]
                }
            )
        ),
        mcp.routes["/api/import/review"](
            FakeRequest(
                body={"decisions": [{"bucket_id": "plain", "action": "pin"}]}
            )
        ),
    )

    payloads = [_json(response) for response in responses]
    assert sum(payload["applied"] for payload in payloads) == 1
    assert sum(payload["errors"] for payload in payloads) == 1
    assert sum(
        bool(row["metadata"].get("pinned"))
        for row in pinned_quota_runtime.rows.values()
    ) == 1


@pytest.mark.asyncio
async def test_import_review_does_not_count_failed_update_as_applied(
    pinned_quota_runtime,
    monkeypatch,
):
    monkeypatch.setattr(rt, "config", {"limits": {"max_pinned": 2}})

    async def failed_update(bucket_id, **updates):
        pinned_quota_runtime.updates.append((bucket_id, updates))
        return False

    monkeypatch.setattr(pinned_quota_runtime, "update", failed_update)
    mcp = FakeMcp()
    import_api.register(mcp)

    response = await mcp.routes["/api/import/review"](
        FakeRequest(body={"decisions": [{"bucket_id": "plain", "action": "pin"}]})
    )

    assert response.status_code == 200
    assert _json(response) == {"applied": 0, "errors": 1}
    assert pinned_quota_runtime.rows["plain"]["metadata"]["pinned"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["unknown", "archive-now"])
async def test_import_review_unknown_action_is_an_error(
    pinned_quota_runtime,
    action,
):
    mcp = FakeMcp()
    import_api.register(mcp)

    response = await mcp.routes["/api/import/review"](
        FakeRequest(body={"decisions": [{"bucket_id": "plain", "action": action}]})
    )

    assert response.status_code == 200
    assert _json(response) == {"applied": 0, "errors": 1}
    assert pinned_quota_runtime.updates == []


@pytest.mark.asyncio
async def test_import_review_delete_false_is_not_counted_as_applied(
    pinned_quota_runtime,
    monkeypatch,
):
    async def failed_delete(_bucket_id):
        return False

    monkeypatch.setattr(
        pinned_quota_runtime,
        "delete",
        failed_delete,
        raising=False,
    )
    mcp = FakeMcp()
    import_api.register(mcp)

    response = await mcp.routes["/api/import/review"](
        FakeRequest(body={"decisions": [{"bucket_id": "plain", "action": "delete"}]})
    )

    assert response.status_code == 200
    assert _json(response) == {"applied": 0, "errors": 1}


@pytest.mark.asyncio
async def test_import_review_noise_rejects_locked_bucket_without_partial_write(
    pinned_quota_runtime,
):
    mcp = FakeMcp()
    import_api.register(mcp)

    response = await mcp.routes["/api/import/review"](
        FakeRequest(
            body={
                "decisions": [
                    {"bucket_id": "already-pinned", "action": "noise"}
                ]
            }
        )
    )

    assert response.status_code == 200
    assert _json(response) == {"applied": 0, "errors": 1}
    metadata = pinned_quota_runtime.rows["already-pinned"]["metadata"]
    assert metadata["importance"] == 10
    assert metadata.get("resolved") is not True
    assert pinned_quota_runtime.updates == []
