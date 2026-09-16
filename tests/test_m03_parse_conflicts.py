"""Fresh conflict and reviewed-content CAS tests."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sys

import pytest

from import_transaction import ImportCandidate, ImportTransactionError, apply_import_batch as _apply_import_batch, review_bucket_snapshot
from ombrebrain.eventsourcing.footprint import import_origin


async def apply_import_batch(**kwargs):
    kwargs.setdefault("footprint_origin", import_origin("system", "system"))
    return await _apply_import_batch(**kwargs)

_HELPERS = Path(__file__).resolve().parent / "helpers"
if str(_HELPERS) not in sys.path:
    sys.path.insert(0, str(_HELPERS))
from m03_process_worker import (
    DiskManager,
    make_markdown as _markdown,
    put_markdown as _put,
)


@pytest.mark.asyncio
async def test_new_conflict_after_parse_forces_review_without_overwrite(tmp_path):
    # Review saw absence, but another writer creates the ID before apply.
    late = _put(tmp_path, "late_same.md", _markdown("same", "late live", owner="cheng"))
    before = hashlib.sha256(late.read_bytes()).hexdigest()
    with pytest.raises(ImportTransactionError, match="new conflict appeared"):
        await apply_import_batch(
            buckets_dir=str(tmp_path),
            bucket_manager=DiskManager(str(tmp_path)),
            job_owner="cheng",
            candidates=[
                ImportCandidate(
                    source_id="same",
                    markdown=_markdown("same", "imported", owner="cheng"),
                    decision="import",
                )
            ],
        )
    assert hashlib.sha256(late.read_bytes()).hexdigest() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["overwrite", "keep_both"])
async def test_reviewed_conflict_content_hash_change_is_rejected(tmp_path, decision):
    live = _put(tmp_path, "same.md", _markdown("same", "reviewed", owner="cheng"))
    snapshot = review_bucket_snapshot(str(tmp_path), "same")
    live.write_bytes(_markdown("same", "changed after review", owner="cheng"))
    changed_hash = hashlib.sha256(live.read_bytes()).hexdigest()
    with pytest.raises(ImportTransactionError, match="reviewed conflict changed"):
        await apply_import_batch(
            buckets_dir=str(tmp_path),
            bucket_manager=DiskManager(str(tmp_path)),
            job_owner="cheng",
            candidates=[
                ImportCandidate(
                    source_id="same",
                    markdown=_markdown("same", "imported", owner="cheng"),
                    decision=decision,
                    conflicted_at_review=True,
                    expected_live_sha256=snapshot["sha256"],
                    expected_live_relative_path=snapshot["relative_path"],
                )
            ],
        )
    assert hashlib.sha256(live.read_bytes()).hexdigest() == changed_hash


@pytest.mark.asyncio
async def test_forged_overwrite_without_review_is_rejected(tmp_path):
    _put(tmp_path, "same.md", _markdown("same", "live", owner="cheng"))
    snapshot = review_bucket_snapshot(str(tmp_path), "same")
    with pytest.raises(ImportTransactionError, match="not authorized"):
        await apply_import_batch(
            buckets_dir=str(tmp_path),
            bucket_manager=DiskManager(str(tmp_path)),
            job_owner="cheng",
            candidates=[
                ImportCandidate(
                    source_id="same",
                    markdown=_markdown("same", "imported", owner="cheng"),
                    decision="overwrite",
                    conflicted_at_review=False,
                    expected_live_sha256=snapshot["sha256"],
                    expected_live_relative_path=snapshot["relative_path"],
                )
            ],
        )
