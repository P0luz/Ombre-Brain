"""M-03 Markdown staging, overwrite, history, and rollback behavior."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sys

import frontmatter
import pytest

from import_transaction import (
    ImportCandidate,
    ImportRecoveryError,
    apply_import_batch as _apply_import_batch,
    mark_import_transaction_committed,
    review_bucket_snapshot,
)
from ombrebrain.eventsourcing.footprint import import_origin


async def apply_import_batch(**kwargs):
    kwargs.setdefault("footprint_origin", import_origin("system", "system"))
    return await _apply_import_batch(**kwargs)

_HELPERS = Path(__file__).resolve().parent / "helpers"
if str(_HELPERS) not in sys.path:
    sys.path.insert(0, str(_HELPERS))
from m03_process_worker import DiskManager, make_markdown as _markdown, put_markdown


def _put(root: Path, leaf: str, raw: bytes) -> Path:
    return put_markdown(root, leaf, raw)


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.mark.asyncio
async def test_overwrite_archives_exact_old_body_under_independent_history_id(tmp_path):
    old_raw = _markdown("same", "exact old body", owner="cheng")
    old_path = _put(tmp_path, "old_same.md", old_raw)
    snapshot = review_bucket_snapshot(str(tmp_path), "same")
    result = await apply_import_batch(
        buckets_dir=str(tmp_path),
        bucket_manager=DiskManager(str(tmp_path)),
        job_owner="cheng",
        candidates=[
            ImportCandidate(
                source_id="same",
                markdown=_markdown("same", "new body", owner="cheng"),
                decision="overwrite",
                conflicted_at_review=True,
                expected_live_sha256=snapshot["sha256"],
                expected_live_relative_path=snapshot["relative_path"],
            )
        ],
    )
    assert result.markdown_committed is True
    assert result.history_id_map["same"] != "same"
    live = review_bucket_snapshot(str(tmp_path), "same")
    assert live and live["sha256"] != snapshot["sha256"]
    history_id = result.history_id_map["same"]
    history = review_bucket_snapshot(str(tmp_path), history_id)
    assert history is not None
    history_post = frontmatter.load(tmp_path / history["relative_path"])
    assert history_post.content == "exact old body"
    assert history_post["superseded_by"] == "same"
    assert not old_path.exists()
    mark_import_transaction_committed(str(tmp_path), result.txid)


@pytest.mark.asyncio
async def test_keep_both_preserves_old_and_rewrites_frontmatter_to_new_id(tmp_path):
    old_path = _put(tmp_path, "old_same.md", _markdown("same", "old", owner="cheng"))
    old_hash = _hash(old_path)
    snapshot = review_bucket_snapshot(str(tmp_path), "same")
    result = await apply_import_batch(
        buckets_dir=str(tmp_path),
        bucket_manager=DiskManager(str(tmp_path)),
        job_owner="cheng",
        candidates=[
            ImportCandidate(
                source_id="same",
                markdown=_markdown("same", "imported", owner="cheng"),
                decision="keep_both",
                conflicted_at_review=True,
                expected_live_sha256=snapshot["sha256"],
                expected_live_relative_path=snapshot["relative_path"],
            )
        ],
    )
    target_id = result.imported_id_map["same"]
    assert target_id != "same"
    assert _hash(old_path) == old_hash
    target = review_bucket_snapshot(str(tmp_path), target_id)
    assert target is not None
    post = frontmatter.load(tmp_path / target["relative_path"])
    assert post["id"] == target_id
    assert post.content == "imported"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "point",
    [
        "stage.mkdir",
        "stage.write.before",
        "stage.write.after",
        "journal.validated",
        "locks.acquired",
        "fresh.verify",
        "old.snapshot",
        "journal.prepared",
        "publish.history.before",
        "publish.history.after",
        "publish.target.before",
        "publish.target.after",
    ],
)
async def test_each_failpoint_restores_exact_old_generation(tmp_path, point):
    old_raw = _markdown("same", "old body", owner="cheng")
    old_path = _put(tmp_path, "old_same.md", old_raw)
    snapshot = review_bucket_snapshot(str(tmp_path), "same")
    fired = False

    def injector(actual):
        nonlocal fired
        if not fired and actual == point:
            fired = True
            raise OSError(f"{point} injected")

    with pytest.raises(Exception):
        await apply_import_batch(
            buckets_dir=str(tmp_path),
            bucket_manager=DiskManager(str(tmp_path)),
            job_owner="cheng",
            candidates=[
                ImportCandidate(
                    source_id="same",
                    markdown=_markdown("same", "new body", owner="cheng"),
                    decision="overwrite",
                    conflicted_at_review=True,
                    expected_live_sha256=snapshot["sha256"],
                    expected_live_relative_path=snapshot["relative_path"],
                )
            ],
            fault_injector=injector,
        )
    assert fired is True
    assert old_path.exists()
    assert old_path.read_bytes() == old_raw
    assert review_bucket_snapshot(str(tmp_path), "same")["sha256"] == snapshot["sha256"]
    archive_files = [
        path
        for path in (tmp_path / "archive").rglob("*.md")
        if path.is_file()
    ] if (tmp_path / "archive").exists() else []
    assert archive_files == []


@pytest.mark.asyncio
async def test_rollback_failure_is_combined_and_never_reported_success(tmp_path):
    _put(tmp_path, "old_same.md", _markdown("same", "old", owner="cheng"))
    snapshot = review_bucket_snapshot(str(tmp_path), "same")

    def injector(point):
        if point == "publish.target.after":
            raise RuntimeError("original publish failure")
        if point == "rollback.before":
            raise OSError("rollback failure")

    with pytest.raises(ImportRecoveryError) as caught:
        await apply_import_batch(
            buckets_dir=str(tmp_path),
            bucket_manager=DiskManager(str(tmp_path)),
            job_owner="cheng",
            candidates=[
                ImportCandidate(
                    source_id="same",
                    markdown=_markdown("same", "new", owner="cheng"),
                    decision="overwrite",
                    conflicted_at_review=True,
                    expected_live_sha256=snapshot["sha256"],
                    expected_live_relative_path=snapshot["relative_path"],
                )
            ],
            fault_injector=injector,
        )
    assert "rollback also failed" in str(caught.value)


@pytest.mark.asyncio
async def test_failure_after_markdown_commit_marker_keeps_authoritative_new_body(
    tmp_path,
):
    _put(tmp_path, "old_same.md", _markdown("same", "old", owner="cheng"))
    snapshot = review_bucket_snapshot(str(tmp_path), "same")

    def injector(point):
        if point == "journal.markdown_committed":
            raise RuntimeError("postcommit derived failure")

    with pytest.raises(RuntimeError, match="postcommit derived failure"):
        await apply_import_batch(
            buckets_dir=str(tmp_path),
            bucket_manager=DiskManager(str(tmp_path)),
            job_owner="cheng",
            candidates=[
                ImportCandidate(
                    source_id="same",
                    markdown=_markdown("same", "new", owner="cheng"),
                    decision="overwrite",
                    conflicted_at_review=True,
                    expected_live_sha256=snapshot["sha256"],
                    expected_live_relative_path=snapshot["relative_path"],
                )
            ],
            fault_injector=injector,
        )
    live = review_bucket_snapshot(str(tmp_path), "same")
    assert frontmatter.load(tmp_path / live["relative_path"]).content == "new"
