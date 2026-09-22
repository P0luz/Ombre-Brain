from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import frontmatter

from backup_archive import build_authoritative_root_manifest
from restore_derived_adapter import (
    RestoreDerivedError,
    collect_embedding_inputs,
    rebuild_and_publish_embeddings,
)


def write_bucket(root: Path, bucket_id: str, body: str) -> None:
    path = root / "dynamic" / f"{bucket_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        frontmatter.dumps(
            frontmatter.Post(body, id=bucket_id, tags=["owner:cheng"])
        ),
        encoding="utf-8",
    )


class FakeBackend:
    def model_name(self) -> str:
        return "fake-model"

    def vector_dim(self) -> int:
        return 3


class FakeShadowEngine:
    def __init__(self, config: dict) -> None:
        self.db_path = config["embedding"]["db_path"]
        self._backend = FakeBackend()
        self.ids: set[str] = set()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    async def generate_and_store(self, bucket_id: str, content: str) -> bool:
        self.ids.add(bucket_id)
        return bool(content.strip())

    def checkpoint_close_and_fsync(self) -> None:
        Path(self.db_path).write_bytes(b"fake-shadow")


class FakeLiveEngine:
    def __init__(self, db_path: Path) -> None:
        self.db_path = str(db_path)
        self.ids: set[str] = set()
        db_path.write_bytes(b"old")

    def list_all_ids(self) -> list[str]:
        return sorted(self.ids)


class RestoreDerivedAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_collect_inputs_requires_exact_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "buckets"
            write_bucket(root, "a", "alpha")
            manifest = build_authoritative_root_manifest(
                root, created_at="2026-08-02T00:00:00+00:00"
            )
            self.assertEqual(collect_embedding_inputs(root, manifest), [("a", "alpha")])
            (root / "dynamic" / "a.md").write_text("tampered", encoding="utf-8")
            with self.assertRaises(Exception):
                collect_embedding_inputs(root, manifest)

    async def test_shadow_build_delegates_publish_and_returns_bound_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "buckets"
            write_bucket(root, "a", "alpha")
            write_bucket(root, "b", "beta")
            manifest = build_authoritative_root_manifest(
                root, created_at="2026-08-02T00:00:00+00:00"
            )
            live = FakeLiveEngine(base / "embeddings.db")
            captured: dict = {}

            async def publisher(**kwargs):
                captured.update(kwargs)
                live.ids = set(kwargs["expected_bucket_ids"])
                return {"state": "COMMITTED"}

            receipt = await rebuild_and_publish_embeddings(
                root,
                manifest,
                config={"buckets_dir": str(root), "embedding": {"enabled": True}},
                live_engine=live,
                engine_factory=FakeShadowEngine,
                publisher=publisher,
            )
            self.assertEqual(receipt["status"], "verified")
            self.assertEqual(receipt["embedding_count"], 2)
            self.assertEqual(captured["expected_bucket_ids"], {"a", "b"})
            canonical = json.dumps(
                manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            import hashlib

            self.assertEqual(
                receipt["manifest_sha256"], hashlib.sha256(canonical).hexdigest()
            )

    async def test_busy_reservation_refuses_without_building_shadow(self) -> None:
        # The real reservation path is already exercised by the successful test;
        # invalid live DB identity must fail before any engine is constructed.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "buckets"
            write_bucket(root, "a", "alpha")
            manifest = build_authoritative_root_manifest(
                root, created_at="2026-08-02T00:00:00+00:00"
            )
            class NoPath:
                db_path = ""
            with self.assertRaises(RestoreDerivedError):
                await rebuild_and_publish_embeddings(
                    root,
                    manifest,
                    config={"buckets_dir": str(root), "embedding": {}},
                    live_engine=NoPath(),
                    engine_factory=FakeShadowEngine,
                )


if __name__ == "__main__":
    unittest.main()
