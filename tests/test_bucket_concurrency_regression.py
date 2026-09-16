"""bucket_manager 并发一致性回归测试 —— 验证 _bucket_turn 真的把同一个

bucket_id 上的 archive()/update()/delete()/touch() 串行化了。

找茬会话复现场景（2026-07-15）：archive()/update()/delete()/touch() 各自独立做
find_file → load → mutate → atomic_write，互不知会。衰减引擎后台 archive() 撞上
一次 trace/hold 的 update() 时，后到的那个可能基于自己读到的旧 file_path 写回，
在文件已经被 move 进 archive/ 之后，在原路径「复活」一份带旧内容的桶——同一个
bucket_id 最终在 archive/ 和 dynamic/ 各留一份。

本文件用 asyncio.gather 并发触发 archive() + update()，锁死「结束后这个
bucket_id 只应该存在一份」这个不变量。
"""
import asyncio

import pytest


@pytest.mark.asyncio
async def test_concurrent_archive_and_update_leaves_single_copy(bucket_mgr):
    bid = await bucket_mgr.create_internal(content="并发前的原始内容")

    await asyncio.gather(
        bucket_mgr.archive(bid),
        bucket_mgr.update(bid, content="并发时的新内容"),
        return_exceptions=True,
    )

    all_buckets = await bucket_mgr.list_all(include_archive=True)
    matches = [b for b in all_buckets if b["id"] == bid]
    assert len(matches) == 1, (
        f"bucket_id={bid} 结束后应该只有 1 份，实际 {len(matches)} 份: "
        f"{[(b['metadata'].get('type'), b['content']) for b in matches]}"
    )


@pytest.mark.asyncio
async def test_concurrent_touch_and_delete_leaves_single_copy(bucket_mgr):
    bid = await bucket_mgr.create_internal(content="并发前的原始内容")

    await asyncio.gather(
        bucket_mgr.touch(bid, ripple=False),
        bucket_mgr.delete(bid),
        return_exceptions=True,
    )

    all_buckets = await bucket_mgr.list_all(include_archive=True)
    matches = [b for b in all_buckets if b["id"] == bid]
    assert len(matches) == 1, (
        f"bucket_id={bid} 结束后应该只有 1 份，实际 {len(matches)} 份"
    )
