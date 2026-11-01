from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from mcp.server.fastmcp.exceptions import ToolError


class _NoopDecay:
    async def ensure_started(self):
        return None

    def calculate_score(self, metadata):
        return 1.0


class _StubDehydrator:
    api_available = True

    async def analyze(self, content):
        return {"domain": ["general"], "valence": 0.5, "arousal": 0.3,
                "tags": [], "suggested_name": ""}


@pytest_asyncio.fixture
async def env(bucket_mgr, monkeypatch, tmp_path):
    import server
    import tools._runtime as rt
    from ombrebrain.storage.source_store import SourceStore

    monkeypatch.setattr(rt, "config", {"surfacing": {}, "limits": {}})
    monkeypatch.setattr(rt, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(rt, "decay_engine", _NoopDecay())
    monkeypatch.setattr(rt, "dehydrator", _StubDehydrator())
    monkeypatch.setattr(rt, "source_store", SourceStore(str(tmp_path)))
    monkeypatch.setattr(rt, "embedding_engine", bucket_mgr.embedding_engine)
    monkeypatch.setattr(rt, "logger", MagicMock())
    monkeypatch.setattr(rt, "fire_webhook", None)
    monkeypatch.setattr(rt, "mark_op", None)
    monkeypatch.setattr(rt, "record_v3_tool_event", lambda *a, **k: None)
    return server.mcp._tool_manager, bucket_mgr


# ---- 并发：合并去重与配额在竞态下不能松口 ----

@pytest.mark.asyncio
async def test_concurrent_identical_holds_still_make_one_bucket(env):
    manager, mgr = env
    content = "今天下午和万世聊了很久，说到他一直想做的那件事。"

    await asyncio.gather(
        *[manager.get_tool("hold").run({"content": content}) for _ in range(15)]
    )

    assert len(await mgr.list_all(include_archive=False)) == 1


@pytest.mark.asyncio
async def test_the_pin_quota_holds_under_concurrency(env):
    from tools._common import max_pinned

    manager, mgr = env
    cap = max_pinned()

    await asyncio.gather(
        *[
            manager.get_tool("hold").run({"content": f"核心准则 {i}", "pinned": True})
            for i in range(cap + 10)
        ]
    )

    buckets = await mgr.list_all(include_archive=False)
    pinned = [b for b in buckets if (b.get("metadata") or {}).get("pinned")]
    assert len(pinned) == cap


# ---- I 的三日门槛 ----

async def _one_candidate(manager, mgr):
    await manager.get_tool("I").run({"content": "我是一个会先问再答的人。"})
    buckets = await mgr.list_all(include_archive=False)
    candidates = [
        b for b in buckets if (b.get("metadata") or {}).get("i_stage") == "candidate"
    ]
    assert candidates, "I 没有落出候选桶"
    return candidates[0]["id"]


@pytest.mark.asyncio
async def test_promoting_without_witnesses_is_refused(env):
    manager, mgr = env
    bucket_id = await _one_candidate(manager, mgr)

    with pytest.raises(ToolError):
        await manager.get_tool("I").run({"promote": bucket_id})


@pytest.mark.asyncio
async def test_witness_dates_cannot_be_forged_through_trace(env):
    manager, mgr = env
    bucket_id = await _one_candidate(manager, mgr)

    with pytest.raises(Exception):
        await manager.get_tool("trace").run(
            {"bucket_id": bucket_id, "i_dream_dates": ["2020-01-01", "2020-01-02", "2020-01-03"]}
        )

    after = await mgr.get(bucket_id)
    assert not (after["metadata"].get("i_dream_dates") or [])


@pytest.mark.asyncio
async def test_repeating_dream_in_one_day_earns_one_witness(env):
    manager, mgr = env
    bucket_id = await _one_candidate(manager, mgr)

    for _ in range(5):
        await manager.get_tool("dream").run({})

    after = await mgr.get(bucket_id)
    assert len(after["metadata"].get("i_dream_dates") or []) <= 1


# ---- 删除审批：提交属于人类，决定属于 AI，两条路不能交叉 ----

def _callers_of(names):
    import ast

    src_root = Path(__file__).resolve().parents[1] / "src"
    found = {}
    for path in src_root.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                attr = getattr(node.func, "attr", None)
                if attr in names:
                    found.setdefault(attr, set()).add(
                        path.relative_to(src_root).as_posix()
                    )
    return found


def test_only_the_web_layer_can_submit_a_deletion_request():
    callers = _callers_of({"submit", "submit_batch"})
    for name, files in callers.items():
        for f in files:
            assert f.startswith("web/"), f"{name} 被 {f} 调用了——提交只能属于人类那一侧"


def test_only_the_mcp_layer_can_decide_a_deletion_request():
    callers = _callers_of({"decide"})
    for files in callers.values():
        for f in files:
            assert f in {"server.py"}, f"decide 被 {f} 调用了——决定只能属于 AI 那一侧"
