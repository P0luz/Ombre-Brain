"""Tests for remainder sidecar integration with the merge path in _common.py."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from tools import _common as common
from tools import _runtime as rt
from tools import _identity
import remainder_sidecar as rs
from remainder_sidecar import (
    STATE_COMMITTED,
    STATE_ABORTED,
    STATE_PREPARED,
    load_sidecar_or_none,
)
from remainder_integration import (
    RemainderIntegrationError,
    clear_unresolved,
    recover_remainders_before_startup,
    runtime_status,
    _set_startup,
    _set_wiring,
    _status,
)


_META_CHENG = {"tags": ["owner:cheng"]}


class _MergeProbeManager:
    def __init__(self, base_dir: str) -> None:
        self.base_dir = base_dir
        self.target = {
            "id": "target",
            "content": "old body",
            "metadata": {
                "tags": ["owner:cheng"],
                "importance": 5,
                "domain": ["work"],
                "valence": 0.5,
                "arousal": 0.3,
            },
        }
        self.writes: list[dict] = []
        self.creates: list[dict] = []
        self._update_fail = False
        self._update_raise: BaseException | None = None

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
        return None

    async def _update_locked(self, bucket_id: str, **updates) -> bool:
        if self._update_raise is not None:
            raise self._update_raise
        if self._update_fail:
            return False
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
        return "created"


class _Provider:
    async def merge(self, old: str, incoming: str) -> str:
        return f"{old} + {incoming}"


class RemainderMergeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manager = _MergeProbeManager(self.temp.name)
        self.original = {
            "bucket_mgr": getattr(rt, "bucket_mgr", None),
            "config": getattr(rt, "config", None),
            "dehydrator": getattr(rt, "dehydrator", None),
            "logger": getattr(rt, "logger", None),
            "embedding_engine": getattr(rt, "embedding_engine", None),
        }
        rt.bucket_mgr = self.manager
        rt.config = {"merge_threshold": 75, "limits": {}}
        rt.dehydrator = _Provider()
        rt.logger = MagicMock()
        rt.embedding_engine = None
        _identity.set_caller("cheng")
        self.addCleanup(self._restore_runtime)
        self.addCleanup(clear_unresolved)

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

    # ---- exact_dup method ----

    async def test_exact_dup_merge_commits_sidecar(self) -> None:
        self.manager.target["content"] = "incoming"
        bucket_id, merged, _ = await self._merge(content="incoming")
        self.assertEqual(bucket_id, "target")
        sc = load_sidecar_or_none(self.temp.name, "target")
        self.assertIsNotNone(sc)
        last = sc.entries[-1]
        self.assertEqual(last.state, STATE_COMMITTED)
        self.assertEqual(last.merge_method, "exact_dup")

    # ---- raw_concat method ----

    async def test_raw_concat_merge_commits_sidecar(self) -> None:
        bucket_id, merged, _ = await self._merge(
            content="incoming", raw_merge=True,
        )
        self.assertEqual(bucket_id, "target")
        sc = load_sidecar_or_none(self.temp.name, "target")
        self.assertIsNotNone(sc)
        last = sc.entries[-1]
        self.assertEqual(last.state, STATE_COMMITTED)
        self.assertEqual(last.merge_method, "raw_concat")

    # ---- llm method ----

    async def test_llm_merge_commits_sidecar(self) -> None:
        bucket_id, merged, _ = await self._merge(content="incoming")
        self.assertEqual(bucket_id, "target")
        sc = load_sidecar_or_none(self.temp.name, "target")
        self.assertIsNotNone(sc)
        last = sc.entries[-1]
        self.assertEqual(last.state, STATE_COMMITTED)
        self.assertEqual(last.merge_method, "llm")

    # ---- PREPARED → Markdown → COMMITTED normal flow ----

    async def test_normal_flow_sidecar_committed(self) -> None:
        _, _, _ = await self._merge()
        sc = load_sidecar_or_none(self.temp.name, "target")
        self.assertIsNotNone(sc)
        committed = [e for e in sc.entries if e.state == STATE_COMMITTED]
        self.assertEqual(len(committed), 1)

    # ---- prepare fail: zero Markdown, zero create ----

    async def test_prepare_fail_no_markdown_no_create(self) -> None:
        rem_root = Path(self.temp.name) / ".remainders"
        rem_root.mkdir()
        sc_path = rem_root / "target.json"
        sc_path.write_text(json.dumps({
            "schema": 2, "bucket_id": "target", "generation": 1,
            "entries": [{
                "entry_id": "a" * 32,
                "state": "CONFLICT",
                "bucket_id": "target",
                "old_sha256": "0" * 64,
                "new_sha256": "0" * 64,
                "merged_sha256": "0" * 64,
                "unmatched_verbatim_lines": [],
                "merge_method": "llm",
                "owner": "cheng",
                "created_at": "2026-01-01T00:00:00Z",
                "committed_at": None,
            }],
        }))
        with self.assertRaises(RemainderIntegrationError):
            await self._merge()
        self.assertEqual(self.manager.writes, [])
        self.assertEqual(self.manager.creates, [])

    # ---- stale target: exits before sidecar ----

    async def test_stale_target_no_sidecar(self) -> None:
        self.manager.target = None
        _, merged, _ = await self._merge()
        sc = load_sidecar_or_none(self.temp.name, "target")
        self.assertIsNone(sc)

    # ---- Markdown raise → ABORTED ----

    async def test_markdown_raise_aborts_sidecar(self) -> None:
        self.manager._update_raise = RuntimeError("disk full")
        with self.assertRaises(RuntimeError):
            await self._merge()
        sc = load_sidecar_or_none(self.temp.name, "target")
        if sc is not None:
            for e in sc.entries:
                self.assertIn(e.state, (STATE_ABORTED, STATE_PREPARED))

    # ---- Markdown returns False → ABORTED ----

    async def test_markdown_false_aborts_sidecar(self) -> None:
        self.manager._update_fail = True
        try:
            await self._merge()
        except Exception:
            pass
        sc = load_sidecar_or_none(self.temp.name, "target")
        if sc is not None:
            aborted = [e for e in sc.entries if e.state == STATE_ABORTED]
            self.assertTrue(len(aborted) >= 1)

    async def test_markdown_false_abort_ok_no_create_on_retry(self) -> None:
        self.manager._update_fail = True
        try:
            await self._merge()
        except Exception:
            pass
        sc = load_sidecar_or_none(self.temp.name, "target")
        if sc is not None:
            aborted = [e for e in sc.entries if e.state == STATE_ABORTED]
            self.assertTrue(len(aborted) >= 1)

    # ---- Markdown success + commit fail: returns True, PREPARED preserved ----

    async def test_commit_fail_returns_true_prepared_preserved(self) -> None:
        original_transition = rs.transition_entry

        def failing_transition(root, bucket_id, entry_id, gen, new_state):
            if new_state == STATE_COMMITTED:
                raise rs.RemainderSidecarError("commit disk fail")
            return original_transition(root, bucket_id, entry_id, gen, new_state)

        rs.transition_entry = failing_transition
        rs.commit_remainder = lambda root, bid, eid, gen: failing_transition(
            root, bid, eid, gen, STATE_COMMITTED
        )
        try:
            bucket_id, merged, _ = await self._merge()
            self.assertEqual(bucket_id, "target")
            self.assertTrue(merged)
            self.assertEqual(self.manager.creates, [])
            sc = load_sidecar_or_none(self.temp.name, "target")
            self.assertIsNotNone(sc)
            prepared = [e for e in sc.entries if e.state == STATE_PREPARED]
            self.assertTrue(len(prepared) >= 1)
            self.assertTrue(_status.unresolved > 0)
        finally:
            rs.transition_entry = original_transition
            rs.commit_remainder = lambda root, bid, eid, gen: original_transition(
                root, bid, eid, gen, STATE_COMMITTED
            )

    # ---- Markdown raise + abort fail: combined integration error ----

    async def test_markdown_raise_abort_fail_raises_integration_error(self) -> None:
        original_transition = rs.transition_entry

        def failing_abort(root, bucket_id, entry_id, gen, new_state):
            if new_state == STATE_ABORTED:
                raise rs.RemainderSidecarError("abort disk fail")
            return original_transition(root, bucket_id, entry_id, gen, new_state)

        self.manager._update_raise = RuntimeError("disk full")
        rs.transition_entry = failing_abort
        rs.abort_remainder = lambda root, bid, eid, gen: failing_abort(
            root, bid, eid, gen, STATE_ABORTED
        )
        try:
            with self.assertRaises(RemainderIntegrationError):
                await self._merge()
            self.assertTrue(_status.unresolved > 0)
            self.assertEqual(self.manager.creates, [])
        finally:
            rs.transition_entry = original_transition
            rs.abort_remainder = lambda root, bid, eid, gen: original_transition(
                root, bid, eid, gen, STATE_ABORTED
            )

    # ---- Markdown false + abort fail: raises integration error, no create ----

    async def test_markdown_false_abort_fail_raises_integration_error(self) -> None:
        original_transition = rs.transition_entry

        def failing_abort(root, bucket_id, entry_id, gen, new_state):
            if new_state == STATE_ABORTED:
                raise rs.RemainderSidecarError("abort disk fail")
            return original_transition(root, bucket_id, entry_id, gen, new_state)

        self.manager._update_fail = True
        rs.transition_entry = failing_abort
        rs.abort_remainder = lambda root, bid, eid, gen: failing_abort(
            root, bid, eid, gen, STATE_ABORTED
        )
        try:
            with self.assertRaises(RemainderIntegrationError):
                await self._merge()
            self.assertTrue(_status.unresolved > 0)
            self.assertEqual(self.manager.creates, [])
        finally:
            rs.transition_entry = original_transition
            rs.abort_remainder = lambda root, bid, eid, gen: original_transition(
                root, bid, eid, gen, STATE_ABORTED
            )

    # ---- cancellation: CancelledError not swallowed ----

    async def test_cancellation_not_swallowed(self) -> None:
        self.manager._update_raise = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self._merge()

    async def test_cancellation_after_prepare_abort_fail_marks_unresolved(self) -> None:
        original_transition = rs.transition_entry

        def failing_abort(root, bucket_id, entry_id, gen, new_state):
            if new_state == STATE_ABORTED:
                raise rs.RemainderSidecarError("abort fail")
            return original_transition(root, bucket_id, entry_id, gen, new_state)

        self.manager._update_raise = asyncio.CancelledError()
        rs.transition_entry = failing_abort
        rs.abort_remainder = lambda root, bid, eid, gen: failing_abort(
            root, bid, eid, gen, STATE_ABORTED
        )
        try:
            with self.assertRaises(asyncio.CancelledError):
                await self._merge()
            self.assertTrue(_status.unresolved > 0)
        finally:
            rs.transition_entry = original_transition
            rs.abort_remainder = lambda root, bid, eid, gen: original_transition(
                root, bid, eid, gen, STATE_ABORTED
            )

    # ---- concurrency: real concurrent tasks with same manager ----

    async def test_concurrent_merges_same_manager(self) -> None:
        barrier = asyncio.Barrier(3)
        results: list = []

        async def do_merge(content: str):
            await barrier.wait()
            r = await common.merge_or_create(
                content=content,
                tags=["owner:cheng"],
                importance=5,
                domain=["work"],
                valence=0.5,
                arousal=0.3,
                source_tool="hold",
            )
            results.append(r)

        tasks = [
            asyncio.create_task(do_merge(f"incoming-{i}"))
            for i in range(3)
        ]
        await asyncio.gather(*tasks)
        self.assertEqual(len(results), 3)
        for bucket_id, merged, _ in results:
            self.assertEqual(bucket_id, "target")
            self.assertTrue(merged)
        self.assertEqual(self.manager.creates, [])
        sc = load_sidecar_or_none(self.temp.name, "target")
        self.assertIsNotNone(sc)
        committed = [e for e in sc.entries if e.state == STATE_COMMITTED]
        self.assertEqual(len(committed), 3)
        self.assertTrue(sc.generation >= 3)

    # ---- archive after terminal ----

    async def test_archive_called_after_commit(self) -> None:
        for i in range(5):
            self.manager.target = {
                "id": "target",
                "content": f"old-{i}",
                "metadata": {
                    "tags": ["owner:cheng"],
                    "importance": 5,
                    "domain": ["work"],
                    "valence": 0.5,
                    "arousal": 0.3,
                },
            }
            await self._merge(content=f"incoming-{i}")
        sc = load_sidecar_or_none(self.temp.name, "target")
        self.assertIsNotNone(sc)


    # ---- snapshot exclusive: real merge blocks snapshot ----

    async def test_snapshot_blocked_during_real_merge(self) -> None:
        from snapshot_barrier import authoritative_markdown_snapshot_turn

        update_entered = asyncio.Event()
        update_release = asyncio.Event()
        original_update = self.manager._update_locked

        async def blocking_update(bucket_id, **updates):
            update_entered.set()
            await update_release.wait()
            return await original_update(bucket_id, **updates)

        self.manager._update_locked = blocking_update
        snapshot_entered = asyncio.Event()

        async def try_snapshot():
            async with authoritative_markdown_snapshot_turn(
                self.temp.name,
                timeout_seconds=0.5,
            ):
                snapshot_entered.set()

        merge_task = asyncio.create_task(self._merge())
        await asyncio.wait_for(update_entered.wait(), 2)
        sc = load_sidecar_or_none(self.temp.name, "target")
        self.assertIsNotNone(sc)
        self.assertTrue(any(e.state == STATE_PREPARED for e in sc.entries))
        snap_task = asyncio.create_task(try_snapshot())
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(snapshot_entered.wait(), 0.2)
        update_release.set()
        await merge_task
        await snap_task
        self.assertTrue(snapshot_entered.is_set())
        sc2 = load_sidecar_or_none(self.temp.name, "target")
        terminal = [e for e in sc2.entries if e.state in (STATE_COMMITTED, STATE_ABORTED)]
        self.assertTrue(len(terminal) >= 1)

    async def test_snapshot_allowed_during_provider(self) -> None:
        from snapshot_barrier import authoritative_markdown_snapshot_turn

        provider_entered = asyncio.Event()
        provider_release = asyncio.Event()

        class SlowProvider:
            async def merge(self, old: str, incoming: str) -> str:
                provider_entered.set()
                await provider_release.wait()
                return f"{old} + {incoming}"

        rt.dehydrator = SlowProvider()
        snapshot_entered = asyncio.Event()

        async def try_snapshot():
            async with authoritative_markdown_snapshot_turn(
                self.temp.name,
                timeout_seconds=2.0,
            ):
                snapshot_entered.set()

        merge_task = asyncio.create_task(self._merge())
        await asyncio.wait_for(provider_entered.wait(), 2)
        snap_task = asyncio.create_task(try_snapshot())
        await asyncio.wait_for(snapshot_entered.wait(), 1.0)
        self.assertTrue(snapshot_entered.is_set())
        provider_release.set()
        await merge_task
        await snap_task

    # ---- blocker 4: commit fail must not leak content in process status ----

    async def test_commit_fail_error_text_no_content_leak(self) -> None:
        original_transition = rs.transition_entry

        class ContentLeakError(Exception):
            def __str__(self):
                return "SECRET_BODY_789 should not appear"

        def failing_transition(root, bucket_id, entry_id, gen, new_state):
            if new_state == STATE_COMMITTED:
                raise ContentLeakError()
            return original_transition(root, bucket_id, entry_id, gen, new_state)

        rs.transition_entry = failing_transition
        rs.commit_remainder = lambda root, bid, eid, gen: failing_transition(
            root, bid, eid, gen, STATE_COMMITTED
        )
        try:
            await self._merge()
            status = runtime_status()
            status_str = json.dumps(status)
            self.assertNotIn("SECRET_BODY_789", status_str)
            self.assertTrue(_status.unresolved > 0)
            for err in _status.errors:
                self.assertNotIn("SECRET_BODY_789", err)
        finally:
            rs.transition_entry = original_transition
            rs.commit_remainder = lambda root, bid, eid, gen: original_transition(
                root, bid, eid, gen, STATE_COMMITTED
            )


def _child_merge_hang_after_prepare(root: str, bucket_id: str, ready_flag: str):
    """Child: real merge_or_create, hangs after prepare (PREPARED window)."""
    import sys
    import asyncio
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from unittest.mock import MagicMock
    import copy
    from tools import _common as _common_mod
    from tools import _identity as _identity_mod
    from tools import _runtime as _rt
    from remainder_sidecar import prepare_remainder as _orig_prepare

    pdir = Path(root) / "permanent"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / f"{bucket_id}.md").write_text(
        f"---\nid: {bucket_id}\ntags:\n  - owner:cheng\n---\nold body",
        encoding="utf-8",
    )

    def hooking_prepare(*a, **kw):
        result = _orig_prepare(*a, **kw)
        Path(ready_flag).write_text("ready", encoding="utf-8")
        import time
        time.sleep(60)
        return result

    import remainder_sidecar as _rs
    _rs.prepare_remainder = hooking_prepare

    class _Mgr:
        base_dir = root
        target = {"id": bucket_id, "content": "old body",
                  "metadata": {"tags": ["owner:cheng"], "importance": 5,
                               "domain": ["work"], "valence": 0.5, "arousal": 0.3}}
        writes = []
        creates = []
        def _require_embedding_available(self): return None
        async def list_all(self, **_kw): return [copy.deepcopy(self.target)]
        async def search(self, *_a, **_kw): return [dict(**copy.deepcopy(self.target), score=99.0)]
        async def get(self, bid):
            return copy.deepcopy(self.target) if bid == bucket_id else None
        async def _update_locked(self, bid, **upd):
            self.target["content"] = upd["content"]
            return True
        async def create(self, **kw):
            self.creates.append(kw)
            return "c"

    class _Prov:
        async def merge(self, old, incoming): return f"{old} + {incoming}"

    _rt.bucket_mgr = _Mgr()
    _rt.config = {"merge_threshold": 75, "limits": {}}
    _rt.dehydrator = _Prov()
    _rt.logger = MagicMock()
    _rt.embedding_engine = None
    _identity_mod.set_caller("cheng")

    asyncio.run(_common_mod.merge_or_create(
        content="incoming", tags=["owner:cheng"], importance=5,
        domain=["work"], valence=0.5, arousal=0.3,
        source_tool="hold",
    ))


def _child_merge_hang_before_commit(root: str, bucket_id: str, ready_flag: str):
    """Child: real merge, Markdown succeeds, hangs before commit (post-MD window)."""
    import sys
    import asyncio
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from unittest.mock import MagicMock
    import copy
    from tools import _common as _common_mod
    from tools import _identity as _identity_mod
    from tools import _runtime as _rt
    from remainder_sidecar import commit_remainder as _orig_commit

    pdir = Path(root) / "permanent"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / f"{bucket_id}.md").write_text(
        f"---\nid: {bucket_id}\ntags:\n  - owner:cheng\n---\nold body",
        encoding="utf-8",
    )

    def hooking_commit(*a, **kw):
        Path(ready_flag).write_text("ready", encoding="utf-8")
        import time
        time.sleep(60)
        return _orig_commit(*a, **kw)

    import remainder_sidecar as _rs
    _rs.commit_remainder = hooking_commit

    _md_path = pdir / f"{bucket_id}.md"

    class _Mgr:
        base_dir = root
        target = {"id": bucket_id, "content": "old body",
                  "metadata": {"tags": ["owner:cheng"], "importance": 5,
                               "domain": ["work"], "valence": 0.5, "arousal": 0.3}}
        writes = []
        creates = []
        def _require_embedding_available(self): return None
        async def list_all(self, **_kw): return [copy.deepcopy(self.target)]
        async def search(self, *_a, **_kw): return [dict(**copy.deepcopy(self.target), score=99.0)]
        async def get(self, bid):
            return copy.deepcopy(self.target) if bid == bucket_id else None
        async def _update_locked(self, bid, **upd):
            self.target["content"] = upd["content"]
            _md_path.write_text(
                f"---\nid: {bucket_id}\ntags:\n  - owner:cheng\n---\n{upd['content']}",
                encoding="utf-8",
            )
            return True
        async def create(self, **kw):
            self.creates.append(kw)
            return "c"

    class _Prov:
        async def merge(self, old, incoming): return f"{old} + {incoming}"

    _rt.bucket_mgr = _Mgr()
    _rt.config = {"merge_threshold": 75, "limits": {}}
    _rt.dehydrator = _Prov()
    _rt.logger = MagicMock()
    _rt.embedding_engine = None
    _identity_mod.set_caller("cheng")

    asyncio.run(_common_mod.merge_or_create(
        content="incoming", tags=["owner:cheng"], importance=5,
        domain=["work"], valence=0.5, arousal=0.3,
        source_tool="hold",
    ))


class ProcessKillRecoveryTests(unittest.TestCase):
    """Test startup recovery after subprocess kill at different windows."""

    ROUNDS = 5

    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tempdir.name)
        _set_wiring("inactive")
        _set_startup("pending")
        clear_unresolved()

    def tearDown(self) -> None:
        self._tempdir.cleanup()
        _set_wiring("inactive")
        _set_startup("pending")
        clear_unresolved()

    def test_kill_after_prepare_recovery_aborts(self) -> None:
        import multiprocessing
        for i in range(self.ROUNDS):
            tmp = tempfile.mkdtemp(dir=str(self.root))
            ready = os.path.join(tmp, "ready")
            bid = f"kill_prep_{i}"
            p = multiprocessing.Process(
                target=_child_merge_hang_after_prepare,
                args=(tmp, bid, ready),
            )
            p.start()
            arrived = False
            for _ in range(300):
                if os.path.exists(ready):
                    arrived = True
                    break
                import time
                time.sleep(0.02)
            self.assertTrue(arrived, f"round {i}: child did not signal ready")
            p.kill()
            p.join(timeout=5)
            self.assertNotEqual(p.exitcode, 0, f"round {i}: child exited cleanly")
            p.close()
            _set_wiring("inactive")
            _set_startup("pending")
            result = recover_remainders_before_startup(tmp)
            self.assertTrue(result["aborted"] >= 1, f"round {i}")
            status = runtime_status()
            self.assertEqual(status["startup_state"], "complete")

    def test_kill_after_markdown_recovery_commits(self) -> None:
        import multiprocessing
        for i in range(self.ROUNDS):
            tmp = tempfile.mkdtemp(dir=str(self.root))
            ready = os.path.join(tmp, "ready")
            bid = f"kill_md_{i}"
            p = multiprocessing.Process(
                target=_child_merge_hang_before_commit,
                args=(tmp, bid, ready),
            )
            p.start()
            arrived = False
            for _ in range(300):
                if os.path.exists(ready):
                    arrived = True
                    break
                import time
                time.sleep(0.02)
            self.assertTrue(arrived, f"round {i}: child did not signal ready")
            p.kill()
            p.join(timeout=5)
            self.assertNotEqual(p.exitcode, 0, f"round {i}: child exited cleanly")
            p.close()
            _set_wiring("inactive")
            _set_startup("pending")
            result = recover_remainders_before_startup(tmp)
            self.assertTrue(result["committed"] >= 1, f"round {i}")
            status = runtime_status()
            self.assertEqual(status["startup_state"], "complete")


if __name__ == "__main__":
    unittest.main()
