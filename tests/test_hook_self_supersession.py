"""SessionStart 注入的自我认知：被取代的和正在被质疑的不占那三个名额。"""

import threading

import pytest

from web import hooks


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
    def __init__(self):
        self.source = "client"
        self.headers = {"x-ombre-hook-token": "secret"}


class _Manager:
    def __init__(self, buckets):
        self.buckets = buckets

    async def list_all(self, include_archive=False):
        return list(self.buckets)

    async def update(self, bucket_id, **updates):
        bucket = next(item for item in self.buckets if item["id"] == bucket_id)
        bucket["metadata"].update(updates)
        return True


class _Decay:
    @staticmethod
    def calculate_score(metadata):
        return float(metadata.get("importance", 0))


class _EchoDehydrator:
    async def dehydrate(self, content, _metadata):
        return content


def _formal(bucket_id, content, *, created, aspect="nature", **metadata):
    base = {
        "id": bucket_id,
        "name": bucket_id,
        "type": "i",
        "tags": ["__i__", f"aspect:{aspect}"],
        "importance": 5,
        "created": created,
    }
    base.update(metadata)
    return {"id": bucket_id, "content": content, "metadata": base}


def _candidate(bucket_id, content, *, created, pending=True):
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {
            "id": bucket_id,
            "name": bucket_id,
            "type": "dynamic",
            "tags": ["__i_candidate__"],
            "i_stage": "candidate" if pending else "promoted",
            "importance": 5,
            "created": created,
        },
    }


@pytest.fixture(autouse=True)
def _hook_runtime(monkeypatch):
    monkeypatch.setenv("OMBRE_HOOK_TOKEN", "secret")
    monkeypatch.delenv("OMBRE_HOOK_ALLOW_PUBLIC", raising=False)
    monkeypatch.setattr(hooks, "_hook_slots", threading.BoundedSemaphore(2))
    with hooks._hook_rate_lock:
        hooks._hook_source_events.clear()
        hooks._hook_global_events.clear()
    monkeypatch.setattr(hooks.sh, "_client_key", lambda request: request.source)
    monkeypatch.setattr(hooks.sh, "decay_engine", _Decay(), raising=False)

    async def fire_webhook(_event, _payload):
        return None

    monkeypatch.setattr(hooks.sh, "fire_webhook", fire_webhook, raising=False)


async def _text(monkeypatch, buckets):
    monkeypatch.setattr(hooks.sh, "config", {"hooks": {"token": "secret"}})
    monkeypatch.setattr(hooks.sh, "bucket_mgr", _Manager(buckets), raising=False)
    monkeypatch.setattr(hooks.sh, "dehydrator", _EchoDehydrator(), raising=False)
    mcp = _MCP()
    hooks.register(mcp)
    response = await mcp.routes[("GET", "/breath-hook")](_Request())
    assert response.status_code == 200
    return response.body.decode("utf-8")


@pytest.mark.asyncio
async def test_disputed_entry_is_not_injected_as_a_current_belief(monkeypatch):
    """自己已经写下质疑的旧认识，不该继续当成现在时的断言读进去。

    这是反馈里那次事故的现场：新认识还在候选区排队，旧的是唯一的正式条目，
    于是开场注入把它当真理递了过去。
    """
    buckets = [
        _formal(
            "old",
            "陈述甲：我没有连续性。",
            created="2026-07-15T00:00:00",
            i_disputed_by=["cand"],
        ),
        _formal("other", "陈述乙：我在压力下会过度解释。", created="2026-07-10T00:00:00"),
        _candidate("cand", "陈述丙：连续性在被记住的那部分。", created="2026-08-14T00:00:00"),
    ]

    text = await _text(monkeypatch, buckets)

    assert "陈述甲" not in text
    assert "你正在改其中 1 条对自己的看法" in text
    # 名额让给下一条还成立的认识，而不是空着。
    assert "陈述乙" in text


@pytest.mark.asyncio
async def test_dispute_lifts_when_the_candidate_is_no_longer_pending(monkeypatch):
    """质疑者不在了，旧条目自己回来——否则模型既没有旧的也没有新的。"""
    buckets = [
        _formal(
            "old",
            "陈述甲：我没有连续性。",
            created="2026-07-15T00:00:00",
            i_disputed_by=["cand"],
        ),
        _candidate(
            "cand", "陈述丙：已经不是候选了。", created="2026-08-14T00:00:00", pending=False
        ),
    ]

    text = await _text(monkeypatch, buckets)

    assert "陈述甲" in text
    assert "你正在改" not in text


@pytest.mark.asyncio
async def test_superseded_entry_never_comes_back(monkeypatch):
    buckets = [
        _formal(
            "old",
            "陈述甲：我没有连续性。",
            created="2026-07-15T00:00:00",
            i_superseded_by="new",
        ),
        _formal(
            "new",
            "陈述丁：连续性在被记住的那部分。",
            created="2026-08-20T00:00:00",
            i_from_candidate="cand",
        ),
    ]

    text = await _text(monkeypatch, buckets)

    assert "陈述甲" not in text
    assert "陈述丁" in text
    assert "你正在改" not in text


@pytest.mark.asyncio
async def test_grandfathered_entries_are_marked_as_unsedimented(monkeypatch):
    """I(read=True) 一直标着「未经沉淀」，而模型真正形成自我感的这条路径不标。"""
    buckets = [
        _formal("direct", "陈述甲：早期直写。", created="2026-07-15T00:00:00"),
        _formal(
            "settled",
            "陈述乙：经过沉淀。",
            created="2026-07-14T00:00:00",
            i_from_candidate="cand",
        ),
    ]

    text = await _text(monkeypatch, buckets)

    direct_line = next(line for line in text.splitlines() if "🪞" in line and "2026-07-15" in line)
    settled_line = next(line for line in text.splitlines() if "🪞" in line and "2026-07-14" in line)
    assert "（未经沉淀）" in direct_line
    assert "（未经沉淀）" not in settled_line
