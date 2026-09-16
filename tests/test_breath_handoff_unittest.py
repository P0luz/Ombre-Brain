"""Standard-library regression tests for breath(mode="handoff").

The production environment intentionally has no pytest installed, so these tests
remain runnable with ``python -m unittest`` while also being discoverable by
pytest in development environments.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import tools._runtime as rt
from tools import _identity
from tools.breath import dispatch
from tools.breath.handoff import HandoffBudget, build_handoff, surface_handoff
from tools.i.profile_contract import SCHEMA as PROFILE_SCHEMA


def bucket(
    bucket_id,
    owner,
    content,
    *,
    tags=None,
    bucket_type="dynamic",
    importance=5,
    created="2026-07-29T00:00:00+00:00",
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


class HandoffPureTests(unittest.TestCase):
    def test_identity_block_is_first_and_non_compressible(self):
        result = build_handoff([], "huaiyin_cc", HandoffBudget(total_chars=80))
        text = result["rendered"]
        self.assertTrue(text.startswith("=== Identity / 不可压缩 ==="))
        self.assertIn("self=怀音-cc", text)
        self.assertIn("caller=huaiyin_cc", text)
        self.assertIn("不是澄，也不是怀音", text)

    def test_foreign_untagged_and_ambiguous_buckets_are_denied(self):
        ambiguous = bucket("ambiguous", "cheng", "owner 冲突", pinned=True)
        ambiguous["metadata"]["tags"].append("owner:huaiyin")
        result = build_handoff(
            [
                bucket("mine", "cheng", "我的当前线索"),
                bucket("foreign", "huaiyin", "怀音的身份材料", pinned=True),
                bucket("unknown", "", "没有 owner 的旧材料", pinned=True),
                ambiguous,
                bucket("shared", "shared_core", "共同护栏", pinned=True),
                bucket("context", "shared_context", "普通共享上下文", pinned=True),
            ],
            "cheng",
        )
        text = result["rendered"]
        self.assertIn("mine", text)
        self.assertIn("shared", text)
        self.assertNotIn("foreign", text)
        self.assertNotIn("unknown", text)
        self.assertNotIn("ambiguous", text)
        self.assertNotIn("context", text)

    def test_portrait_requires_full_v1_contract_and_honors_revocation(self):
        result = build_handoff(
            [
                bucket(
                    "grounded",
                    "cheng",
                    "有证据的画像结论",
                    tags=["profile_v1", "scope:self", "voice:self"],
                    bucket_type="i",
                    profile_schema=PROFILE_SCHEMA,
                    evidence_id="codex:abc:L12",
                    confidence=0.85,
                    updated_at="2026-07-29T07:00:00Z",
                    status="active",
                    dont_surface=True,
                ),
                bucket(
                    "guess",
                    "cheng",
                    "只有模型印象",
                    tags=["profile_v1", "scope:self", "voice:self"],
                    bucket_type="i",
                    profile_schema=PROFILE_SCHEMA,
                    confidence=0.8,
                    updated_at="2026-07-29T07:00:00Z",
                    status="active",
                    dont_surface=True,
                ),
                bucket(
                    "revoked",
                    "cheng",
                    "已经撤回的画像",
                    tags=["profile_v1", "scope:self", "voice:self"],
                    bucket_type="i",
                    profile_schema=PROFILE_SCHEMA,
                    evidence_id="codex:abc:L13",
                    confidence=0.9,
                    updated_at="2026-07-29T07:00:00Z",
                    status="revoked",
                    dont_surface=True,
                ),
                bucket(
                    "legacy_marker",
                    "cheng",
                    "旧 profile_fact 只有证据，不再视为稳定画像",
                    tags=["profile_fact"],
                    evidence_id="codex:abc:L14",
                ),
            ],
            "cheng",
        )
        portrait_ids = {
            item["bucket_id"] for item in result["sections"]["portrait"]
        }
        self.assertEqual(portrait_ids, {"grounded"})
        self.assertIn("codex:abc:L12", result["rendered"])
        self.assertIn("confidence=0.85", result["rendered"])
        self.assertIn("updated_at=2026-07-29T07:00:00Z", result["rendered"])

    def test_private_bucket_with_explicit_foreign_owner_segment_is_denied(self):
        result = build_handoff(
            [
                bucket(
                    "mixed",
                    "cheng",
                    "澄侧开头\n---\n【旧合并段·owner:huaiyin】怀音侧亲历",
                    tags=["relationship"],
                ),
                bucket(
                    "shared_rule",
                    "shared_core",
                    "规则示例可同时提 owner:cheng 与 owner:huaiyin",
                    pinned=True,
                ),
            ],
            "cheng",
        )
        self.assertNotIn("mixed", result["rendered"])
        self.assertIn("shared_rule", result["rendered"])

    def test_budget_never_removes_identity(self):
        many = [
            bucket(
                f"b{i}",
                "cheng",
                "很长的连续性内容" * 100,
                created=f"2026-07-{20+i:02d}T00:00:00+00:00",
            )
            for i in range(1, 5)
        ]
        result = build_handoff(
            many,
            "cheng",
            HandoffBudget(total_chars=220, item_chars=180),
        )
        self.assertIn("self=澄", result["rendered"])
        self.assertLessEqual(len(result["rendered"]), 220)

    def test_active_plan_is_focus_and_reminder(self):
        result = build_handoff(
            [
                bucket(
                    "plan",
                    "cheng",
                    "下一步做隔离复验",
                    bucket_type="plan",
                    status="active",
                ),
                bucket(
                    "done",
                    "cheng",
                    "已经完成",
                    bucket_type="plan",
                    status="resolved",
                ),
            ],
            "cheng",
        )
        focus_ids = {
            item["bucket_id"] for item in result["sections"]["current_focus"]
        }
        reminder_ids = {
            item["bucket_id"] for item in result["sections"]["reminders"]
        }
        self.assertEqual(focus_ids, {"plan"})
        self.assertEqual(reminder_ids, {"plan"})

    def test_domain_and_string_flags_are_normalized(self):
        result = build_handoff(
            [
                bucket(
                    "relationship",
                    "cheng",
                    "与 hana 的关系锚点",
                    domain="关系",
                    dont_surface="0",
                ),
                bucket("open", "cheng", "仍需继续", resolved="0"),
                bucket("closed", "cheng", "已经结束", resolved="1"),
                bucket("hidden", "cheng", "不要浮现", dont_surface="1"),
            ],
            "cheng",
        )
        relationship_ids = {
            item["bucket_id"] for item in result["sections"]["relationship"]
        }
        recent_ids = {
            item["bucket_id"] for item in result["sections"]["recent_continuity"]
        }
        self.assertEqual(relationship_ids, {"relationship"})
        self.assertIn("open", recent_ids)
        self.assertNotIn("closed", recent_ids)
        self.assertNotIn("hidden", result["rendered"])

    def test_recent_continuity_uses_created_not_read_touch_time(self):
        result = build_handoff(
            [
                bucket(
                    "old_but_touched",
                    "cheng",
                    "旧事今天被检索过",
                    created="2026-07-01T00:00:00+00:00",
                    last_active="2026-07-29T12:00:00+00:00",
                ),
                bucket(
                    "new_event",
                    "cheng",
                    "今天真正发生的事",
                    created="2026-07-29T11:00:00+00:00",
                    last_active="2026-07-29T11:00:00+00:00",
                ),
            ],
            "cheng",
            HandoffBudget(recent_items=1),
        )
        recent_ids = [
            item["bucket_id"]
            for item in result["sections"]["recent_continuity"]
        ]
        self.assertEqual(recent_ids, ["new_event"])

    def test_non_reminder_sections_do_not_repeat_same_bucket(self):
        shared_core = bucket(
            "core_relationship",
            "shared_core",
            "同时是核心和关系锚点",
            tags=["relationship"],
            pinned=True,
        )
        result = build_handoff([shared_core], "cheng")
        ids = [
            item["bucket_id"]
            for name, items in result["sections"].items()
            if name != "reminders"
            for item in items
        ]
        self.assertEqual(ids.count("core_relationship"), 1)

    def test_all_identity_contracts_use_matching_owner_and_caller(self):
        expected = {
            "cheng": ("澄", "owner:cheng", "caller=cheng"),
            "huaiyin": ("怀音", "owner:huaiyin", "caller=huaiyin"),
            "huaiyin_cc": ("怀音-cc", "owner:huaiyin_cc", "caller=huaiyin_cc"),
        }
        for caller, required in expected.items():
            with self.subTest(caller=caller):
                text = build_handoff([], caller)["rendered"]
                for value in required:
                    self.assertIn(value, text)


class _NoopDecay:
    async def ensure_started(self):
        return None


class HandoffDispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_config = rt.config
        self.old_bucket_mgr = rt.bucket_mgr
        self.old_decay_engine = rt.decay_engine
        self.old_logger = rt.logger
        self.old_mark_op = rt.mark_op
        self.bucket_mgr = MagicMock()
        self.bucket_mgr.list_all = AsyncMock(
            return_value=[
                bucket("mine", "cheng", "当前施工线索"),
                bucket("foreign", "huaiyin", "不该泄漏"),
            ]
        )
        rt.config = {"surfacing": {"handoff_max_chars": 6000}}
        rt.bucket_mgr = self.bucket_mgr
        rt.decay_engine = _NoopDecay()
        rt.logger = MagicMock()
        rt.mark_op = None

    def tearDown(self):
        _identity.set_caller("")
        rt.config = self.old_config
        rt.bucket_mgr = self.old_bucket_mgr
        rt.decay_engine = self.old_decay_engine
        rt.logger = self.old_logger
        rt.mark_op = self.old_mark_op

    async def test_missing_caller_refuses_without_reading_buckets(self):
        _identity.set_caller("")
        text = await surface_handoff()
        self.assertIn("已拒绝", text)
        self.assertIn("没有可识别的 caller", text)
        self.bucket_mgr.list_all.assert_not_awaited()

    async def test_dispatch_mode_handoff_short_circuits_old_branches(self):
        _identity.set_caller("cheng")
        with patch(
            "tools.breath.surface_default",
            new=AsyncMock(side_effect=AssertionError("old branch must not run")),
        ):
            text = await dispatch(
                query="ignored",
                tags="ignored",
                catalog=True,
                mode="handoff",
            )
        self.assertTrue(text.startswith("=== Identity / 不可压缩 ==="))
        self.assertIn("mine", text)
        self.assertNotIn("foreign", text)
        self.bucket_mgr.list_all.assert_awaited_once_with(include_archive=False)

    async def test_unknown_mode_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "仅支持"):
            await dispatch(mode="surprise")


if __name__ == "__main__":
    unittest.main()
