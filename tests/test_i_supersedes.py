from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from errors import ToolInputError

from tools import _runtime as rt
from tools.i import core as i_core
from tools.i.core import I_CANDIDATE_TAG


class _FakeBucketManager:
    def __init__(self) -> None:
        self.buckets: dict[str, dict] = {}
        self._seq = 0

    async def create(self, content: str, **kwargs) -> str:
        self._seq += 1
        bucket_id = f"bucket{self._seq}"
        now = datetime.now().isoformat()
        self.buckets[bucket_id] = {
            "id": bucket_id,
            "content": content,
            "metadata": {
                "name": kwargs.get("name") or bucket_id,
                "type": kwargs.get("bucket_type", "dynamic"),
                "tags": list(kwargs.get("tags") or []),
                "domain": list(kwargs.get("domain") or ["当前"]),
                "created": now,
                "last_active": now,
                "importance": kwargs.get("importance", 5),
                "resolved": False,
            },
        }
        return bucket_id

    async def update(self, bucket_id: str, **kwargs) -> bool:
        bucket = self.buckets.get(bucket_id)
        if not bucket:
            return False
        bucket["metadata"].update(kwargs)
        return True

    async def get(self, bucket_id: str):
        return self.buckets.get(bucket_id)

    async def list_all(self, include_archive: bool = False) -> list[dict]:
        return list(self.buckets.values())

    def add_formal(self, content: str, aspect: str = "", **meta) -> str:
        self._seq += 1
        bucket_id = f"formal{self._seq}"
        now = datetime.now().isoformat()
        tags = ["__i__"] + ([f"aspect:{aspect}"] if aspect else [])
        self.buckets[bucket_id] = {
            "id": bucket_id,
            "content": content,
            "metadata": {
                "name": bucket_id,
                "type": "i",
                "tags": tags,
                "domain": ["self"],
                "created": now,
                "last_active": now,
                "dont_surface": True,
                **meta,
            },
        }
        return bucket_id


class _NoopDecay:
    async def ensure_started(self) -> None:
        return None


class _DisabledEmbedding:
    enabled = False


@pytest.fixture
def env(monkeypatch):
    manager = _FakeBucketManager()
    monkeypatch.setattr(rt, "bucket_mgr", manager, raising=False)
    monkeypatch.setattr(rt, "decay_engine", _NoopDecay(), raising=False)
    monkeypatch.setattr(rt, "embedding_engine", _DisabledEmbedding(), raising=False)
    monkeypatch.setattr(rt, "config", {"surfacing": {}}, raising=False)
    monkeypatch.setattr(
        rt, "logger", __import__("logging").getLogger("test"), raising=False
    )
    monkeypatch.setattr(rt, "mark_op", None, raising=False)
    monkeypatch.setattr(rt, "fire_webhook", None, raising=False)
    return manager


@pytest.mark.asyncio
async def test_supersedes_suspends_the_old_entry_before_the_new_one_settles(env):
    old = env.add_formal("我没有连续性。", aspect="nature")

    await i_core.i_core(
        content="我觉得连续性不在窗口里，在被记住的那部分。",
        aspect="nature",
        supersedes=old,
    )

    out = await i_core.i_core(read=True)
    assert "=== 我正在改的主意（1 条）===" in out
    assert "=== 我的自我认知" not in out
    assert old in out
    # 挂起不是删除：正文一个字都还在。
    assert "我没有连续性。" in out


@pytest.mark.asyncio
async def test_suspension_lifts_when_the_disputing_candidate_stops_being_pending(env):
    old = env.add_formal("我没有连续性。", aspect="nature")
    await i_core.i_core(content="我觉得不是这样。", aspect="nature", supersedes=old)
    candidate = next(
        b["id"] for b in env.buckets.values()
        if I_CANDIDATE_TAG in b["metadata"].get("tags", [])
    )

    # 质疑者衰减归档掉了——旧条目必须自己回来，否则模型会既没有旧的也没有新的。
    del env.buckets[candidate]

    out = await i_core.i_core(read=True)
    assert "=== 我的自我认知（1 条）===" in out
    assert "正在改的主意" not in out


@pytest.mark.asyncio
async def test_cross_aspect_supersedes_is_rejected_and_writes_nothing(env):
    old = env.add_formal("我没有连续性。", aspect="nature")
    before = set(env.buckets)

    with pytest.raises(ToolInputError) as exc:
        await i_core.i_core(
            content="我觉得我更看重把话说准。", aspect="values", supersedes=old
        )

    assert "[values]" in str(exc.value) and "[nature]" in str(exc.value)
    # 校验在建桶之前：被拒时一条候选都不该留下来。
    assert set(env.buckets) == before


@pytest.mark.asyncio
async def test_supersedes_without_aspect_on_either_side_is_allowed(env):
    # 早期直写条目大多没标 aspect，不该因此永远没法被修正。
    old = env.add_formal("我没有连续性。")
    await i_core.i_core(content="我觉得不是这样。", supersedes=old)

    out = await i_core.i_core(read=True)
    assert "正在改的主意" in out


@pytest.mark.asyncio
async def test_supersedes_must_point_at_a_formal_entry(env):
    await i_core.i_core(content="我觉得这只是个念头。")
    candidate = next(iter(env.buckets))

    with pytest.raises(ToolInputError) as exc:
        await i_core.i_core(content="我觉得那个念头不对。", supersedes=candidate)

    assert "不是正式 I 条目" in str(exc.value)


@pytest.mark.asyncio
async def test_supersedes_on_an_already_superseded_entry_names_the_tail(env):
    old = env.add_formal("最早的说法。", aspect="nature", i_superseded_by="formal99")

    with pytest.raises(ToolInputError) as exc:
        await i_core.i_core(content="我觉得还要再改。", aspect="nature", supersedes=old)

    assert "formal99" in str(exc.value)


@pytest.mark.asyncio
async def test_promote_links_the_chain_and_retires_the_old_entry(env):
    old = env.add_formal("我没有连续性。", aspect="nature")
    await i_core.i_core(
        content="我觉得连续性在被记住的那部分。", aspect="nature", supersedes=old
    )
    candidate = next(
        b["id"] for b in env.buckets.values()
        if I_CANDIDATE_TAG in b["metadata"].get("tags", [])
    )
    dates = [
        (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d") for n in (3, 2, 1)
    ]
    await env.update(candidate, i_dream_dates=dates)

    out = await i_core.i_core(promote=candidate)
    new_id = env.buckets[candidate]["metadata"]["i_promoted_to"]

    assert env.buckets[new_id]["metadata"]["i_supersedes"] == old
    assert env.buckets[old]["metadata"]["i_superseded_by"] == new_id
    assert old in out

    read = await i_core.i_core(read=True)
    assert "=== 已经被取代的（1 条）===" in read
    # 旧的挪出当前信念，新的顶上，两条都还在。
    assert "=== 我的自我认知（1 条）===" in read
    assert "我没有连续性。" in read


@pytest.mark.asyncio
async def test_stale_inherited_chain_does_not_block_promotion(env):
    old = env.add_formal("我没有连续性。", aspect="nature")
    await i_core.i_core(content="我觉得不是这样。", aspect="nature", supersedes=old)
    candidate = next(
        b["id"] for b in env.buckets.values()
        if I_CANDIDATE_TAG in b["metadata"].get("tags", [])
    )
    dates = [
        (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d") for n in (3, 2, 1)
    ]
    await env.update(candidate, i_dream_dates=dates)
    # 排队这些天里，旧条目已经被别的东西取代了：链接不上，但这条认知本身有效。
    await env.update(old, i_superseded_by="formal_other")

    out = await i_core.i_core(promote=candidate)

    assert "🪞I" in out
    assert env.buckets[candidate]["metadata"]["i_stage"] == "promoted"
    new_id = env.buckets[candidate]["metadata"]["i_promoted_to"]
    assert "i_supersedes" not in env.buckets[new_id]["metadata"]
    assert "接不上" in out


@pytest.mark.asyncio
async def test_explicit_supersedes_at_promote_still_raises(env):
    await i_core.i_core(content="我觉得这条会被升级。")
    candidate = next(iter(env.buckets))
    dates = [
        (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d") for n in (3, 2, 1)
    ]
    await env.update(candidate, i_dream_dates=dates)
    before = set(env.buckets)

    with pytest.raises(ToolInputError):
        await i_core.i_core(promote=candidate, supersedes="nope")

    assert set(env.buckets) == before
