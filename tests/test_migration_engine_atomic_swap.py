"""Migration compatibility and E-MIG-01 boundary regression tests.

The old fixed `.migrating` swap protocol is intentionally retired.  Whole-DB
publication now requires a caller-owned E-MIG-01 reservation and an internally
named private shadow generation; legacy helpers remain non-mutating only.
"""
import ast
import os
from pathlib import Path

import pytest

from migration_engine import (
    MigrationConfig,
    _run_migration,
    _write_checkpoint,
    backup_db_once,
    checkpoint_path_for,
    read_status,
    reserve_migration,
    reset_stale_migration_state,
    staging_db_path_for,
    status_path_for,
    target_signature,
)
from web import embedding as web_embedding
import utils


ROOT = Path(__file__).resolve().parents[1]


class FakeTargetEngine:
    def __init__(self, db_path):
        self.db_path = db_path
        self.calls = []
        self.meta = {}

    async def generate_and_store(self, bucket_id, content):
        self.calls.append(bucket_id)
        with open(self.db_path, "a", encoding="utf-8") as f:
            f.write(f"{bucket_id}\n")
        return True

    def _write_meta(self, key, value):
        self.meta[key] = value


@pytest.mark.asyncio
async def test_direct_worker_call_requires_transaction_reservation(tmp_path):
    live_db = str(tmp_path / "embeddings.db")
    Path(live_db).write_text("OLD-LIVE-CONTENT\n", encoding="utf-8")
    cfg = MigrationConfig(
        buckets_dir=str(tmp_path / "buckets"),
        db_path=live_db,
        target_backend="api",
        target_model="test-model",
        target_dim=8,
        target_engine=FakeTargetEngine(staging_db_path_for(live_db)),
        fetch_buckets=lambda: None,
    )

    with pytest.raises(TypeError, match="reservation"):
        await _run_migration(cfg)

    assert Path(live_db).read_text(encoding="utf-8") == "OLD-LIVE-CONTENT\n"


@pytest.mark.asyncio
async def test_fixed_legacy_staging_path_is_never_publishable(tmp_path):
    buckets_dir = str(tmp_path / "buckets")
    os.makedirs(buckets_dir, exist_ok=True)
    live_db = str(tmp_path / "embeddings.db")
    Path(live_db).write_text("OLD-LIVE-CONTENT\n", encoding="utf-8")
    completions = []

    cfg = MigrationConfig(
        buckets_dir=buckets_dir,
        db_path=live_db,
        target_backend="api",
        target_model="test-model",
        target_dim=8,
        target_engine=FakeTargetEngine(staging_db_path_for(live_db)),
        fetch_buckets=lambda: None,
    )
    reservation = reserve_migration(live_db)
    assert reservation is not None
    try:
        await _run_migration(
            cfg,
            reservation=reservation,
            on_complete=completions.append,
        )
    finally:
        reservation.close()

    assert Path(live_db).read_text(encoding="utf-8") == "OLD-LIVE-CONTENT\n"
    status = read_status(status_path_for(buckets_dir))
    assert status["phase"] == "failed"
    assert "private shadow" in status["error"]
    assert completions == [False]


def test_backup_compatibility_helper_does_not_create_fixed_backup(tmp_path):
    live_db = tmp_path / "embeddings.db"
    live_db.write_bytes(b"live")

    reported = backup_db_once(str(live_db))

    assert reported == str(live_db) + ".backup"
    assert not Path(reported).exists()


def test_embedding_yaml_persistence_failure_propagates(monkeypatch):
    def fail_persist(_mutator):
        raise OSError("simulated yaml write failure")

    monkeypatch.setattr(utils, "atomic_update_config_yaml", fail_persist)

    with pytest.raises(OSError, match="simulated yaml write failure"):
        web_embedding._persist_embedding_yaml({"model": "test-model"})


def test_embedding_package_mode_uses_parent_migration_module():
    tree = ast.parse(
        (ROOT / "src" / "web" / "embedding.py").read_text(encoding="utf-8")
    )
    migration_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "migration_engine"
    ]

    assert not [node for node in migration_imports if node.level == 1]
    assert len([node for node in migration_imports if node.level == 2]) >= 3


def test_dashboard_treats_publish_failure_as_visible_terminal_state():
    dashboard = (ROOT / "frontend" / "dashboard.html").read_text(encoding="utf-8")

    assert "s.phase === 'publish_failed'" in dashboard
    assert "['completed', 'failed', 'publish_failed'].includes(d.status.phase)" in dashboard


@pytest.mark.asyncio
async def test_reset_stale_migration_state_wipes_mismatched_checkpoint_and_staging(tmp_path):
    buckets_dir = str(tmp_path / "buckets")
    os.makedirs(buckets_dir, exist_ok=True)
    live_db = str(tmp_path / "embeddings.db")
    staged_path = staging_db_path_for(live_db)
    with open(staged_path, "w", encoding="utf-8") as f:
        f.write("stale vectors from a different model\n")

    ckpt_path = checkpoint_path_for(buckets_dir)
    _write_checkpoint(ckpt_path, {"b1"}, target_signature("api", "old-model", 4))

    # 换目标（不同 model/dim）→ 旧 checkpoint 和 staging db 都必须被清掉，
    # 否则断点续传会把「old-model 的 b1 已完成」误当成「new-model 的 b1 已完成」。
    reset_stale_migration_state(buckets_dir, live_db, target_signature("api", "new-model", 8))

    assert not os.path.exists(ckpt_path)
    assert not os.path.exists(staged_path)


@pytest.mark.asyncio
async def test_reset_stale_migration_state_keeps_matching_checkpoint(tmp_path):
    buckets_dir = str(tmp_path / "buckets")
    os.makedirs(buckets_dir, exist_ok=True)
    live_db = str(tmp_path / "embeddings.db")
    staged_path = staging_db_path_for(live_db)
    with open(staged_path, "w", encoding="utf-8") as f:
        f.write("in-progress vectors for the same target\n")

    ckpt_path = checkpoint_path_for(buckets_dir)
    sig = target_signature("api", "same-model", 8)
    _write_checkpoint(ckpt_path, {"b1"}, sig)

    reset_stale_migration_state(buckets_dir, live_db, sig)

    assert os.path.exists(ckpt_path), "目标一致时不该清掉正在续传的 checkpoint"
    assert os.path.exists(staged_path), "目标一致时不该清掉正在续传的 staging db"
