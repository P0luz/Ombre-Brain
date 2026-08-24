"""thread 的读取行为：话题时间线按时间升序、每站一行、类型排除、空查询提示。"""

import pytest

import tools._runtime as rt
from tools.thread import thread


def _bucket(bid, title="", created="", btype="dynamic", content=""):
    meta = {}
    if title:
        meta["title"] = title
    if created:
        meta["created"] = created
    if btype != "dynamic":
        meta["type"] = btype
    return {"id": bid, "content": content or title, "metadata": meta}


class FakeBucketManager:
    def __init__(self, buckets):
        self.buckets = list(buckets)
        self.search_calls = 0

    async def search(self, query, limit=10, **kwargs):
        self.search_calls += 1
        return list(self.buckets)


@pytest.mark.asyncio
async def test_thread_sorts_by_time_ascending():
    # search 返回相关性顺序（故意乱时间），thread 必须按 created 升序重排
    mgr = FakeBucketManager(
        [
            _bucket("now", title="现在聊的", created="2026-08-21T00:00:00"),
            _bucket("old", title="最早聊的", created="2026-07-01T00:00:00"),
            _bucket("mid", title="中间聊的", created="2026-08-01T00:00:00"),
        ]
    )
    rt.init(bucket_mgr=mgr, mark_op=None, config={})
    out = await thread("记忆")
    assert "共 3 站" in out
    # 时间升序：最早 → 中间 → 现在
    assert out.index("最早聊的") < out.index("中间聊的") < out.index("现在聊的")
    assert "1." in out and "2." in out and "3." in out
    assert "2026-07-01" in out and "2026-08-21" in out


@pytest.mark.asyncio
async def test_thread_excludes_special_types():
    mgr = FakeBucketManager(
        [
            _bucket("a", title="普通记忆", created="2026-08-01T00:00:00"),
            _bucket("f", title="感受", created="2026-08-02T00:00:00", btype="feel"),
            _bucket("p", title="待办", created="2026-08-03T00:00:00", btype="plan"),
        ]
    )
    rt.init(bucket_mgr=mgr, mark_op=None, config={})
    out = await thread("记忆")
    assert "普通记忆" in out
    assert "感受" not in out
    assert "待办" not in out
    assert "共 1 站" in out


@pytest.mark.asyncio
async def test_thread_empty_query():
    rt.init(bucket_mgr=FakeBucketManager([]), mark_op=None, config={})
    out = await thread("")
    assert "关键词" in out


@pytest.mark.asyncio
async def test_thread_no_results():
    rt.init(bucket_mgr=FakeBucketManager([]), mark_op=None, config={})
    out = await thread("不存在的话题")
    assert "串得起来" in out
