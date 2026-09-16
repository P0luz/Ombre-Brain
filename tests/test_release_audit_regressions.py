import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from bucket_manager import BucketManager
from ombrebrain.retrieval.bucket_scoring import calc_time_score
from decay_engine import _days_since_active
from dehydrator import Dehydrator
from embedding_engine import EmbeddingEngine
from import_memory import ImportEngine
from tools import _common as common
from tools import _identity
from tools import _runtime as tools_runtime
from tools.dream.candidates import collect_candidates
from utils import load_config, parse_bool
from web import config_api


ROOT = Path(__file__).resolve().parents[1]


class _FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(func):
            for method in methods:
                self.routes[(method, path)] = func
            return func

        return decorator


class _JsonRequest:
    method = "POST"

    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class _CountingEmbedding:
    enabled = True

    def __init__(self):
        self.calls = []

    async def generate_and_store(self, bucket_id, content):
        self.calls.append((bucket_id, content))
        return True


class _Logger:
    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass


def _json(response):
    return json.loads(response.body.decode("utf-8"))


def test_quoted_false_stays_false_in_helpers_and_yaml(monkeypatch, tmp_path):
    assert parse_bool("false") is False
    assert parse_bool(" OFF ") is False
    assert parse_bool("yes") is True
    with pytest.raises(ValueError):
        parse_bool("maybe")

    config_path = tmp_path / "config.yaml"
    config_path.write_text('mcp_require_auth: "false"\n', encoding="utf-8")
    monkeypatch.delenv("OMBRE_MCP_REQUIRE_AUTH", raising=False)

    config = load_config(str(config_path))

    assert config["mcp_require_auth"] is False


def test_utc_timestamp_is_recent_across_scoring_decay_and_dream():
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    metadata = {
        "created": timestamp,
        "last_active": timestamp,
        "type": "dynamic",
    }

    assert calc_time_score(metadata) > 0.99
    assert _days_since_active(metadata) < 0.01
    assert collect_candidates([{"id": "utc", "metadata": metadata}], 1)[0]["id"] == "utc"


@pytest.mark.asyncio
async def test_bucket_update_normalizes_string_false(tmp_path):
    engine = _CountingEmbedding()
    manager = BucketManager(
        {"buckets_dir": str(tmp_path / "vault")}, embedding_engine=engine
    )
    bucket_id = await manager.create_internal(content="状态边界测试")

    assert await manager.update(bucket_id, pinned=True)
    assert await manager.update(
        bucket_id,
        pinned="false",
        resolved="false",
        digested="false",
        dont_surface="false",
        first_of_kind="false",
        anchor="false",
    )

    bucket = await manager.get(bucket_id)
    assert bucket is not None
    metadata = bucket["metadata"]
    for field in (
        "pinned",
        "resolved",
        "digested",
        "dont_surface",
        "first_of_kind",
    ):
        assert metadata[field] is False
    assert "anchor" not in metadata
    assert metadata["type"] == "dynamic"


@pytest.mark.asyncio
async def test_merge_updates_embedding_exactly_once(tmp_path, monkeypatch):
    engine = _CountingEmbedding()
    manager = BucketManager(
        {"buckets_dir": str(tmp_path / "vault")}, embedding_engine=engine
    )
    old_content = "旧记忆"
    new_content = "新记忆"
    bucket_id = await manager.create_internal(
        content=old_content, tags=["owner:cheng"]
    )

    async def fake_search(*_args, **_kwargs):
        bucket = await manager.get(bucket_id)
        assert bucket is not None
        bucket["score"] = 100
        return [bucket]

    class _NoCompression:
        async def judge_same_event(self, *_args, **_kwargs):
            return {"same_event": True, "confidence": 0.99, "reason": "同一事件"}

        def invalidate_cache(self, _content):
            pass

    monkeypatch.setattr(manager, "search", fake_search)
    monkeypatch.setattr(tools_runtime, "bucket_mgr", manager)
    monkeypatch.setattr(tools_runtime, "embedding_engine", engine)
    monkeypatch.setattr(tools_runtime, "dehydrator", _NoCompression())
    monkeypatch.setattr(tools_runtime, "config", {"merge_threshold": 75})
    monkeypatch.setattr(tools_runtime, "logger", _Logger())

    with _identity.caller_context("cheng"):
        result_id, merged, _warning = await common.merge_or_create(
            content=new_content,
            tags=["owner:cheng"],
            importance=5,
            domain=["测试"],
            valence=0.5,
            arousal=0.3,
            raw_merge=True,
            source_tool="hold",
        )

    assert merged is True
    assert result_id == bucket_id
    assert engine.calls == [
        (bucket_id, old_content),
        (bucket_id, f"{old_content}\n\n---\n{new_content}"),
    ]


def test_import_parser_does_not_truthify_string_false():
    raw = json.dumps([
        {
            "content": "保留这条记忆",
            "preserve_raw": "false",
            "is_pattern": "false",
        }
    ])

    [item] = ImportEngine._parse_extraction(raw)

    assert item["preserve_raw"] is False
    assert item["is_pattern"] is False


@pytest.mark.asyncio
async def test_plan_judge_does_not_truthify_string_false(tmp_path, monkeypatch):
    dehydrator = Dehydrator({
        "buckets_dir": str(tmp_path / "vault"),
        "dehydration": {"api_key": "test-key"},
    })

    async def fake_chat(*_args, **_kwargs):
        return '{"resolved":"false","confidence":0.8,"reason":"not done"}'

    monkeypatch.setattr(dehydrator, "_chat", fake_chat)
    result = await dehydrator.judge_plan_resolution("计划", "仍在进行")
    dehydrator._cache_conn.close()

    assert result["resolved"] is False


def test_embedding_engine_treats_quoted_false_as_disabled(tmp_path):
    engine = EmbeddingEngine({
        "buckets_dir": str(tmp_path),
        "embedding": {"enabled": "false", "api_key": "unused"},
    })

    assert engine.enabled is False
    assert engine._backend is None


@pytest.mark.asyncio
async def test_embedding_cache_uses_bounded_provider_input_identity(tmp_path):
    engine = EmbeddingEngine({
        "buckets_dir": str(tmp_path),
        "embedding": {"enabled": False},
    })

    class _Backend:
        def __init__(self):
            self.calls = []

        async def generate_async(self, text):
            self.calls.append(text)
            return [0.25, 0.75]

    backend = _Backend()
    engine._backend = backend
    visible_prefix = "长" * 2000
    first = visible_prefix + ("甲" * 50_000)
    second = visible_prefix + ("乙" * 50_000)

    assert await engine._generate_async(first) == [0.25, 0.75]
    assert await engine._generate_async(second) == [0.25, 0.75]

    # Provider 只会看到前 2000 字符，所以两个输入必须命中同一项；缓存只保留
    # 固定长度摘要，不能把任一超长正文作为 dict key 留在内存。
    assert backend.calls == [first]
    assert list(engine._query_cache) == [
        hashlib.sha256(visible_prefix.encode("utf-8")).hexdigest()
    ]


@pytest.mark.asyncio
async def test_config_api_reloads_one_embedding_engine_everywhere(monkeypatch, tmp_path):
    bucket_holder = SimpleNamespace(embedding_engine=object())
    import_holder = SimpleNamespace(embedding_engine=object())
    migrate_holder = SimpleNamespace(_embedding_engine=object())
    monkeypatch.setattr(config_api.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(config_api.sh, "config", {
        "buckets_dir": str(tmp_path),
        "embedding": {"enabled": True, "api_key": ""},
    })
    monkeypatch.setattr(config_api.sh, "bucket_mgr", bucket_holder)
    monkeypatch.setattr(config_api.sh, "import_engine", import_holder)
    monkeypatch.setattr(config_api.sh, "migrate_engine", migrate_holder)
    monkeypatch.setattr(config_api.sh, "embedding_engine", object())
    monkeypatch.setattr(tools_runtime, "embedding_engine", object())

    mcp = _FakeMCP()
    config_api.register(mcp)
    response = await mcp.routes[("POST", "/api/config")](
        _JsonRequest({"embedding": {"enabled": "false"}, "persist": "false"})
    )
    payload = _json(response)

    assert response.status_code == 200
    assert payload["ok"] is True
    engine = config_api.sh.embedding_engine
    assert engine.enabled is False
    assert config_api.sh.config["embedding"]["enabled"] is False
    assert bucket_holder.embedding_engine is engine
    assert import_holder.embedding_engine is engine
    assert migrate_holder._embedding_engine is engine
    assert tools_runtime.embedding_engine is engine


@pytest.mark.asyncio
async def test_mcp_token_regeneration_publishes_only_after_persist(monkeypatch):
    original_token = "original-token"
    attempted = {}
    monkeypatch.setattr(config_api.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(config_api.sh, "config", {
        "transport": "streamable-http",
        "mcp_token": original_token,
    })

    def fail_persist(mutator):
        saved = {"mcp_token": original_token}
        mutator(saved)
        attempted.update(saved)
        assert config_api.sh.config["mcp_token"] == original_token
        raise OSError("simulated config write failure")

    monkeypatch.setattr(config_api, "atomic_update_config_yaml", fail_persist)
    mcp = _FakeMCP()
    config_api.register(mcp)

    response = await mcp.routes[("POST", "/api/mcp-token/regenerate")](
        _JsonRequest({})
    )

    assert response.status_code == 500
    assert attempted["mcp_token"] != original_token
    assert config_api.sh.config["mcp_token"] == original_token


def test_embedding_is_owned_by_bucket_manager_on_normal_write_paths():
    paths = (
        "src/import_memory.py",
        "src/web/plans.py",
        "src/web/import_api.py",
        "src/web/letters.py",
    )
    for rel in paths:
        source = (ROOT / rel).read_text(encoding="utf-8")
        assert "generate_and_store(" not in source, rel

    common_source = (ROOT / "src/tools/_common.py").read_text(encoding="utf-8")
    assert common_source.count("generate_and_store(") == 1
    assert common_source.index("sync_embedding_after_commit") < common_source.index(
        "generate_and_store("
    )


def test_dashboard_source_uses_real_host_mount_contract():
    # 单一真源：运行时（src/web/dashboard.py）只读 frontend/dashboard.html，
    # 仓库根目录不再维护同名镜像文件，故这里只校验这一份的内容契约。
    frontend_dashboard = ROOT / "frontend" / "dashboard.html"

    source = frontend_dashboard.read_text(encoding="utf-8")
    assert "${OMBRE_HOST_VAULT_DIR:-./buckets}</code> 挂到容器的 <code>/app/buckets" in source
    assert "${OMBRE_HOST_VAULT_DIR:-./buckets}:/data" not in source
    assert 'id="settings-host-vault-save"' in source
    assert "if (d.compose_managed)" in source
    assert 'data-decision-id="' in source
    assert "replayV3Decision(this.dataset.decisionId)" in source
