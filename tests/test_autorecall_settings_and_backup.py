"""自动召回：面板设置接口 + 原文库随 ZIP 备份往返。"""

import json
import os
import sqlite3
import zipfile
import io

import frontmatter
import pytest

from bucket_manager import BucketManager
from embedding_engine import EmbeddingEngine
from migrate_engine import MigrateEngine
from ombrebrain.autorecall import TranscriptStore
from ombrebrain.storage.backup_archive import build_export_archive, build_export_archive_file
from web import autorecall


# ---------------- 设置接口 ----------------

class _MCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(handler):
            for method in methods:
                self.routes[(method, path)] = handler
            return handler
        return decorator


class _Req:
    def __init__(self, body=None):
        self._body = body


def _settings_routes(monkeypatch, tmp_path, *, authed=True, persist_error=None):
    config = {"hooks": {"token": "t"}, "autorecall": {"enabled": False}}
    saved = {}

    def fake_persist(mutate):
        if persist_error:
            raise persist_error
        mutate(saved)
        return saved

    async def read_json(request):
        if not isinstance(request._body, dict):
            raise ValueError("JSON body must be an object")
        return request._body

    monkeypatch.setattr(autorecall.sh, "config", config, raising=False)
    monkeypatch.setattr(autorecall.sh, "_require_auth",
                        lambda request: None if authed else autorecall.JSONResponse({}, status_code=401))
    monkeypatch.setattr(autorecall.sh, "_read_json_object", read_json)
    monkeypatch.setattr(autorecall, "atomic_update_config_yaml", fake_persist)
    monkeypatch.setattr(autorecall.sh, "autorecall_service", None, raising=False)
    mcp = _MCP()
    autorecall.register(mcp)
    return mcp.routes, config, saved


def _j(resp):
    return json.loads(resp.body.decode())


@pytest.mark.asyncio
async def test_settings_require_auth(monkeypatch, tmp_path):
    routes, _, _ = _settings_routes(monkeypatch, tmp_path, authed=False)
    assert (await routes[("GET", "/api/settings/autorecall")](_Req())).status_code == 401
    assert (await routes[("POST", "/api/settings/autorecall")](_Req({"enabled": True}))).status_code == 401


@pytest.mark.asyncio
async def test_settings_get_defaults(monkeypatch, tmp_path):
    routes, _, _ = _settings_routes(monkeypatch, tmp_path)
    data = _j(await routes[("GET", "/api/settings/autorecall")](_Req()))
    assert data["enabled"] is False and data["reminder_every"] == 30 and data["agree_top"] == 2
    assert "来着" in data["wake_words"] and data["hook_token_configured"] is True


@pytest.mark.asyncio
async def test_settings_post_persists_then_applies(monkeypatch, tmp_path):
    routes, config, saved = _settings_routes(monkeypatch, tmp_path)
    resp = await routes[("POST", "/api/settings/autorecall")](_Req({
        "enabled": True, "wake_words": "记得，Mochi, 玉米蛇", "reminder_every": 20, "agree_top": 0,
    }))
    data = _j(resp)
    assert resp.status_code == 200 and data["enabled"] is True and data["reminder_every"] == 20
    assert data["wake_words"] == ["记得", "Mochi", "玉米蛇"]
    assert saved["autorecall"]["enabled"] is True and config["autorecall"]["agree_top"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"enabled": "yes"}, {"reminder_every": 501}, {"agree_top": True}, {"restore_rounds": -1},
    {"unknown": 1}, {"wake_words": [1, 2]}, {"wake_words": ["x" * 21]},
])
async def test_settings_post_rejects_bad_values(monkeypatch, tmp_path, body):
    routes, config, saved = _settings_routes(monkeypatch, tmp_path)
    assert (await routes[("POST", "/api/settings/autorecall")](_Req(body))).status_code == 400
    assert saved == {} and config["autorecall"] == {"enabled": False}


@pytest.mark.asyncio
async def test_settings_persist_failure_changes_nothing(monkeypatch, tmp_path):
    routes, config, _ = _settings_routes(monkeypatch, tmp_path, persist_error=OSError("disk full"))
    resp = await routes[("POST", "/api/settings/autorecall")](_Req({"enabled": True}))
    assert resp.status_code == 503 and config["autorecall"] == {"enabled": False}


# ---------------- 备份往返 ----------------

class _Backend:
    def vector_dim(self):
        return 2


def _config(root):
    return {"buckets_dir": str(root), "embedding": {"enabled": False},
            "storage": {"external_change_poll_seconds": 0}}


def _engine(config):
    engine = EmbeddingEngine(config)
    engine.model = "test-embedding"
    engine._backend = _Backend()
    return engine


def _vault(root):
    path = root / "dynamic" / "general" / "memory_m1.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.dumps(frontmatter.Post(
        "记忆正文", id="m1", name="M", type="dynamic", domain=["general"], created="2026-07-11T12:00:00",
    )), encoding="utf-8")
    store = TranscriptStore(root)
    store.append("sess", [{"key": "u1", "role": "user", "text": "今天想吃火锅"},
                          {"key": "a1", "role": "assistant", "text": "好呀"}])
    store.link([(store.session_turns("sess")[0]["id"], "m1", 3.5)])
    return store


_META = {"exported_at": "2026-10-14T12:00:00", "version": "test",
         "embedding": {"model": "test-embedding", "dim": 2, "backend": "api"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("disk", [False, True], ids=["memory", "disk"])
async def test_transcript_round_trips_through_zip(tmp_path, disk):
    source = tmp_path / "source"
    _vault(source)
    engine = _engine(_config(source))
    if disk:
        path, manifest = build_export_archive_file(str(source), engine.db_path, _META)
        payload = open(path, "rb").read()
        os.remove(path)
    else:
        payload, manifest = build_export_archive(str(source), engine.db_path, _META)
    assert any(f["path"] == "autorecall/transcript.sqlite3" for f in manifest["files"])

    target = tmp_path / "target"
    config = _config(target)
    target_engine = _engine(config)
    migrate = MigrateEngine(config, BucketManager(config, embedding_engine=target_engine), target_engine)
    parsed = await migrate.parse_zip(payload)
    assert parsed["ok"] is True
    await migrate.apply({})

    restored = TranscriptStore(target)
    assert [t["text"] for t in restored.session_turns("sess")] == ["今天想吃火锅", "好呀"]
    assert restored.links_for_bucket("m1")[0]["score"] == 3.5


def test_vault_without_transcript_exports_nothing_extra(tmp_path):
    source = tmp_path / "source"
    (source / "dynamic").mkdir(parents=True)
    _, manifest = build_export_archive(str(source), _engine(_config(source)).db_path, _META)
    assert not any(f["path"].startswith("autorecall/") for f in manifest["files"])


@pytest.mark.asyncio
async def test_tampered_transcript_snapshot_rejected(tmp_path):
    source = tmp_path / "source"
    _vault(source)
    payload, _ = build_export_archive(str(source), _engine(_config(source)).db_path, _META)
    evil = tmp_path / "evil.sqlite3"
    conn = sqlite3.connect(evil)
    conn.execute("CREATE TABLE evil(x)")
    conn.commit()
    conn.close()
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(payload)) as src, zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            if info.filename == "backup_manifest.json":
                continue  # 去掉清单，模拟旧格式包，只靠结构校验拦
            data = evil.read_bytes() if info.filename == "autorecall/transcript.sqlite3" else src.read(info)
            dst.writestr(info.filename, data)
    target = tmp_path / "target"
    config = _config(target)
    target_engine = _engine(config)
    migrate = MigrateEngine(config, BucketManager(config, embedding_engine=target_engine), target_engine)
    parsed = await migrate.parse_zip(out.getvalue())
    assert parsed["ok"] is False
    assert "自动召回原文库" in json.dumps(parsed, ensure_ascii=False)
