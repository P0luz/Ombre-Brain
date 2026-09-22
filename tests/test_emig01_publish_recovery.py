"""Fault-injected E-MIG-01 publication and startup recovery tests."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
from pathlib import Path
import sys

import pytest
import yaml

import embedding_publish as ep
from embedding_publish import (
    PublishRecoveryError,
    PublishRollbackError,
    get_publish_health,
    inspect_generation,
    publish_shadow_generation,
    recover_pending_publish,
)

_HELPERS = Path(__file__).resolve().parent / "helpers"
if str(_HELPERS) not in sys.path:
    sys.path.insert(0, str(_HELPERS))

from emig01_process_worker import crash_publish_worker, create_embedding_db


def _pair(tmp_path: Path):
    db = tmp_path / "embeddings.db"
    create_embedding_db(db, model="old-model", rows={"old": [0.0, 1.0]})
    old_hash = inspect_generation(db)["sha256"]
    shadow = ep.create_shadow_path(db)
    create_embedding_db(shadow, model="new-model", rows={"new": [1.0, 0.0]})
    return db, shadow, old_hash


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.asyncio
async def test_successful_publish_retains_exact_old_generation(tmp_path):
    db, shadow, old_hash = _pair(tmp_path)
    events: list[str] = []
    manifest = await publish_shadow_generation(
        db_path=db,
        shadow_path=shadow,
        expected_model="new-model",
        expected_dim=2,
        expected_count=1,
        expected_bucket_ids={"new"},
        runtime_close=lambda: events.append("close"),
        runtime_apply=lambda: events.append("apply"),
        runtime_open_probe=lambda: events.append("probe"),
    )
    assert manifest["state"] == "COMMITTED"
    assert inspect_generation(db)["model"] == "new-model"
    old = db.parent / ".embedding-generations" / f"{manifest['txid']}.old.db"
    assert _sha(old) == old_hash
    assert events == ["close", "apply", "probe"]
    assert get_publish_health()["ok"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["config", "runtime_apply", "runtime_probe"])
async def test_post_exchange_failure_restores_db_config_and_runtime(
    tmp_path,
    failure,
    monkeypatch,
):
    db, shadow, old_hash = _pair(tmp_path)
    config = tmp_path / "config.yaml"
    old_config = {"embedding": {"model": "old-model", "dim": 2}}
    config.write_text(yaml.safe_dump(old_config), encoding="utf-8")
    runtime = {"generation": "old"}
    if failure == "config":
        original_patch = ep._apply_embedding_patch
        calls = 0

        def fail_candidate_only(path, patch):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("config persist injected")
            return original_patch(path, patch)

        monkeypatch.setattr(ep, "_apply_embedding_patch", fail_candidate_only)

    def apply():
        runtime["generation"] = "new"
        if failure == "runtime_apply":
            raise RuntimeError("component apply injected")

    def probe():
        if failure == "runtime_probe" and runtime["generation"] == "new":
            raise RuntimeError("component probe injected")

    with pytest.raises((OSError, RuntimeError)):
        await publish_shadow_generation(
            db_path=db,
            shadow_path=shadow,
            expected_model="new-model",
            expected_dim=2,
            expected_count=1,
            config_path=config,
            config_forward_patch={"model": "new-model", "dim": 2},
            runtime_close=lambda: None,
            runtime_apply=apply,
            runtime_restore=lambda: runtime.update(generation="old"),
            runtime_open_probe=probe,
        )
    assert _sha(db) == old_hash
    assert yaml.safe_load(config.read_text(encoding="utf-8")) == old_config
    assert runtime["generation"] == "old"
    manifest = json.loads(
        (tmp_path / ".embedding-publish.json").read_text(encoding="utf-8")
    )
    assert manifest["state"] == "ABORTED_RESTORED"


@pytest.mark.asyncio
async def test_rollback_failure_is_combined_and_health_fails_closed(tmp_path):
    db, shadow, _old_hash = _pair(tmp_path)

    def fail_apply():
        raise RuntimeError("original apply failure")

    def fail_restore():
        raise OSError("rollback component failure")

    with pytest.raises(PublishRollbackError) as caught:
        await publish_shadow_generation(
            db_path=db,
            shadow_path=shadow,
            expected_model="new-model",
            expected_dim=2,
            expected_count=1,
            runtime_apply=fail_apply,
            runtime_restore=fail_restore,
        )
    text = str(caught.value)
    assert "original apply failure" in text
    assert "rollback component failure" in text
    health = get_publish_health()
    assert health["ok"] is False
    assert health["state"] == "rollback_failed"


@pytest.mark.asyncio
async def test_unknown_current_hash_during_rollback_never_guesses(tmp_path):
    db, shadow, _old_hash = _pair(tmp_path)

    def corrupt_and_fail():
        with db.open("ab") as handle:
            handle.write(b"unknown-generation")
        raise RuntimeError("after corruption")

    with pytest.raises(PublishRollbackError) as caught:
        await publish_shadow_generation(
            db_path=db,
            shadow_path=shadow,
            expected_model="new-model",
            expected_dim=2,
            expected_count=1,
            runtime_apply=corrupt_and_fail,
        )
    assert isinstance(caught.value.rollback, PublishRecoveryError)
    assert "without guessing" in str(caught.value.rollback)
    assert get_publish_health()["ok"] is False


@pytest.mark.parametrize("crash_after", [1, 2])
def test_hard_exit_after_each_namespace_step_recovers_exact_old_generation(
    tmp_path,
    crash_after,
):
    db, shadow, old_hash = _pair(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    process = ctx.Process(
        target=crash_publish_worker,
        args=(str(db), str(shadow), crash_after),
    )
    process.start()
    process.join(timeout=30)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail("hard-exit publish worker did not terminate")
    assert process.exitcode == 70 + crash_after
    recovered = recover_pending_publish(None, db)
    assert recovered["ok"] is True
    assert recovered["state"] == "aborted_restored"
    assert _sha(db) == old_hash
    manifest = json.loads(
        (tmp_path / ".embedding-publish.json").read_text(encoding="utf-8")
    )
    assert manifest["state"] == "ABORTED_RESTORED"


def test_corrupt_or_unknown_manifest_fails_closed(tmp_path):
    db = tmp_path / "embeddings.db"
    create_embedding_db(db, rows={"old": [0.0, 1.0]})
    manifest = tmp_path / ".embedding-publish.json"
    manifest.write_text('{"schema":1,"txid":"bad","state":"COMMITTED"}', encoding="utf-8")
    with pytest.raises(PublishRecoveryError):
        recover_pending_publish(None, db)


@pytest.mark.asyncio
async def test_shadow_validation_failure_never_touches_live_db(tmp_path):
    db, shadow, old_hash = _pair(tmp_path)
    # A provider-created shadow with one malformed vector must fail before
    # runtime drain, manifest creation, or namespace mutation.
    import sqlite3

    conn = sqlite3.connect(shadow)
    try:
        conn.execute(
            "UPDATE embeddings SET embedding = ? WHERE bucket_id = ?",
            ('[1.0, "bad"]', "new"),
        )
        conn.commit()
    finally:
        conn.close()
    drained = False

    def close_runtime():
        nonlocal drained
        drained = True

    with pytest.raises(ep.ShadowValidationError):
        await publish_shadow_generation(
            db_path=db,
            shadow_path=shadow,
            expected_model="new-model",
            expected_dim=2,
            expected_count=1,
            runtime_close=close_runtime,
        )
    assert _sha(db) == old_hash
    assert drained is False
    assert not (tmp_path / ".embedding-publish.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failpoint",
    ["prepared_manifest", "first_exchange", "second_exchange", "stable_manifest"],
)
async def test_namespace_and_manifest_faults_restore_exact_old_generation(
    tmp_path,
    monkeypatch,
    failpoint,
):
    db, shadow, old_hash = _pair(tmp_path)
    original_json = ep._durable_json
    original_replace = ep._write_through_replace
    injected = False

    def fault_json(path, value):
        nonlocal injected
        if (
            not injected
            and (
                failpoint == "prepared_manifest"
                and value.get("state") == "PUBLISH_INTENT"
                or failpoint == "stable_manifest"
                and value.get("state") == "COMMITTED"
            )
        ):
            injected = True
            raise OSError(f"{failpoint} injected")
        return original_json(path, value)

    def fault_replace(source, target):
        nonlocal injected
        is_first = target.name.endswith(".old.db")
        is_second = target == db and source == shadow
        if not injected and (
            failpoint == "first_exchange"
            and is_first
            or failpoint == "second_exchange"
            and is_second
        ):
            injected = True
            raise OSError(f"{failpoint} injected")
        return original_replace(source, target)

    monkeypatch.setattr(ep, "_durable_json", fault_json)
    monkeypatch.setattr(ep, "_write_through_replace", fault_replace)
    restored = {"called": False}
    with pytest.raises(OSError, match="injected"):
        await publish_shadow_generation(
            db_path=db,
            shadow_path=shadow,
            expected_model="new-model",
            expected_dim=2,
            expected_count=1,
            runtime_restore=lambda: restored.update(called=True),
        )
    assert injected is True
    assert _sha(db) == old_hash
    assert restored["called"] is True


@pytest.mark.asyncio
async def test_component_close_failure_before_manifest_never_mutates_namespace(
    tmp_path,
):
    db, shadow, old_hash = _pair(tmp_path)
    calls = 0

    def fail_first_close_only():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("component close injected")

    with pytest.raises(OSError, match="component close injected"):
        await publish_shadow_generation(
            db_path=db,
            shadow_path=shadow,
            expected_model="new-model",
            expected_dim=2,
            expected_count=1,
            runtime_close=fail_first_close_only,
            runtime_restore=lambda: None,
        )
    assert _sha(db) == old_hash
    assert shadow.exists()
    assert not (tmp_path / ".embedding-publish.json").exists()


@pytest.mark.asyncio
async def test_shadow_fsync_failure_occurs_before_runtime_drain_or_manifest(
    tmp_path,
    monkeypatch,
):
    db, shadow, old_hash = _pair(tmp_path)
    original = ep._fsync_file

    def fail_shadow_only(path):
        if Path(path) == shadow:
            raise OSError("shadow fsync injected")
        return original(path)

    monkeypatch.setattr(ep, "_fsync_file", fail_shadow_only)
    drained = {"called": False}
    with pytest.raises(OSError, match="shadow fsync injected"):
        await publish_shadow_generation(
            db_path=db,
            shadow_path=shadow,
            expected_model="new-model",
            expected_dim=2,
            expected_count=1,
            runtime_close=lambda: drained.update(called=True),
        )
    assert _sha(db) == old_hash
    assert drained["called"] is False
    assert not (tmp_path / ".embedding-publish.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch",
    [
        {"api_key": "must-never-enter-manifest"},
        {"unknown_field": "value"},
        {"dim": float("nan")},
        {"dim": float("inf")},
    ],
)
async def test_unsafe_config_forward_patch_is_rejected_before_publish(
    tmp_path,
    patch,
):
    db, shadow, old_hash = _pair(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(
        "embedding:\n  model: old-model\n  dim: 2\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        await publish_shadow_generation(
            db_path=db,
            shadow_path=shadow,
            expected_model="new-model",
            expected_dim=2,
            expected_count=1,
            config_path=config,
            config_forward_patch=patch,
        )
    assert _sha(db) == old_hash
    assert not (tmp_path / ".embedding-publish.json").exists()
