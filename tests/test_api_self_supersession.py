"""/api/self 必须把「被取代 / 正在被质疑 / 未经沉淀」告诉 Dashboard。

不标出来的话，人看到的是一堆并列的「我认为」，中间夹着几条模型早就不这么想
了的——而人恰恰是最该看到「这条被替换了」的那个。
"""

from __future__ import annotations

import json

import pytest

from web import buckets as web_buckets


class _MCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(handler):
            for method in methods:
                self.routes[(method, path)] = handler
            return handler

        return decorator


class _Manager:
    def __init__(self, items):
        self.items = items

    async def list_all(self, include_archive=False):
        return list(self.items)


def _formal(bucket_id, content, **metadata):
    base = {
        "name": bucket_id,
        "type": "i",
        "tags": ["__i__", "aspect:nature"],
        "created": "2026-07-15T00:00:00",
    }
    base.update(metadata)
    return {"id": bucket_id, "content": content, "metadata": base}


def _candidate(bucket_id, *, pending=True):
    return {
        "id": bucket_id,
        "content": "我觉得不是这样。",
        "metadata": {
            "name": bucket_id,
            "type": "dynamic",
            "tags": ["__i_candidate__"],
            "i_stage": "candidate" if pending else "promoted",
            "created": "2026-08-14T00:00:00",
        },
    }


async def _self_payload(monkeypatch, items):
    monkeypatch.setattr(web_buckets.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(web_buckets.sh, "bucket_mgr", _Manager(items), raising=False)
    mcp = _MCP()
    web_buckets.register(mcp)
    response = await mcp.routes[("GET", "/api/self")](object())
    assert response.status_code == 200
    return {row["id"]: row for row in json.loads(response.body.decode("utf-8"))}


@pytest.mark.asyncio
async def test_supersession_and_dispute_reach_the_dashboard(monkeypatch):
    rows = await _self_payload(
        monkeypatch,
        [
            _formal("retired", "旧看法。", i_superseded_by="tail"),
            _formal("doubted", "正在改的看法。", i_disputed_by=["cand"]),
            _formal("settled", "当前看法。", i_from_candidate="c0"),
            _candidate("cand"),
        ],
    )

    assert rows["retired"]["superseded_by"] == "tail"
    assert rows["doubted"]["disputed_by"] == ["cand"]
    assert rows["settled"]["superseded_by"] == ""
    assert rows["settled"]["disputed_by"] == []
    # 早期直写的条目在 Dashboard 上也要能和沉淀过的分开。
    assert rows["settled"]["sedimented"] is True
    assert rows["retired"]["sedimented"] is False


@pytest.mark.asyncio
async def test_dispute_from_a_dead_candidate_is_not_reported(monkeypatch):
    rows = await _self_payload(
        monkeypatch,
        [
            _formal("doubted", "旧看法。", i_disputed_by=["gone", "promoted"]),
            _candidate("promoted", pending=False),
        ],
    )

    assert rows["doubted"]["disputed_by"] == []
