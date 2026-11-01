import json
from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from ombrebrain.policy.surfacing import SurfacePolicyVM
from tools.breath import dispatch


MUTED = "这条被主动静音了。"
DIGESTED = "这条已经消化过了。"
PLAIN = "这条是普通记忆。"


class DisabledEmbedding:
    enabled = False


class ExplodingDehydrator:
    async def dehydrate(self, *_args, **_kwargs):
        raise AssertionError("这些用例不该调 LLM")


class NoopDecay:
    is_running = True

    async def ensure_started(self):
        return None

    def calculate_score(self, metadata):
        return float(metadata.get("importance") or 5)


class SearchBucketManager:
    def __init__(self, buckets):
        self.buckets = list(buckets)
        self.touched = []

    async def get(self, _bucket_id):
        return None

    async def get_including_archive(self, _bucket_id):
        return None

    async def search(self, _query, **_kwargs):
        return list(self.buckets)

    async def list_all(self, include_archive=False):
        return list(self.buckets)

    async def touch_many(self, bucket_ids, ripple=False):
        self.touched.extend(bucket_ids)

    async def get_stats(self):
        return {"permanent_count": 0, "dynamic_count": len(self.buckets)}

    def footprint_snapshot(self):
        raise RuntimeError("no footprint in tests")


def _bucket(bucket_id, content, **meta):
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {
            "name": bucket_id,
            "type": "dynamic",
            "importance": 7,
            "domain": ["回归测试"],
            "created": "2026-08-01T10:00:00",
            "last_active": "2026-08-01T10:00:00",
            "activation_count": 3,
            **meta,
        },
    }


def _install(monkeypatch, manager):
    monkeypatch.setattr(rt, "config", {"surfacing": {}})
    monkeypatch.setattr(rt, "bucket_mgr", manager)
    monkeypatch.setattr(rt, "decay_engine", NoopDecay())
    monkeypatch.setattr(rt, "dehydrator", ExplodingDehydrator())
    monkeypatch.setattr(rt, "embedding_engine", DisabledEmbedding())
    monkeypatch.setattr(rt, "logger", MagicMock())
    monkeypatch.setattr(rt, "fire_webhook", None)
    monkeypatch.setattr(rt, "mark_op", None)
    monkeypatch.setattr(rt, "record_v3_tool_event", lambda *_a, **_k: None)
    monkeypatch.setattr(rt, "deletion_requests", None, raising=False)
    monkeypatch.setattr(rt, "them_service", None, raising=False)
    monkeypatch.setattr("tools.breath.search.random.random", lambda: 1.0)


@pytest.fixture
def manager(monkeypatch):
    mgr = SearchBucketManager([
        _bucket("plain", PLAIN),
        _bucket("muted", MUTED, dont_surface=True),
        _bucket("digested", DIGESTED, digested=True),
    ])
    _install(monkeypatch, mgr)
    return mgr


def _ids(output):
    body = output.split("=== ombre:result-ids ===", 1)[1]
    return json.loads(body.split("```json", 1)[1].split("```", 1)[0])


@pytest.mark.asyncio
async def test_manual_search_is_unchanged(manager):
    out = await dispatch(query="记忆")

    assert PLAIN in out
    assert MUTED in out
    assert DIGESTED in out


@pytest.mark.asyncio
async def test_manual_is_the_default(manager):
    assert await dispatch(query="记忆") == await dispatch(query="记忆", mode="manual")


@pytest.mark.asyncio
async def test_automatic_drops_dont_surface(manager):
    out = await dispatch(query="记忆", mode="automatic")

    assert PLAIN in out
    assert MUTED not in out


@pytest.mark.asyncio
async def test_automatic_drops_digested(manager):
    out = await dispatch(query="记忆", mode="automatic")

    assert DIGESTED not in out


@pytest.mark.asyncio
async def test_automatic_keeps_core_principles(monkeypatch):
    mgr = SearchBucketManager([
        _bucket("core", "我说话要算数。", pinned=True, type="permanent", importance=10),
        _bucket("anchored", "这是我的坐标系。", anchor=True),
        _bucket("guarded", "这条只防衰减。", protected=True),
    ])
    _install(monkeypatch, mgr)

    out = await dispatch(query="这", mode="automatic")

    assert "我说话要算数。" in out
    assert "这是我的坐标系。" in out
    assert "这条只防衰减。" in out


@pytest.mark.asyncio
async def test_a_digested_core_principle_survives_automatic(monkeypatch):
    mgr = SearchBucketManager([
        _bucket(
            "core", "核心准则但被消化过。",
            pinned=True, type="permanent", importance=10, digested=True,
        ),
    ])
    _install(monkeypatch, mgr)

    out = await dispatch(query="核心", mode="automatic")

    assert "核心准则但被消化过。" in out


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "Manual", "auto", "nonsense", "AUTOMATIC "])
async def test_unknown_mode_falls_back_to_manual(manager, bad):
    out = await dispatch(query="记忆", mode=bad)

    if bad.strip().lower() == "automatic":
        assert MUTED not in out
    else:
        assert MUTED in out


@pytest.mark.asyncio
async def test_no_id_block_by_default(manager):
    out = await dispatch(query="记忆")

    assert "ombre:result-ids" not in out


@pytest.mark.asyncio
async def test_id_block_lists_returned_buckets(manager):
    out = await dispatch(query="记忆", with_ids=True)

    payload = _ids(out)
    assert payload["schema"] == 1
    assert set(payload["bucket_ids"]) == {"plain", "muted", "digested"}
    assert payload["count"] == 3
    assert payload["mode"] == "manual"


@pytest.mark.asyncio
async def test_id_block_reports_what_the_filter_removed(manager):
    out = await dispatch(query="记忆", mode="automatic", with_ids=True)

    payload = _ids(out)
    assert payload["bucket_ids"] == ["plain"]
    assert payload["mode"] == "automatic"
    assert payload["omitted_by_policy"] == 2


@pytest.mark.asyncio
async def test_id_block_appears_even_when_nothing_matched(monkeypatch):
    mgr = SearchBucketManager([_bucket("muted", MUTED, dont_surface=True)])
    _install(monkeypatch, mgr)

    out = await dispatch(query="记忆", mode="automatic", with_ids=True)

    payload = _ids(out)
    assert payload["bucket_ids"] == []
    assert payload["count"] == 0
    assert payload["omitted_by_policy"] == 1


@pytest.mark.asyncio
async def test_id_block_does_not_disturb_the_rendered_text(manager):
    plain = await dispatch(query="记忆")
    with_ids = await dispatch(query="记忆", with_ids=True)

    assert with_ids.startswith(plain)


def test_policy_automatic_is_search_plus_two_markers():
    vm = SurfacePolicyVM.default()
    muted = {"id": "m", "metadata": {"type": "dynamic", "dont_surface": True}}
    digested = {"id": "d", "metadata": {"type": "dynamic", "digested": True}}

    assert vm.evaluate_bucket(muted, mode="search").allowed is True
    assert vm.evaluate_bucket(digested, mode="search").allowed is True
    assert vm.evaluate_bucket(muted, mode="automatic").allowed is False
    assert vm.evaluate_bucket(digested, mode="automatic").allowed is False
