from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import tools._runtime as rt
from tools import _identity
from tools.breath import dispatch
from tools.breath.handoff import HandoffBudget, build_handoff, surface_handoff


def bucket(
    bucket_id: str,
    owner: str,
    content: str,
    *,
    tags: list[str] | None = None,
    bucket_type: str = "dynamic",
    importance: int = 5,
    created: str = "2026-09-01T00:00:00+00:00",
    **metadata,
):
    all_tags = list(tags or [])
    if owner:
        all_tags.append(f"owner:{owner}")
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {
            "name": bucket_id,
            "tags": all_tags,
            "type": bucket_type,
            "importance": importance,
            "created": created,
            **metadata,
        },
    }


def test_identity_is_first_and_survives_tiny_budget():
    text = build_handoff(
        [bucket("mine", "cheng", "x" * 1000)],
        "cheng",
        HandoffBudget(total_chars=100, item_chars=120),
    )["rendered"]
    assert text.startswith("=== Identity / 不可压缩 ===")
    assert "self=澄" in text
    assert "caller=cheng" in text
    assert len(text) <= 100


def test_foreign_untagged_ambiguous_and_mixed_private_buckets_are_denied():
    ambiguous = bucket("ambiguous", "cheng", "owner conflict", pinned=True)
    ambiguous["metadata"]["tags"].append("owner:huaiyin")
    result = build_handoff(
        [
            bucket("mine", "cheng", "我的线索"),
            bucket("foreign", "huaiyin", "别人的线索", pinned=True),
            bucket("untagged", "", "没有门牌", pinned=True),
            ambiguous,
            bucket("mixed", "cheng", "本线\nowner:huaiyin 的旧合并段"),
            bucket("shared", "shared_core", "共同护栏", pinned=True),
            bucket("context", "shared_context", "普通共享上下文", pinned=True),
        ],
        "cheng",
    )["rendered"]
    assert "mine" in result
    assert "shared" in result
    for denied in ("foreign", "untagged", "ambiguous", "mixed", "context"):
        assert denied not in result


def test_portrait_accepts_legacy_evidence_profile_and_v36_promoted_i_only():
    result = build_handoff(
        [
            bucket(
                "legacy",
                "cheng",
                "旧证据画像",
                tags=["profile_v1", "scope:self", "voice:self"],
                bucket_type="i",
                evidence_id="event:1",
                confidence=0.9,
                updated_at="2026-08-01T00:00:00Z",
                status="active",
                dont_surface=True,
            ),
            bucket(
                "promoted",
                "cheng",
                "新版沉淀 I",
                tags=["__i__"],
                bucket_type="i",
                i_from_candidate="candidate-1",
                i_dream_dates=["2026-08-01", "2026-08-02", "2026-08-03"],
                dont_surface=True,
            ),
            bucket(
                "guess",
                "cheng",
                "没有证据",
                bucket_type="i",
                dont_surface=True,
            ),
        ],
        "cheng",
    )
    ids = {item["bucket_id"] for item in result["sections"]["portrait"]}
    assert ids == {"legacy", "promoted"}
    assert "candidate-1" in result["rendered"]


@pytest.mark.asyncio
async def test_missing_caller_refuses_before_bucket_read(monkeypatch):
    manager = MagicMock()
    manager.list_all = AsyncMock()
    monkeypatch.setattr(rt, "bucket_mgr", manager)
    _identity.set_caller("")
    text = await surface_handoff()
    assert "已拒绝" in text
    assert "可信传输" in text
    manager.list_all.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_handoff_short_circuits_decay_and_other_modes(monkeypatch):
    manager = MagicMock()
    manager.list_all = AsyncMock(
        return_value=[
            bucket("mine", "cheng", "当前施工线索"),
            bucket("foreign", "huaiyin", "不该泄漏"),
        ]
    )
    decay = MagicMock()
    decay.ensure_started = AsyncMock(side_effect=AssertionError("handoff must not start decay"))
    monkeypatch.setattr(rt, "bucket_mgr", manager)
    monkeypatch.setattr(rt, "decay_engine", decay)
    monkeypatch.setattr(rt, "config", {"surfacing": {"handoff_max_chars": 6000}})
    monkeypatch.setattr(rt, "mark_op", None)
    monkeypatch.setattr(rt, "record_v3_tool_event", lambda *args, **kwargs: None)
    _identity.set_caller("cheng")
    try:
        text = await dispatch(query="ignored", catalog=True, mode="handoff")
    finally:
        _identity.set_caller("")
    assert text.startswith("=== Identity / 不可压缩 ===")
    assert "mine" in text
    assert "foreign" not in text
    manager.list_all.assert_awaited_once_with(include_archive=False)
    decay.ensure_started.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError, match="仅支持"):
        await dispatch(mode="surprise")
