"""Focused M-01 lease lifetime and provider-ordering tests."""

from __future__ import annotations

import asyncio
import errno
import logging
import multiprocessing
import os
from pathlib import Path
import sys

import pytest

from bucket_manager import BucketManager, _filesystem_turn
from tools import _common as common
from tools import _identity
from tools import _runtime as rt

_HELPERS = Path(__file__).resolve().parent / "helpers"
if str(_HELPERS) not in sys.path:
    sys.path.insert(0, str(_HELPERS))

from m01_process_worker import FakeDehydrator, exit_while_holding_worker


@pytest.mark.asyncio
async def test_filesystem_turn_releases_after_context_exception(tmp_path):
    vault = str(tmp_path / "exception-vault")

    with pytest.raises(RuntimeError, match="body failed"):
        async with _filesystem_turn(vault, "exception-release"):
            raise RuntimeError("body failed")

    async with _filesystem_turn(vault, "exception-release", timeout_seconds=0.1):
        pass


@pytest.mark.asyncio
async def test_filesystem_turn_releases_after_task_cancellation(tmp_path):
    vault = str(tmp_path / "cancel-vault")
    entered = asyncio.Event()
    never = asyncio.Event()

    async def holder() -> None:
        async with _filesystem_turn(vault, "cancel-release"):
            entered.set()
            await never.wait()

    task = asyncio.create_task(holder())
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with _filesystem_turn(vault, "cancel-release", timeout_seconds=0.1):
        pass


@pytest.mark.asyncio
async def test_filesystem_turn_propagates_non_contention_lock_error(
    tmp_path,
    monkeypatch,
):
    vault = str(tmp_path / "unsupported-vault")
    unsupported = getattr(errno, "EOPNOTSUPP", errno.ENOSYS)
    calls = 0

    def fail_lock(*_args) -> None:
        nonlocal calls
        calls += 1
        raise OSError(unsupported, "kernel locking unsupported")

    if os.name == "nt":
        import msvcrt

        monkeypatch.setattr(msvcrt, "locking", fail_lock)
    else:
        import fcntl

        monkeypatch.setattr(fcntl, "flock", fail_lock)

    with pytest.raises(OSError) as caught:
        async with _filesystem_turn(vault, "unsupported", timeout_seconds=0.1):
            pytest.fail("a non-contention error must fail before entering")

    assert caught.value.errno == unsupported
    assert not isinstance(caught.value, TimeoutError)
    assert calls == 1


@pytest.mark.asyncio
async def test_distinct_filesystem_turn_keys_do_not_block_each_other(tmp_path):
    vault = str(tmp_path / "scope-vault")
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def first() -> None:
        async with _filesystem_turn(vault, "content-A"):
            first_entered.set()
            await release_first.wait()

    async def second() -> None:
        await first_entered.wait()
        async with _filesystem_turn(vault, "content-B", timeout_seconds=0.1):
            second_entered.set()

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())
    await asyncio.wait_for(second_entered.wait(), timeout=1)
    release_first.set()
    await asyncio.gather(first_task, second_task)


@pytest.mark.asyncio
async def test_cancelled_content_and_quota_waiters_do_not_poison_later_turns(
    tmp_path,
    monkeypatch,
):
    manager = type("Manager", (), {"base_dir": str(tmp_path / "turn-vault")})()
    monkeypatch.setattr(rt, "bucket_mgr", manager, raising=False)

    for turn_factory, key in (
        (common._content_turn, "same exact text"),
        (common._quota_turn, "pinned"),
    ):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def holder() -> None:
            async with turn_factory(key):
                entered.set()
                await release.wait()

        async def waiter() -> None:
            async with turn_factory(key):
                pytest.fail("cancelled waiter entered its turn")

        first = asyncio.create_task(holder())
        await asyncio.wait_for(entered.wait(), timeout=1)
        cancelled = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled

        release.set()
        await first

        async def final_waiter() -> None:
            async with turn_factory(key):
                pass

        await asyncio.wait_for(final_waiter(), timeout=1)


@pytest.mark.asyncio
async def test_process_exit_releases_kernel_lease(tmp_path):
    vault = str(tmp_path / "exit-vault")
    key = "abrupt-process-exit"
    ctx = multiprocessing.get_context("spawn")
    entered = ctx.Event()
    process = ctx.Process(
        target=exit_while_holding_worker,
        args=(vault, key, entered),
    )
    process.start()
    assert entered.wait(timeout=20)
    process.join(timeout=20)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail("lease-holder process did not exit within the timeout")
    assert process.exitcode == 0

    async with _filesystem_turn(vault, key, timeout_seconds=0.2):
        pass


@pytest.mark.asyncio
async def test_embedding_provider_starts_after_content_quota_and_bucket_turns_release(
    tmp_path,
    monkeypatch,
):
    """The provider callback must be able to reserve every mutation turn."""

    vault = str(tmp_path / "provider-order-vault")
    config = {
        "buckets_dir": vault,
        "merge_threshold": 75,
        "matching": {"fuzzy_threshold": 50, "max_results": 50},
        "wikilink": {"enabled": False},
        "limits": {"max_pinned": 10},
        "scoring_weights": {},
        "embedding": {"enabled": True},
    }
    Path(vault).mkdir(parents=True)
    content = "M-01 provider ordering for high importance hold"
    provider_started = asyncio.Event()
    probed: list[str] = []

    class ProbingEmbedding:
        enabled = True

        async def generate_and_store(self, bucket_id: str, _content: str) -> bool:
            provider_started.set()

            async def acquire_all() -> None:
                async with common._content_turn(content):
                    probed.append("content")
                async with common._quota_turn("high_importance"):
                    probed.append("quota")
                async with _filesystem_turn(
                    vault,
                    f"bucket-{bucket_id}",
                    timeout_seconds=0.15,
                ):
                    probed.append("bucket")

            await asyncio.wait_for(acquire_all(), timeout=0.5)
            return True

        async def get_embedding(self, _bucket_id: str):
            return [0.1, 0.2]

        async def search_similar(self, _query: str, top_k: int = 10):
            del top_k
            return []

        def delete_embedding(self, _bucket_id: str) -> None:
            return None

    embedding = ProbingEmbedding()
    manager = BucketManager(config, embedding_engine=embedding)
    rt.config = config
    rt.bucket_mgr = manager
    rt.embedding_engine = embedding
    rt.dehydrator = FakeDehydrator()
    rt.logger = logging.getLogger("m01-provider-order")
    rt.fire_webhook = None
    rt.mark_op = None
    _identity.set_caller("cheng")
    monkeypatch.setattr(common, "_HIGH_IMP_HARD_CAP", 3)
    monkeypatch.setattr(common, "_HIGH_IMP_SOFT_WARN", 3)

    bucket_id, merged, warning = await asyncio.wait_for(
        common.merge_or_create(
            content=content,
            tags=["owner:cheng"],
            importance=9,
            domain=["m01"],
            valence=0.5,
            arousal=0.3,
            raw_merge=True,
            source_tool="hold",
        ),
        timeout=2,
    )

    assert bucket_id
    assert merged is False
    assert warning == ""
    assert provider_started.is_set()
    assert probed == ["content", "quota", "bucket"]
    stored = await manager.get(bucket_id)
    assert stored is not None
    assert stored["content"] == content
