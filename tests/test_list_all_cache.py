"""list_all() 活跃桶缓存与外部文件变更回归测试（性能 P1）。

缓存必须：命中返回、写操作后失效、touch 就地更新、返回副本不污染缓存。
不能改变任何可见语义（桶数/内容/元数据都要与直读磁盘一致）。
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

import frontmatter
import pytest


@pytest.mark.asyncio
async def test_cache_hit_after_first_list(bucket_mgr):
    await bucket_mgr.create_internal(content="内容一号一号一号", name="一", domain=["测试"])
    assert bucket_mgr._active_cache is None
    first = await bucket_mgr.list_all()
    assert bucket_mgr._active_cache is not None  # 建了缓存
    second = await bucket_mgr.list_all()
    assert len(first) == len(second) == 1


@pytest.mark.asyncio
async def test_write_invalidates_cache(bucket_mgr):
    await bucket_mgr.create_internal(content="内容一号一号一号", name="一", domain=["测试"])
    await bucket_mgr.list_all()
    assert bucket_mgr._active_cache is not None
    # 新建 → 集合变了 → 缓存作废
    await bucket_mgr.create_internal(content="内容二号二号二号", name="二", domain=["测试"])
    assert bucket_mgr._active_cache is None
    again = await bucket_mgr.list_all()
    assert len(again) == 2   # 反映了新桶


@pytest.mark.asyncio
async def test_returned_list_is_copy_not_cache(bucket_mgr):
    await bucket_mgr.create_internal(content="内容", name="一", domain=["测试"])
    got = await bucket_mgr.list_all()
    got[0]["score"] = 123          # 调用方在返回对象上写顶层键
    got[0]["vector_match"] = True
    fresh = await bucket_mgr.list_all()   # 走缓存
    assert "score" not in fresh[0]        # 不该污染缓存
    assert "vector_match" not in fresh[0]


@pytest.mark.asyncio
async def test_touch_updates_cache_in_place(bucket_mgr):
    bid = await bucket_mgr.create_internal(content="内容一号一号一号", name="一", domain=["测试"])
    await bucket_mgr.list_all()  # 建缓存
    before = next(b for b in bucket_mgr._active_cache if b["id"] == bid)
    before_count = float(before["metadata"].get("activation_count") or 0)
    await bucket_mgr.touch(bid)
    after = next(b for b in bucket_mgr._active_cache if b["id"] == bid)
    assert float(after["metadata"].get("activation_count") or 0) == before_count + 1
    assert after["metadata"].get("last_active")


@pytest.mark.asyncio
async def test_cache_matches_disk_after_delete(bucket_mgr):
    b1 = await bucket_mgr.create_internal(content="留下的内容啊啊啊", name="留", domain=["测试"])
    b2 = await bucket_mgr.create_internal(content="删掉的内容哦哦哦", name="删", domain=["测试"])
    await bucket_mgr.list_all()
    await bucket_mgr.delete(b2)   # 软删 → 失效缓存
    active = await bucket_mgr.list_all()
    ids = {b["id"] for b in active}
    assert b1 in ids and b2 not in ids


class _OutboxProbe:
    def __init__(self):
        self.enqueued = []
        self.discarded = []

    def enqueue(self, bucket_id, content):
        self.enqueued.append((bucket_id, content))
        return True

    def discard(self, bucket_id):
        self.discarded.append(bucket_id)
        return True


@pytest.mark.asyncio
async def test_external_content_edit_refreshes_cache_and_queues_embedding(bucket_mgr):
    bucket_mgr.external_change_poll_seconds = 0
    bucket_id = await bucket_mgr.create_internal(
        content="old external-edit content",
        name="external",
        domain=["test"],
    )
    outbox = _OutboxProbe()
    bucket_mgr.attach_embedding_outbox(outbox)
    await bucket_mgr.list_all()

    path = bucket_mgr._find_bucket_file(bucket_id)
    post = frontmatter.load(path)
    post.content = "new content written by Obsidian"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(frontmatter.dumps(post))

    refreshed = await bucket_mgr.list_all()

    bucket = next(item for item in refreshed if item["id"] == bucket_id)
    assert bucket["content"] == "new content written by Obsidian"
    assert outbox.enqueued == [(bucket_id, "new content written by Obsidian")]
    assert bucket_mgr._bm25_dirty is True
    assert bucket_mgr.external_change_status()["detected"] == 1


@pytest.mark.asyncio
async def test_external_metadata_edit_does_not_requeue_unchanged_content(bucket_mgr):
    bucket_mgr.external_change_poll_seconds = 0
    bucket_id = await bucket_mgr.create_internal(content="same body", importance=5)
    outbox = _OutboxProbe()
    bucket_mgr.attach_embedding_outbox(outbox)
    await bucket_mgr.list_all()

    path = bucket_mgr._find_bucket_file(bucket_id)
    post = frontmatter.load(path)
    post["importance"] = 9
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(frontmatter.dumps(post))

    [refreshed] = await bucket_mgr.list_all()

    assert refreshed["metadata"]["importance"] == 9
    assert outbox.enqueued == []


@pytest.mark.asyncio
async def test_external_markdown_create_is_discovered_and_queued(bucket_mgr):
    bucket_mgr.external_change_poll_seconds = 0
    outbox = _OutboxProbe()
    bucket_mgr.attach_embedding_outbox(outbox)
    await bucket_mgr.list_all()

    target = Path(bucket_mgr.dynamic_dir) / "external" / "external-note.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(
        "memory created directly in Obsidian",
        id="external-note",
        name="External note",
        type="dynamic",
        domain=["external"],
        importance=5,
    )
    target.write_text(frontmatter.dumps(post), encoding="utf-8")

    refreshed = await bucket_mgr.list_all()

    assert [item["id"] for item in refreshed] == ["external-note"]
    assert outbox.enqueued == [
        ("external-note", "memory created directly in Obsidian")
    ]


@pytest.mark.asyncio
async def test_external_physical_delete_cleans_derived_index(bucket_mgr):
    bucket_mgr.external_change_poll_seconds = 0
    bucket_id = await bucket_mgr.create_internal(content="physically removed outside OB")
    outbox = _OutboxProbe()
    bucket_mgr.attach_embedding_outbox(outbox)
    await bucket_mgr.list_all()
    assert bucket_id in bucket_mgr.embedding_engine._store

    path = bucket_mgr._find_bucket_file(bucket_id)
    Path(path).unlink()
    refreshed = await bucket_mgr.list_all()

    assert refreshed == []
    assert bucket_id in outbox.discarded
    assert bucket_id not in bucket_mgr.embedding_engine._store


@pytest.mark.asyncio
async def test_external_archive_move_keeps_pending_and_existing_vector(bucket_mgr):
    bucket_mgr.external_change_poll_seconds = 0
    bucket_id = await bucket_mgr.create_internal(content="moved to archive outside OB")
    outbox = _OutboxProbe()
    bucket_mgr.attach_embedding_outbox(outbox)
    await bucket_mgr.list_all()
    assert bucket_id in bucket_mgr.embedding_engine._store

    source = Path(bucket_mgr._find_bucket_file(bucket_id))
    destination = Path(bucket_mgr.archive_dir) / source.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.replace(destination)

    refreshed = await bucket_mgr.list_all()

    assert refreshed == []
    assert bucket_id not in outbox.discarded
    assert bucket_id in bucket_mgr.embedding_engine._store


@pytest.mark.asyncio
async def test_internal_touch_updates_file_fingerprint_without_false_external_event(bucket_mgr):
    bucket_mgr.external_change_poll_seconds = 0
    bucket_id = await bucket_mgr.create_internal(content="touch fingerprint")
    await bucket_mgr.list_all()

    await bucket_mgr.touch(bucket_id, ripple=False)
    await bucket_mgr.list_all()

    assert bucket_mgr.external_change_status()["detected"] == 0


def test_cache_builder_cannot_publish_stale_content_after_invalidation(
    bucket_mgr,
    monkeypatch,
):
    bucket_id = asyncio.run(
        bucket_mgr.create_internal("AAAA same-size body", name="cache-race", domain=["test"])
    )
    path = Path(bucket_mgr._find_bucket_file(bucket_id))
    bucket_mgr._invalidate_bm25()
    parsed_old = threading.Event()
    release_builder = threading.Event()
    original_load = bucket_mgr._load_bucket
    blocked = False
    fixed_state = bucket_mgr._scan_active_file_state()

    def coordinated_load(file_path):
        nonlocal blocked
        bucket = original_load(file_path)
        if not blocked and Path(file_path) == path:
            blocked = True
            parsed_old.set()
            release_builder.wait(timeout=2)
        return bucket

    monkeypatch.setattr(bucket_mgr, "_load_bucket", coordinated_load)
    # Hide the file change from the mtime/size fingerprint to prove the
    # managed-write generation CAS, rather than the external poll, rejects the
    # stale publish.
    monkeypatch.setattr(
        bucket_mgr,
        "_scan_active_file_state",
        lambda: dict(fixed_state),
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(lambda: asyncio.run(bucket_mgr.list_all()))
        assert parsed_old.wait(timeout=2)

        post = frontmatter.load(path)
        post.content = "BBBB same-size body"
        path.write_text(frontmatter.dumps(post), encoding="utf-8")
        bucket_mgr._invalidate_bm25()
        release_builder.set()
        [result] = future.result(timeout=2)

    assert result["content"] == "BBBB same-size body"
    [cached] = asyncio.run(bucket_mgr.list_all())
    assert cached["content"] == "BBBB same-size body"
