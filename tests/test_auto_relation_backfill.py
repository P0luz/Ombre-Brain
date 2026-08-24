"""backfill_auto_relations：给存量桶全量回填自动关系边。

- embedding 未启用时安全返回
- 有相似对时建边（auto=True），幂等（重复跑不重复建）
"""

from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from tools._relation_link import backfill_auto_relations


class FakeSimEngine:
    """search_similar 固定返回指定的 (bucket_id, score) 对。"""

    def __init__(self, pairs):
        self.enabled = True
        self.pairs = list(pairs)

    async def search_similar(self, query, top_k=10):
        return list(self.pairs)


@pytest.fixture
def rel_runtime(monkeypatch, bucket_mgr, test_config):
    monkeypatch.setattr(rt, "bucket_mgr", bucket_mgr, raising=False)
    monkeypatch.setattr(rt, "logger", MagicMock(), raising=False)
    monkeypatch.setattr(rt, "embedding_engine", None, raising=False)
    return bucket_mgr


@pytest.mark.asyncio
async def test_backfill_without_engine_is_safe(rel_runtime):
    result = await backfill_auto_relations([])
    assert result["engine_disabled"] == 1
    assert result["built"] == 0


@pytest.mark.asyncio
async def test_backfill_builds_and_is_idempotent(rel_runtime):
    manager = rel_runtime
    a = await manager.create(content="A 的正文", domain=["测试"])
    b = await manager.create(content="B 的正文", domain=["测试"])
    # 让相似检索命中真实的 b（对任何 query 都返回 b 的真实 id）
    rt.embedding_engine = FakeSimEngine([(b, 0.9)])

    all_buckets = await manager.list_all(include_archive=False)
    first = await backfill_auto_relations(all_buckets)
    assert first["scanned"] >= 1
    assert first["built"] >= 1

    alinks = (await manager.get(a))["metadata"]["relation_links"]
    assert any(
        l["target_bucket_id"] == b
        and l["type"] in ("same_event", "continuation_of", "related_to")
        and l.get("auto") is True
        for l in alinks
    )

    # 幂等：边已存在，再跑一遍 built 应为 0
    all_buckets = await manager.list_all(include_archive=False)
    second = await backfill_auto_relations(all_buckets)
    assert second["built"] == 0
