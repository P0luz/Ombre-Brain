from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pytest

from tools import dream
from tools import _runtime as rt
from tools.i import core as i_core


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
                "valence": kwargs.get("valence", 0.5),
                "arousal": kwargs.get("arousal", 0.3),
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


class _NoopDecay:
    async def ensure_started(self) -> None:
        return None

    def calculate_score(self, _metadata) -> float:
        return 1.0


class _DisabledEmbedding:
    enabled = False


@pytest.fixture
def env(monkeypatch):
    manager = _FakeBucketManager()
    monkeypatch.setattr(rt, "bucket_mgr", manager, raising=False)
    monkeypatch.setattr(rt, "decay_engine", _NoopDecay(), raising=False)
    monkeypatch.setattr(rt, "embedding_engine", _DisabledEmbedding(), raising=False)
    monkeypatch.setattr(rt, "config", {"surfacing": {}}, raising=False)
    monkeypatch.setattr(rt, "logger", logging.getLogger("test"), raising=False)
    monkeypatch.setattr(rt, "mark_op", None, raising=False)
    monkeypatch.setattr(rt, "fire_webhook", None, raising=False)
    return manager


def _flood_plans(manager, count=8, size=400):
    """用 active plan 把 ②~⑤ 段的预算吃掉。

    没用 feel：feel 段是「按相关性挑选」的，合成数据和近期记忆没有词面重合会被
    整段筛掉，于是根本占不到预算——那样测出来的「候选段出现了」只说明预算本来
    就够，跟预留有没有生效无关。plan 段渲染全部 active plan，没有相关性门槛，
    才是这里能稳定复现的那个抢占者。
    """
    now = datetime.now().isoformat()
    for index in range(count):
        bucket_id = f"plan{index}"
        manager.buckets[bucket_id] = {
            "id": bucket_id,
            "content": f"第{index}条计划。" + "要做的事" * size,
            "metadata": {
                "name": bucket_id,
                "type": "plan",
                "tags": [],
                "domain": ["当前"],
                "status": "active",
                "created": now,
                "last_active": now,
                "importance": 5,
                "resolved": False,
            },
        }


def _recent_memory(manager, bucket_id="recent1", text="今天发生了一件普通的事。"):
    """垫一条近期记忆：没有它 dream 会短路成「没有需要消化的新记忆」，
    整个 ①~⑥ 都不渲染，也就测不到预留。"""
    now = datetime.now().isoformat()
    manager.buckets[bucket_id] = {
        "id": bucket_id,
        "content": text,
        "metadata": {
            "name": bucket_id,
            "type": "dynamic",
            "tags": [],
            "domain": ["当前"],
            "created": now,
            "last_active": now,
            "importance": 5,
            "resolved": False,
        },
    }


async def _a_candidate(manager) -> str:
    await i_core.i_core(content="我觉得我需要更早说不。")
    return next(
        bucket["id"]
        for bucket in manager.buckets.values()
        if i_core.is_pending_candidate(bucket)
    )


@pytest.mark.asyncio
async def test_ready_candidates_are_listed_compactly(env):
    """攒够的压成一行提醒，不再占整块预算，也不再计见证。"""
    candidate_id = await _a_candidate(env)
    dates = ["2026-08-01", "2026-08-02", "2026-08-03"]
    # 同样放到窗口外：①段的近期正文本身就会记见证，那条路和这里无关。
    old = (datetime.now() - timedelta(days=30)).isoformat()
    await env.update(
        candidate_id, i_dream_dates=list(dates), created=old, last_active=old
    )
    _recent_memory(env)

    out = await dream.dispatch(window_hours=48)

    assert "已经攒够见证" in out
    assert candidate_id in out
    # 压成一行的不算见证：它需要的是模型去 promote，不是再看一遍。
    assert env.buckets[candidate_id]["metadata"]["i_dream_dates"] == dates


@pytest.mark.asyncio
async def test_ready_candidates_alone_do_not_short_circuit_the_dream(env):
    """没有近期记忆、只剩攒够的候选时，dream 不该整段短路。

    上面那条用例垫了 `_recent_memory`，而短路只在**没有**近期记忆时发生——
    于是「所有候选都攒够」这个最该提醒的时刻，反而是唯一提醒不出来的时刻。
    """
    candidate_id = await _a_candidate(env)
    old = (datetime.now() - timedelta(days=30)).isoformat()
    await env.update(
        candidate_id,
        i_dream_dates=["2026-08-01", "2026-08-02", "2026-08-03"],
        created=old,
        last_active=old,
    )

    out = await dream.dispatch(window_hours=48)

    assert "没有需要消化的新记忆" not in out
    assert "已经攒够见证" in out
    assert candidate_id in out
