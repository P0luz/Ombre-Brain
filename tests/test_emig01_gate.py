"""Windows spawn acceptance for E-MIG-01 kernel gates."""

from __future__ import annotations

import asyncio
import multiprocessing
from pathlib import Path
import sys

import pytest

from embedding_publish import (
    async_embedding_db_turn,
    create_shadow_path,
    embedding_db_turn,
    reserve_migration,
)

_HELPERS = Path(__file__).resolve().parent / "helpers"
if str(_HELPERS) not in sys.path:
    sys.path.insert(0, str(_HELPERS))

from emig01_process_worker import (
    late_reader_worker,
    publisher_worker,
    reservation_worker,
    shared_reader_worker,
)

_TIMEOUT = 20


def _stop(processes) -> None:
    for process in processes:
        process.join(timeout=3)
        if process.is_alive():
            process.terminate()
            process.join(timeout=3)


def test_multiple_readers_coexist_and_publisher_intent_blocks_late_reader(tmp_path):
    db = str(tmp_path / "embeddings.db")
    ctx = multiprocessing.get_context("spawn")
    reader_release = ctx.Event()
    publisher_release = ctx.Event()
    r1_entered, r2_entered = ctx.Event(), ctx.Event()
    r1_exited, r2_exited = ctx.Event(), ctx.Event()
    publish_entered = ctx.Event()
    late_entered = ctx.Event()
    readers = [
        ctx.Process(
            target=shared_reader_worker,
            args=(db, entered, reader_release, exited),
        )
        for entered, exited in ((r1_entered, r1_exited), (r2_entered, r2_exited))
    ]
    publisher = ctx.Process(
        target=publisher_worker,
        args=(db, publish_entered, publisher_release),
    )
    late = ctx.Process(target=late_reader_worker, args=(db, late_entered))
    processes = [*readers, publisher, late]
    try:
        for process in readers:
            process.start()
        assert r1_entered.wait(_TIMEOUT)
        assert r2_entered.wait(_TIMEOUT), "shared readers were serialized"
        publisher.start()
        # Give the publisher time to take intent and block on the resource.
        assert not publish_entered.wait(0.4)
        late.start()
        assert not late_entered.wait(0.4), "late reader bypassed publish intent"
        reader_release.set()
        assert publish_entered.wait(_TIMEOUT)
        assert not late_entered.wait(0.3)
        publisher_release.set()
        assert late_entered.wait(_TIMEOUT)
    finally:
        reader_release.set()
        publisher_release.set()
        _stop(processes)
    assert all(process.exitcode == 0 for process in processes)


def test_reservation_is_cross_process_single_flight_and_released(tmp_path):
    db = str(tmp_path / "embeddings.db")
    ctx = multiprocessing.get_context("spawn")
    entered = ctx.Event()
    release = ctx.Event()
    result = ctx.Queue()
    holder = ctx.Process(
        target=reservation_worker,
        args=(db, entered, release, result),
    )
    holder.start()
    try:
        assert entered.wait(_TIMEOUT)
        assert result.get(timeout=_TIMEOUT) is True
        assert reserve_migration(db) is None
        release.set()
        holder.join(timeout=_TIMEOUT)
        assert holder.exitcode == 0
        reservation = reserve_migration(db)
        assert reservation is not None
        reservation.close()
    finally:
        release.set()
        _stop([holder])


def test_hard_process_exit_releases_reservation(tmp_path):
    db = str(tmp_path / "embeddings.db")
    ctx = multiprocessing.get_context("spawn")
    entered = ctx.Event()
    release = ctx.Event()
    result = ctx.Queue()
    process = ctx.Process(
        target=reservation_worker,
        args=(db, entered, release, result),
        kwargs={"hard_exit": True},
    )
    process.start()
    assert entered.wait(_TIMEOUT)
    process.join(timeout=_TIMEOUT)
    assert not process.is_alive()
    reservation = reserve_migration(db)
    assert reservation is not None
    reservation.close()


@pytest.mark.asyncio
async def test_async_reader_cancellation_does_not_poison_gate(tmp_path):
    db = str(tmp_path / "embeddings.db")
    entered = asyncio.Event()
    never = asyncio.Event()

    async def holder():
        async with async_embedding_db_turn(db):
            entered.set()
            await never.wait()

    task = asyncio.create_task(holder())
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with embedding_db_turn(db):
        pass


def test_shadow_leaf_is_internal_same_parent_and_not_request_controlled(tmp_path):
    db = tmp_path / "outside..evil" / "embeddings.db"
    reservation = reserve_migration(db)
    assert reservation is not None
    try:
        shadow = create_shadow_path(db, reservation=reservation)
    finally:
        reservation.close()
    assert shadow.parent == db.parent / ".embedding-generations"
    assert shadow.name == f"{reservation.txid}.shadow.db"
    assert ".." not in shadow.name
    assert "/" not in shadow.name and "\\" not in shadow.name
