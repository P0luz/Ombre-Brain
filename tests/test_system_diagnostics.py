import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import web.system as system


class FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(fn):
            for method in methods:
                self.routes[(method, path)] = fn
            return fn

        return decorator


class FakeBucketManager:
    def __init__(self):
        self.ledger_thread_id = None

    async def get_stats(self):
        return {
            "permanent_count": 1,
            "dynamic_count": 2,
            "archive_count": 3,
        }

    def ledger_integrity_report(self):
        self.ledger_thread_id = threading.get_ident()
        return {
            "ok": True,
            "path": "buckets/_ledger/events.jsonl",
            "ledger_role": "mirror",
            "canonical": False,
            "valid_events": 2,
            "invalid_lines": [],
            "latest_seq": 2,
            "schema_versions": [1],
            "trace_catalog_projection": {
                "projection_name": "trace_catalog",
                "projection_role": "shadow",
                "canonical": False,
                "trace_count": 1,
                "tombstone_count": 0,
                "applied_seq": 2,
                "source_latest_seq": 2,
                "lag": 0,
                "unknown_event_count": 0,
            },
            "sqlite_projection": {
                "projection_name": "trace_catalog_sqlite",
                "projection_role": "shadow",
                "canonical": False,
                "trace_count": 1,
                "tombstone_count": 0,
                "applied_seq": 2,
                "source_latest_seq": 2,
                "lag": 0,
                "unknown_event_count": 0,
                "fts_enabled": True,
            },
            "replay": {
                "ok": True,
                "event_count": 2,
                "latest_seq": 2,
                "projection_name": "trace_catalog",
                "projection_trace_count": 1,
                "tombstone_count": 0,
                "unknown_event_count": 0,
                "violations": [],
            },
        }


class FakeDecayEngine:
    is_running = True


class StandbyEmbeddingEngine:
    enabled = True
    _backend = None
    model = ""
    db_path = ""


class FakeGithubSync:
    def status(self):
        return {
            "enabled": True,
            "repo": "owner/repo",
            "branch": "main",
            "path_prefix": "ombre",
            "last_sync": None,
            "last_status": "idle",
            "last_error": "",
            "last_count": 0,
            "is_validated": False,
        }


@pytest.mark.asyncio
async def test_system_diagnostics_reports_missing_ai_configuration(monkeypatch, tmp_path):
    buckets_dir = tmp_path / "buckets"
    buckets_dir.mkdir()
    server_src = tmp_path / "src"
    server_src.mkdir()
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    web_src = server_src / "web"
    web_src.mkdir()
    policy_src = server_src / "ombrebrain" / "policy"
    policy_src.mkdir(parents=True)
    (server_src / "server.py").write_text(
        "\n".join(
            [
                "class FakeMCP:",
                "    def tool(self):",
                "        def deco(fn):",
                "            return fn",
                "        return deco",
                "mcp = FakeMCP()",
                "@mcp.tool()",
                "async def breath():",
                "    pass",
                "@mcp.tool()",
                "async def hold():",
                "    pass",
                "@mcp.tool()",
                "async def grow():",
                "    pass",
                "@mcp.tool()",
                "async def trace():",
                "    pass",
                "@mcp.tool()",
                "async def dream():",
                "    pass",
                "@mcp.tool()",
                "async def pulse():",
                "    pass",
                "@mcp.tool()",
                "async def release():",
                "    pass",
            ]
        ),
        encoding="utf-8",
    )
    (web_src / "system.py").write_text(
        "\n".join(
            [
                "# diagnostics boundary",
            ]
        ),
        encoding="utf-8",
    )
    (web_src / "search.py").write_text("# search adapter\n", encoding="utf-8")
    (policy_src / "surfacing.py").write_text("# surface policy\n", encoding="utf-8")
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "ADR-0001-diagnostics.md").write_text(
        """# ADR-0001: Diagnostics boundary

## Decision

Expose diagnostics through contracts.

## Why this is not cognition

It does not decide behavior.

## Why this is not a database feature

It preserves bounded surfacing.

## How forgetting still works

It does not change decay or archival.

## How tombstones are preserved

It does not remove tombstones.

## How present thinking remains with the LLM

It reports context only.

## Rejected alternatives

No release gate.

## Tests required

Diagnostics regression tests.
""",
        encoding="utf-8",
    )

    monkeypatch.setattr(system.sh, "config", {
        "buckets_dir": str(buckets_dir),
        "dehydration": {
            "api_key": "",
            "base_url": "https://api.example.test/v1",
            "model": "deepseek-chat",
            "timeout_seconds": 120,
        },
        "embedding": {
            "enabled": True,
            "api_key": "",
            "model": "bge-m3",
            "timeout_seconds": 150,
        },
        "mcp_require_auth": False,
        "github_sync": {"repo": "owner/repo", "branch": "main", "path_prefix": "ombre"},
    })
    bucket_mgr = FakeBucketManager()
    event_loop_thread_id = threading.get_ident()
    monkeypatch.setattr(system.sh, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(system.sh, "decay_engine", FakeDecayEngine())
    monkeypatch.setattr(system.sh, "embedding_engine", StandbyEmbeddingEngine())
    monkeypatch.setattr(system.sh, "github_sync_instance", FakeGithubSync())
    monkeypatch.setattr(system.sh, "version", "2.4.8")
    monkeypatch.setattr(system.sh, "repo_root", str(tmp_path))
    monkeypatch.setattr(system.sh, "_is_setup_needed", lambda: False)
    # Keep this unit test independent of the host running pytest. The test
    # exercises diagnostics fields, not Docker mount detection.
    monkeypatch.setattr(system.sh, "in_docker", lambda: False)

    (buckets_dir / ".tunnel_config.json").write_text(
        json.dumps({"token": "configured", "auto_start": True}),
        encoding="utf-8",
    )

    payload = await system.build_system_diagnostics()
    by_id = {check["id"]: check for check in payload["checks"]}

    assert bucket_mgr.ledger_thread_id is not None
    assert bucket_mgr.ledger_thread_id != event_loop_thread_id
    assert not (buckets_dir / ".ombrebrain-v3").exists()
    assert list(buckets_dir.glob(".ombre_diagnostics_probe_*")) == []
    assert payload["ok"] is False
    assert payload["summary"]["error"] >= 2
    assert by_id["storage"]["status"] == "ok"
    assert by_id["auth"]["status"] == "error"
    assert by_id["auth"]["details"]["public_exposure_risk"] is True
    assert "隧道" in by_id["auth"]["message"]
    assert by_id["ledger"]["status"] == "ok"
    assert by_id["ledger"]["details"]["canonical"] is False
    assert by_id["ledger"]["details"]["valid_events"] == 2
    assert by_id["ledger"]["details"]["trace_catalog_projection"]["lag"] == 0
    assert by_id["ledger"]["details"]["sqlite_projection"]["projection_name"] == "trace_catalog_sqlite"
    assert by_id["ledger"]["details"]["sqlite_projection"]["lag"] == 0
    assert by_id["ledger"]["details"]["replay"]["ok"] is True
    assert by_id["ledger"]["details"]["replay"]["event_count"] == 2
    assert by_id["observability_boundary"]["status"] == "ok"
    obs_details = by_id["observability_boundary"]["details"]
    assert obs_details["report"]["ok"] is True
    assert obs_details["report"]["metric_count"] >= 4
    assert {item["name"] for item in obs_details["metrics"]} >= {
        "trace_count_by_state",
        "archive_growth",
        "projection_lag",
        "tombstone_count",
    }
    assert by_id["public_tool_manifest"]["status"] == "ok"
    tool_details = by_id["public_tool_manifest"]["details"]
    assert tool_details["report"]["ok"] is True
    assert set(tool_details["tool_names"]) >= {"breath", "hold", "grow", "trace", "dream", "pulse"}
    assert "release" in tool_details["compatibility_tool_names"]
    assert by_id["adr_requirements"]["status"] == "ok"
    adr_details = by_id["adr_requirements"]["details"]
    assert adr_details["report"]["ok"] is True
    assert adr_details["report"]["document_count"] == 1
    assert adr_details["documents"][0]["path"].endswith("ADR-0001-diagnostics.md")
    assert by_id["code_standards"]["status"] == "ok"
    code_details = by_id["code_standards"]["details"]
    assert code_details["report"]["ok"] is True
    assert set(code_details["report"]["artifacts"]) >= {
        "src/server.py",
        "src/web/system.py",
        "src/web/search.py",
        "src/ombrebrain/policy/surfacing.py",
    }
    assert by_id["red_lines"]["status"] == "ok"
    red_details = by_id["red_lines"]["details"]
    assert red_details["report"]["ok"] is True
    assert red_details["report"]["violation_count"] == 0
    assert set(red_details["report"]["features"]) >= {
        "system_diagnostics",
        "public_tool_manifest",
        "code_standards",
    }
    assert by_id["crash_recovery"]["status"] == "ok"
    crash_details = by_id["crash_recovery"]["details"]
    assert crash_details["decision_count"] == 3
    assert all(item["ok"] for item in crash_details["decisions"])
    assert {item["path_name"] for item in crash_details["decisions"]} == {"write", "read", "recovery_plan"}
    assert by_id["migration_preservation"]["status"] == "ok"
    migration_details = by_id["migration_preservation"]["details"]
    assert migration_details["decision_count"] == 2
    assert all(item["ok"] for item in migration_details["decisions"])
    assert {item["decision_name"] for item in migration_details["decisions"]} == {"records", "phase_plan"}
    assert by_id["surface_context"]["status"] == "ok"
    surface_context_details = by_id["surface_context"]["details"]
    assert surface_context_details["compiler_version"] == "surface-context.v1"
    assert surface_context_details["item_count"] == 1
    assert surface_context_details["items"][0]["instructional_force"] == "none"
    assert surface_context_details["items"][0]["may_control_reasoning"] is False
    assert surface_context_details["items"][0]["redactions"]


def test_writable_probe_uses_unique_files_and_always_cleans_up(monkeypatch, tmp_path):
    legacy_probe = tmp_path / ".ombre_diagnostics_probe"
    legacy_probe.write_text("do-not-overwrite", encoding="utf-8")
    observed_paths = []
    observed_lock = threading.Lock()
    real_mkstemp = system.tempfile.mkstemp

    def recording_mkstemp(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        with observed_lock:
            observed_paths.append(path)
        return fd, path

    monkeypatch.setattr(system.tempfile, "mkstemp", recording_mkstemp)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda _index: system._probe_writable_dir(str(tmp_path)), range(16))
        )

    assert results == [(True, "")] * 16
    assert len(observed_paths) == 16
    assert len(set(observed_paths)) == 16
    assert all(not system.os.path.exists(path) for path in observed_paths)
    assert legacy_probe.read_text(encoding="utf-8") == "do-not-overwrite"


def test_writable_probe_cleans_up_when_opening_the_probe_fails(monkeypatch, tmp_path):
    observed_paths = []
    real_mkstemp = system.tempfile.mkstemp

    def recording_mkstemp(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        observed_paths.append(path)
        return fd, path

    def fail_fdopen(*_args, **_kwargs):
        raise OSError("synthetic probe failure")

    monkeypatch.setattr(system.tempfile, "mkstemp", recording_mkstemp)
    monkeypatch.setattr(system.os, "fdopen", fail_fdopen)

    writable, error = system._probe_writable_dir(str(tmp_path))

    assert writable is False
    assert "synthetic probe failure" in error
    assert len(observed_paths) == 1
    assert not system.os.path.exists(observed_paths[0])


@pytest.mark.asyncio
async def test_system_diagnostics_route_requires_auth_and_returns_payload(monkeypatch):
    expected = {
        "ok": True,
        "summary": {"ok": 1, "warning": 0, "error": 0},
        "checks": [{"id": "runtime", "label": "运行时", "status": "ok", "message": "ready", "details": {}}],
    }

    async def fake_build():
        return expected

    monkeypatch.setattr(system.sh, "_require_auth", lambda request: None)
    monkeypatch.setattr(system, "build_system_diagnostics", fake_build, raising=False)

    mcp = FakeMCP()
    system.register(mcp)

    response = await mcp.routes[("GET", "/api/system/diagnostics")](object())
    payload = json.loads(response.body)

    assert payload == expected
