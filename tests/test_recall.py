"""recall 的读取行为：路口按方向分组、邻居标题/日期、类型排除、无关系提示。"""

import pytest

import tools._runtime as rt
from tools.recall import recall


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
    return {"id": bid, "content": content, "metadata": meta}


class FakeBucketManager:
    def __init__(self, buckets):
        self.buckets = {b["id"]: b for b in buckets}

    async def get(self, bucket_id):
        return self.buckets.get(bucket_id)


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_recall_renders_junction_by_direction():
    mgr = FakeBucketManager(
        [
            _bucket(
                "a",
                title="起点",
                created="2026-08-01T00:00:00",
                content="这是起点。",
                links=[
                    {"target_bucket_id": "b", "type": "continuation_of", "label": "", "status": "active"},
                    {"target_bucket_id": "c", "type": "same_event", "label": "", "status": "active"},
                ],
            ),
            _bucket("b", title="上游", created="2026-07-01T00:00:00", content="上游。"),
            _bucket("c", title="同刻", created="2026-08-01T02:00:00", content="同刻。"),
        ]
    )
    rt.init(bucket_mgr=mgr, mark_op=None)
    out = await recall("a")
    assert "起点" in out
    assert "← 之前（1）" in out
    assert "上游" in out and "〔2026-07-01〕" in out
    assert "≈ 同刻（1）" in out
    assert "这是起点" in out  # 正文逐字返回


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_recall_reports_isolated_pearl():
    mgr = FakeBucketManager([_bucket("a", title="孤珠", content="没有连接。")])
    rt.init(bucket_mgr=mgr, mark_op=None)
    out = await recall("a")
    assert "还没被串起来的珍珠" in out


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_recall_excludes_special_types():
    mgr = FakeBucketManager(
        [
            _bucket(
                "a",
                title="普通",
                links=[
                    {"target_bucket_id": "f", "type": "related_to", "label": "", "status": "active"},
                ],
            ),
            _bucket("f", title="感受", btype="feel"),
        ]
    )
    rt.init(bucket_mgr=mgr, mark_op=None)
    out = await recall("a")
    assert "还没被串起来的珍珠" in out  # feel 邻居被排除


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_recall_missing_bucket():
    mgr = FakeBucketManager([])
    rt.init(bucket_mgr=mgr, mark_op=None)
    out = await recall("nope")
    assert "找不到" in out
