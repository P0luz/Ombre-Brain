"""BM25 后台线程重建回归测试（性能 P4）。

脏标记时不再在请求里同步重建（jieba 全库分词 ~17s 阻塞事件循环），改为后台 to_thread
重建、建好原子换入。单次查询用当前索引打分，绝不因 BM25 卡住。
"""
import asyncio

import pytest


@pytest.mark.asyncio
async def test_rebuild_swaps_and_clears_dirty(bucket_mgr):
    if bucket_mgr._bm25 is None:
        pytest.skip("BM25 软依赖未安装（rank_bm25/jieba）")
    await bucket_mgr.create_internal(content="关于向量检索和关键词匹配的记忆", name="检索", domain=["技术"])
    old = bucket_mgr._bm25
    bucket_mgr._bm25_dirty = True
    snapshot = await bucket_mgr.list_all()

    await bucket_mgr._rebuild_bm25_async(snapshot)

    assert bucket_mgr._bm25_dirty is False
    assert bucket_mgr._bm25_rebuilding is False
    assert bucket_mgr._bm25 is not old            # 原子换入了新索引


@pytest.mark.asyncio
async def test_build_returns_fresh_index(bucket_mgr):
    if bucket_mgr._bm25 is None:
        pytest.skip("BM25 软依赖未安装")
    snapshot = await bucket_mgr.list_all()
    idx = bucket_mgr._build_bm25_index(snapshot)
    assert idx is not bucket_mgr._bm25


@pytest.mark.asyncio
async def test_search_does_not_block_on_dirty_bm25(bucket_mgr):
    if bucket_mgr._bm25 is None:
        pytest.skip("BM25 软依赖未安装")
    for i in range(5):
        await bucket_mgr.create_internal(content=f"第{i}条关于检索性能的记忆内容", name=f"桶{i}", domain=["技术"])
    # create 已经把 dirty 置真。一次 search 应立即返回（不同步重建），并调度后台重建。
    bucket_mgr._bm25_dirty = True
    res = await bucket_mgr.search("检索性能")
    assert isinstance(res, list)
    # 后台重建应已被调度：等它跑完，dirty 清零
    for _ in range(50):
        if not bucket_mgr._bm25_dirty and not bucket_mgr._bm25_rebuilding:
            break
        await asyncio.sleep(0.05)
    assert bucket_mgr._bm25_dirty is False


@pytest.mark.asyncio
async def test_only_one_rebuild_launches(bucket_mgr, monkeypatch):
    if bucket_mgr._bm25 is None:
        pytest.skip("BM25 软依赖未安装")
    await bucket_mgr.create_internal(content="内容内容内容内容内容", name="一", domain=["技术"])
    launches = {"n": 0}
    real = bucket_mgr._build_bm25_index

    def counting_build(buckets):
        launches["n"] += 1
        return real(buckets)
    monkeypatch.setattr(bucket_mgr, "_build_bm25_index", counting_build)

    bucket_mgr._bm25_dirty = True
    # 连续两次查询，dirty 期间只应起一个后台重建
    await bucket_mgr.search("一")
    await bucket_mgr.search("一")
    for _ in range(50):
        if not bucket_mgr._bm25_rebuilding:
            break
        await asyncio.sleep(0.05)
    assert launches["n"] == 1
