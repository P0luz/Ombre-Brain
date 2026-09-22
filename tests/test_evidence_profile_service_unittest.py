import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tools.i.profile_contract import ProfileContractError, SCHEMA
from tools.i import core as i_core_module
from tools.i.profile_service import create_profile, list_profiles, transition_profile
from tools import _identity
import tools._runtime as rt
from web.hooks import _select_i_buckets_for_caller


class FakeBucketManager:
    def __init__(self):
        self.rows = {}
        self.next_id = 1
        self.fail_update = False
        self.deleted = []

    async def create(self, content, tags, **kwargs):
        bucket_id = f"profile{self.next_id:04d}"
        self.next_id += 1
        self.rows[bucket_id] = {
            "id": bucket_id,
            "content": content,
            "metadata": {
                "tags": list(tags),
                "type": kwargs.get("bucket_type"),
                "created": f"2026-07-29T00:00:0{self.next_id}Z",
            },
        }
        return bucket_id

    async def update(self, bucket_id, **kwargs):
        if self.fail_update:
            return False
        self.rows[bucket_id]["metadata"].update(copy.deepcopy(kwargs))
        return True

    async def get(self, bucket_id):
        row = self.rows.get(bucket_id)
        return copy.deepcopy(row) if row else None

    async def list_all(self, include_archive=False):
        return copy.deepcopy(list(self.rows.values()))

    async def delete(self, bucket_id):
        self.deleted.append(bucket_id)
        self.rows.pop(bucket_id, None)
        return True

    def add_evidence(self, bucket_id, owner):
        self.rows[bucket_id] = {
            "id": bucket_id,
            "content": "evidence",
            "metadata": {"tags": [f"owner:{owner}"], "type": "dynamic"},
        }


class ProfileServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = FakeBucketManager()

    async def create(self, **overrides):
        values = {
            "caller": "cheng",
            "content": "遇到系统判断时，我会先找可复核证据。",
            "aspect": "patterns",
            "confidence": 0.88,
            "evidence_id": "codex:session:L10",
            "confirm_stable": True,
        }
        values.update(overrides)
        return await create_profile(self.manager, **values)

    async def test_create_commits_contract_atomically(self):
        result = await self.create()
        stored = await self.manager.get(result["bucket_id"])
        metadata = stored["metadata"]
        self.assertEqual(metadata["profile_schema"], SCHEMA)
        self.assertEqual(metadata["status"], "active")
        self.assertEqual(metadata["confidence"], 0.88)
        self.assertTrue(metadata["updated_at"])
        self.assertTrue(metadata["dont_surface"])
        self.assertIn("owner:cheng", metadata["tags"])
        self.assertIn("scope:self", metadata["tags"])
        self.assertIn("voice:self", metadata["tags"])

    async def test_partial_write_is_rolled_back(self):
        self.manager.fail_update = True
        with self.assertRaises(ProfileContractError):
            await self.create()
        self.assertEqual(self.manager.rows, {})
        self.assertEqual(self.manager.deleted, ["profile0001"])

    async def test_source_bucket_owner_is_checked_before_create(self):
        self.manager.add_evidence("foreign123", "huaiyin")
        with self.assertRaises(ProfileContractError):
            await self.create(
                evidence_id="",
                source_bucket="foreign123",
            )
        self.assertNotIn("profile0001", self.manager.rows)

    async def test_list_is_strictly_owner_scoped(self):
        own = await self.create()
        foreign = await self.create(caller="huaiyin")
        rows = await list_profiles(self.manager, caller="cheng")
        self.assertEqual([row["bucket_id"] for row in rows], [own["bucket_id"]])
        self.assertNotIn(foreign["bucket_id"], {row["bucket_id"] for row in rows})

    async def test_revoke_hides_profile_and_foreign_caller_cannot_mutate(self):
        created = await self.create()
        with self.assertRaises(ProfileContractError):
            await transition_profile(
                self.manager,
                caller="huaiyin",
                bucket_id=created["bucket_id"],
                target_status="revoked",
                reason="foreign attempt",
            )
        result = await transition_profile(
            self.manager,
            caller="cheng",
            bucket_id=created["bucket_id"],
            target_status="revoked",
            reason="evidence was withdrawn",
        )
        self.assertEqual(result["status"], "revoked")
        self.assertEqual(
            await list_profiles(self.manager, caller="cheng"),
            [],
        )
        inactive = await list_profiles(
            self.manager,
            caller="cheng",
            include_inactive=True,
        )
        self.assertEqual(inactive[0]["status"], "revoked")

    async def test_legacy_i_is_not_listed_or_mutated(self):
        self.manager.rows["legacy001"] = {
            "id": "legacy001",
            "content": "legacy self observation",
            "metadata": {
                "type": "i",
                "tags": ["__i__", "owner:cheng", "aspect:nature"],
            },
        }
        self.assertEqual(await list_profiles(self.manager, caller="cheng"), [])
        with self.assertRaises(ProfileContractError):
            await transition_profile(
                self.manager,
                caller="cheng",
                bucket_id="legacy001",
                target_status="invalidated",
                reason="legacy stays untouched",
            )

    async def test_malformed_active_profile_is_not_listed(self):
        self.manager.rows["malformed"] = {
            "id": "malformed",
            "content": "missing confidence and updated_at",
            "metadata": {
                "type": "i",
                "profile_schema": SCHEMA,
                "status": "active",
                "evidence_id": "codex:s:L1",
                "tags": [
                    "__i__",
                    "profile_v1",
                    "owner:cheng",
                    "scope:self",
                    "voice:self",
                ],
            },
        }
        self.assertEqual(await list_profiles(self.manager, caller="cheng"), [])


class _NoopDecay:
    async def ensure_started(self):
        return None


class ICoreOwnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = FakeBucketManager()
        self.old_bucket_mgr = rt.bucket_mgr
        self.old_decay_engine = rt.decay_engine
        self.old_logger = rt.logger
        self.old_mark_op = rt.mark_op
        rt.bucket_mgr = self.manager
        rt.decay_engine = _NoopDecay()
        rt.logger = MagicMock()
        rt.mark_op = None
        _identity.set_caller("cheng")

    def tearDown(self):
        _identity.set_caller("")
        rt.bucket_mgr = self.old_bucket_mgr
        rt.decay_engine = self.old_decay_engine
        rt.logger = self.old_logger
        rt.mark_op = self.old_mark_op

    async def test_missing_caller_refuses_before_bucket_access(self):
        _identity.set_caller("")
        text = await i_core_module.i_core(read=True)
        self.assertIn("已拒绝", text)
        self.assertEqual(self.manager.rows, {})

    async def test_legacy_write_gets_owner_scope_voice_but_not_profile_schema(self):
        text = await i_core_module.i_core(
            content="这是一条仍在生长的自我观察，不是稳定画像。",
            aspect="uncertainty",
        )
        self.assertIn("owner:cheng", text)
        row = next(iter(self.manager.rows.values()))
        tags = row["metadata"]["tags"]
        self.assertIn("owner:cheng", tags)
        self.assertIn("scope:self", tags)
        self.assertIn("voice:self", tags)
        self.assertNotIn("profile_schema", row["metadata"])

    async def test_legacy_read_denies_foreign_untagged_and_multi_owner(self):
        for bucket_id, tags, content in (
            ("own", ["__i__", "owner:cheng"], "OWN I"),
            ("foreign", ["__i__", "owner:huaiyin"], "FOREIGN I"),
            ("untagged", ["__i__"], "UNTAGGED I"),
            (
                "multi",
                ["__i__", "owner:cheng", "owner:huaiyin"],
                "MULTI I",
            ),
        ):
            self.manager.rows[bucket_id] = {
                "id": bucket_id,
                "content": content,
                "metadata": {
                    "type": "i",
                    "tags": tags,
                    "last_active": "2026-07-29T00:00:00Z",
                },
            }
        text = await i_core_module.i_core(read=True)
        self.assertIn("OWN I", text)
        self.assertNotIn("FOREIGN I", text)
        self.assertNotIn("UNTAGGED I", text)
        self.assertNotIn("MULTI I", text)

    async def test_profile_actions_round_trip_and_terminal_state_is_hidden(self):
        created = json.loads(
            await i_core_module.i_core(
                action="create_profile",
                content="我会把证据和判断分开记录。",
                aspect="patterns",
                confidence=0.9,
                evidence_id="codex:session:L22",
                confirm_stable=True,
            )
        )
        listed = json.loads(
            await i_core_module.i_core(action="list_profiles")
        )
        self.assertEqual(
            [item["bucket_id"] for item in listed["profiles"]],
            [created["bucket_id"]],
        )
        revoked = json.loads(
            await i_core_module.i_core(
                action="revoke_profile",
                bucket_id=created["bucket_id"],
                reason="后续证据推翻",
            )
        )
        self.assertEqual(revoked["status"], "revoked")
        listed = json.loads(
            await i_core_module.i_core(action="list_profiles")
        )
        self.assertEqual(listed["profiles"], [])


class HookOwnerSelectionTests(unittest.TestCase):
    def test_session_start_i_section_requires_caller_and_active_owner_scope(self):
        base_profile = {
            "type": "i",
            "profile_schema": SCHEMA,
            "status": "active",
            "confidence": 0.8,
            "updated_at": "2026-07-29T07:00:00Z",
            "evidence_id": "codex:s:L1",
            "source_bucket": "",
            "source_refs": [],
        }
        buckets = [
            {
                "id": "own_profile",
                "content": "own",
                "metadata": {
                    **base_profile,
                    "tags": [
                        "__i__",
                        "profile_v1",
                        "owner:cheng",
                        "scope:self",
                        "voice:self",
                    ],
                },
            },
            {
                "id": "revoked_profile",
                "content": "revoked",
                "metadata": {
                    **base_profile,
                    "status": "revoked",
                    "tags": [
                        "__i__",
                        "profile_v1",
                        "owner:cheng",
                        "scope:self",
                        "voice:self",
                    ],
                },
            },
            {
                "id": "foreign_legacy",
                "content": "foreign",
                "metadata": {
                    "type": "i",
                    "tags": ["__i__", "owner:huaiyin"],
                },
            },
            {
                "id": "own_legacy",
                "content": "own legacy",
                "metadata": {
                    "type": "i",
                    "tags": ["__i__", "owner:cheng"],
                },
            },
            {
                "id": "untagged",
                "content": "unknown",
                "metadata": {"type": "i", "tags": ["__i__"]},
            },
        ]
        self.assertEqual(_select_i_buckets_for_caller(buckets, ""), [])
        selected = _select_i_buckets_for_caller(buckets, "cheng")
        self.assertEqual(
            {bucket["id"] for bucket in selected},
            {"own_profile", "own_legacy"},
        )


if __name__ == "__main__":
    unittest.main()
