"""Untrusted archive metadata, ID, path, type, and owner policy tests."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

from import_transaction import (
    ADMIN_RESTORE_SCOPE,
    ImportCandidate,
    ImportTransactionError,
    apply_import_batch as _apply_import_batch,
    review_bucket_snapshot,
)
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
@pytest.mark.parametrize(
    "bucket_id",
    [
        "",
        "..",
        "../escape",
        r"..\escape",
        r"C:\absolute",
        r"\\server\share",
        "name:stream",
        "nul\0id",
        "x" * 201,
    ],
)
async def test_malicious_bucket_ids_are_rejected_without_live_write(
    tmp_path,
    bucket_id,
):
    with pytest.raises(ImportTransactionError):
        await apply_import_batch(
            buckets_dir=str(tmp_path),
            bucket_manager=DiskManager(str(tmp_path)),
            job_owner="cheng",
            candidates=[
                ImportCandidate(
                    source_id=bucket_id,
                    markdown=_markdown(bucket_id or "different", "body", owner="cheng"),
                )
            ],
        )
    assert not list(tmp_path.glob("dynamic/**/*.md"))


@pytest.mark.asyncio
async def test_frontmatter_id_mismatch_is_rejected(tmp_path):
    with pytest.raises(ImportTransactionError, match="ID mismatch"):
        await apply_import_batch(
            buckets_dir=str(tmp_path),
            bucket_manager=DiskManager(str(tmp_path)),
            job_owner="cheng",
            candidates=[
                ImportCandidate(
                    source_id="expected",
                    markdown=_markdown("embedded-other", "body", owner="cheng"),
                )
            ],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("bucket_type", ["../../outside", "unknown", 42])
async def test_wrong_or_unsupported_bucket_type_is_rejected(
    tmp_path,
    bucket_type,
):
    with pytest.raises(ImportTransactionError, match="type"):
        await apply_import_batch(
            buckets_dir=str(tmp_path),
            bucket_manager=DiskManager(str(tmp_path)),
            job_owner="cheng",
            candidates=[
                ImportCandidate(
                    source_id="safe",
                    markdown=_markdown(
                        "safe", "body", owner="cheng", type=bucket_type
                    ),
                )
            ],
        )


@pytest.mark.asyncio
async def test_multi_owner_and_wrong_tag_container_fail_closed(tmp_path):
    bad = [
        _markdown(
            "multi",
            "body",
            tags=["owner:cheng", "owner:huaiyin"],
        ),
        _markdown("wrong-tags", "body", tags="owner:cheng"),
    ]
    for index, raw in enumerate(bad):
        with pytest.raises((ImportTransactionError, ValueError)):
            await apply_import_batch(
                buckets_dir=str(tmp_path),
                bucket_manager=DiskManager(str(tmp_path)),
                job_owner=ADMIN_RESTORE_SCOPE,
                candidates=[
                    ImportCandidate(
                        source_id="multi" if index == 0 else "wrong-tags",
                        markdown=raw,
                    )
                ],
            )


@pytest.mark.asyncio
async def test_untagged_requires_assignment_and_assignment_is_persisted(tmp_path):
    raw = _markdown("untagged", "body", owner="")
    with pytest.raises(ImportTransactionError, match="explicit owner assignment"):
        await apply_import_batch(
            buckets_dir=str(tmp_path),
            bucket_manager=DiskManager(str(tmp_path)),
            job_owner="cheng",
            candidates=[ImportCandidate(source_id="untagged", markdown=raw)],
        )
    result = await apply_import_batch(
        buckets_dir=str(tmp_path),
        bucket_manager=DiskManager(str(tmp_path)),
        job_owner="cheng",
        candidates=[
            ImportCandidate(
                source_id="untagged",
                markdown=raw,
                assigned_owner="cheng",
            )
        ],
    )
    target = review_bucket_snapshot(
        str(tmp_path), result.imported_id_map["untagged"]
    )
    assert target["owner"] == "cheng"


@pytest.mark.asyncio
async def test_personal_scope_rejects_foreign_owner_and_cross_owner_overwrite(
    tmp_path,
):
    with pytest.raises(ImportTransactionError, match="outside the job scope"):
        await apply_import_batch(
            buckets_dir=str(tmp_path),
            bucket_manager=DiskManager(str(tmp_path)),
            job_owner="cheng",
            candidates=[
                ImportCandidate(
                    source_id="foreign",
                    markdown=_markdown("foreign", "body", owner="huaiyin"),
                )
            ],
        )

    _put(tmp_path, "same.md", _markdown("same", "live", owner="cheng"))
    snapshot = review_bucket_snapshot(str(tmp_path), "same")
    with pytest.raises(ImportTransactionError, match="owner mismatch|outside"):
        await apply_import_batch(
            buckets_dir=str(tmp_path),
            bucket_manager=DiskManager(str(tmp_path)),
            job_owner=ADMIN_RESTORE_SCOPE,
            candidates=[
                ImportCandidate(
                    source_id="same",
                    markdown=_markdown("same", "foreign", owner="huaiyin"),
                    decision="overwrite",
                    conflicted_at_review=True,
                    expected_live_sha256=snapshot["sha256"],
                    expected_live_relative_path=snapshot["relative_path"],
                )
            ],
        )
    assert review_bucket_snapshot(str(tmp_path), "same")["owner"] == "cheng"


@pytest.mark.asyncio
async def test_symlink_or_directory_target_is_never_followed_or_replaced(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text("outside", encoding="utf-8")
    target = tmp_path / "dynamic" / "m03" / "safe_safe.md"
    target.parent.mkdir(parents=True)
    try:
        target.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    with pytest.raises(ImportTransactionError):
        await apply_import_batch(
            buckets_dir=str(tmp_path),
            bucket_manager=DiskManager(str(tmp_path)),
            job_owner="cheng",
            candidates=[
                ImportCandidate(
                    source_id="safe",
                    markdown=_markdown("safe", "body", owner="cheng"),
                )
            ],
        )
    assert outside.read_text(encoding="utf-8") == "outside"

    target.unlink()
    target.mkdir()
    with pytest.raises(ImportTransactionError):
        await apply_import_batch(
            buckets_dir=str(tmp_path),
            bucket_manager=DiskManager(str(tmp_path)),
            job_owner="cheng",
            candidates=[
                ImportCandidate(
                    source_id="safe",
                    markdown=_markdown("safe", "body", owner="cheng"),
                )
            ],
        )
