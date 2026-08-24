"""feel 定向连回源事件（3.9.0）。

- store_feel 带 source_bucket：落 references 边（feel → 源事件）+ 即时补 referenced_by 反边。
- feel 桶 recall：只显示 references 定向边，不显示其他边（不横向乱连）。
- 源事件 recall：通过 referenced_by 顺出当时的感受。
- feel 检索：语义优先（>=0.65），字面只在无语义命中时兜底（改回原版，模糊优先）。
"""

from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from tools.hold.feel import store_feel
from tools.recall import recall
from tools.breath.feel import surface_feels


@pytest.fixture
def feel_runtime(monkeypatch, bucket_mgr):
    monkeypatch.setattr(rt, "bucket_mgr", bucket_mgr, raising=False)
    monkeypatch.setattr(rt, "logger", MagicMock(), raising=False)
    monkeypatch.setattr(rt, "mark_op", None, raising=False)
    return bucket_mgr


@pytest.mark.asyncio
async def test_store_feel_links_source_as_references(feel_runtime):
    manager = feel_runtime
    src = await manager.create(content="源事件正文", domain=["测试"])
    out = await store_feel(
        content="那一刻的感受",
        extra_tags=[],
        valence=0.7,
        arousal=0.5,
        source_bucket=src,
        why_remembered="",
    )
    feel_id = out.split("→")[-1]
    assert feel_id.startswith("feel_")

    # feel 桶身上有 references 边指向源事件
    feel_meta = (await manager.get(feel_id))["metadata"]
    refs = [l for l in feel_meta.get("relation_links", []) if l["type"] == "references"]
    assert len(refs) == 1 and refs[0]["target_bucket_id"] == src
    assert refs[0]["status"] == "active"

    # 源事件身上有 referenced_by 反边指向 feel（即时补，不等 dream）
    src_meta = (await manager.get(src))["metadata"]
    rev = [l for l in src_meta.get("relation_links", []) if l["type"] == "referenced_by"]
    assert len(rev) == 1 and rev[0]["target_bucket_id"] == feel_id


def _bucket(bid, title="", created="", btype="dynamic", content="", links=None):
    meta = {}
    if title:
        meta["title"] = title
    if created:
        meta["created"] = created
    if btype != "dynamic":
        meta["type"] = btype
    if links:
        meta["relation_links"] = links
    return {"id": bid, "content": content or title, "metadata": meta}


class FakeMgr:
    def __init__(self, buckets):
        self.buckets = {b["id"]: b for b in buckets}

    async def get(self, bucket_id):
        return self.buckets.get(bucket_id)


@pytest.mark.asyncio
async def test_feel_recall_shows_only_references():
    """feel 桶 recall 只显示 references 定向边，不显示 related_to（不横向乱连）。"""
    feel = _bucket(
        "f1",
        btype="feel",
        content="感受正文",
        links=[
            {"target_bucket_id": "e1", "type": "references", "label": "", "status": "active"},
            {"target_bucket_id": "e2", "type": "related_to", "label": "", "status": "active"},
        ],
    )
    mgr = FakeMgr(
        [
            feel,
            _bucket("e1", title="源事件", created="2025-12-05T00:00:00", content="事件1"),
            _bucket("e2", title="无关事件", created="2025-12-06T00:00:00", content="事件2"),
        ]
    )
    rt.init(bucket_mgr=mgr, mark_op=None)
    out = await recall("f1")
    assert "引用" in out
    assert "源事件" in out
    assert "无关事件" not in out  # related_to 对 feel 不显示


@pytest.mark.asyncio
async def test_event_recall_surfaces_feel():
    """源事件 recall 通过 referenced_by 顺出当时的感受。"""
    event = _bucket(
        "e1",
        content="事件正文",
        links=[
            {"target_bucket_id": "f1", "type": "referenced_by", "label": "", "status": "active"},
        ],
    )
    mgr = FakeMgr(
        [
            event,
            _bucket("f1", title="那条感受", btype="feel", content="感受"),
        ]
    )
    rt.init(bucket_mgr=mgr, mark_op=None)
    out = await recall("e1")
    assert "被引用" in out
    assert "那条感受" in out


class FakeEmbed:
    enabled = True

    async def search_similar(self, query, top_k=10, allowed_bucket_ids=None):
        # 语义只命中 b_id（不相关），字面命中的 a_id 应由合并逻辑补上
        return [("b_id", 0.8)]


class ListMgr:
    def __init__(self, buckets):
        self.buckets = buckets

    async def list_all(self, include_archive=False):
        return list(self.buckets)

    def footprint_snapshot(self):
        return None


class FakeEmbedNone:
    enabled = True

    async def search_similar(self, query, top_k=10, allowed_bucket_ids=None):
        # 语义无命中：字面兜底应生效
        return []


@pytest.mark.asyncio
async def test_feel_search_semantic_suppresses_literal():
    """语义有命中时，字面不兜底——感受靠共振连接，不被精确匹配挤掉惊喜感。"""
    feels = [
        {"id": "a_id", "content": "下雨天的感受", "metadata": {"type": "feel", "created": "2025-01-01T00:00:00"}},
        {"id": "b_id", "content": "今天好累", "metadata": {"type": "feel", "created": "2025-01-02T00:00:00"}},
    ]
    rt.init(bucket_mgr=ListMgr(feels), embedding_engine=FakeEmbed(), logger=MagicMock(), mark_op=None)
    out = await surface_feels(query="下雨", max_tokens=10000)
    assert "b_id" in out  # 语义命中（embedding 返回 b_id，0.8 >= 0.65）
    assert "a_id" not in out  # 字面命中的「下雨」不并进来——模糊优先


@pytest.mark.asyncio
async def test_feel_search_literal_fallback_when_no_semantic():
    """向量无命中时，字面兜底仍生效——确定找回的路没断。"""
    feels = [
        {"id": "a_id", "content": "下雨天的感受", "metadata": {"type": "feel", "created": "2025-01-01T00:00:00"}},
    ]
    rt.init(bucket_mgr=ListMgr(feels), embedding_engine=FakeEmbedNone(), logger=MagicMock(), mark_op=None)
    out = await surface_feels(query="下雨", max_tokens=10000)
    assert "a_id" in out  # 语义空 → 字面兜底接住
