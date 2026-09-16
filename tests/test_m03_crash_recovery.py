"""Durable journal recovery at namespace transition crash points."""

from __future__ import annotations

import multiprocessing
from pathlib import Path
import sys

import frontmatter
import pytest

from import_transaction import recover_import_transactions, review_bucket_snapshot

_HELPERS = Path(__file__).resolve().parent / "helpers"
if str(_HELPERS) not in sys.path:
    sys.path.insert(0, str(_HELPERS))
from m03_process_worker import (
    crash_apply_worker,
    make_markdown as _markdown,
    put_markdown as _put,
)


@pytest.mark.parametrize(
    ("point", "code", "expected_body"),
    [
        ("journal.prepared", 91, "old"),
        ("publish.history.after", 92, "old"),
        ("publish.target.after", 93, "old"),
        ("journal.markdown_committed", 94, "new"),
    ],
)
def test_hard_exit_recovers_deterministically_from_durable_state(
    tmp_path,
    point,
    code,
    expected_body,
):
    _put(tmp_path, "same.md", _markdown("same", "old", owner="cheng"))
    snapshot = review_bucket_snapshot(str(tmp_path), "same")
    candidate = {
        "job_owner": "cheng",
        "source_id": "same",
        "markdown": _markdown("same", "new", owner="cheng"),
        "decision": "overwrite",
        "conflicted_at_review": True,
        "expected_live_sha256": snapshot["sha256"],
        "expected_live_relative_path": snapshot["relative_path"],
    }
    ctx = multiprocessing.get_context("spawn")
    process = ctx.Process(
        target=crash_apply_worker,
        args=(str(tmp_path), candidate, point, code),
    )
    process.start()
    process.join(30)
    if process.is_alive():
        process.terminate()
        process.join(3)
        pytest.fail("crash worker did not terminate")
    assert process.exitcode == code
    recovered = recover_import_transactions(str(tmp_path))
    assert recovered
    live = review_bucket_snapshot(str(tmp_path), "same")
    post = frontmatter.load(tmp_path / live["relative_path"])
    assert post.content == expected_body


def test_unknown_live_hash_fails_closed_instead_of_guessing(tmp_path):
    _put(tmp_path, "same.md", _markdown("same", "old", owner="cheng"))
    snapshot = review_bucket_snapshot(str(tmp_path), "same")
    candidate = {
        "job_owner": "cheng",
        "source_id": "same",
        "markdown": _markdown("same", "new", owner="cheng"),
        "decision": "overwrite",
        "conflicted_at_review": True,
        "expected_live_sha256": snapshot["sha256"],
        "expected_live_relative_path": snapshot["relative_path"],
    }
    ctx = multiprocessing.get_context("spawn")
    process = ctx.Process(
        target=crash_apply_worker,
        args=(str(tmp_path), candidate, "publish.target.after", 95),
    )
    process.start()
    process.join(30)
    assert process.exitcode == 95
    live = review_bucket_snapshot(str(tmp_path), "same")
    path = tmp_path / live["relative_path"]
    path.write_bytes(_markdown("same", "unknown third generation", owner="cheng"))
    with pytest.raises(Exception, match="unknown|hash"):
        recover_import_transactions(str(tmp_path))
