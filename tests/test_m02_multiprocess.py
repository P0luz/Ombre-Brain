"""Windows-spawn concurrency and hard-exit tests for M-02."""

from __future__ import annotations

import asyncio
import multiprocessing
from pathlib import Path
import queue
import sys

import pytest
import yaml

from config_transaction import async_open_config_transaction, run_config_transaction

_HELPERS = Path(__file__).resolve().parent / "helpers"
if str(_HELPERS) not in sys.path:
    sys.path.insert(0, str(_HELPERS))

from m02_process_worker import (
    hard_exit_at_persist_point,
    hard_exit_holding_worker,
    increment_from_fresh_worker,
    mutate_worker,
)

_TIMEOUT = 30


def _finish(processes):
    for process in processes:
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=3)


def _assert_results(results, count):
    output = []
    try:
        for _ in range(count):
            output.append(results.get(timeout=_TIMEOUT))
    except queue.Empty as exc:
        raise AssertionError("M-02 worker timed out") from exc
    failures = [item for item in output if not item["ok"]]
    assert failures == [], "\n".join(item["traceback"] for item in failures)
    return output


def test_36_spawned_writers_to_distinct_fields_lose_no_updates(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("root: keep\n", encoding="utf-8")
    ctx = multiprocessing.get_context("spawn")
    starts = [ctx.Event() for _ in range(36)]
    ready = [ctx.Event() for _ in range(36)]
    results = ctx.Queue()
    processes = [
        ctx.Process(
            target=mutate_worker,
            args=(
                str(path),
                "writers",
                f"field_{index}",
                index,
                ready[index],
                starts[index],
                results,
            ),
        )
        for index in range(36)
    ]
    try:
        for process in processes:
            process.start()
        assert all(event.wait(_TIMEOUT) for event in ready)
        for event in starts:
            event.set()
        _assert_results(results, len(processes))
    finally:
        for event in starts:
            event.set()
        _finish(processes)
    persisted = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert persisted["root"] == "keep"
    assert persisted["writers"] == {
        f"field_{index}": index for index in range(36)
    }


def test_same_field_has_deterministic_explicit_serial_order(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("shared: old\n", encoding="utf-8")
    ctx = multiprocessing.get_context("spawn")
    results = ctx.Queue()
    first_ready, second_ready = ctx.Event(), ctx.Event()
    first_go, second_go = ctx.Event(), ctx.Event()
    first = ctx.Process(
        target=mutate_worker,
        args=(str(path), "values", "same", "first", first_ready, first_go, results),
    )
    second = ctx.Process(
        target=mutate_worker,
        args=(str(path), "values", "same", "second", second_ready, second_go, results),
    )
    processes = [first, second]
    try:
        first.start()
        second.start()
        assert first_ready.wait(_TIMEOUT) and second_ready.wait(_TIMEOUT)
        first_go.set()
        first.join(_TIMEOUT)
        assert first.exitcode == 0
        second_go.set()
        second.join(_TIMEOUT)
        _assert_results(results, 2)
    finally:
        first_go.set()
        second_go.set()
        _finish(processes)
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["values"]["same"] == "second"


def test_stale_pre_reads_cannot_overwrite_lock_fresh_counter(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("counter: 0\n", encoding="utf-8")
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    results = ctx.Queue()
    ready = [ctx.Event() for _ in range(12)]
    processes = [
        ctx.Process(
            target=increment_from_fresh_worker,
            args=(str(path), event, start, results),
        )
        for event in ready
    ]
    try:
        for process in processes:
            process.start()
        assert all(event.wait(_TIMEOUT) for event in ready)
        start.set()
        output = _assert_results(results, len(processes))
    finally:
        start.set()
        _finish(processes)
    assert all(item["stale"] == 0 for item in output)
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["counter"] == 12


def test_hard_exit_releases_exclusive_config_lease(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("value: old\n", encoding="utf-8")
    ctx = multiprocessing.get_context("spawn")
    entered = ctx.Event()
    process = ctx.Process(
        target=hard_exit_holding_worker,
        args=(str(path), entered),
    )
    process.start()
    assert entered.wait(_TIMEOUT)
    process.join(_TIMEOUT)
    assert process.exitcode == 0
    result = run_config_transaction(
        path,
        lambda config: config.__setitem__("value", "after-exit"),
    )
    assert result.persisted["value"] == "after-exit"


@pytest.mark.asyncio
async def test_cancelled_async_holder_releases_config_lease(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("value: old\n", encoding="utf-8")
    entered = asyncio.Event()
    never = asyncio.Event()

    async def holder():
        async with async_open_config_transaction(path):
            entered.set()
            await never.wait()

    task = asyncio.create_task(holder())
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = await asyncio.wait_for(
        asyncio.to_thread(
            run_config_transaction,
            path,
            lambda config: config.__setitem__("value", "after-cancel"),
        ),
        timeout=3,
    )
    assert result.persisted["value"] == "after-cancel"


@pytest.mark.parametrize(
    ("point", "code", "expected"),
    [
        ("persist.replace", 81, "old"),
        ("persist.replace_after", 82, "candidate"),
    ],
)
def test_hard_exit_never_leaves_partial_yaml(tmp_path, point, code, expected):
    path = tmp_path / "config.yaml"
    path.write_text("generation: old\n", encoding="utf-8")
    ctx = multiprocessing.get_context("spawn")
    process = ctx.Process(
        target=hard_exit_at_persist_point,
        args=(str(path), point, code),
    )
    process.start()
    process.join(_TIMEOUT)
    if process.is_alive():
        process.terminate()
        process.join(3)
        pytest.fail("hard-exit transaction did not terminate")
    assert process.exitcode == code
    persisted = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert persisted == {"generation": expected}
    run_config_transaction(path, lambda config: config.__setitem__("valid", True))
