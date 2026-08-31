"""安全部署模式、首次向导路由和独立页面的回归测试。"""

import json
from pathlib import Path
from collections.abc import Callable
import shutil
import subprocess
from typing import Any

import pytest
import yaml

from ombrebrain.security.deployment_profile import (
    assess_mcp_network_safety,
    build_profile_patch,
    enforce_mcp_network_guard,
    effective_configuration_report,
    insecure_mcp_override_enabled,
    is_loopback_bind_host,
    mcp_network_safety_issue,
    normalize_public_https_origin,
    profile_catalog,
    validate_profile_patch,
)
from server_app import HTTPRuntimeSettings
import web.onboarding as onboarding
import web.config_api as config_api
import web.oauth as oauth
import utils


class FakeMCP:
    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Any] = {}

    def custom_route(self, path: str, methods: list[str]) -> Callable[[Any], Any]:
        def decorator(handler: Any) -> Any:
            for method in methods:
                self.routes[(method, path)] = handler
            return handler
        return decorator


class JsonRequest:
    def __init__(self, body: dict[str, Any] | None = None) -> None:
        self._body = body or {}
        self.headers: dict[str, str] = {}
        self.query_params: dict[str, str] = {}
        self.cookies: dict[str, str] = {}

    async def json(self) -> dict[str, Any]:
        return self._body


def _payload(response: Any) -> dict[str, Any]:
    return json.loads(response.body.decode("utf-8"))


def _onboarding_section(start_marker: str, end_marker: str) -> str:
    html = (Path(__file__).resolve().parents[1] / "frontend" / "onboarding.html").read_text(
        encoding="utf-8"
    )
    start = html.index(start_marker)
    end = html.index(end_marker, start)
    return html[start:end]


def test_profile_defaults_keep_local_and_public_authenticated() -> None:
    local = build_profile_patch("local")
    public = build_profile_patch("public_secure", {"public_url": "https://ob.example"})

    assert local["transport"] == "streamable-http"
    assert local["mcp_require_auth"] is True
    assert public["mcp_require_auth"] is True
    assert public["mcp_auth_mode"] == "oauth"
    assert validate_profile_patch(local) == []
    assert validate_profile_patch(public) == []
    catalog_local = next(item for item in profile_catalog() if item["id"] == "local")
    assert catalog_local["defaults"]["mcp_require_auth"] is True


def test_public_profile_allows_explicit_hybrid_because_oauth_remains_available() -> None:
    patch = build_profile_patch(
        "public_secure", {"public_url": "https://ob.example"}
    )
    patch["mcp_auth_mode"] = "hybrid"

    assert validate_profile_patch(patch) == []

    patch["mcp_auth_mode"] = "token"
    assert "公网安全模式必须包含 OAuth 鉴权（oauth 或 hybrid）" in validate_profile_patch(
        patch
    )


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "127.42.0.8", "::1", "[::1]", "::ffff:127.0.0.1"],
)
def test_loopback_classifier_accepts_only_explicit_loopback_hosts(host: str) -> None:
    assert is_loopback_bind_host(host) is True


@pytest.mark.parametrize(
    "host",
    [
        "",
        "0.0.0.0",
        "::",
        "192.168.1.2",
        "10.0.0.3",
        "localhost",
        "LOCALHOST.",
        "ob.local",
        "localhost.example",
    ],
)
def test_loopback_classifier_rejects_wildcard_lan_and_unknown_hosts(host: str) -> None:
    assert is_loopback_bind_host(host) is False


@pytest.mark.parametrize("transport", ["streamable-http"])
def test_network_mcp_without_auth_requires_a_confirmed_loopback_boundary(
    transport: str,
) -> None:
    config = {"transport": transport, "mcp_require_auth": False}

    safe = assess_mcp_network_safety(
        config,
        environment={"OMBRE_BIND_HOST": "127.0.0.1"},
    )
    wildcard = assess_mcp_network_safety(
        config,
        environment={"OMBRE_BIND_HOST": "0.0.0.0"},
    )

    assert safe["loopback_only"] is True
    assert safe["guard_required"] is False
    assert wildcard["loopback_only"] is False
    assert wildcard["guard_required"] is True
    assert "0.0.0.0" in mcp_network_safety_issue(wildcard)


@pytest.mark.parametrize(
    ("environment", "guard_required"),
    [
        ({"OMBRE_BIND_ADDRESS": "127.0.0.1"}, False),
        ({"OMBRE_BIND_ADDRESS": "::1"}, False),
        ({"OMBRE_BIND_ADDRESS": "0.0.0.0"}, True),
        ({"OMBRE_BIND_ADDRESS": "192.168.1.20"}, True),
        ({}, True),
        ({"OMBRE_BIND_HOST": "127.0.0.1"}, False),
    ],
)
def test_docker_boundary_fails_closed_when_host_binding_is_unknown_or_non_loopback(
    environment: dict[str, str], guard_required: bool
) -> None:
    decision = assess_mcp_network_safety(
        {"transport": "streamable-http", "mcp_require_auth": False},
        environment=environment,
        in_docker=True,
    )

    assert decision["guard_required"] is guard_required


@pytest.mark.parametrize("value", ["1", "yes", "on", "TRUE ", "false", ""])
def test_insecure_override_accepts_only_explicit_true(value: str) -> None:
    expected = value.strip().lower() == "true"
    assert insecure_mcp_override_enabled({"OMBRE_ALLOW_INSECURE_MCP": value}) is expected


def test_explicit_override_is_reported_but_does_not_trigger_guard() -> None:
    decision = assess_mcp_network_safety(
        {"transport": "streamable-http", "mcp_require_auth": False},
        environment={
            "OMBRE_BIND_HOST": "0.0.0.0",
            "OMBRE_ALLOW_INSECURE_MCP": "true",
        },
    )

    assert decision["override_active"] is True
    assert decision["guard_required"] is False
    assert mcp_network_safety_issue(decision) == ""


def test_startup_network_check_preserves_explicit_open_access_and_diagnostics() -> None:
    runtime = {"transport": "streamable-http", "mcp_require_auth": False}

    decision = enforce_mcp_network_guard(
        runtime,
        environment={"OMBRE_BIND_HOST": "0.0.0.0"},
    )

    assert runtime["mcp_require_auth"] is False
    assert decision["guard_required"] is True
    assert decision["guard_active"] is False
    assert runtime["_mcp_network_security"] == decision

    report = effective_configuration_report(
        runtime,
        {"transport": "streamable-http", "mcp_require_auth": False},
        environment={"OMBRE_BIND_HOST": "0.0.0.0"},
    )
    assert report["saved"]["mcp_require_auth"] is False
    assert report["effective"]["mcp_require_auth"] is False
    assert report["mcp_network_security"]["guard_required"] is True
    assert report["mcp_network_security"]["guard_active"] is False
    assert report["restart_required"] is False

    repaired_report = effective_configuration_report(
        runtime,
        {"transport": "streamable-http", "mcp_require_auth": True},
        environment={"OMBRE_BIND_HOST": "0.0.0.0"},
    )
    assert repaired_report["saved"]["mcp_require_auth"] is True
    assert repaired_report["effective"]["mcp_require_auth"] is False
    assert repaired_report["mcp_network_security"]["guard_active"] is False
    assert repaired_report["restart_required"] is True

    platform_managed_report = effective_configuration_report(
        runtime,
        {"transport": "streamable-http", "mcp_require_auth": True},
        environment={
            "OMBRE_BIND_HOST": "0.0.0.0",
            "OMBRE_MCP_REQUIRE_AUTH": "false",
        },
    )
    assert platform_managed_report["saved"]["mcp_require_auth"] is True
    assert platform_managed_report["effective"]["mcp_require_auth"] is False
    assert platform_managed_report["mcp_network_security"]["guard_active"] is False
    assert platform_managed_report["mcp_network_security"]["auth_environment_override"] is True
    assert platform_managed_report["overrides"] == [{
        "env": "OMBRE_MCP_REQUIRE_AUTH",
        "field": "mcp_require_auth",
        "value": "false",
    }]
    assert platform_managed_report["restart_required"] is False


def test_explicit_open_config_drives_mcp_middleware_and_oauth_from_one_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = {
        "transport": "streamable-http",
        "mcp_require_auth": False,
        "mcp_auth_mode": "oauth",
    }
    enforce_mcp_network_guard(
        runtime,
        environment={"OMBRE_BIND_HOST": "0.0.0.0"},
    )
    monkeypatch.setattr(oauth.sh, "config", runtime)

    assert HTTPRuntimeSettings.from_config(runtime).auth_required is False
    assert oauth._oauth_required_from_config() is False


def test_system_diagnostics_directs_platform_managed_guard_to_the_platform() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "src" / "web" / "system.py"
    ).read_text(encoding="utf-8")

    assert 'network_security.get("auth_environment_override")' in source
    assert "仅在 Dashboard 重复保存不会覆盖平台环境变量" in source
    assert "OMBRE_ALLOW_INSECURE_MCP，然后改用 OAuth 或静态 Token" in source


def test_authenticated_or_stdio_mcp_never_needs_the_network_guard() -> None:
    authenticated = assess_mcp_network_safety(
        {"transport": "streamable-http", "mcp_require_auth": True},
        environment={"OMBRE_BIND_HOST": "0.0.0.0"},
    )
    stdio = assess_mcp_network_safety(
        {"transport": "stdio", "mcp_require_auth": False},
        environment={},
    )

    assert authenticated["guard_required"] is False
    assert stdio["guard_required"] is False


def test_public_profile_rejects_non_https_and_cannot_disable_oauth() -> None:
    patch = build_profile_patch("public_secure")
    patch["mcp_require_auth"] = False
    patch["deployment"]["public_url"] = "http://ob.example"

    issues = validate_profile_patch(patch)

    assert "公网安全模式不能关闭 OAuth" in issues
    assert "公网地址必须是 HTTPS 域名或完整的 /mcp 地址" in issues


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("OB.Example", "https://ob.example"),
        ("https://OB.Example:443/", "https://ob.example"),
        ("https://ob.example:8443/mcp", "https://ob.example:8443"),
        ("https://[2001:db8::1]/mcp/", "https://[2001:db8::1]"),
    ],
)
def test_public_address_normalizes_domain_or_mcp_url_to_https_origin(
    value: str, expected: str
) -> None:
    assert normalize_public_https_origin(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://ob.example",
        "https://user:pass@ob.example",
        "https://ob.example/other",
        "https://ob.example/mcp?token=secret",
        "https://ob.example/#fragment",
        "https://ob_example",
        "https://ob.example\\@evil.example",
    ],
)
def test_public_address_rejects_unsafe_or_ambiguous_values(value: str) -> None:
    assert normalize_public_https_origin(value) == ""


def test_effective_report_exposes_environment_override_without_hiding_saved_value() -> None:
    report = effective_configuration_report(
        {"transport": "streamable-http", "mcp_require_auth": False, "buckets_dir": "/data"},
        {"transport": "streamable-http", "mcp_require_auth": True, "deployment": {"profile": "public_secure", "onboarding_completed": True}},
        environment={"OMBRE_MCP_REQUIRE_AUTH": "false"},
        config_path="/data/config.yaml",
        persistence={"persistent": True, "mode": "volume"},
    )

    assert report["saved"]["mcp_require_auth"] is True
    assert report["effective"]["mcp_require_auth"] is False
    # Restarting alone cannot defeat a platform-managed environment override.
    assert report["restart_required"] is False
    assert report["overrides"] == [{"env": "OMBRE_MCP_REQUIRE_AUTH", "field": "mcp_require_auth", "value": "false"}]
    assert report["environment_sources"] == report["overrides"]


def test_effective_report_includes_public_url_in_restart_comparison() -> None:
    report = effective_configuration_report(
        {
            "transport": "streamable-http",
            "mcp_require_auth": True,
            "deployment": {"public_url": "https://old.example"},
        },
        {
            "transport": "streamable-http",
            "mcp_require_auth": True,
            "deployment": {
                "profile": "public_secure",
                "public_url": "https://new.example/mcp",
            },
        },
    )

    assert report["saved"]["public_url"] == "https://new.example"
    assert report["effective"]["public_url"] == "https://old.example"
    assert report["restart_required"] is True


def test_effective_report_includes_auth_mode_and_environment_override() -> None:
    report = effective_configuration_report(
        {
            "transport": "streamable-http",
            "mcp_require_auth": True,
            "mcp_auth_mode": "token",
            "deployment": {"public_url": "https://ob.example"},
        },
        {
            "transport": "streamable-http",
            "mcp_require_auth": True,
            "mcp_auth_mode": "oauth",
            "deployment": {"public_url": "https://ob.example"},
        },
        environment={"OMBRE_MCP_AUTH_MODE": "token"},
    )

    assert report["saved"]["mcp_auth_mode"] == "oauth"
    assert report["effective"]["mcp_auth_mode"] == "token"
    assert report["restart_required"] is True
    assert report["overrides"] == [
        {"env": "OMBRE_MCP_AUTH_MODE", "field": "mcp_auth_mode", "value": "token"}
    ]


def test_effective_report_preserves_hybrid_mode() -> None:
    report = effective_configuration_report(
        {
            "transport": "streamable-http",
            "mcp_require_auth": True,
            "mcp_auth_mode": "hybrid",
        },
        {
            "transport": "streamable-http",
            "mcp_require_auth": True,
            "mcp_auth_mode": "hybrid",
        },
    )

    assert report["saved"]["mcp_auth_mode"] == "hybrid"
    assert report["effective"]["mcp_auth_mode"] == "hybrid"


def test_effective_report_flags_manual_auth_configuration_without_onboarding() -> None:
    """用户没走 /onboarding，但已经在「MCP 连接」面板手动保存过鉴权——
    profile 仍是 unconfigured，但 manual_auth_configured 要能让诊断识别出
    这是一次主动选择，而不是从没配置过。"""
    report = effective_configuration_report(
        {"transport": "streamable-http", "mcp_require_auth": True},
        {"mcp_require_auth": True},
    )

    assert report["profile"] == "unconfigured"
    assert report["manual_auth_configured"] is True

    report_mode_only = effective_configuration_report(
        {"transport": "streamable-http", "mcp_require_auth": True, "mcp_auth_mode": "token"},
        {"mcp_auth_mode": "token"},
    )

    assert report_mode_only["manual_auth_configured"] is True


def test_effective_report_manual_auth_configured_is_false_for_fresh_install() -> None:
    report = effective_configuration_report(
        {"transport": "stdio", "mcp_require_auth": True},
        {},
    )

    assert report["profile"] == "unconfigured"
    assert report["manual_auth_configured"] is False


def test_effective_report_does_not_warn_for_matching_platform_defaults() -> None:
    report = effective_configuration_report(
        {"transport": "streamable-http", "mcp_require_auth": True, "buckets_dir": "/app/buckets"},
        {"transport": "streamable-http", "mcp_require_auth": True},
        environment={
            "OMBRE_TRANSPORT": "streamable-http",
            "OMBRE_CONFIG_PATH": "/app/buckets/config.yaml",
        },
    )

    assert report["overrides"] == []
    assert len(report["environment_sources"]) == 2
    assert report["restart_required"] is False


@pytest.mark.asyncio
async def test_onboarding_apply_preserves_unrelated_config_and_requires_auth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"merge_threshold": 82, "embedding": {"enabled": True}}), encoding="utf-8")
    monkeypatch.setattr(onboarding, "config_file_path", lambda: str(config_path))
    monkeypatch.setattr(utils, "config_file_path", lambda: str(config_path))
    monkeypatch.setattr(onboarding.sh, "_require_auth", lambda request: None)
    monkeypatch.setattr(onboarding.sh, "config", {"transport": "streamable-http", "mcp_require_auth": True, "buckets_dir": str(tmp_path)})
    monkeypatch.setattr(onboarding.sh, "data_dir_persistence", lambda path: {"persistent": True, "mode": "local", "note": "ok"})
    mcp = FakeMCP()
    onboarding.register(mcp)

    response = await mcp.routes[("POST", "/api/onboarding/apply")](JsonRequest({"profile": "public_secure", "options": {"public_url": "https://ob.example"}, "confirm": True}))
    data = _payload(response)
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert data["ok"] is True
    assert data["restart_required"] is True
    assert persisted["merge_threshold"] == 82
    assert persisted["embedding"] == {"enabled": True}
    assert persisted["mcp_require_auth"] is True
    assert persisted["deployment"]["profile"] == "public_secure"

    local_response = await mcp.routes[("POST", "/api/onboarding/apply")](
        JsonRequest({"profile": "local", "options": {}, "confirm": True})
    )
    persisted_local = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert local_response.status_code == 200
    assert persisted_local["deployment"]["profile"] == "local"
    assert persisted_local["mcp_require_auth"] is True
    assert "public_url" not in persisted_local["deployment"]


@pytest.mark.asyncio
async def test_onboarding_rejects_unsafe_advanced_no_auth_before_writing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.yaml"
    original = {"merge_threshold": 81}
    config_path.write_text(yaml.safe_dump(original), encoding="utf-8")
    monkeypatch.setattr(onboarding, "config_file_path", lambda: str(config_path))
    monkeypatch.setattr(onboarding.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(
        onboarding.sh,
        "config",
        {"transport": "streamable-http", "mcp_require_auth": True},
    )
    monkeypatch.setattr(onboarding.sh, "in_docker", lambda: False)
    monkeypatch.setenv("OMBRE_BIND_HOST", "0.0.0.0")
    monkeypatch.delenv("OMBRE_ALLOW_INSECURE_MCP", raising=False)
    mcp = FakeMCP()
    onboarding.register(mcp)
    request = JsonRequest({
        "profile": "advanced",
        "options": {"mcp_require_auth": False},
    })

    preflight = await mcp.routes[("POST", "/api/onboarding/preflight")](request)
    apply = await mcp.routes[("POST", "/api/onboarding/apply")](JsonRequest({
        "profile": "advanced",
        "options": {"mcp_require_auth": False},
        "confirm": True,
    }))

    preflight_payload = _payload(preflight)
    assert preflight_payload["ok"] is False
    assert preflight_payload["mcp_network_security"]["guard_required"] is True
    assert apply.status_code == 400
    assert yaml.safe_load(config_path.read_text(encoding="utf-8")) == original


@pytest.mark.asyncio
async def test_onboarding_allows_advanced_no_auth_on_explicit_loopback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(onboarding, "config_file_path", lambda: str(config_path))
    monkeypatch.setattr(utils, "config_file_path", lambda: str(config_path))
    monkeypatch.setattr(onboarding.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(
        onboarding.sh,
        "config",
        {"transport": "streamable-http", "mcp_require_auth": True},
    )
    monkeypatch.setattr(onboarding.sh, "in_docker", lambda: False)
    monkeypatch.setattr(
        onboarding.sh,
        "data_dir_persistence",
        lambda _path: {"persistent": True, "mode": "local", "note": "ok"},
    )
    monkeypatch.setenv("OMBRE_BIND_HOST", "127.0.0.1")
    monkeypatch.delenv("OMBRE_ALLOW_INSECURE_MCP", raising=False)
    mcp = FakeMCP()
    onboarding.register(mcp)

    response = await mcp.routes[("POST", "/api/onboarding/apply")](JsonRequest({
        "profile": "advanced",
        "options": {"mcp_require_auth": False},
        "confirm": True,
    }))

    assert response.status_code == 200
    assert yaml.safe_load(config_path.read_text(encoding="utf-8"))["mcp_require_auth"] is False


@pytest.mark.asyncio
async def test_onboarding_apply_uses_shared_atomic_config_writer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, Any]] = []

    def shared_writer(mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        # Simulate another Dashboard writer publishing a key immediately before
        # the onboarding turn acquires the shared lock.
        latest = {"github": {"repo": "owner/repo"}}
        mutate(latest)
        calls.append(latest)
        return latest

    monkeypatch.setattr(onboarding, "atomic_update_config_yaml", shared_writer)
    monkeypatch.setattr(onboarding, "config_file_path", lambda: str(tmp_path / "config.yaml"))
    monkeypatch.setattr(onboarding.sh, "_require_auth", lambda request: None)
    monkeypatch.setattr(
        onboarding.sh,
        "config",
        {"transport": "streamable-http", "mcp_require_auth": True, "buckets_dir": str(tmp_path)},
    )
    monkeypatch.setattr(
        onboarding.sh,
        "data_dir_persistence",
        lambda path: {"persistent": True, "mode": "local", "note": "ok"},
    )
    mcp = FakeMCP()
    onboarding.register(mcp)

    response = await mcp.routes[("POST", "/api/onboarding/apply")](
        JsonRequest(
            {
                "profile": "public_secure",
                "options": {"public_url": "ob.example/mcp"},
                "confirm": True,
            }
        )
    )

    assert response.status_code == 200
    assert calls[0]["github"] == {"repo": "owner/repo"}
    assert calls[0]["deployment"]["public_url"] == "https://ob.example"


@pytest.mark.asyncio
async def test_onboarding_apply_is_immediately_visible_as_saved_but_not_effective(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.yaml"
    runtime = {
        "transport": "stdio",
        "mcp_require_auth": False,
        "mcp_auth_mode": "token",
        "deployment": {
            "profile": "advanced",
            "public_url": "https://old.example",
        },
        "buckets_dir": str(tmp_path),
    }
    config_path.write_text(yaml.safe_dump(runtime), encoding="utf-8")
    monkeypatch.setattr(utils, "config_file_path", lambda: str(config_path))
    monkeypatch.setattr(onboarding, "config_file_path", lambda: str(config_path))
    monkeypatch.setattr(onboarding.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(onboarding.sh, "config", runtime)
    monkeypatch.setattr(onboarding.sh, "in_docker", lambda: False)
    monkeypatch.setattr(
        onboarding.sh,
        "data_dir_persistence",
        lambda _path: {"persistent": True, "mode": "local", "note": "ok"},
    )
    monkeypatch.delenv("OMBRE_MCP_TOKEN", raising=False)
    mcp = FakeMCP()
    config_api.register(mcp)
    onboarding.register(mcp)

    applied = await mcp.routes[("POST", "/api/onboarding/apply")](
        JsonRequest(
            {
                "profile": "public_secure",
                "options": {"public_url": "new.example/mcp"},
                "confirm": True,
            }
        )
    )
    dashboard = await mcp.routes[("GET", "/api/config")](JsonRequest())
    applied_payload = _payload(applied)
    dashboard_payload = _payload(dashboard)

    assert applied.status_code == 200
    assert applied_payload["report"]["saved"]["public_url"] == "https://new.example"
    assert applied_payload["report"]["effective"]["public_url"] == "https://old.example"
    assert applied_payload["report"]["restart_required"] is True
    assert dashboard_payload["deployment"] == {
        "public_url": "https://new.example",
        "public_url_effective": "https://old.example",
    }
    assert dashboard_payload["mcp_require_auth"] is True
    assert dashboard_payload["mcp_require_auth_effective"] is False
    assert dashboard_payload["mcp_auth_mode"] == "oauth"
    assert dashboard_payload["mcp_auth_mode_effective"] == "token"
    assert dashboard_payload["transport"] == "streamable-http"
    assert dashboard_payload["transport_effective"] == "stdio"
    assert dashboard_payload["restart_required"] is True


def test_onboarding_page_has_file_contract_and_safe_json_parser() -> None:
    text = Path("frontend/onboarding.html").read_text(encoding="utf-8")

    assert "onboarding.html — Ombre Brain 首次部署向导" in text
    assert "本机模式" not in text  # 模式文案来自后端单一目录，页面不维护第二份。
    assert "readJsonSafe" in text
    assert "/api/onboarding/preflight" in text
    assert "/api/onboarding/apply" in text
    assert "已保存公网地址" in text
    assert "当前生效公网地址" in text
    assert "已保存鉴权模式" in text
    assert "当前生效鉴权模式" in text

    dashboard = Path("frontend/dashboard.html").read_text(encoding="utf-8")
    assert 'href="/onboarding"' in dashboard
    assert "打开安全部署向导" in dashboard
    assert "saveMcpAddress()" in dashboard
    assert "deployment: {public_url: publicUrl}" in dashboard
    assert "(cfg.deployment || {}).public_url" in dashboard


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_onboarding_auth_status_bypasses_http_cache() -> None:
    boot_source = (
        _onboarding_section("async function readJsonSafe", "function optionsForSelection")
        + _onboarding_section("async function boot()", "document.getElementById('public-url')")
    )
    script = r"""
const fetchCalls = [];
const location = {href:'unchanged'};
async function fetch(url, options) {
  fetchCalls.push({url, options: options ?? null});
  if (url !== '/auth/status') throw new Error('unexpected fetch: ' + url);
  return {
    status: 200,
    async text() { return JSON.stringify({authenticated:false}); },
  };
}
""" + boot_source + r"""

(async function() {
  await boot();
  process.stdout.write(JSON.stringify({fetchCalls, locationHref:location.href}));
})().catch(error => {
  console.error(error);
  process.exit(1);
});
"""
    completed = subprocess.run(
        [shutil.which("node"), "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout)

    assert result == {
        "fetchCalls": [
            {"url": "/auth/status", "options": {"cache": "no-store"}},
        ],
        "locationHref": "/",
    }
