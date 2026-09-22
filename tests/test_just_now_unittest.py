from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tools.just_now.store import (
    JustNowConflict,
    JustNowCorruptStore,
    JustNowLedger,
    JustNowLimits,
    JustNowValidationError,
)
from tools import _identity
import tools._runtime as rt
from tools.just_now import core as just_now_core


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value += timedelta(**kwargs)


class JustNowV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "just-now.json"
        self.clock = Clock()
        self.limits = JustNowLimits(
            ttl_seconds=60,
            max_items_per_stream=3,
            max_content_chars=120,
            max_read_items=3,
        )
        self.event_counter = 0
        self.ledger = JustNowLedger(
            self.path,
            limits=self.limits,
            now_fn=self.clock.now,
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def append(self, content: str, **kwargs):
        defaults = {
            "caller": "cheng",
            "source": "codex",
            "task_id": "task-a",
            "role": "user",
            "content": content,
            "occurred_at": self.clock.now(),
        }
        defaults.update(kwargs)
        if not defaults.get("event_id") and not defaults.get("source_cursor"):
            self.event_counter += 1
            defaults["event_id"] = f"auto-{self.event_counter}"
        return self.ledger.append(**defaults)

    def read(self, **kwargs):
        defaults = {
            "caller": "cheng",
            "source": "codex",
            "task_id": "task-a",
        }
        defaults.update(kwargs)
        return self.ledger.read(**defaults)

    def test_append_and_read_preserve_chronological_order(self):
        first = self.append("第一句", event_id="e1", source_cursor="10")
        self.clock.advance(seconds=1)
        second = self.append(
            "第二句",
            role="assistant",
            event_id="e2",
            source_cursor="11",
        )
        result = self.read()

        self.assertEqual([item["content"] for item in result["items"]], ["第一句", "第二句"])
        self.assertEqual([item["role"] for item in result["items"]], ["user", "assistant"])
        self.assertEqual(result["next_after_seq"], second["seq"])
        self.assertLess(first["seq"], second["seq"])

    def test_initial_limited_read_returns_most_recent_items(self):
        for index in range(3):
            self.append(f"message-{index}", event_id=f"e{index}")
            self.clock.advance(seconds=1)
        result = self.read(limit=2)
        self.assertEqual(
            [item["content"] for item in result["items"]],
            ["message-1", "message-2"],
        )

    def test_caller_source_and_task_are_strictly_isolated(self):
        self.append("澄 task-a")
        self.append("澄 task-b", task_id="task-b")
        self.append("怀音 task-a", caller="huaiyin")
        self.append("AISay task-a", source="aisay")

        self.assertEqual([item["content"] for item in self.read()["items"]], ["澄 task-a"])
        self.assertEqual(
            [item["content"] for item in self.read(task_id="task-b")["items"]],
            ["澄 task-b"],
        )
        self.assertEqual(
            [item["content"] for item in self.read(caller="huaiyin")["items"]],
            ["怀音 task-a"],
        )
        self.assertEqual(
            [item["content"] for item in self.read(source="aisay")["items"]],
            ["AISay task-a"],
        )

    def test_expired_messages_are_removed_from_result_and_disk(self):
        self.append("短期秘密", event_id="e1")
        self.clock.advance(seconds=61)

        self.assertEqual(self.read()["items"], [])
        disk = self.path.read_text(encoding="utf-8")
        self.assertNotIn("短期秘密", disk)

    def test_cursor_ack_persists_then_expires(self):
        ack = self.ledger.ack_cursor(
            caller="cheng",
            source="aisay",
            task_id="room-1",
            cursor="msg-42",
        )
        self.assertEqual(ack["last_cursor"], "msg-42")

        reloaded = JustNowLedger(
            self.path,
            limits=self.limits,
            now_fn=self.clock.now,
        )
        self.assertEqual(
            reloaded.read(
                caller="cheng",
                source="aisay",
                task_id="room-1",
            )["last_cursor"],
            "msg-42",
        )

        self.clock.advance(seconds=61)
        self.assertEqual(
            reloaded.read(
                caller="cheng",
                source="aisay",
                task_id="room-1",
            )["last_cursor"],
            "",
        )

    def test_messages_persist_across_ledger_restart(self):
        self.append("重启前", event_id="e1")
        reloaded = JustNowLedger(
            self.path,
            limits=self.limits,
            now_fn=self.clock.now,
        )
        result = reloaded.read(
            caller="cheng",
            source="codex",
            task_id="task-a",
        )
        self.assertEqual([item["content"] for item in result["items"]], ["重启前"])

    def test_same_cursor_is_idempotent_but_conflict_is_visible(self):
        first = self.append("同一句", event_id="e1", source_cursor="20")
        self.clock.advance(seconds=5)
        duplicate = self.append("同一句", event_id="e2", source_cursor="20")
        self.assertTrue(duplicate["deduped"])
        self.assertEqual(duplicate["seq"], first["seq"])
        self.assertEqual(len(self.read()["items"]), 1)

        with self.assertRaises(JustNowConflict):
            self.append("内容变了", event_id="e3", source_cursor="20")

    def test_stream_capacity_evicts_oldest(self):
        for index in range(4):
            self.append(
                f"message-{index}",
                event_id=f"e{index}",
                source_cursor=str(index),
            )
            self.clock.advance(seconds=1)
        self.assertEqual(
            [item["content"] for item in self.read()["items"]],
            ["message-1", "message-2", "message-3"],
        )

    def test_after_seq_returns_only_newer_items(self):
        first = self.append("one", event_id="e1")
        self.clock.advance(seconds=1)
        self.append("two", event_id="e2")
        self.clock.advance(seconds=1)
        self.append("three", event_id="e3")
        result = self.read(after_seq=first["seq"], limit=1)
        self.assertEqual([item["content"] for item in result["items"]], ["two"])

    def test_old_event_is_not_resurrected_when_recorded_late(self):
        old_time = self.clock.now() - timedelta(seconds=61)
        result = self.append(
            "已经过期",
            occurred_at=old_time,
            event_id="old",
        )
        self.assertTrue(result["expired"])
        self.assertFalse(result["stored"])
        self.assertEqual(self.read()["items"], [])

    def test_invalid_role_caller_content_and_future_time_are_rejected(self):
        with self.assertRaises(JustNowValidationError):
            self.append("x", role="tool")
        with self.assertRaises(JustNowValidationError):
            self.append("x", caller="unknown")
        with self.assertRaises(JustNowValidationError):
            self.append(" ")
        with self.assertRaises(JustNowValidationError):
            self.append("x" * 121)
        with self.assertRaises(JustNowValidationError):
            self.append(
                "future",
                occurred_at=self.clock.now() + timedelta(minutes=6),
            )
        with self.assertRaises(JustNowValidationError):
            self.ledger.append(
                caller="cheng",
                source="codex",
                task_id="task-a",
                role="user",
                content="missing occurrence",
                event_id="e1",
            )
        with self.assertRaises(JustNowValidationError):
            self.ledger.append(
                caller="cheng",
                source="codex",
                task_id="task-a",
                role="user",
                content="missing idempotency key",
                occurred_at=self.clock.now(),
            )

    def test_corrupt_store_is_not_silently_overwritten(self):
        self.path.write_text("{broken", encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaises(JustNowCorruptStore):
            self.append("不能覆盖坏文件")
        self.assertEqual(self.path.read_bytes(), before)

    def test_tampered_stream_identity_is_rejected(self):
        self.append("owner-safe", event_id="e1")
        state = json.loads(self.path.read_text(encoding="utf-8"))
        stream = next(iter(state["streams"].values()))
        stream["caller"] = "huaiyin"
        self.path.write_text(
            json.dumps(state, ensure_ascii=False),
            encoding="utf-8",
        )
        with self.assertRaises(JustNowCorruptStore):
            self.read()

    def test_atomic_save_leaves_no_temp_files(self):
        self.append("atomic", event_id="e1")
        parsed = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(parsed["schema"], "ombre-just-now-v1")
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_clear_stream_only_clears_exact_stream(self):
        self.append("task-a", event_id="e1")
        self.append("task-b", task_id="task-b", event_id="e2")
        removed = self.ledger.clear_stream(
            caller="cheng",
            source="codex",
            task_id="task-a",
        )
        self.assertEqual(removed, 1)
        self.assertEqual(self.read()["items"], [])
        self.assertEqual(
            [item["content"] for item in self.read(task_id="task-b")["items"]],
            ["task-b"],
        )

    def test_single_instance_concurrent_appends_do_not_lose_writes(self):
        concurrent_path = Path(self.tempdir.name) / "concurrent.json"
        limits = JustNowLimits(
            ttl_seconds=60,
            max_items_per_stream=20,
            max_content_chars=120,
            max_read_items=20,
        )
        ledger = JustNowLedger(
            concurrent_path,
            limits=limits,
            now_fn=self.clock.now,
        )

        def append_one(index):
            return ledger.append(
                caller="cheng",
                source="runner",
                task_id="runner-1",
                role="assistant",
                content=f"event-{index}",
                occurred_at=self.clock.now(),
                event_id=f"event-{index}",
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(append_one, range(20)))
        result = ledger.read(
            caller="cheng",
            source="runner",
            task_id="runner-1",
        )
        self.assertEqual(len(result["items"]), 20)
        self.assertEqual(
            {item["content"] for item in result["items"]},
            {f"event-{index}" for index in range(20)},
        )

    def test_naive_timestamp_and_unbounded_read_are_rejected(self):
        with self.assertRaises(JustNowValidationError):
            self.append("naive", occurred_at=datetime(2026, 7, 29, 12, 0))
        with self.assertRaises(JustNowValidationError):
            self.read(limit=4)


class JustNowDispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "dispatch.json"
        self.old_config = rt.config
        self.old_logger = rt.logger
        self.old_mark_op = rt.mark_op
        rt.config = {
            "just_now": {
                "path": str(self.path),
                "ttl_seconds": 3600,
                "max_items_per_stream": 10,
                "max_content_chars": 500,
                "max_read_items": 10,
            }
        }
        rt.logger = MagicMock()
        rt.mark_op = None
        just_now_core._ledger_cache = None
        _identity.set_caller("cheng")

    def tearDown(self):
        _identity.set_caller("")
        just_now_core._ledger_cache = None
        rt.config = self.old_config
        rt.logger = self.old_logger
        rt.mark_op = self.old_mark_op
        self.tempdir.cleanup()

    async def append(self, content="刚才那句", **kwargs):
        now = datetime.now(timezone.utc).isoformat()
        defaults = {
            "action": "append",
            "source": "codex",
            "task_id": "task-a",
            "role": "user",
            "content": content,
            "occurred_at": now,
            "event_id": "event-a",
        }
        defaults.update(kwargs)
        return await just_now_core.dispatch(**defaults)

    async def test_missing_caller_refuses_without_creating_store(self):
        _identity.set_caller("")
        text = await just_now_core.dispatch(
            action="read",
            source="codex",
            task_id="task-a",
        )
        self.assertIn("没有可识别的 caller", text)
        self.assertFalse(self.path.exists())

    async def test_append_read_ack_and_confirmed_clear(self):
        appended = json.loads(await self.append())
        self.assertTrue(appended["stored"])
        self.assertFalse(appended["policy"]["long_term_memory"])

        read = json.loads(
            await just_now_core.dispatch(
                action="read",
                source="codex",
                task_id="task-a",
            )
        )
        self.assertEqual([item["content"] for item in read["items"]], ["刚才那句"])

        ack = json.loads(
            await just_now_core.dispatch(
                action="ack",
                source="codex",
                task_id="task-a",
                cursor="cursor-7",
            )
        )
        self.assertEqual(ack["last_cursor"], "cursor-7")

        refused = await just_now_core.dispatch(
            action="clear",
            source="codex",
            task_id="task-a",
        )
        self.assertIn("confirm=true", refused)
        still_there = json.loads(
            await just_now_core.dispatch(
                action="read",
                source="codex",
                task_id="task-a",
            )
        )
        self.assertEqual(len(still_there["items"]), 1)

        cleared = json.loads(
            await just_now_core.dispatch(
                action="clear",
                source="codex",
                task_id="task-a",
                confirm=True,
            )
        )
        self.assertEqual(cleared["removed"], 1)

    async def test_dispatch_uses_context_caller_for_isolation(self):
        await self.append()
        _identity.set_caller("huaiyin")
        read = json.loads(
            await just_now_core.dispatch(
                action="read",
                source="codex",
                task_id="task-a",
            )
        )
        self.assertEqual(read["caller"], "huaiyin")
        self.assertEqual(read["items"], [])

    async def test_corrupt_store_is_reported_without_overwrite(self):
        self.path.write_text("{broken", encoding="utf-8")
        before = self.path.read_bytes()
        text = await just_now_core.dispatch(
            action="read",
            source="codex",
            task_id="task-a",
        )
        self.assertIn("账本损坏", text)
        self.assertEqual(self.path.read_bytes(), before)
        rt.logger.error.assert_called()


if __name__ == "__main__":
    unittest.main()
