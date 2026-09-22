"""Cross-process overwrite and keep-both serialization."""

from __future__ import annotations

import multiprocessing
from pathlib import Path
import queue
import sys

import frontmatter

from import_transaction import review_bucket_snapshot

_HELPERS = Path(__file__).resolve().parent / "helpers"
if str(_HELPERS) not in sys.path:
    sys.path.insert(0, str(_HELPERS))
from m03_process_worker import (
    apply_worker,
    make_markdown as _markdown,
    put_markdown as _put,
)

_TIMEOUT = 30


def _finish(processes):
    for process in processes:
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(3)


def test_six_spawned_keep_both_writers_receive_unique_ids(tmp_path):
    _put(tmp_path, "same.md", _markdown("same", "live", owner="cheng"))
    snapshot = review_bucket_snapshot(str(tmp_path), "same")
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    results = ctx.Queue()
    ready = [ctx.Event() for _ in range(6)]
    processes = [
        ctx.Process(
            target=apply_worker,
            args=(
                str(tmp_path),
                {
                    "job_owner": "cheng",
                    "source_id": "same",
                    "markdown": _markdown(
                        "same", f"imported {index}", owner="cheng"
                    ),
                    "decision": "keep_both",
                    "conflicted_at_review": True,
                    "expected_live_sha256": snapshot["sha256"],
                    "expected_live_relative_path": snapshot["relative_path"],
                },
                ready[index],
                start,
                results,
            ),
        )
        for index in range(6)
    ]
    try:
        for process in processes:
            process.start()
        assert all(event.wait(_TIMEOUT) for event in ready)
        start.set()
        output = [results.get(timeout=_TIMEOUT) for _ in processes]
    finally:
        start.set()
        _finish(processes)
    assert all(item["ok"] for item in output), output
    ids = [item["target"]["same"] for item in output]
    assert len(ids) == len(set(ids)) == 6
    assert review_bucket_snapshot(str(tmp_path), "same")["sha256"] == snapshot["sha256"]
    for bucket_id in ids:
        assert review_bucket_snapshot(str(tmp_path), bucket_id) is not None


def test_two_spawned_overwrites_cannot_both_commit_stale_review(tmp_path):
    _put(tmp_path, "same.md", _markdown("same", "live", owner="cheng"))
    snapshot = review_bucket_snapshot(str(tmp_path), "same")
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    results = ctx.Queue()
    ready = [ctx.Event(), ctx.Event()]
    processes = [
        ctx.Process(
            target=apply_worker,
            args=(
                str(tmp_path),
                {
                    "job_owner": "cheng",
                    "source_id": "same",
                    "markdown": _markdown(
                        "same", f"candidate {index}", owner="cheng"
                    ),
                    "decision": "overwrite",
                    "conflicted_at_review": True,
                    "expected_live_sha256": snapshot["sha256"],
                    "expected_live_relative_path": snapshot["relative_path"],
                },
                ready[index],
                start,
                results,
            ),
        )
        for index in range(2)
    ]
    try:
        for process in processes:
            process.start()
        assert all(event.wait(_TIMEOUT) for event in ready)
        start.set()
        output = [results.get(timeout=_TIMEOUT) for _ in processes]
    except queue.Empty as exc:
        raise AssertionError("overwrite workers timed out") from exc
    finally:
        start.set()
        _finish(processes)
    assert sum(bool(item["ok"]) for item in output) == 1, output
    live = review_bucket_snapshot(str(tmp_path), "same")
    post = frontmatter.load(tmp_path / live["relative_path"])
    assert post.content in {"candidate 0", "candidate 1"}
