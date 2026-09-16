from __future__ import annotations

import ast
import itertools
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import frontmatter
import pytest

from bucket_manager import BucketManager
import bucket_manager as bucket_manager_module
from embedding_outbox import content_sha256
from import_transaction import ImportCandidate, apply_import_batch, review_bucket_snapshot
from ombrebrain.eventsourcing.footprint import (
    FootprintOriginError,
    cli_origin,
    dashboard_letter_origin,
    import_origin,
    mcp_origin,
    system_origin,
    validate_origin,
)
from tools import _identity, _runtime as rt
from tools.hold.feel import store_feel
from tools.hold.pinned import store_pinned
from tools.i.core import _write_i
from tools.i.profile_service import create_profile
from tools.plan.core import letter_write, plan_create
from tools._common import merge_or_create
import write_memory as write_memory_module


class _DiskManager:
    def __init__(self, root: Path):
        self.base_dir = str(root)
        self.config = {"buckets_dir": str(root)}

    def _bucket_turn(self, bucket_id):
        from bucket_manager import _filesystem_turn

        return _filesystem_turn(self.base_dir, f"bucket-{bucket_id}")

    async def list_all(self, include_archive=False, fresh=False):
        return []

    def _invalidate_bm25(self):
        return None


def _markdown(bucket_id: str, body: str, origin=None) -> bytes:
    metadata = {
        "id": bucket_id,
        "name": bucket_id,
        "tags": ["owner:cheng"],
        "domain": ["test"],
        "importance": 5,
        "type": "dynamic",
    }
    if origin is not None:
        metadata["footprint_origin"] = origin
    return frontmatter.dumps(frontmatter.Post(body, **metadata)).encode("utf-8")


def _put(root: Path, raw: bytes, leaf: str) -> Path:
    path = root / "dynamic" / "test" / leaf
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


class _Embedding:
    enabled = True

    def __init__(self):
        self.saved = {}

    async def generate_and_store(self, bucket_id, content):
        self.saved[bucket_id] = content
        return True

    def delete_embedding(self, bucket_id):
        self.saved.pop(bucket_id, None)


class _CaptureManager:
    def __init__(self):
        self.created = []
        self.updated = []
        self.deleted = []

    async def create(self, **kwargs):
        self.created.append(kwargs)
        return f"captured-{len(self.created)}"

    async def update(self, bucket_id, **kwargs):
        self.updated.append((bucket_id, kwargs))
        return True

    async def delete(self, bucket_id):
        self.deleted.append(bucket_id)
        return True

    async def get(self, bucket_id):
        return None

    async def list_all(self, include_archive=False, fresh=False):
        return []


def _config(tmp_path: Path) -> dict:
    root = tmp_path / "buckets"
    root.mkdir(parents=True, exist_ok=True)
    return {
        "buckets_dir": str(root),
        "matching": {},
        "wikilink": {"enabled": False},
        "scoring_weights": {},
    }


def test_origin_allowlist_and_canonical_builders():
    assert mcp_origin("hold", "cheng") == {
        "schema": 1,
        "via": "hold",
        "actor_kind": "mcp_tool",
        "actor_principal": "cheng",
        "surface": "mcp",
    }
    assert dashboard_letter_origin()["actor_principal"] == "human"
    assert import_origin("system", "system")["surface"] == "import_transaction"
    assert cli_origin()["actor_principal"] == "local_operator"
    assert system_origin()["surface"] == "system"


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {
            "schema": True,
            "via": "hold",
            "actor_kind": "mcp_tool",
            "actor_principal": "cheng",
            "surface": "mcp",
        },
        {
            "schema": 1,
            "via": "hold",
            "actor_kind": "mcp_tool",
            "actor_principal": "unknown",
            "surface": "mcp",
        },
        {
            "schema": 1,
            "via": "direct",
            "actor_kind": "system",
            "actor_principal": "system",
            "surface": "system",
            "extra": "no",
        },
        {
            "schema": 1,
            "via": "DIRECT ",
            "actor_kind": "USER",
            "actor_principal": "LOCAL-OPERATOR",
            "surface": "CLI",
        },
        {
            "schema": 1,
            "via": "letter",
            "actor_kind": "web-dashboard",
            "actor_principal": "human",
            "surface": "web-dashboard",
        },
        {
            "schema": 1,
            "via": "hold",
            "actor_kind": "mcp_tool",
            "actor_principal": 7,
            "surface": "mcp",
        },
        {
            "schema": 1,
            "via": "hold",
            "actor_kind": "mcp_tool",
            "actor_principal": "cheng",
            "surface": "mcp",
            1: "extra-non-string-key",
            "z-extra": True,
        },
    ],
)
def test_invalid_origins_fail_closed(bad):
    with pytest.raises(FootprintOriginError):
        validate_origin(bad)


def test_all_finite_schema_tuples_match_only_the_frozen_allowlist():
    vias = ("hold", "grow", "import", "plan", "letter", "i", "direct")
    kinds = (
        "user",
        "codex",
        "claude",
        "gpt",
        "gemini",
        "mcp_tool",
        "web_dashboard",
        "system",
    )
    principals = (
        "cheng",
        "huaiyin",
        "huaiyin_cc",
        "human",
        "system",
        "local_operator",
        "unknown",
    )
    surfaces = ("mcp", "web_dashboard", "import_transaction", "cli", "system")

    def expected(via, kind, principal, surface):
        if surface == "mcp":
            return via in {"hold", "grow", "import", "plan", "letter", "i"} and kind == "mcp_tool" and principal in {"cheng", "huaiyin", "huaiyin_cc"}
        if surface == "web_dashboard":
            return via == "letter" and kind == "web_dashboard" and principal == "human"
        if surface == "import_transaction":
            return via == "import" and (
                (kind == "mcp_tool" and principal in {"cheng", "huaiyin", "huaiyin_cc"})
                or (kind == "web_dashboard" and principal == "human")
                or (kind == "system" and principal == "system")
            )
        if surface == "cli":
            return via == "direct" and kind == "user" and principal == "local_operator"
        return via == "direct" and kind == "system" and principal == "system"

    for via, kind, principal, surface in itertools.product(
        vias, kinds, principals, surfaces
    ):
        value = {
            "schema": 1,
            "via": via,
            "actor_kind": kind,
            "actor_principal": principal,
            "surface": surface,
        }
        if expected(via, kind, principal, surface):
            assert validate_origin(value) == value
        else:
            with pytest.raises(FootprintOriginError):
                validate_origin(value)


@pytest.mark.asyncio
async def test_create_requires_origin_before_markdown_publication(tmp_path):
    manager = BucketManager(_config(tmp_path), embedding_engine=_Embedding())
    with pytest.raises(FootprintOriginError):
        await manager.create(content="no receipt", tags=["owner:cheng"])
    assert not list((tmp_path / "buckets").rglob("*.md"))


@pytest.mark.asyncio
async def test_origin_is_in_first_markdown_and_update_cannot_mutate_it(tmp_path):
    manager = BucketManager(_config(tmp_path), embedding_engine=_Embedding())
    origin = mcp_origin("hold", "cheng")
    bucket_id = await manager.create(
        content="authoritative content",
        tags=["owner:shared_core"],
        source_tool="hold",
        footprint_origin=origin,
    )
    path = Path(manager._find_bucket_file(bucket_id))
    post = frontmatter.load(path)
    assert dict(post["footprint_origin"]) == origin
    assert "owner:shared_core" in post["tags"]
    before = path.read_bytes()

    with pytest.raises(ValueError, match="immutable"):
        await manager.update(bucket_id, footprint_origin=system_origin())
    assert path.read_bytes() == before

    assert await manager.update(bucket_id, last_merged_by="grow")
    merged = frontmatter.load(path)
    assert dict(merged["footprint_origin"]) == origin
    assert merged["last_merged_by"] == "grow"


@pytest.mark.asyncio
async def test_atomic_publish_receives_content_and_origin_together(tmp_path, monkeypatch):
    manager = BucketManager(_config(tmp_path), embedding_engine=_Embedding())
    seen = []
    real_create = bucket_manager_module._atomic_create_text

    def capture(path, rendered):
        seen.append(frontmatter.loads(rendered))
        return real_create(path, rendered)

    monkeypatch.setattr(bucket_manager_module, "_atomic_create_text", capture)
    origin = mcp_origin("hold", "cheng")
    await manager.create(
        content="same atomic payload",
        tags=["owner:cheng"],
        footprint_origin=origin,
    )
    assert len(seen) == 1
    assert seen[0].content == "same atomic payload"
    assert dict(seen[0]["footprint_origin"]) == origin


@pytest.mark.asyncio
async def test_m05_prepare_failure_publishes_no_markdown(tmp_path, monkeypatch):
    config = _config(tmp_path)
    config["m05_embedding_outbox"] = {
        "enabled": True,
        "root": str(tmp_path / "outbox"),
    }
    manager = BucketManager(config, embedding_engine=_Embedding())

    def fail_prepare(*args, **kwargs):
        raise OSError("prepare failed")

    monkeypatch.setattr(bucket_manager_module, "prepare_upsert", fail_prepare)
    with pytest.raises(OSError, match="prepare failed"):
        await manager.create(
            content="must not publish",
            tags=["owner:cheng"],
            footprint_origin=mcp_origin("hold", "cheng"),
        )
    assert not list((tmp_path / "buckets").rglob("*.md"))


@pytest.mark.asyncio
async def test_m05_provider_failure_keeps_authoritative_origin(tmp_path):
    class _FailingEmbedding(_Embedding):
        async def generate_and_store(self, bucket_id, content):
            raise RuntimeError("provider down")

    config = _config(tmp_path)
    config["m05_embedding_outbox"] = {
        "enabled": True,
        "root": str(tmp_path / "outbox"),
    }
    manager = BucketManager(config, embedding_engine=_FailingEmbedding())
    origin = mcp_origin("grow", "cheng")
    with pytest.raises(RuntimeError, match="provider down"):
        await manager.create(
            content="durable before provider",
            tags=["owner:cheng"],
            footprint_origin=origin,
        )
    files = list((tmp_path / "buckets").rglob("*.md"))
    assert len(files) == 1
    post = frontmatter.load(files[0])
    assert post.content == "durable before provider"
    assert dict(post["footprint_origin"]) == origin


def test_origin_never_changes_m05_content_hash():
    content = "hash only the authoritative body"
    before = content_sha256(content)
    _ = mcp_origin("hold", "cheng")
    assert content_sha256(content) == before


@pytest.mark.asyncio
async def test_anchor_release_preserves_origin_object(tmp_path):
    manager = BucketManager(_config(tmp_path), embedding_engine=_Embedding())
    origin = mcp_origin("hold", "cheng")
    bucket_id = await manager.create(
        content="anchor candidate",
        tags=["owner:cheng"],
        source_tool="hold",
        footprint_origin=origin,
    )
    assert await manager.set_anchor(bucket_id, True)
    anchored = await manager.get(bucket_id)
    assert anchored["metadata"]["footprint_origin"] == origin
    assert await manager.set_anchor(bucket_id, False)
    released = await manager.get(bucket_id)
    assert released["metadata"]["footprint_origin"] == origin


@pytest.mark.asyncio
async def test_import_replaces_package_origin_with_fresh_local_receipt(tmp_path):
    incoming = mcp_origin("hold", "huaiyin")
    local = import_origin("system", "system")
    result = await apply_import_batch(
        buckets_dir=str(tmp_path),
        bucket_manager=_DiskManager(tmp_path),
        job_owner="cheng",
        candidates=[
            ImportCandidate(
                source_id="new-memory",
                markdown=_markdown("new-memory", "imported", incoming),
            )
        ],
        footprint_origin=local,
    )
    target = review_bucket_snapshot(str(tmp_path), result.imported_id_map["new-memory"])
    post = frontmatter.load(tmp_path / target["relative_path"])
    assert dict(post["footprint_origin"]) == local


@pytest.mark.asyncio
@pytest.mark.parametrize("origin_state", ["valid", "missing", "malformed"])
async def test_overwrite_preserves_valid_live_origin_or_stays_unrecorded(
    tmp_path, origin_state
):
    if origin_state == "valid":
        live_origin = mcp_origin("hold", "cheng")
    elif origin_state == "malformed":
        live_origin = {
            "schema": 1,
            "via": "HOLD",
            "actor_kind": "mcp-tool",
            "actor_principal": "cheng",
            "surface": "mcp",
        }
    else:
        live_origin = None
    live_path = _put(
        tmp_path,
        _markdown("same", "old", live_origin),
        "old_same.md",
    )
    snapshot = review_bucket_snapshot(str(tmp_path), "same")
    result = await apply_import_batch(
        buckets_dir=str(tmp_path),
        bucket_manager=_DiskManager(tmp_path),
        job_owner="cheng",
        candidates=[
            ImportCandidate(
                source_id="same",
                markdown=_markdown(
                    "same", "new", mcp_origin("grow", "huaiyin")
                ),
                decision="overwrite",
                conflicted_at_review=True,
                expected_live_sha256=snapshot["sha256"],
                expected_live_relative_path=snapshot["relative_path"],
            )
        ],
        footprint_origin=import_origin("system", "system"),
    )
    target = review_bucket_snapshot(str(tmp_path), result.imported_id_map["same"])
    post = frontmatter.load(tmp_path / target["relative_path"])
    if origin_state == "valid":
        assert dict(post["footprint_origin"]) == live_origin
    else:
        assert "footprint_origin" not in post.metadata
    assert not live_path.exists() or Path(target["relative_path"]).name == live_path.name


@pytest.mark.asyncio
async def test_keep_both_replaces_input_origin_with_fresh_local_receipt(tmp_path):
    _put(tmp_path, _markdown("same", "live"), "live_same.md")
    snapshot = review_bucket_snapshot(str(tmp_path), "same")
    local = import_origin("system", "system")
    result = await apply_import_batch(
        buckets_dir=str(tmp_path),
        bucket_manager=_DiskManager(tmp_path),
        job_owner="cheng",
        candidates=[
            ImportCandidate(
                source_id="same",
                markdown=_markdown(
                    "same", "incoming", mcp_origin("grow", "huaiyin")
                ),
                decision="keep_both",
                conflicted_at_review=True,
                expected_live_sha256=snapshot["sha256"],
                expected_live_relative_path=snapshot["relative_path"],
            )
        ],
        footprint_origin=local,
    )
    target_id = result.imported_id_map["same"]
    assert target_id != "same"
    target = review_bucket_snapshot(str(tmp_path), target_id)
    post = frontmatter.load(tmp_path / target["relative_path"])
    assert dict(post["footprint_origin"]) == local


@pytest.mark.asyncio
async def test_plan_and_letter_mcp_paths_pass_explicit_actor_receipts():
    manager = _CaptureManager()
    rt.init(
        bucket_mgr=manager,
        decay_engine=SimpleNamespace(ensure_started=AsyncMock()),
        logger=MagicMock(),
    )
    _identity.set_caller("cheng")

    await plan_create("finish E1")
    assert manager.created[0]["footprint_origin"] == mcp_origin("plan", "cheng")
    assert "owner:cheng" in manager.created[0]["tags"]

    await letter_write(author="ai", content="hello")
    assert manager.created[1]["footprint_origin"] == mcp_origin("letter", "cheng")
    assert "owner:cheng" in manager.created[1]["tags"]


@pytest.mark.asyncio
async def test_plan_dedup_requires_caller_and_never_crosses_owner_boundary():
    manager = _CaptureManager()
    foreign = {
        "id": "foreign-plan",
        "content": "same plan",
        "metadata": {
            "type": "plan",
            "status": "active",
            "tags": ["owner:huaiyin"],
        },
    }
    manager.list_all = AsyncMock(return_value=[foreign])
    rt.init(
        bucket_mgr=manager,
        decay_engine=SimpleNamespace(ensure_started=AsyncMock()),
        logger=MagicMock(),
    )

    _identity.set_caller("")
    with pytest.raises(ValueError, match="recognized MCP caller"):
        await plan_create("same plan")
    manager.list_all.assert_not_awaited()

    _identity.set_caller("cheng")
    result = await plan_create("same plan")
    assert result.startswith("📋plan→captured-1")
    assert manager.created[0]["tags"] == ["__plan__", "owner:cheng"]

    manager.created.clear()
    same_owner = {
        **foreign,
        "id": "same-owner-plan",
        "metadata": {
            **foreign["metadata"],
            "tags": ["owner:cheng"],
        },
    }
    manager.list_all = AsyncMock(return_value=[same_owner])
    duplicate = await plan_create("same plan")
    assert "same-owner-plan" in duplicate
    assert manager.created == []


@pytest.mark.asyncio
async def test_feel_and_i_paths_pass_explicit_actor_receipts():
    manager = _CaptureManager()
    rt.init(bucket_mgr=manager, logger=MagicMock())
    _identity.set_caller("huaiyin_cc")

    await store_feel("a feeling", ["owner:huaiyin_cc"], 0.6, 0.4, "", "")
    assert manager.created[0]["footprint_origin"] == mcp_origin(
        "hold", "huaiyin_cc"
    )

    result = await _write_i("stable observation", "nature", "huaiyin_cc")
    assert "captured-2" in result
    assert manager.created[1]["footprint_origin"] == mcp_origin(
        "i", "huaiyin_cc"
    )


@pytest.mark.asyncio
async def test_profile_path_passes_explicit_actor_receipt():
    manager = _CaptureManager()
    result = await create_profile(
        manager,
        caller="cheng",
        content="I verify before claiming completion.",
        aspect="patterns",
        confidence=0.9,
        evidence_id="evidence-1",
        confirm_stable=True,
    )
    assert result["owner"] == "cheng"
    assert manager.created[0]["footprint_origin"] == mcp_origin("i", "cheng")


@pytest.mark.asyncio
@pytest.mark.parametrize("source_tool", ["hold", "grow"])
async def test_merge_create_paths_persist_matching_mcp_origin(tmp_path, source_tool):
    manager = BucketManager(_config(tmp_path), embedding_engine=_Embedding())
    rt.init(
        bucket_mgr=manager,
        config={"merge_threshold": 75},
        dehydrator=SimpleNamespace(
            invalidate_cache=MagicMock(),
            merge=AsyncMock(side_effect=lambda old, new: old + "\n" + new),
        ),
        embedding_engine=manager.embedding_engine,
        logger=MagicMock(),
    )
    _identity.set_caller("cheng")
    bucket_id, merged, _warning = await merge_or_create(
        content=f"fresh {source_tool} memory",
        tags=["path-test"],
        importance=5,
        domain=["test"],
        valence=0.5,
        arousal=0.3,
        source_tool=source_tool,
    )
    assert merged is False
    bucket = await manager.get(bucket_id)
    assert bucket["metadata"]["footprint_origin"] == mcp_origin(
        source_tool, "cheng"
    )


@pytest.mark.asyncio
async def test_pinned_hold_supplies_source_tool_and_origin(tmp_path):
    manager = BucketManager(_config(tmp_path), embedding_engine=_Embedding())
    rt.init(
        bucket_mgr=manager,
        config={"limits": {"max_pinned": 20}},
        dehydrator=SimpleNamespace(
            analyze=AsyncMock(
                return_value={
                    "domain": ["rule"],
                    "valence": 0.5,
                    "arousal": 0.3,
                    "tags": ["core"],
                    "suggested_name": "rule",
                }
            )
        ),
        embedding_engine=manager.embedding_engine,
        logger=MagicMock(),
    )
    _identity.set_caller("cheng")
    result = await store_pinned("always verify", ["owner:cheng"], -1, -1, "")
    bucket_id = result.split("→", 1)[1].split(" ", 1)[0]
    bucket = await manager.get(bucket_id)
    assert bucket["metadata"]["source_tool"] == "hold"
    assert bucket["metadata"]["footprint_origin"] == mcp_origin("hold", "cheng")


def test_cli_requires_exact_owner_and_writes_cli_origin(tmp_path, monkeypatch):
    dynamic = tmp_path / "buckets" / "dynamic"
    dynamic.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(write_memory_module, "VAULT_DIR", str(dynamic))
    bucket_id = write_memory_module.write_memory(
        "local note",
        "cli body",
        ["test"],
        ["manual"],
        "cheng",
    )
    post = frontmatter.load(next(dynamic.glob("*.md")))
    assert post["id"] == bucket_id
    assert post["tags"][-1] == "owner:cheng"
    assert dict(post["footprint_origin"]) == cli_origin()
    with pytest.raises(ValueError, match="explicit validated target owner"):
        write_memory_module.write_memory(
            "bad", "bad", ["test"], [], "human"
        )


def test_reverse_call_graph_has_no_unclassified_production_create(tmp_path):
    source_root = Path(__file__).resolve().parents[1] / "src"
    direct_calls = []
    deferred_calls = []
    apply_calls = []
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "create":
                text = ast.unparse(func.value)
                if text not in {
                    "self.client.chat.completions",
                    "self._client.embeddings",
                    "client.chat.completions",
                }:
                    direct_calls.append(
                        (path.relative_to(source_root).as_posix(), node.lineno, {kw.arg for kw in node.keywords})
                    )
            if isinstance(func, ast.Name) and func.id == "_create_bucket_deferred":
                deferred_calls.append(
                    (path.relative_to(source_root).as_posix(), node.lineno, {kw.arg for kw in node.keywords})
                )
            if (
                isinstance(func, ast.Name)
                and func.id == "apply_import_batch"
            ):
                apply_calls.append(
                    (path.relative_to(source_root).as_posix(), node.lineno, {kw.arg for kw in node.keywords})
                )
    assert direct_calls
    assert all(
        "footprint_origin" in keywords
        or (relative == "tools/_common.py" and None in keywords)
        for relative, _, keywords in direct_calls
    ), direct_calls
    assert deferred_calls
    assert all(
        "footprint_origin" in keywords
        or (relative == "tools/_common.py" and None in keywords)
        for relative, _, keywords in deferred_calls
    ), deferred_calls
    assert apply_calls
    assert all("footprint_origin" in keywords for _, _, keywords in apply_calls)

    adapters = {
        "bucket_manager.py": (
            "_atomic_create_text",
            '"footprint_origin": canonical_origin',
        ),
        "import_transaction.py": (
            "_write_bytes",
            'metadata["footprint_origin"]',
        ),
        "write_memory.py": (
            "_atomic_create_text",
            '"footprint_origin": cli_origin()',
        ),
    }
    for relative, required in adapters.items():
        source = (source_root / relative).read_text(encoding="utf-8")
        assert all(marker in source for marker in required), relative
