"""event_time 与 references 的写入/读取行为。

- event_time：回溯记忆的语义时间，thread 串珠按它排序（优先于 created）。
- references：模型显式声明的引用边，落成 relation_links 的 references 类型。
"""

import pytest

import tools._runtime as rt
from tools.thread import thread
from ombrebrain.storage.relation_store import (
    bucket_date,
    normalize_relation_links,
    render_junction,
)


def _bucket(bid, title="", created="", event_time="", btype="dynamic", content="", links=None):
    meta = {}
    if title:
        meta["title"] = title
    if created:
        meta["created"] = created
    if event_time:
        meta["event_time"] = event_time
    if btype != "dynamic":
        meta["type"] = btype
    if links:
        meta["relation_links"] = links
    return {"id": bid, "content": content or title, "metadata": meta}


class FakeBucketManager:
    def __init__(self, buckets):
        self.buckets = list(buckets)

    async def search(self, query, limit=10, **kwargs):
        return list(self.buckets)

    async def get(self, bucket_id):
        for b in self.buckets:
            if b["id"] == bucket_id:
                return b
        return None


@pytest.mark.asyncio
async def test_thread_sorts_by_event_time_not_created():
    """回溯记忆的 created 相同（同一天记下），但 event_time 不同，thread 应按 event_time 排序。"""
    mgr = FakeBucketManager(
        [
            # created 都是同一天（回溯写入日），event_time 是真实发生时间
            _bucket("mar", title="三月的事", created="2026-08-10T00:00:00", event_time="2025-03-01T00:00:00"),
            _bucket("dec", title="十二月的事", created="2026-08-10T00:00:00", event_time="2025-12-05T00:00:00"),
            _bucket("apr", title="四月的事", created="2026-08-10T00:00:00", event_time="2025-04-20T00:00:00"),
        ]
    )
    rt.init(bucket_mgr=mgr, mark_op=None, config={})
    out = await thread("记忆")
    assert "共 3 站" in out
    # event_time 升序：3月 → 4月 → 12月
    assert out.index("三月的事") < out.index("四月的事") < out.index("十二月的事")


@pytest.mark.asyncio
async def test_thread_falls_back_to_created_when_no_event_time():
    mgr = FakeBucketManager(
        [
            _bucket("a", title="早", created="2026-07-01T00:00:00"),
            _bucket("b", title="晚", created="2026-08-01T00:00:00"),
        ]
    )
    rt.init(bucket_mgr=mgr, mark_op=None, config={})
    out = await thread("记忆")
    assert out.index("早") < out.index("晚")


@pytest.mark.asyncio
async def test_bucket_date_prefers_event_time():
    assert bucket_date({"event_time": "2025-12-05T00:00:00", "created": "2026-08-10T00:00:00"}) == "2025-12-05"
    assert bucket_date({"created": "2026-08-10T00:00:00"}) == "2026-08-10"


@pytest.mark.asyncio
async def test_render_junction_shows_references():
    bucket = _bucket(
        "a",
        title="引用了别处的记忆",
        links=[
            {"target_bucket_id": "b", "type": "references", "label": "", "status": "active"},
            {"target_bucket_id": "c", "type": "related_to", "label": "", "status": "active"},
        ],
    )

    async def get_neighbor(nid):
        return {"id": nid, "content": "正文", "metadata": {"title": f"目标{nid}", "created": "2025-12-05T00:00:00"}}

    junction = await render_junction(bucket, get_neighbor)
    assert "🔗 引用（1）" in junction
    assert "目标b" in junction
    assert "↔ 相关（1）" in junction


@pytest.mark.asyncio
async def test_normalize_accepts_references_type():
    links = normalize_relation_links(
        [
            {"target_bucket_id": "b", "type": "references", "label": "", "status": "active"},
            {"target_bucket_id": "c", "type": "referenced_by", "label": "", "status": "active"},
        ]
    )
    assert [l["type"] for l in links] == ["references", "referenced_by"]


@pytest.mark.asyncio
async def test_create_writes_event_time_and_references(bucket_mgr):
    """真实落盘：create 应把 event_time 和 references 写进 metadata。"""
    ref_id = await bucket_mgr.create(content="先有的一段记忆", domain=["测试"])
    bid = await bucket_mgr.create(
        content="后写的一段，提到了之前",
        domain=["测试"],
        event_time="2025-12-05",
        references=[ref_id],
    )
    bucket = await bucket_mgr.get(bid)
    meta = bucket["metadata"]
    assert meta.get("event_time") == "2025-12-05"
    assert meta.get("event_time_source") == "manual"
    links = meta.get("relation_links") or []
    refs = [l for l in links if l.get("type") == "references"]
    assert len(refs) == 1
    assert refs[0]["target_bucket_id"] == ref_id
    assert refs[0].get("auto") is not True  # 手动边，不标 auto
