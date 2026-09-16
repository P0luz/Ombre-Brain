import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tools.i.profile_contract import (
    ProfileContractError,
    SCHEMA,
    build_profile_draft,
    is_active_profile,
    parse_source_refs,
    validate_evidence_bucket,
    validate_transition,
)


class EvidenceProfileContractTests(unittest.TestCase):
    def draft(self, **overrides):
        values = {
            "caller": "cheng",
            "content": "我会先验证，再把判断写成稳定结论。",
            "aspect": "patterns",
            "confidence": 0.86,
            "evidence_id": "codex:session-1:L42",
            "confirm_stable": True,
            "updated_at": "2026-07-29T07:00:00Z",
        }
        values.update(overrides)
        return build_profile_draft(**values)

    def test_valid_draft_uses_i_owner_scope_voice_contract(self):
        draft = self.draft(
            source_bucket="0bb1c61c3ed1",
            source_refs="salon:message:100, codex:session-1:L42",
        )
        self.assertEqual(draft.tags[-3:], ("owner:cheng", "scope:self", "voice:self"))
        self.assertEqual(draft.metadata["profile_schema"], SCHEMA)
        self.assertEqual(draft.metadata["status"], "active")
        self.assertEqual(
            draft.all_evidence_refs,
            (
                "bucket:0bb1c61c3ed1",
                "codex:session-1:L42",
                "salon:message:100",
            ),
        )
        self.assertTrue(is_active_profile(draft.metadata))

    def test_requires_recognized_caller(self):
        for caller in ("", "unknown", "shared"):
            with self.subTest(caller=caller):
                with self.assertRaises(ProfileContractError):
                    self.draft(caller=caller)

    def test_requires_existing_i_aspect(self):
        with self.assertRaises(ProfileContractError):
            self.draft(aspect="personality")

    def test_requires_finite_confidence_in_range(self):
        for value in (-0.01, 1.01, math.nan, math.inf, "not-a-number"):
            with self.subTest(value=value):
                with self.assertRaises(ProfileContractError):
                    self.draft(confidence=value)

    def test_requires_evidence(self):
        with self.assertRaises(ProfileContractError):
            self.draft(evidence_id="", source_bucket="", source_refs="")

    def test_requires_explicit_stability_confirmation(self):
        with self.assertRaises(ProfileContractError):
            self.draft(confirm_stable=False)

    def test_references_are_deduplicated_and_bounded(self):
        self.assertEqual(
            parse_source_refs("codex:a:L1,codex:a:L1\nsalon:message:2"),
            ("codex:a:L1", "salon:message:2"),
        )
        with self.assertRaises(ProfileContractError):
            parse_source_refs([f"codex:s:L{i}" for i in range(13)])
        with self.assertRaises(ProfileContractError):
            parse_source_refs("contains whitespace")

    def test_terminal_transitions_require_reason_and_cannot_reactivate(self):
        self.assertEqual(
            validate_transition("active", "revoked", "证据被原作者撤回"),
            "revoked",
        )
        self.assertEqual(
            validate_transition("revoked", "revoked", "幂等重放"),
            "revoked",
        )
        for current, target, reason in (
            ("active", "active", "no"),
            ("active", "invalidated", ""),
            ("revoked", "superseded", "try again"),
            ("invalidated", "active", "reactivate"),
        ):
            with self.subTest(current=current, target=target):
                with self.assertRaises(ProfileContractError):
                    validate_transition(current, target, reason)

    def test_incomplete_or_terminal_metadata_is_never_active(self):
        good = self.draft().metadata
        cases = [
            {**good, "status": "revoked"},
            {**good, "confidence": 2.0},
            {**good, "updated_at": ""},
            {
                **good,
                "evidence_id": "",
                "source_bucket": "",
                "source_refs": [],
            },
            {**good, "profile_schema": "legacy"},
        ]
        for metadata in cases:
            with self.subTest(metadata=metadata):
                self.assertFalse(is_active_profile(metadata))

    def test_source_bucket_requires_exact_allowed_owner(self):
        for owner in ("cheng", "shared", "shared_core"):
            validate_evidence_bucket(
                {"metadata": {"tags": [f"owner:{owner}"]}},
                "cheng",
            )
        for bucket in (
            None,
            {"metadata": {"tags": []}},
            {"metadata": {"tags": ["owner:cheng", "owner:huaiyin"]}},
            {"metadata": {"tags": ["owner:huaiyin"]}},
            {"metadata": {"tags": ["owner:shared_context"]}},
        ):
            with self.subTest(bucket=bucket):
                with self.assertRaises(ProfileContractError):
                    validate_evidence_bucket(bucket, "cheng")


if __name__ == "__main__":
    unittest.main()
