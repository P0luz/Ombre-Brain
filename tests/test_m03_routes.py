"""M-03 upload/apply job reservation and accepted-response contract."""

from __future__ import annotations

import asyncio
import json

import pytest

from web import _shared as sh
from web import import_api


class FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(handler):
            for method in methods:
                self.routes[(method, path)] = handler
            return handler

        return decorator


class FakeRequest:
    def __init__(self, *, body=b"", json_body=None, content_type="application/zip"):
        self._body = body
        self._json = json_body
        self.headers = {"content-type": content_type}

    async def body(self):
        return self._body

    async def json(self):
        if isinstance(self._json, BaseException):
            raise self._json
        return self._json


class FakeMigrate:
    def __init__(self):
        self.phase = "parsed"
        self.job_id = "a" * 32
        self.reserved_parse = False
        self.reservation = ""
        self.abandoned = []
        self.apply_calls = []

    def reserve_parse(self):
        self.reserved_parse = True
        return self.job_id

    def abandon_parse(self, reservation, message):
        self.abandoned.append(("parse", reservation, message))
        return True

    async def parse_zip(self, payload, *, reservation_id):
        assert self.reserved_parse
        assert reservation_id == self.job_id
        assert payload == b"zip"
        return {"ok": True, "phase": "parsed", "job_id": self.job_id}

    def reserve_apply(self, expected_job_id):
        if expected_job_id != self.job_id or self.phase != "parsed":
            return None
        self.reservation = "b" * 32
        self.phase = "applying"
        return self.reservation

    def abandon_apply(self, reservation, message):
        self.abandoned.append(("apply", reservation, message))
        self.phase = "error"
        return True

    async def apply(self, decisions, **kwargs):
        self.apply_calls.append((decisions, kwargs))
        self.phase = "done"

    def get_status(self):
        return {"phase": self.phase, "job_id": self.job_id}


def _setup(monkeypatch):
    mcp = FakeMCP()
    engine = FakeMigrate()
    monkeypatch.setattr(sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(sh, "migrate_engine", engine)
    import_api.register(mcp)
    return mcp, engine


def _json(response):
    return json.loads(response.body.decode("utf-8"))


@pytest.mark.asyncio
async def test_upload_reserves_job_before_reading_payload(monkeypatch):
    mcp, engine = _setup(monkeypatch)

    class AssertReservedRequest(FakeRequest):
        async def body(self):
            assert engine.reserved_parse is True
            return await super().body()

    response = await mcp.routes[("POST", "/api/migrate/upload")](
        AssertReservedRequest(body=b"zip")
    )
    assert response.status_code == 200
    assert _json(response)["job_id"] == engine.job_id


@pytest.mark.asyncio
async def test_concurrent_upload_reservation_conflict_returns_409(monkeypatch):
    mcp, engine = _setup(monkeypatch)
    engine.reserve_parse = lambda: None
    response = await mcp.routes[("POST", "/api/migrate/upload")](
        FakeRequest(body=b"zip")
    )
    assert response.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("job_id", ["", "stale"])
async def test_apply_requires_current_job_id(monkeypatch, job_id):
    mcp, _engine = _setup(monkeypatch)
    response = await mcp.routes[("POST", "/api/migrate/apply")](
        FakeRequest(json_body={"job_id": job_id, "decisions": {}})
    )
    assert response.status_code == 409
    assert _json(response).get("ok") is not True


@pytest.mark.asyncio
async def test_202_is_accepted_not_completed(monkeypatch):
    mcp, engine = _setup(monkeypatch)
    response = await mcp.routes[("POST", "/api/migrate/apply")](
        FakeRequest(
            json_body={
                "job_id": engine.job_id,
                "decisions": {"one": "keep_both"},
                "assigned_owners": {"one": "cheng"},
            }
        )
    )
    body = _json(response)
    assert response.status_code == 202
    assert body["accepted"] is True
    assert body["completed"] is False
    assert body["job_id"] == engine.job_id
    await asyncio.sleep(0)
    assert engine.apply_calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"job_id": "a" * 32, "decisions": []},
        {"job_id": "a" * 32, "decisions": {"one": "delete"}},
        {"job_id": "a" * 32, "assigned_owners": []},
        {"job_id": "a" * 32, "assigned_owners": {"one": "unknown"}},
    ],
)
async def test_wrong_decision_or_owner_types_are_rejected_before_reservation(
    monkeypatch,
    payload,
):
    mcp, engine = _setup(monkeypatch)
    response = await mcp.routes[("POST", "/api/migrate/apply")](
        FakeRequest(json_body=payload)
    )
    assert response.status_code == 400
    assert engine.reservation == ""
    assert _json(response).get("ok") is not True


@pytest.mark.asyncio
async def test_schedule_failure_abandons_apply_and_returns_500(monkeypatch):
    mcp, engine = _setup(monkeypatch)

    def fail_schedule(coro):
        coro.close()
        raise RuntimeError("scheduler unavailable")

    monkeypatch.setattr(import_api, "spawn_background", fail_schedule)
    response = await mcp.routes[("POST", "/api/migrate/apply")](
        FakeRequest(json_body={"job_id": engine.job_id, "decisions": {}})
    )
    assert response.status_code == 500
    assert engine.abandoned and engine.abandoned[-1][0] == "apply"
    assert _json(response).get("ok") is not True
