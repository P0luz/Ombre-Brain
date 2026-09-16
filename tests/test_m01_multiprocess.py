"""M-01 cross-process write and quota acceptance tests.

All child interpreters use Windows-compatible ``spawn`` and set their isolated
temporary vault before importing project modules.  Assertions read Markdown
directly from disk so no process-local BucketManager cache can mask a failure.
"""

from __future__ import annotations

import multiprocessing
from pathlib import Path
import queue
import sys
from typing import Iterable

import frontmatter

_HELPERS = Path(__file__).resolve().parent / "helpers"
if str(_HELPERS) not in sys.path:
    sys.path.insert(0, str(_HELPERS))

from m01_process_worker import run_write_worker


_SPAWN_TIMEOUT = 25


def _disk_buckets(vault: Path) -> list[dict]:
    rows: list[dict] = []
    for path in vault.rglob("*.md"):
        post = frontmatter.load(path)
        rows.append(
            {
                "path": path,
                "id": str(post.get("id") or ""),
                "content": post.content or "",
                "metadata": dict(post.metadata),
            }
        )
    return rows


def _spawn_writes(
    vault: Path,
    writes: Iterable[tuple[str, str, str]],
    *,
    high_cap: int = 24,
    pinned_cap: int = 20,
    ordered_first_reviews: bool = False,
) -> list[dict]:
    write_specs = list(writes)
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    ready = ctx.Queue()
    results = ctx.Queue()
    coordination = (
        [
            {
                "selection_observed": ctx.Event(),
                "allow_first_review": ctx.Event(),
                "first_review_finished": ctx.Event(),
            }
            for _ in write_specs
        ]
        if ordered_first_reviews
        else [None for _ in write_specs]
    )
    processes = [
        ctx.Process(
            target=run_write_worker,
            args=(
                str(vault),
                action,
                content,
                caller,
                high_cap,
                pinned_cap,
                ready,
                start,
                results,
                coordination[index],
            ),
        )
        for index, (action, content, caller) in enumerate(write_specs)
    ]

    for process in processes:
        process.start()

    try:
        warmed = [ready.get(timeout=_SPAWN_TIMEOUT) for _ in processes]
        assert all(item["kind"] == "ready" for item in warmed)
        assert all(item["cached"] == 0 for item in warmed)
        start.set()
        if ordered_first_reviews:
            assert all(gate is not None for gate in coordination)
            assert all(
                gate["selection_observed"].wait(timeout=_SPAWN_TIMEOUT)
                for gate in coordination
                if gate is not None
            ), "not every ordered writer completed its empty first selection"
            for index, gate in enumerate(coordination):
                assert gate is not None
                gate["allow_first_review"].set()
                assert gate["first_review_finished"].wait(
                    timeout=_SPAWN_TIMEOUT
                ), f"ordered writer {index} did not finish its first commit review"
        output = [results.get(timeout=_SPAWN_TIMEOUT) for _ in processes]
    except queue.Empty as exc:
        raise AssertionError("M-01 spawn worker exceeded its bounded timeout") from exc
    finally:
        start.set()
        for gate in coordination:
            if gate is not None:
                gate["allow_first_review"].set()
        for process in processes:
            process.join(timeout=_SPAWN_TIMEOUT)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    failures = [item for item in output if item.get("kind") == "error"]
    assert failures == [], "\n\n".join(item["traceback"] for item in failures)
    assert all(process.exitcode == 0 for process in processes), [
        process.exitcode for process in processes
    ]
    return output


def test_same_caller_same_exact_content_commits_one_owner_correct_bucket(
    tmp_path,
):
    vault = tmp_path / "same-owner-vault"
    content = "M-01 exact content from two spawned cheng writers"

    _spawn_writes(
        vault,
        [
            ("merge", content, "cheng"),
            ("merge", content, "cheng"),
        ],
    )

    matching = [row for row in _disk_buckets(vault) if row["content"] == content]
    assert len(matching) == 1
    tags = matching[0]["metadata"].get("tags") or []
    assert [tag for tag in tags if str(tag).startswith("owner:")] == ["owner:cheng"]


def test_same_caller_exact_content_uses_storage_canonical_form(tmp_path):
    vault = tmp_path / "same-owner-canonical-vault"
    raw_content = "alpha\u202ebeta"

    _spawn_writes(
        vault,
        [
            ("merge", raw_content, "cheng"),
            ("merge", raw_content, "cheng"),
        ],
    )

    matching = [row for row in _disk_buckets(vault) if row["content"] == "alphabeta"]
    assert len(matching) == 1
    tags = matching[0]["metadata"].get("tags") or []
    assert [tag for tag in tags if str(tag).startswith("owner:")] == ["owner:cheng"]


def test_second_losing_writer_merges_distinct_incoming_metadata(tmp_path):
    vault = tmp_path / "same-owner-metadata-vault"
    content = "M-01 raced exact retry must not swallow incoming metadata"

    output = _spawn_writes(
        vault,
        [
            ("merge", content, "cheng"),
            ("merge_high", content, "cheng"),
        ],
        ordered_first_reviews=True,
    )

    by_action = {item["action"]: item for item in output}
    assert by_action["merge"]["first_review_outcome"] == "created"
    assert by_action["merge"]["merged"] is False
    assert by_action["merge_high"]["first_review_outcome"] == "raced_exact"
    assert by_action["merge_high"]["merged"] is True
    assert by_action["merge"]["bucket_id"] == by_action["merge_high"]["bucket_id"]

    matching = [row for row in _disk_buckets(vault) if row["content"] == content]
    assert len(matching) == 1
    metadata = matching[0]["metadata"]
    assert metadata.get("importance") == 9
    assert "race:high" in (metadata.get("tags") or [])
    assert set(metadata.get("domain") or []) == {"m01", "race:incoming"}
    assert [
        tag for tag in (metadata.get("tags") or [])
        if str(tag).startswith("owner:")
    ] == ["owner:cheng"]


def test_different_callers_same_exact_content_commit_separate_owner_buckets(
    tmp_path,
):
    vault = tmp_path / "different-owner-vault"
    content = "M-01 same bytes must remain private to each caller"

    _spawn_writes(
        vault,
        [
            ("merge", content, "cheng"),
            ("merge", content, "huaiyin"),
        ],
    )

    matching = [row for row in _disk_buckets(vault) if row["content"] == content]
    assert len(matching) == 2
    owners = {
        tag
        for row in matching
        for tag in (row["metadata"].get("tags") or [])
        if str(tag).startswith("owner:")
    }
    assert owners == {"owner:cheng", "owner:huaiyin"}
    assert len({row["id"] for row in matching}) == 2


def test_spawned_pinned_writers_cannot_exceed_hard_cap_with_stale_caches(
    tmp_path,
):
    vault = tmp_path / "pinned-quota-vault"
    cap = 2

    _spawn_writes(
        vault,
        [("pinned", f"M-01 pinned contender {index}", "cheng") for index in range(6)],
        pinned_cap=cap,
    )

    rows = _disk_buckets(vault)
    pinned = [row for row in rows if row["metadata"].get("pinned") is True]
    assert len(pinned) == cap
    assert all(
        row["metadata"].get("importance") == 10
        and "owner:cheng" in (row["metadata"].get("tags") or [])
        for row in pinned
    )


def test_spawned_high_importance_writers_cannot_exceed_hard_cap_with_stale_caches(
    tmp_path,
):
    vault = tmp_path / "high-quota-vault"
    cap = 2

    _spawn_writes(
        vault,
        [("high", f"M-01 high contender {index}", "cheng") for index in range(6)],
        high_cap=cap,
    )

    rows = _disk_buckets(vault)
    assert len(rows) == 6
    high = [
        row
        for row in rows
        if int(row["metadata"].get("importance") or 0) >= 9
        and not row["metadata"].get("pinned")
        and not row["metadata"].get("protected")
    ]
    assert len(high) == cap
    assert all("owner:cheng" in (row["metadata"].get("tags") or []) for row in rows)
