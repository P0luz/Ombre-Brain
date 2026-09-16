"""归档不丢记忆回归测试（谨慎加固 D1）。

decay 只归档不删（已审计确认）。archive() 早先直接用原文件名做目标，万一 archive/
里已有同名文件，shutil.move 会覆盖 → 悄悄盖掉一条早先归档的记忆。现补了防撞名后缀。
本测试确保「归档多条相似记忆，谁也不会把谁盖掉」。
"""
import pytest


@pytest.mark.asyncio
async def test_archiving_similar_buckets_keeps_all(bucket_mgr):
    id1 = await bucket_mgr.create_internal(content="第一条内容 AAA", name="重名记忆",
                                  domain=["测试"], bucket_type="dynamic", importance=3)
    id2 = await bucket_mgr.create_internal(content="第二条内容 BBB", name="重名记忆",
                                  domain=["测试"], bucket_type="dynamic", importance=3)

    assert await bucket_mgr.archive(id1) is True
    assert await bucket_mgr.archive(id2) is True

    archived = await bucket_mgr.list_all(include_archive=True)
    blob = "\n".join(b.get("content", "") for b in archived)
    assert "第一条内容 AAA" in blob
    assert "第二条内容 BBB" in blob


@pytest.mark.asyncio
async def test_archive_collision_guard_appends_suffix(bucket_mgr, tmp_path, monkeypatch):
    """强制 dest 撞名：让 archive 目标基名固定，验证第二次归档不覆盖第一次。"""
    id1 = await bucket_mgr.create_internal(content="原始归档内容 X", name="固定名",
                                  domain=["测试"], bucket_type="dynamic", importance=3)
    id2 = await bucket_mgr.create_internal(content="后来归档内容 Y", name="固定名",
                                  domain=["测试"], bucket_type="dynamic", importance=3)

    # 把两个桶的文件基名强制成同一个，制造真正的 archive 撞名
    import os
    real_basename = os.path.basename
    monkeypatch.setattr(os.path, "basename",
                        lambda p: "collide.md" if str(p).endswith(".md") else real_basename(p))

    assert await bucket_mgr.archive(id1) is True
    assert await bucket_mgr.archive(id2) is True
    monkeypatch.undo()

    archived = await bucket_mgr.list_all(include_archive=True)
    blob = "\n".join(b.get("content", "") for b in archived)
    assert "原始归档内容 X" in blob   # 没被第二次归档覆盖
    assert "后来归档内容 Y" in blob
