"""
========================================
deployment_profile.py — 部署模式与安全默认的纯领域规则
========================================

把“本机 / 公网安全 / 高级自定义”三种用户选择归一化为明确配置，并校验
公网匿名暴露、传输方式和 OAuth 等安全不变量。

不做什么：不读写文件、不注册 HTTP 路由、不修改环境变量、不重启服务。
对外暴露：profile_catalog()、build_profile_patch()、validate_profile_patch()、
assess_mcp_network_safety()、current_mcp_network_security()、
enforce_mcp_network_guard()、
effective_configuration_report()。
========================================
"""

from __future__ import annotations

import ipaddress
import os
from typing import Any, Mapping

from ombrebrain.security.public_origin import (
    configured_public_origin,
    normalize_public_origin,
)


PROFILE_LOCAL = "local"
PROFILE_PUBLIC = "public_secure"
PROFILE_ADVANCED = "advanced"
_PROFILE_NAMES = frozenset({PROFILE_LOCAL, PROFILE_PUBLIC, PROFILE_ADVANCED})
_NETWORK_TRANSPORTS = frozenset({"streamable-http"})
_MCP_NETWORK_SECURITY_KEY = "_mcp_network_security"


def normalize_public_https_origin(value: Any) -> str:
    """Apply public-deployment HTTPS policy around the shared URL parser."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    # The Dashboard explicitly invites a hostname.  Only an omitted scheme is
    # promoted; an explicit http:// value remains invalid rather than silently
    # changing what the operator entered.
    candidate = raw if "://" in raw else f"https://{raw}"
    normalized = normalize_public_origin(candidate, allow_mcp_endpoint=True)
    return normalized if normalized.startswith("https://") else ""


def profile_catalog() -> list[dict[str, Any]]:
    """返回前端可展示的三种模式；安全含义只在这里定义一次。"""
    return [
        {
            "id": PROFILE_LOCAL,
            "name": "本机模式",
            "description": "仅同一设备使用，默认保留 MCP 鉴权；局域网/NAS 推荐静态 Token。",
            "recommended_for": "本机回环连接",
            "defaults": {"transport": "streamable-http", "mcp_require_auth": True},
        },
        {
            "id": PROFILE_PUBLIC,
            "name": "公网安全模式",
            "description": "通过 HTTPS 域名远程连接，强制 OAuth 保护 MCP。",
            "recommended_for": "Zeabur、Render、Cloudflare Tunnel、公开域名",
            "defaults": {
                "transport": "streamable-http",
                "mcp_require_auth": True,
                "mcp_auth_mode": "oauth",
            },
        },
        {
            "id": PROFILE_ADVANCED,
            "name": "高级自定义",
            "description": "自行管理反向代理、外部鉴权和网络边界；系统持续显示风险。",
            "recommended_for": "已有安全网关或自定义客户端",
            "defaults": {},
        },
    ]


def normalize_profile(value: Any) -> str:
    """归一化部署模式标识；未知值显式报错，不静默猜测。"""
    profile = str(value or "").strip().lower()
    aliases = {
        "public": PROFILE_PUBLIC,
        "secure": PROFILE_PUBLIC,
        "public-secure": PROFILE_PUBLIC,
        "custom": PROFILE_ADVANCED,
    }
    profile = aliases.get(profile, profile)
    if profile not in _PROFILE_NAMES:
        raise ValueError("profile 必须是 local、public_secure 或 advanced")
    return profile


def _as_bool(value: Any, *, default: bool) -> bool:
    """严格解析向导布尔值，拒绝把字符串 false 当作真。"""
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _environment_bool_override(
    environment: Mapping[str, str], name: str
) -> bool | None:
    """返回可识别的环境布尔覆盖；非法值不视为有效覆盖。"""
    normalized = str(environment.get(name, "") or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def is_loopback_bind_host(value: Any) -> bool:
    """只把明确的 IPv4/IPv6 回环地址视为仅本机边界。

    未知主机名不能安全推断为回环；通配地址、局域网地址和空值都按非回环处理。
    """
    host = str(value or "").strip().lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    host = host.split("%", 1)[0]
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped and mapped.is_loopback)


def insecure_mcp_override_enabled(
    environment: Mapping[str, str] | None = None,
) -> bool:
    """高风险逃生阀只接受明确的 true，避免宽松布尔解析意外放行。"""
    env = environment if environment is not None else os.environ
    return str(env.get("OMBRE_ALLOW_INSECURE_MCP", "") or "").strip().lower() == "true"


def assess_mcp_network_safety(
    config: Mapping[str, Any],
    *,
    environment: Mapping[str, str] | None = None,
    in_docker: bool = False,
) -> dict[str, Any]:
    """评估免鉴权 MCP 是否被限制在可确认的回环边界内。"""
    env = environment if environment is not None else os.environ
    transport = str(config.get("transport") or "stdio").strip().lower()
    auth_required = _as_bool(config.get("mcp_require_auth"), default=True)
    # 这里读取容器内监听面的默认值并立即进入下方鉴权边界门禁，不负责启动监听。
    bind_host = str(
        env.get("OMBRE_BIND_HOST", "") or "0.0.0.0"  # nosec B104
    ).strip()
    external_bind = str(env.get("OMBRE_BIND_ADDRESS", "") or "").strip()
    allow_insecure = insecure_mcp_override_enabled(env)

    if in_docker and is_loopback_bind_host(bind_host):
        boundary_host = bind_host
        boundary_source = "OMBRE_BIND_HOST"
        boundary_known = True
    elif in_docker:
        boundary_host = external_bind
        boundary_source = "OMBRE_BIND_ADDRESS"
        boundary_known = bool(boundary_host)
    else:
        boundary_host = bind_host
        boundary_source = "OMBRE_BIND_HOST"
        boundary_known = bool(boundary_host)

    network_transport = transport in _NETWORK_TRANSPORTS
    loopback_only = boundary_known and is_loopback_bind_host(boundary_host)
    insecure_requested = network_transport and not auth_required
    override_active = insecure_requested and not loopback_only and allow_insecure
    guard_required = insecure_requested and not loopback_only and not allow_insecure

    if not network_transport:
        reason = "stdio 不开放网络 MCP 端点"
    elif auth_required:
        reason = "MCP 鉴权已开启"
    elif loopback_only:
        reason = "免鉴权 MCP 仅绑定已确认的回环边界"
    elif override_active:
        reason = "已通过 OMBRE_ALLOW_INSECURE_MCP 显式接受非回环免鉴权风险"
    elif boundary_known:
        reason = f"免鉴权 MCP 将暴露到非回环边界 {boundary_host}"
    else:
        reason = f"无法从 {boundary_source} 确认容器外部网络边界"

    return {
        "transport": transport,
        "auth_required": auth_required,
        "in_docker": bool(in_docker),
        "bind_host": bind_host,
        "external_bind_address": external_bind,
        "boundary_host": boundary_host,
        "boundary_source": boundary_source,
        "boundary_known": boundary_known,
        "loopback_only": loopback_only,
        "allow_insecure": allow_insecure,
        "insecure_requested": insecure_requested,
        "override_active": override_active,
        "guard_required": guard_required,
        "guard_active": False,
        "reason": reason,
    }


def mcp_network_safety_issue(decision: Mapping[str, Any]) -> str:
    """把安全判定转换为供 API 返回的阻断原因。"""
    if not decision.get("guard_required"):
        return ""
    boundary = str(decision.get("boundary_host") or "未声明")
    source = str(decision.get("boundary_source") or "网络配置")
    return (
        "关闭 MCP 鉴权仅允许用于已确认的本机回环边界；"
        f"当前 {source}={boundary}。请开启 OAuth/静态 Token，"
        "或仅在明确承担风险时设置 OMBRE_ALLOW_INSECURE_MCP=true。"
    )


def enforce_mcp_network_guard(
    runtime_config: dict[str, Any],
    *,
    environment: Mapping[str, str] | None = None,
    in_docker: bool = False,
) -> dict[str, Any]:
    """记录免鉴权网络风险，但不覆盖用户明确选择的运行配置。

    ``mcp_require_auth`` 是 MCP 中间件的唯一开关。风险评估仍供启动日志、
    Dashboard 和部署向导使用，但不能把已保存或环境变量中的 ``false``
    静默改回 ``true``，否则客户端只会看到与配置相矛盾的 401。
    """
    decision = assess_mcp_network_safety(
        runtime_config,
        environment=environment,
        in_docker=in_docker,
    )
    runtime_config[_MCP_NETWORK_SECURITY_KEY] = dict(decision)
    return decision


def current_mcp_network_security(
    runtime_config: Mapping[str, Any],
    *,
    desired_auth_required: Any | None = None,
    environment: Mapping[str, str] | None = None,
    in_docker: bool = False,
) -> dict[str, Any]:
    """结合启动快照与当前已保存值，返回不会陈旧的门禁状态。

    启动门禁会保留一份快照，用来解释旧的免鉴权配置为何在当前进程中
    实际启用了鉴权。用户随后把已保存值修正为开启鉴权时，这份历史快照
    不能继续被 UI 当作“尚未修复”；反之，原危险配置尚未修正时仍需保留
    ``guard_active``，避免把运行态安全收紧误报成普通的待重启差异。
    """
    env = environment if environment is not None else os.environ
    environment_auth = _environment_bool_override(env, "OMBRE_MCP_REQUIRE_AUTH")
    stored = runtime_config.get(_MCP_NETWORK_SECURITY_KEY)
    snapshot = dict(stored) if isinstance(stored, Mapping) else None
    if desired_auth_required is None:
        decision = snapshot or assess_mcp_network_safety(
            runtime_config,
            environment=env,
            in_docker=in_docker,
        )
        decision["auth_environment_override"] = environment_auth is not None
        decision["auth_environment_value"] = environment_auth
        return decision

    desired_auth = _as_bool(
        desired_auth_required,
        default=_as_bool(runtime_config.get("mcp_require_auth"), default=True),
    )
    if environment_auth is not None:
        # 平台环境变量与 load_config() 一样具有最终优先级。仅修改 config.yaml
        # 不能修复仍由平台注入 false 的旧部署，状态页必须继续显示门禁生效。
        desired_auth = environment_auth
    desired_config = dict(runtime_config)
    desired_config.pop(_MCP_NETWORK_SECURITY_KEY, None)
    desired_config["mcp_require_auth"] = desired_auth
    decision = assess_mcp_network_safety(
        desired_config,
        environment=env,
        in_docker=in_docker,
    )
    if (
        snapshot
        and snapshot.get("guard_active")
        and not desired_auth
        and decision.get("guard_required")
    ):
        decision["guard_active"] = True
    decision["auth_environment_override"] = environment_auth is not None
    decision["auth_environment_value"] = environment_auth
    return decision


def build_profile_patch(profile: Any, options: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """把用户选择转换成可写入 config.yaml 的最小补丁。"""
    normalized = normalize_profile(profile)
    opts = dict(options or {})
    if normalized == PROFILE_LOCAL:
        auth_required = True
    elif normalized == PROFILE_PUBLIC:
        auth_required = True
    else:
        auth_required = _as_bool(opts.get("mcp_require_auth"), default=True)
    transport = str(opts.get("transport") or "streamable-http").strip().lower()
    if transport in {"http", "streamable", "streamable_http", "streamablehttp"}:
        transport = "streamable-http"
    patch: dict[str, Any] = {
        "transport": transport,
        "mcp_require_auth": auth_required,
        "deployment": {
            "profile": normalized,
            "onboarding_completed": True,
        },
    }
    if normalized == PROFILE_PUBLIC:
        patch["mcp_auth_mode"] = "oauth"
    public_url = str(opts.get("public_url") or "").strip()
    if public_url:
        normalized_public_url = normalize_public_https_origin(public_url)
        if not normalized_public_url:
            raise ValueError("公网地址必须是 HTTPS 域名或完整的 /mcp 地址")
        patch["deployment"]["public_url"] = normalized_public_url
    return patch


def validate_profile_patch(patch: Mapping[str, Any]) -> list[str]:
    """返回阻止保存的安全问题；空列表表示可以落盘。"""
    deployment = patch.get("deployment")
    if not isinstance(deployment, Mapping):
        return ["缺少 deployment 配置"]
    try:
        profile = normalize_profile(deployment.get("profile"))
    except ValueError as exc:
        return [str(exc)]
    issues: list[str] = []
    transport = str(patch.get("transport") or "").strip().lower()
    auth_required = _as_bool(patch.get("mcp_require_auth"), default=True)
    auth_mode = str(patch.get("mcp_auth_mode") or "oauth").strip().lower()
    public_url = str(deployment.get("public_url") or "").strip()
    if transport not in {"streamable-http", "stdio"}:
        issues.append("transport 必须是 streamable-http 或 stdio")
    if profile == PROFILE_PUBLIC:
        if not auth_required:
            issues.append("公网安全模式不能关闭 OAuth")
        if auth_mode not in {"oauth", "hybrid"}:
            issues.append("公网安全模式必须包含 OAuth 鉴权（oauth 或 hybrid）")
        if public_url:
            if not normalize_public_https_origin(public_url):
                issues.append("公网地址必须是 HTTPS 域名或完整的 /mcp 地址")
    if profile == PROFILE_LOCAL and not auth_required:
        issues.append("本机模式默认保留 MCP 鉴权；免鉴权请使用高级模式并确认网络边界")
    if profile == PROFILE_LOCAL and public_url:
        issues.append("本机模式不能同时声明公网地址")
    return issues


def effective_configuration_report(
    runtime_config: Mapping[str, Any],
    persisted_config: Mapping[str, Any],
    *,
    environment: Mapping[str, str] | None = None,
    in_docker: bool = False,
    config_path: str = "",
    persistence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """生成“已保存值 / 实际值 / 环境覆盖”的单一报告。"""
    env = environment if environment is not None else os.environ
    deployment = persisted_config.get("deployment")
    persisted_deployment = deployment if isinstance(deployment, Mapping) else {}
    # 未走 /onboarding 向导、但已经在 Dashboard「MCP 鉴权」面板里手动保存过一次的用户，
    # config.yaml 里会显式出现 mcp_require_auth（或 mcp_auth_mode）键——这是他们做过
    # 主动选择的证据，不该被当成「从没配置过」持续提醒重新走向导。
    manual_auth_configured = (
        "mcp_require_auth" in persisted_config or "mcp_auth_mode" in persisted_config
    )
    effective_auth = _as_bool(runtime_config.get("mcp_require_auth"), default=True)
    saved_auth = (
        _as_bool(persisted_config.get("mcp_require_auth"), default=effective_auth)
        if "mcp_require_auth" in persisted_config
        else effective_auth
    )
    network_security = current_mcp_network_security(
        runtime_config,
        desired_auth_required=saved_auth,
        environment=env,
        in_docker=in_docker,
    )
    effective_auth_mode = str(runtime_config.get("mcp_auth_mode") or "oauth").strip().lower()
    if effective_auth_mode not in {"oauth", "token", "hybrid"}:
        effective_auth_mode = "oauth"
    saved_auth_mode = (
        str(persisted_config.get("mcp_auth_mode") or "oauth").strip().lower()
        if "mcp_auth_mode" in persisted_config
        else effective_auth_mode
    )
    if saved_auth_mode not in {"oauth", "token", "hybrid"}:
        saved_auth_mode = "oauth"
    environment_sources: list[dict[str, str]] = []
    source_map = {
        "OMBRE_MCP_REQUIRE_AUTH": "mcp_require_auth",
        "OMBRE_MCP_AUTH_MODE": "mcp_auth_mode",
        "OMBRE_TRANSPORT": "transport",
        "OMBRE_CONFIG_PATH": "config_path",
        "OMBRE_BUCKETS_DIR": "buckets_dir",
        "OMBRE_VAULT_DIR": "buckets_dir",
        "OMBRE_BIND_HOST": "bind_host",
        "OMBRE_BIND_ADDRESS": "external_bind_address",
        "OMBRE_ALLOW_INSECURE_MCP": "allow_insecure_mcp",
    }
    for env_name, field in source_map.items():
        value = str(env.get(env_name, "") or "").strip()
        if value:
            environment_sources.append({"env": env_name, "field": field, "value": value})
    effective_transport = str(runtime_config.get("transport") or "stdio")
    saved_transport = (
        str(persisted_config.get("transport") or effective_transport)
        if "transport" in persisted_config
        else effective_transport
    )
    effective_public_url = configured_public_origin(runtime_config)
    saved_public_url = (
        configured_public_origin(persisted_config)
        if isinstance(deployment, Mapping)
        else effective_public_url
    )
    overrides: list[dict[str, str]] = []
    for source in environment_sources:
        field = source["field"]
        if field == "mcp_require_auth":
            env_auth = _as_bool(source["value"], default=saved_auth)
            if env_auth != saved_auth:
                overrides.append(source)
        elif field == "mcp_auth_mode" and saved_auth_mode != effective_auth_mode:
            env_mode = str(source["value"] or "").strip().lower()
            if env_mode == effective_auth_mode:
                overrides.append(source)
        elif field == "transport" and saved_transport != effective_transport:
            overrides.append(source)
    return {
        "profile": str(persisted_deployment.get("profile") or "unconfigured"),
        "onboarding_completed": bool(persisted_deployment.get("onboarding_completed")),
        "manual_auth_configured": manual_auth_configured,
        "config_path": config_path,
        "saved": {
            "transport": saved_transport,
            "mcp_require_auth": saved_auth,
            "mcp_auth_mode": saved_auth_mode,
            "public_url": saved_public_url,
        },
        "effective": {
            "transport": effective_transport,
            "mcp_require_auth": effective_auth,
            "mcp_auth_mode": effective_auth_mode,
            "public_url": effective_public_url,
            "buckets_dir": str(runtime_config.get("buckets_dir") or ""),
            # Report-only: reflects server.py's own OMBRE_BIND_HOST default for
            # display in the diagnostics report; this module never opens a socket.
            "bind_host": network_security["bind_host"],
            "external_bind_address": network_security["external_bind_address"],
        },
        "mcp_network_security": network_security,
        "overrides": overrides,
        "environment_sources": environment_sources,
        "restart_required": (
            (
                saved_auth != effective_auth
                and not network_security.get("guard_active")
                and not network_security.get("auth_environment_override")
            )
            or saved_auth_mode != effective_auth_mode
            or saved_transport != effective_transport
            or saved_public_url != effective_public_url
        ),
        "persistence": dict(persistence or {}),
    }
