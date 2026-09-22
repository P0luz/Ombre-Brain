"""Focused standard-library verification of M-04 Markdown writer-gate wiring."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bucket_manager import BucketManager  # noqa: E402
import import_transaction  # noqa: E402
from snapshot_barrier import (  # noqa: E402
    authoritative_markdown_snapshot_turn,
    markdown_writer_turn,
)
import write_memory  # noqa: E402


class _EmbeddingProbe:
    enabled = True

    def __init__(self, root: Path, lock_root: Path) -> None:
        self.root = root
        self.lock_root = lock_root
        self.outside_writer_gate = False

    async def generate_and_store(self, *_args) -> bool:
        async with authoritative_markdown_snapshot_turn(
            self.root, lock_root=self.lock_root, timeout_seconds=0.2
        ):
            self.outside_writer_gate = True
        return True

    async def search_similar(self, *_args, **_kwargs):
        return []

    def delete_embedding(self, *_args) -> None:
        return None


class WriterGateWiringTests(unittest.IsolatedAsyncioTestCase):
    def _manager(self, root: Path, locks: Path) -> tuple[BucketManager, _EmbeddingProbe]:
        probe = _EmbeddingProbe(root, locks)
        manager = BucketManager(
            {
                "buckets_dir": str(root),
                "matching": {"fuzzy_threshold": 50, "max_results": 5},
                "wikilink": {"enabled": False},
                "limits": {},
                "scoring_weights": {},
            },
            embedding_engine=probe,
        )
        return manager, probe

    async def test_exclusive_blocks_public_create_and_embedding_runs_after_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            root.mkdir()
            locks = Path(temp) / "locks"
            manager, embedding = self._manager(root, locks)

            @asynccontextmanager
            async def gate(path, **_kwargs):
                async with markdown_writer_turn(path, lock_root=locks):
                    yield

            with patch("bucket_manager.markdown_writer_turn", gate):
                async with authoritative_markdown_snapshot_turn(root, lock_root=locks):
                    task = asyncio.create_task(
                        manager.create_internal("body", tags=["owner:cheng"], domain=["work"])
                    )
                    await asyncio.sleep(0.08)
                    self.assertFalse(task.done())
                bucket_id = await asyncio.wait_for(task, 1)
            self.assertTrue(bucket_id)
            self.assertTrue(embedding.outside_writer_gate)

    async def test_public_mutation_paths_use_one_outer_gate(self) -> None:
        source = (ROOT / "src" / "bucket_manager.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count("async with markdown_writer_turn(self.base_dir):"), 6)
        for marker in (
            "async def create(", "async def update(", "async def delete(",
            "async def touch(", "async def set_anchor(", "async def archive(",
        ):
            self.assertIn(marker, source)

    async def test_recovery_acquires_sync_gate_and_cli_uses_same_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            root.mkdir()
            (root / ".import-transactions").mkdir()
            observed = []

            @contextmanager
            def gate(path, **_kwargs):
                observed.append(Path(path).resolve())
                yield

            with patch.object(import_transaction, "markdown_writer_turn_sync", gate):
                self.assertEqual(import_transaction.recover_import_transactions(str(root)), [])
            self.assertEqual(observed, [root.resolve()])

            dynamic = root / "dynamic"
            old_vault = write_memory.VAULT_DIR
            try:
                write_memory.VAULT_DIR = str(dynamic)
                with patch.object(write_memory, "markdown_writer_turn_sync", gate):
                    with patch("builtins.print"):
                        bucket_id = write_memory.write_memory(
                            "manual", "body", ["work"], ["owner:cheng"], "cheng"
                        )
                self.assertTrue((dynamic / f"{bucket_id}.md").is_file())
            finally:
                write_memory.VAULT_DIR = old_vault
            self.assertEqual(observed, [root.resolve(), root.resolve()])

    async def test_real_m03_rollback_reacquires_gate_after_an_exclusive_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            root.mkdir()
            txid = "c" * 32
            txdir = root / ".import-transactions" / txid
            manifest = {
                "schema_version": 1,
                "txid": txid,
                "state": "STAGING",
                "job_owner": "cheng",
                "entries": [],
            }

            async with authoritative_markdown_snapshot_turn(root):
                txdir.mkdir(parents=True)
                (txdir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                recovery = asyncio.create_task(
                    asyncio.to_thread(import_transaction.recover_import_transactions, str(root))
                )
                await asyncio.sleep(0.08)
                self.assertFalse(recovery.done())
            self.assertEqual(await asyncio.wait_for(recovery, 1), [txid])
            recovered_manifest = json.loads((txdir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(recovered_manifest["state"], "ROLLED_BACK")

    async def test_inventory_has_no_ungated_direct_markdown_writer(self) -> None:
        sources = {
            "tools/common": (ROOT / "src" / "tools" / "_common.py").read_text(encoding="utf-8"),
            "m03": (ROOT / "src" / "import_transaction.py").read_text(encoding="utf-8"),
            "web": (ROOT / "src" / "web" / "buckets.py").read_text(encoding="utf-8"),
            "reclassify": (ROOT / "src" / "reclassify_api.py").read_text(encoding="utf-8"),
            "legacy cli": (ROOT / "src" / "write_memory.py").read_text(encoding="utf-8"),
        }
        for name, source in sources.items():
            self.assertIn("markdown_writer_turn", source, name)
        self.assertIn("markdown_writer_turn_sync", sources["m03"])
        self.assertIn("markdown_writer_turn_sync", sources["legacy cli"])
        self.assertIn("await stack.enter_async_context(markdown_writer_turn", sources["m03"])
        self.assertIn("async with markdown_writer_turn", sources["web"])


if __name__ == "__main__":
    unittest.main()
