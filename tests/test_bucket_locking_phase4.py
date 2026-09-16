"""Phase 4 regressions for storage turns and create/ripple races."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
import errno
import hashlib
import os
from pathlib import Path
import re
import threading
from unittest.mock import MagicMock

import frontmatter
import pytest

from bucket_manager import _filesystem_turn
from tools import _common as common
from tools import _runtime as rt
from utils import parse_iso_datetime


@pytest.mark.asyncio
async def test_filesystem_turn_hashes_key_and_does_not_steal_aged_live_lease(
    tmp_path,
):
    base = str(tmp_path / "vault")
    malicious_key = "../../outside\\nested/lock"
    digest = hashlib.sha256(malicious_key.encode("utf-8")).hexdigest()
    lock_path = Path(base) / ".locks" / f"{digest}.lock"

    async with _filesystem_turn(base, malicious_key):
        assert lock_path.is_file()
        assert re.fullmatch(r"[0-9a-f]{64}\.lock", lock_path.name)
        os.utime(lock_path, (0, 0))
        with pytest.raises(TimeoutError):
            async with _filesystem_turn(base, malicious_key, timeout_seconds=0.05):
                pytest.fail("a live kernel lease must not be stolen by file age")

    assert not (tmp_path / "outside").exists()


@pytest.mark.asyncio
async def test_content_turn_delegates_to_non_stealable_kernel_lease(
    tmp_path,
    monkeypatch,
):
    calls = []

    @asynccontextmanager
    async def observed_lease(base_dir, key, timeout_seconds=30.0):
        calls.append((base_dir, key, timeout_seconds))
        yield

    monkeypatch.setattr(common, "_kernel_filesystem_turn", observed_lease)
    monkeypatch.setattr(
        rt,
        "bucket_mgr",
        MagicMock(base_dir=tmp_path / "vault"),
    )
    monkeypatch.setattr(rt, "config", {"dehydration": {"timeout_seconds": 120}})

    async with common._filesystem_content_turn("same-content"):
        pass

    assert calls == [
        (
            str(tmp_path / "vault"),
            "content-same-content",
            common._CONTENT_LOCK_WAIT_MIN_SECONDS,
        )
    ]


@pytest.mark.asyncio
async def test_filesystem_turn_propagates_non_contention_os_error(
    tmp_path,
    monkeypatch,
):
    """不支持或损坏的锁系统调用不能伪装成普通竞争。"""
    base = str(tmp_path / "vault")
    unsupported = getattr(errno, "EOPNOTSUPP", errno.ENOSYS)
    calls = 0

    def fail_lock(*_args):
        nonlocal calls
        calls += 1
        raise OSError(unsupported, "filesystem leases are unsupported")

    if os.name == "nt":
        import msvcrt

        monkeypatch.setattr(msvcrt, "locking", fail_lock)
    else:
        import fcntl

        monkeypatch.setattr(fcntl, "flock", fail_lock)

    with pytest.raises(OSError) as caught:
        async with _filesystem_turn(base, "unsupported-lock", timeout_seconds=0.05):
            pytest.fail("a non-contention lock error must fail before entering")

    assert caught.value.errno == unsupported
    assert not isinstance(caught.value, TimeoutError)
    assert calls == 1


@pytest.mark.asyncio
async def test_filesystem_turn_releases_after_context_exception(tmp_path):
    base = str(tmp_path / "vault")

    with pytest.raises(RuntimeError, match="body failed"):
        async with _filesystem_turn(base, "exception-release"):
            raise RuntimeError("body failed")

    async with _filesystem_turn(base, "exception-release", timeout_seconds=0.05):
        pass


@pytest.mark.asyncio
async def test_filesystem_turn_releases_after_task_cancellation(tmp_path):
    base = str(tmp_path / "vault")
    entered = asyncio.Event()
    wait_forever = asyncio.Event()

    async def holder():
        async with _filesystem_turn(base, "cancel-release"):
            entered.set()
            await wait_forever.wait()

    task = asyncio.create_task(holder())
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with _filesystem_turn(base, "cancel-release", timeout_seconds=0.05):
        pass


def test_filesystem_turn_serializes_independent_event_loops(tmp_path):
    base = str(tmp_path / "vault")
    state_lock = threading.Lock()
    state = {"active": 0, "maximum": 0}

    async def enter_once():
        async with _filesystem_turn(base, "same-bucket"):
            with state_lock:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
            await asyncio.sleep(0.02)
            with state_lock:
                state["active"] -= 1

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _index: asyncio.run(enter_once()), range(6)))

    assert state["maximum"] == 1


def test_active_cache_lock_serializes_independent_event_loops(bucket_mgr, monkeypatch):
    asyncio.run(bucket_mgr.create_internal("cross-loop cache body", domain=["race"]))
    bucket_mgr.external_change_poll_seconds = 0
    entered = threading.Event()
    release = threading.Event()
    original_scan = bucket_mgr._scan_active_file_state
    calls = 0
    calls_guard = threading.Lock()

    def coordinated_scan():
        nonlocal calls
        with calls_guard:
            calls += 1
            call_number = calls
        if call_number == 1:
            entered.set()
            release.wait(timeout=2)
        return original_scan()

    monkeypatch.setattr(bucket_mgr, "_scan_active_file_state", coordinated_scan)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(lambda: asyncio.run(bucket_mgr.list_all()))
        assert entered.wait(timeout=2)
        second = pool.submit(lambda: asyncio.run(bucket_mgr.list_all()))
        release.set()
        assert len(first.result(timeout=2)) == 1
        assert len(second.result(timeout=2)) == 1

    # Re-entering from the original caller after another loop owned the mutex
    # used to raise "Lock ... is bound to a different event loop".
    assert len(asyncio.run(bucket_mgr.list_all())) == 1


def test_bulk_bucket_id_index_avoids_n_by_n_frontmatter_scans(
    bucket_mgr,
    monkeypatch,
):
    for index in range(24):
        asyncio.run(
            bucket_mgr.create_internal(
                f"indexed body {index}",
                name=f"indexed-{index}",
                domain=["race"],
            )
        )

    original_load = frontmatter.load
    loads = 0

    def counted_load(*args, **kwargs):
        nonlocal loads
        loads += 1
        return original_load(*args, **kwargs)

    monkeypatch.setattr(frontmatter, "load", counted_load)
    bucket_mgr._ensure_bucket_path_index()
    build_loads = loads
    assert build_loads == 24

    for index in range(200):
        assert bucket_mgr._find_bucket_file(f"missing-import-{index}") is None
    assert loads == build_loads


@pytest.mark.asyncio
async def test_concurrent_create_override_never_overwrites_same_id(bucket_mgr):
    first, second = await asyncio.gather(
        bucket_mgr.create_internal("first body", bucket_id_override="shared-id"),
        bucket_mgr.create_internal("second body", bucket_id_override="shared-id"),
    )

    assert first != second
    assert {first, second} >= {"shared-id"}
    assert {
        (await bucket_mgr.get(first))["content"],
        (await bucket_mgr.get(second))["content"],
    } == {"first body", "second body"}


@pytest.mark.asyncio
async def test_update_releases_bucket_turn_before_waiting_for_derived_index(
    bucket_mgr,
    monkeypatch,
):
    """慢 embedding provider 不得继续占用持久桶租约。"""
    bucket_id = await bucket_mgr.create_internal("meaning base", domain=["race"])
    indexing_started = asyncio.Event()
    release_indexing = asyncio.Event()

    async def slow_meaning_index(_bucket_id, _meaning):
        indexing_started.set()
        await release_indexing.wait()

    monkeypatch.setattr(bucket_mgr, "_sync_meaning_embedding", slow_meaning_index)
    update_task = asyncio.create_task(
        bucket_mgr.update(bucket_id, meaning_append="new perspective")
    )
    await asyncio.wait_for(indexing_started.wait(), timeout=1)

    acquired = False
    try:
        async with _filesystem_turn(
            str(bucket_mgr.base_dir),
            f"bucket-{bucket_id}",
            timeout_seconds=0.05,
        ):
            acquired = True
    finally:
        release_indexing.set()

    assert acquired is True
    assert await asyncio.wait_for(update_task, timeout=1) is True


@pytest.mark.asyncio
async def test_concurrent_updates_cannot_finish_with_stale_derived_content(
    bucket_mgr,
):
    bucket_id = await bucket_mgr.create_internal("initial", domain=["race"])
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    completed: list[str] = []

    class OrderedEngine:
        enabled = True

        def __init__(self):
            self.hashes = {}

        def get_content_hash(self, requested_id):
            return self.hashes.get(requested_id, "")

        async def generate_and_store(self, requested_id, content):
            if content == "first update":
                first_started.set()
                await release_first.wait()
            completed.append(content)
            self.hashes[requested_id] = hashlib.sha256(
                content.encode("utf-8")
            ).hexdigest()
            return True

    bucket_mgr.embedding_engine = OrderedEngine()
    first = asyncio.create_task(bucket_mgr.update(bucket_id, content="first update"))
    await asyncio.wait_for(first_started.wait(), timeout=1)
    second = asyncio.create_task(bucket_mgr.update(bucket_id, content="second update"))

    # 第二次 Markdown 提交不得等待第一次 provider 调用。
    deadline = asyncio.get_running_loop().time() + 1
    while (await bucket_mgr.get(bucket_id))["content"] != "second update":
        if asyncio.get_running_loop().time() >= deadline:
            pytest.fail("second Markdown update remained blocked by derived indexing")
        await asyncio.sleep(0.01)

    release_first.set()
    assert await asyncio.gather(first, second) == [True, True]
    assert completed[0] == "first update"
    assert completed[-1] == "second update"
    assert completed == ["first update", "second update"]


@pytest.mark.asyncio
async def test_cancelled_newer_update_still_converges_meaning_index(
    bucket_mgr,
    monkeypatch,
):
    """较新请求取消后，迟到的旧 provider 结果也不能成为最终 meaning。"""
    bucket_id = await bucket_mgr.create_internal("meaning cancellation base")
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    stored: list[str] = []

    async def controlled_meaning_index(_bucket_id, meaning):
        current = meaning[-1] if meaning else ""
        if current == "meaning A":
            first_started.set()
            await release_first.wait()
        stored.append(current)

    monkeypatch.setattr(
        bucket_mgr,
        "_sync_meaning_embedding",
        controlled_meaning_index,
    )
    first = asyncio.create_task(
        bucket_mgr.update(bucket_id, meaning=["meaning A"])
    )
    await asyncio.wait_for(first_started.wait(), timeout=1)
    newer = asyncio.create_task(
        bucket_mgr.update(bucket_id, meaning=["meaning B"])
    )

    deadline = asyncio.get_running_loop().time() + 1
    while (await bucket_mgr.get(bucket_id))["metadata"].get("meaning") != [
        "meaning B"
    ]:
        if asyncio.get_running_loop().time() >= deadline:
            pytest.fail("较新的 meaning 未在取消前提交到 Markdown")
        await asyncio.sleep(0.01)

    newer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await newer
    release_first.set()
    assert await asyncio.wait_for(first, timeout=1) is True

    latest = await bucket_mgr.get(bucket_id)
    assert latest["metadata"]["meaning"] == ["meaning B"]
    assert stored[-1] == "meaning B"


@pytest.mark.asyncio
async def test_cancelled_provider_wait_releases_derived_lease(
    bucket_mgr,
    monkeypatch,
):
    bucket_id = await bucket_mgr.create_internal("cancel provider base")
    provider_started = asyncio.Event()
    wait_forever = asyncio.Event()

    async def blocked_meaning_index(_bucket_id, _meaning):
        provider_started.set()
        await wait_forever.wait()

    monkeypatch.setattr(
        bucket_mgr,
        "_sync_meaning_embedding",
        blocked_meaning_index,
    )
    update = asyncio.create_task(
        bucket_mgr.update(bucket_id, meaning=["cancelled meaning"])
    )
    await asyncio.wait_for(provider_started.wait(), timeout=1)
    update.cancel()
    with pytest.raises(asyncio.CancelledError):
        await update

    async with _filesystem_turn(
        str(bucket_mgr.base_dir),
        f"derived-index-{bucket_id}",
        timeout_seconds=0.05,
    ):
        pass


@pytest.mark.asyncio
async def test_hold_merge_releases_bucket_turn_before_derived_index(
    bucket_mgr,
    monkeypatch,
):
    """hold 合并路径不得在私有桶租约内等待 embedding。"""
    old_content = "existing hold event"
    new_content = "additional detail from the same event"
    bucket_id = await bucket_mgr.create_internal(
        old_content,
        domain=["race"],
        tags=["owner:cheng"],
    )

    async def find_target(*_args, **_kwargs):
        bucket = await bucket_mgr.get(bucket_id)
        assert bucket is not None
        bucket["score"] = 100.0
        return [bucket]

    class SameEventJudge:
        async def judge_same_event(self, *_args, **_kwargs):
            return {"same_event": True, "confidence": 0.99, "reason": "same"}

        def invalidate_cache(self, *_args, **_kwargs):
            pass

    indexing_started = asyncio.Event()
    release_indexing = asyncio.Event()

    async def slow_content_index(_bucket_id, _content):
        indexing_started.set()
        await release_indexing.wait()

    monkeypatch.setattr(bucket_mgr, "search", find_target)
    monkeypatch.setattr(bucket_mgr, "_index_after_write", slow_content_index)
    monkeypatch.setattr(rt, "bucket_mgr", bucket_mgr, raising=False)
    monkeypatch.setattr(rt, "embedding_engine", None, raising=False)
    monkeypatch.setattr(rt, "dehydrator", SameEventJudge(), raising=False)
    monkeypatch.setattr(rt, "config", {"merge_threshold": 75}, raising=False)
    monkeypatch.setattr(rt, "logger", MagicMock(), raising=False)

    merge_task = asyncio.create_task(
        common.merge_or_create(
            content=new_content,
            tags=[],
            importance=5,
            domain=["race"],
            valence=0.5,
            arousal=0.3,
            raw_merge=True,
            source_tool="hold",
        )
    )
    acquired = False
    content_turn_acquired = False
    target_turn_acquired = False
    try:
        await asyncio.wait_for(indexing_started.wait(), timeout=1)
        async with _filesystem_turn(
            str(bucket_mgr.base_dir),
            f"bucket-{bucket_id}",
            timeout_seconds=0.05,
        ):
            acquired = True

        async def probe_outer_turns():
            nonlocal content_turn_acquired, target_turn_acquired
            async with common._content_turn(new_content):
                content_turn_acquired = True
            merge_key = hashlib.sha256(
                bucket_id.encode("utf-8", errors="replace")
            ).hexdigest()[: common._CONTENT_LOCK_KEY_HEX]
            async with common._keyed_turn(f"merge-target-{merge_key}"):
                target_turn_acquired = True

        await asyncio.wait_for(probe_outer_turns(), timeout=0.2)
    finally:
        release_indexing.set()
        result = await asyncio.wait_for(merge_task, timeout=1)

    assert acquired is True
    assert content_turn_acquired is True
    assert target_turn_acquired is True
    assert result == (bucket_id, True, "")
    merged_bucket = await bucket_mgr.get(bucket_id)
    assert merged_bucket is not None
    assert merged_bucket["content"] == f"{old_content}\n\n---\n{new_content}"


@pytest.mark.asyncio
async def test_update_enqueues_outbox_after_releasing_bucket_turn(bucket_mgr):
    bucket_id = await bucket_mgr.create_internal("outbox lease base", domain=["race"])

    class ProbingOutbox:
        running = True

        def enqueue(self, requested_id, _content, **_kwargs):
            assert requested_id == bucket_id

            async def acquire_same_bucket():
                async with _filesystem_turn(
                    str(bucket_mgr.base_dir),
                    f"bucket-{bucket_id}",
                    timeout_seconds=0.05,
                ):
                    return True

            with ThreadPoolExecutor(max_workers=1) as pool:
                assert pool.submit(
                    lambda: asyncio.run(acquire_same_bucket())
                ).result(timeout=1)
            return True

        def enqueue_meaning(self, *_args, **_kwargs):
            return True

    bucket_mgr.attach_embedding_outbox(ProbingOutbox())
    assert await bucket_mgr.update(bucket_id, content="outbox lease updated")


@pytest.mark.asyncio
async def test_hold_create_releases_content_and_quota_turns_before_indexing(
    bucket_mgr,
    monkeypatch,
):
    content = "new high importance hold outside coordination turns"
    indexing_started = asyncio.Event()
    release_indexing = asyncio.Event()

    async def no_match(*_args, **_kwargs):
        return []

    async def slow_content_index(_bucket_id, _content):
        indexing_started.set()
        await release_indexing.wait()

    monkeypatch.setattr(bucket_mgr, "search", no_match)
    monkeypatch.setattr(bucket_mgr, "find_exact_content", lambda *_a, **_k: None)
    monkeypatch.setattr(bucket_mgr, "_index_after_write", slow_content_index)
    monkeypatch.setattr(rt, "bucket_mgr", bucket_mgr, raising=False)
    monkeypatch.setattr(rt, "embedding_engine", None, raising=False)
    monkeypatch.setattr(rt, "dehydrator", MagicMock(), raising=False)
    monkeypatch.setattr(
        rt,
        "config",
        {"merge_threshold": 75, "limits": {"high_importance_max": 100}},
        raising=False,
    )
    monkeypatch.setattr(rt, "logger", MagicMock(), raising=False)

    task = asyncio.create_task(
        common.merge_or_create(
            content=content,
            tags=[],
            importance=9,
            domain=["race"],
            valence=0.5,
            arousal=0.3,
            raw_merge=True,
            source_tool="hold",
        )
    )
    try:
        await asyncio.wait_for(indexing_started.wait(), timeout=1)

        async def probe_turns():
            async with common._quota_turn("high_importance"):
                pass
            async with common._content_turn(content):
                pass

        await asyncio.wait_for(probe_turns(), timeout=0.2)
    finally:
        release_indexing.set()

    bucket_id, merged, warning = await asyncio.wait_for(task, timeout=1)
    assert bucket_id
    assert merged is False
    assert warning == ""


@pytest.mark.asyncio
async def test_create_rechecks_id_after_waiting_for_migration_turn(bucket_mgr):
    imported_id = "migration-race-id"
    imported_path = Path(bucket_mgr.dynamic_dir) / "race" / "imported.md"

    async with bucket_mgr._bucket_turn(imported_id):
        create_task = asyncio.create_task(
            bucket_mgr.create_internal(
                "new local body",
                domain=["race"],
                bucket_id_override=imported_id,
            )
        )
        await asyncio.sleep(0.05)
        imported_path.parent.mkdir(parents=True, exist_ok=True)
        imported_path.write_text(
            frontmatter.dumps(
                frontmatter.Post(
                    "imported body",
                    id=imported_id,
                    name="imported",
                    type="dynamic",
                    domain=["race"],
                    created=datetime.now().isoformat(),
                    last_active=datetime.now().isoformat(),
                    activation_count=0,
                    importance=5,
                )
            ),
            encoding="utf-8",
        )

    created_id = await create_task
    assert created_id != imported_id
    assert (await bucket_mgr.get(imported_id))["content"] == "imported body"
    assert (await bucket_mgr.get(created_id))["content"] == "new local body"


@pytest.mark.asyncio
async def test_ripple_reloads_target_under_its_turn_without_lost_touch(bucket_mgr):
    source_id = await bucket_mgr.create_internal("source", domain=["race"])
    target_id = await bucket_mgr.create_internal("target", domain=["race"])
    source = await bucket_mgr.get(source_id)
    reference = parse_iso_datetime(source["metadata"]["created"])

    async with bucket_mgr._bucket_turn(target_id):
        ripple_task = asyncio.create_task(
            bucket_mgr._time_ripple(source_id, reference)
        )
        await asyncio.sleep(0.05)
        assert await bucket_mgr._touch_locked(target_id) is not None

    await ripple_task
    target = await bucket_mgr.get(target_id)
    assert target["metadata"]["activation_count"] == 1.3


@pytest.mark.asyncio
async def test_ripple_does_not_update_target_archived_after_snapshot(bucket_mgr):
    source_id = await bucket_mgr.create_internal("source", domain=["race"])
    target_id = await bucket_mgr.create_internal("target", domain=["race"])
    source = await bucket_mgr.get(source_id)
    reference = parse_iso_datetime(source["metadata"]["created"])

    async with bucket_mgr._bucket_turn(target_id):
        ripple_task = asyncio.create_task(
            bucket_mgr._time_ripple(source_id, reference)
        )
        await asyncio.sleep(0.05)
        assert await bucket_mgr._archive_locked(target_id) is True

    await ripple_task
    target = await bucket_mgr.get(target_id)
    assert target["metadata"]["type"] == "archived"
    assert target["metadata"]["activation_count"] == 0


@pytest.mark.asyncio
async def test_hard_delete_rechecks_provenance_inside_bucket_turn(bucket_mgr):
    bucket_id = await bucket_mgr.create_internal("test body", test_data=True)
    bucket = await bucket_mgr.get(bucket_id)
    path = Path(bucket["path"])

    async with bucket_mgr._bucket_turn(bucket_id):
        delete_task = asyncio.create_task(
            bucket_mgr.hard_delete_test_bucket(bucket_id, reason="race")
        )
        await asyncio.sleep(0.05)
        if path.exists():
            post = frontmatter.load(path)
            post["provenance"] = {
                "kind": "test",
                "created_by": "developer",
                "erasable": False,
            }
            path.write_text(frontmatter.dumps(post), encoding="utf-8")

    assert await delete_task == {"ok": False, "error": "not_erasable_test_data"}
    assert path.is_file()
