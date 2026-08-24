"""存量 feel 回填：把 triggered_by 落成 references 边 + referenced_by 反边。

- collect_missing_feel_links：纯判定，找「triggered_by 有值但缺 references 边」的 feel。
- backfill_feel_links：幂等写入（feel → 源事件 references；源事件 → feel referenced_by）。
"""

from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from tools._relation_link import collect_missing_feel_links, backfill_feel_links


def _bucket(bid, btype="dynamic", triggered_by="", links=None):
    meta = {"type": btype}
    if triggered_by:
        meta["triggered_by"] = triggered_by
    if links:
        meta["relation_links"] = links
    return {"id": bid, "content": bid, "metadata": meta}


# ---------------- 纯判定 ----------------

def test_collect_finds_missing_feel_link():
    buckets = [
        _bucket("f1", btype="feel", triggered_by="e1"),
        _bucket("e1"),
    ]
    assert collect_missing_feel_links(buckets) == [("f1", "e1")]


def test_collect_skips_existing_reference():
    buckets = [
        _bucket("f1", btype="feel", triggered_by="e1", links=[
            {"target_bucket_id": "e1", "type": "references", "label": "", "status": "active"},
        ]),
        _bucket("e1"),
    ]
    assert collect_missing_feel_links(buckets) == []


def test_collect_skips_missing_source_and_self():
    buckets = [
        _bucket("f1", btype="feel", triggered_by="gone"),   # 源不存在
        _bucket("f2", btype="feel", triggered_by="f2"),     # 自己连自己
    ]
    assert collect_missing_feel_links(buckets) == []


def test_collect_only_scans_feel_buckets():
    # 非 feel 桶即使有 triggered_by 也不该被扫到
    buckets = [
        _bucket("d1", btype="dynamic", triggered_by="e1"),
        _bucket("e1"),
    ]
    assert collect_missing_feel_links(buckets) == []


# ---------------- 写入（幂等） ----------------

class _FrontmatterLike:
    """模拟 frontmatter.load() 返回的对象：支持下标读写，键落在 metadata 上。"""

    def __init__(self, metadata):
        self.metadata = metadata

    def __getitem__(self, key):
        return self.metadata.get(key)

    def __setitem__(self, key, value):
        self.metadata[key] = value


class FakeBucketManager:
    def __init__(self, buckets):
        self.buckets = {b["id"]: b for b in buckets}

    async def mutate_relation_links(self, bucket_id, mutation):
        bucket = self.buckets.get(bucket_id)
        if bucket is None:
            return None
        post = _FrontmatterLike(bucket["metadata"])
        changed, result = mutation(post)
        if changed:
            bucket["metadata"] = post.metadata
        return result


@pytest.mark.asyncio
async def test_backfill_builds_both_sides_and_is_idempotent():
    mgr = FakeBucketManager([
        _bucket("f1", btype="feel", triggered_by="e1"),
        _bucket("e1"),
    ])
    rt.init(bucket_mgr=mgr, logger=MagicMock())

    built = await backfill_feel_links(list(mgr.buckets.values()))
    assert built >= 2  # 正边 + 反边各一条

    feel_links = mgr.buckets["f1"]["metadata"].get("relation_links", [])
    event_links = mgr.buckets["e1"]["metadata"].get("relation_links", [])
    assert any(l["type"] == "references" and l["target_bucket_id"] == "e1" for l in feel_links)
    assert any(l["type"] == "referenced_by" and l["target_bucket_id"] == "f1" for l in event_links)

    # 幂等：再跑一遍不再新建
    built_again = await backfill_feel_links(list(mgr.buckets.values()))
    assert built_again == 0
