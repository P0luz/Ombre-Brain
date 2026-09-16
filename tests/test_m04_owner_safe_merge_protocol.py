"""Standard-library checks for M-04's owner-safe merge precondition seam."""

from __future__ import annotations

import asyncio
import copy
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from bucket_manager import BucketManager
from tools import _common as common
from tools import _runtime as rt
from tools import _identity


class _ProbeManager:
    def __init__(self, base_dir: str) -> None:
        self.base_dir = base_dir
        self.target = {
            "id": "target",
            "content": "old body",
            "metadata": {
                "tags": ["owner:cheng"],
                "importance": 5,
                "domain": ["work"],
            },
        }
        self.created = None
        self.writes = []
        self.creates = []

    def _require_embedding_available(self) -> None:
        return None

    async def list_all(self, **_kwargs):
        return [copy.deepcopy(self.target)] if self.target else []

    async def search(self, *_args, **_kwargs):
        return ([{**copy.deepcopy(self.target), "score": 99.0}]
                if self.target else [])

    async def get(self, bucket_id: str):
        if bucket_id == "target" and self.target:
            return copy.deepcopy(self.target)
        if bucket_id == "created" and self.created:
            return copy.deepcopy(self.created)
        return None

    async def _update_locked(self, bucket_id: str, **updates) -> bool:
        if bucket_id != "target" or not self.target:
            return False
        self.writes.append(copy.deepcopy(updates))
        self.target["content"] = updates["content"]
        self.target["metadata"].update({
            key: value for key, value in updates.items() if key != "content"
        })
        return True

    async def create(self, **kwargs) -> str:
        self.creates.append(copy.deepcopy(kwargs))
        self.created = {
            "id": "created",
            "content": kwargs["content"],
            "metadata": {"tags": list(kwargs.get("tags") or [])},
        }
        return "created"


class _Provider:
    def __init__(self, callback=None) -> None:
        self.callback = callback
        self.calls = []

    async def merge(self, old: str, incoming: str) -> str:
        self.calls.append((old, incoming))
        if self.callback:
            return await self.callback(old, incoming)
        return f"{old} + {incoming}"


class OwnerSafeMergeProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manager = _ProbeManager(self.temp.name)
        self.original = {
            "bucket_mgr": getattr(rt, "bucket_mgr", None),
            "config": getattr(rt, "config", None),
            "dehydrator": getattr(rt, "dehydrator", None),
            "logger": getattr(rt, "logger", None),
            "embedding_engine": getattr(rt, "embedding_engine", None),
        }
        rt.bucket_mgr = self.manager
        rt.config = {"merge_threshold": 75, "limits": {}}
        rt.logger = MagicMock()
        rt.embedding_engine = None
        _identity.set_caller("cheng")
        self.addCleanup(self._restore_runtime)

    def _restore_runtime(self) -> None:
        for name, value in self.original.items():
            setattr(rt, name, value)

    async def _merge(self, **overrides):
        args = {
            "content": "incoming",
            "tags": ["owner:cheng"],
            "importance": 5,
            "domain": ["work"],
            "valence": 0.5,
            "arousal": 0.3,
            "source_tool": "hold",
        }
        args.update(overrides)
        return await common.merge_or_create(**args)

    async def test_normal_merge_runs_provider_before_commit(self) -> None:
        async def provider(old: str, incoming: str) -> str:
            self.assertEqual(self.manager.writes, [])
            self.assertEqual(self.manager.creates, [])
            return f"{old} + {incoming}"

        fake = _Provider(provider)
        rt.dehydrator = fake
        bucket_id, merged, warning = await self._merge(source_tool="grow")

        self.assertEqual((bucket_id, merged, warning), ("target", True, ""))
        self.assertEqual(fake.calls, [("old body", "incoming")])
        self.assertEqual(self.manager.target["content"], "old body + incoming")
        self.assertEqual(self.manager.writes[0]["last_merged_by"], "grow")

    async def test_real_temporary_markdown_is_unchanged_until_commit(self) -> None:
        class Embedding:
            enabled = True

            async def generate_and_store(self, *_args):
                return True

            async def search_similar(self, *_args, **_kwargs):
                return []

        config = {
            "buckets_dir": self.temp.name,
            "merge_threshold": 75,
            "matching": {"fuzzy_threshold": 50, "max_results": 5},
            "wikilink": {"enabled": False},
            "limits": {},
            "scoring_weights": {},
        }
        manager = BucketManager(config, embedding_engine=Embedding())
        rt.bucket_mgr = manager
        rt.config = config
        target_id = await manager.create_internal(
            content="old body", tags=["owner:cheng"], domain=["work"]
        )
        target = await manager.get(target_id)
        path = Path(target["path"])
        before = path.read_bytes()
        candidate = {**target, "score": 99.0}
        manager.search = AsyncMock(return_value=[candidate])

        async def provider(old: str, incoming: str) -> str:
            self.assertEqual(path.read_bytes(), before)
            async with common._content_turn(incoming):
                pass
            return f"{old}>{incoming}"

        rt.dehydrator = _Provider(provider)
        result_id, merged, _warning = await self._merge()
        self.assertEqual((result_id, merged), (target_id, True))
        self.assertNotEqual(path.read_bytes(), before)
        stored = await manager.get(target_id)
        self.assertEqual(stored["content"], "old body>incoming")

    async def test_provider_can_reacquire_content_turn_before_commit_seam(self) -> None:
        events = []

        async def provider(old: str, incoming: str) -> str:
            async with common._content_turn(incoming):
                events.append("provider-content-turn")
            return f"{old}|{incoming}"

        @asynccontextmanager
        async def seam():
            events.append("seam-enter")
            try:
                yield
            finally:
                events.append("seam-exit")

        rt.dehydrator = _Provider(provider)
        with patch.object(common, "_m04_merge_commit_turn", seam):
            await self._merge()
        self.assertEqual(events, ["provider-content-turn", "seam-enter", "seam-exit"])

    async def test_stale_target_retries_and_recomputes_only_after_body_change(self) -> None:
        async def provider(old: str, incoming: str) -> str:
            if old == "old body":
                self.manager.target["content"] = "new body"
            return f"{old}>{incoming}"

        fake = _Provider(provider)
        rt.dehydrator = fake
        bucket_id, merged, _warning = await self._merge()

        self.assertEqual((bucket_id, merged), ("target", True))
        self.assertEqual(fake.calls, [("old body", "incoming"), ("new body", "incoming")])
        self.assertEqual(self.manager.target["content"], "new body>incoming")

    async def test_metadata_change_revalidates_without_recomputing_provider(self) -> None:
        async def provider(old: str, incoming: str) -> str:
            self.manager.target["metadata"]["valence"] = 0.8
            return f"{old}>{incoming}"

        fake = _Provider(provider)
        rt.dehydrator = fake
        bucket_id, merged, _warning = await self._merge(valence=0.6)

        self.assertEqual((bucket_id, merged), ("target", True))
        self.assertEqual(fake.calls, [("old body", "incoming")])
        self.assertEqual(self.manager.writes[0]["valence"], 0.7)

    async def test_importance_is_recomputed_from_locked_current_metadata(self) -> None:
        rt.dehydrator = _Provider()

        @asynccontextmanager
        async def bucket_turn(_bucket_id: str):
            self.manager.target["metadata"]["importance"] = 10
            yield

        with patch.object(common, "_bucket_turn", bucket_turn):
            bucket_id, merged, _warning = await self._merge(importance=9)

        self.assertEqual((bucket_id, merged), ("target", True))
        self.assertEqual(self.manager.writes[0]["importance"], 10)
        self.assertEqual(self.manager.target["metadata"]["importance"], 10)

    async def test_target_disappearance_creates_without_mutating_missing_target(self) -> None:
        async def provider(old: str, incoming: str) -> str:
            self.manager.target = None
            return f"{old}>{incoming}"

        rt.dehydrator = _Provider(provider)
        bucket_id, merged, _warning = await self._merge()

        self.assertEqual((bucket_id, merged), ("created", False))
        self.assertEqual(self.manager.writes, [])
        self.assertEqual(self.manager.creates[0]["content"], "incoming")

    async def test_repeated_concurrent_change_exhaustion_creates_without_overwrite(self) -> None:
        generation = 0

        async def provider(old: str, incoming: str) -> str:
            nonlocal generation
            generation += 1
            self.manager.target["content"] = f"concurrent-{generation}"
            return f"{old}>{incoming}"

        fake = _Provider(provider)
        rt.dehydrator = fake
        bucket_id, merged, _warning = await self._merge()

        self.assertEqual((bucket_id, merged), ("created", False))
        self.assertEqual(len(fake.calls), common._MERGE_COMMIT_MAX_ATTEMPTS)
        self.assertEqual(self.manager.writes, [])
        self.assertEqual(self.manager.target["content"], "concurrent-3")

    async def test_raced_exact_at_every_review_fails_closed_without_create(self) -> None:
        self.manager.target["content"] = "incoming"
        raced_exact = copy.deepcopy(self.manager.target)
        self.assertTrue(
            common._mergeable_for_owner(raced_exact, ["owner:cheng"])
        )

        stale_selections = AsyncMock(return_value=None)
        exact_reviews = AsyncMock(return_value=[raced_exact])
        with (
            patch.object(
                common, "_select_merge_target", new=stale_selections
            ),
            patch.object(
                common, "_fresh_active_buckets", new=exact_reviews
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "merge/create state changed repeatedly; retry the write",
            ):
                await self._merge()

        self.assertEqual(
            stale_selections.await_count,
            common._MERGE_COMMIT_MAX_ATTEMPTS,
        )
        self.assertEqual(
            exact_reviews.await_count,
            common._MERGE_COMMIT_MAX_ATTEMPTS + 1,
        )
        self.assertEqual(self.manager.creates, [])
        self.assertEqual(self.manager.writes, [])
        self.assertEqual(self.manager.target["content"], "incoming")

    async def test_provider_failure_and_cancellation_leave_no_markdown_mutation(self) -> None:
        async def failing(_old: str, _incoming: str) -> str:
            raise RuntimeError("provider failed")

        rt.dehydrator = _Provider(failing)
        with self.assertRaisesRegex(RuntimeError, "provider failed"):
            await self._merge()
        self.assertEqual(self.manager.writes, [])
        self.assertEqual(self.manager.creates, [])

        entered = asyncio.Event()
        never = asyncio.Event()

        async def blocking(_old: str, _incoming: str) -> str:
            entered.set()
            await never.wait()
            return "unreachable"

        rt.dehydrator = _Provider(blocking)
        task = asyncio.create_task(self._merge())
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.manager.writes, [])
        self.assertEqual(self.manager.creates, [])
        async with common._content_turn("incoming"):
            pass

    async def test_duplicate_content_skips_provider_and_owner_mismatch_does_not_claim_target(self) -> None:
        self.manager.target["content"] = "incoming"

        async def forbidden(_old: str, _incoming: str) -> str:
            raise AssertionError("exact duplicate must not call provider")

        rt.dehydrator = _Provider(forbidden)
        bucket_id, merged, _warning = await self._merge()
        self.assertEqual((bucket_id, merged), ("target", True))
        self.assertEqual(self.manager.writes[0]["content"], "incoming")

        self.manager = _ProbeManager(self.temp.name)
        rt.bucket_mgr = self.manager

        async def owner_switch(old: str, incoming: str) -> str:
            self.manager.target["metadata"]["tags"] = ["owner:huaiyin"]
            return f"{old}>{incoming}"

        rt.dehydrator = _Provider(owner_switch)
        bucket_id, merged, _warning = await self._merge()
        self.assertEqual((bucket_id, merged), ("created", False))
        self.assertEqual(self.manager.writes, [])
        self.assertEqual(self.manager.target["content"], "old body")

    async def test_high_importance_quota_is_rechecked_in_commit_phase(self) -> None:
        rt.dehydrator = _Provider()
        bucket_id, merged, _warning = await self._merge(importance=9)
        self.assertEqual((bucket_id, merged), ("target", True))
        self.assertEqual(self.manager.writes[0]["importance"], 9)


if __name__ == "__main__":
    unittest.main()
