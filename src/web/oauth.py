"""
========================================
web/oauth.py — MCP 远程鉴权（OAuth 2.1 + PKCE）
========================================

MCP 客户端通过 HTTPS 连接 MCP 时走的 OAuth 流程：
动态注册 → 授权页（输 Dashboard 密码）→ 换 code → 换 Bearer token + refresh token。
token 落盘 <buckets_dir>/.dashboard_mcp_tokens.json，长期有效并支持刷新，
Docker 重启不强制重新授权。

server.py 的 MCP 鉴权中间件需要 _is_valid_mcp_token 来校验 /mcp(-extra) 的 Bearer，
故它对外可见。

对外暴露：
- register(mcp)：注册 /.well-known/* 与 /oauth/* 路由（并在注册时载入持久化 token）
- _is_valid_mcp_token：供 server.py 启动期的 _MCPAuthMiddleware 调用
========================================
"""

import os
import json as _json_lib
import secrets
import time as _time_mod
import urllib.parse as _urlparse
import base64 as _base64
from collections import OrderedDict as _OrderedDict, deque as _deque
import hashlib as _hashlib_oauth
import hmac as _hmac
import html as _html_escape
import ipaddress as _ipaddress
import re as _re
import threading as _threading

from starlette.requests import Request
from starlette.responses import Response

from ombrebrain.security.public_origin import (
    configured_public_origin,
    normalize_http_resource,
    normalize_public_origin,
)
from . import _shared as sh
from .auth import _run_public_password_verification
from tools import _identity

try:
    from utils import parse_bool  # type: ignore
except ImportError:  # pragma: no cover
    from ..utils import parse_bool  # type: ignore

logger = sh.logger

_oauth_clients: dict[str, dict] = {}
_oauth_codes: dict[str, dict] = {}    # code -> {client_id, redirect_uri, code_challenge, expires}
_mcp_tokens: dict[str, float] = {}    # token -> expiry timestamp
_mcp_token_resources: dict[str, str] = {}  # token -> canonical MCP resource
_mcp_token_callers: dict[str, str] = {}  # token -> authenticated local identity
_mcp_refresh_tokens: dict[str, dict] = {}  # refresh_token -> {expires, client_id, resource}

_OAUTH_CODE_TTL = 300               # 5 min
_MCP_TOKEN_TTL = 86400 * 30         # 30 天；避免 100 年秒数溢出部分客户端的 32-bit duration
_MCP_REFRESH_TOKEN_TTL = 86400 * 365
_MCP_SCOPE = "mcp"
_OAUTH_CLIENT_TTL = 86400 * 365
# An unauthenticated DCR entry is useful only while its user completes the
# authorization flow.  Keeping never-authorized clients for a year lets a
# public attacker permanently consume the bounded registry.
_OAUTH_CLIENT_PENDING_TTL = 3600
_MAX_OAUTH_CLIENTS = 1024
# 未授权注册应使用远小于长期已授权客户端的独立配额。与
# _MAX_OAUTH_CLIENTS 分开计数，保持既有已授权客户端容量语义。
_MAX_PENDING_OAUTH_CLIENTS = 128
_MAX_OAUTH_CODES = 1024
_MAX_REDIRECT_URIS = 10
_MAX_REDIRECT_URI_CHARS = 2048
_MAX_REDIRECT_URIS_TOTAL_CHARS = 4096
_MAX_CLIENT_NAME_CHARS = 200
_MAX_CLIENT_ID_CHARS = 256
_MAX_OAUTH_STATE_CHARS = 4096
_MAX_OAUTH_RESOURCE_CHARS = 2048
_MAX_OAUTH_SCOPE_CHARS = 64
_PKCE_PATTERN = _re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_FORBIDDEN_REDIRECT_SCHEMES = {
    "about", "blob", "data", "file", "ftp", "javascript", "vbscript"
}

# Dynamic client registration must remain public for RFC-compatible MCP
# clients, so protect it with a small per-source window plus a larger
# process-wide window.  The global window remains effective when a caller
# rotates addresses; all state is bounded and shared by every event loop.
_OAUTH_REGISTRATION_WINDOW_SECONDS = 60
_OAUTH_REGISTRATION_SOURCE_MAX = 10
_OAUTH_REGISTRATION_GLOBAL_MAX = 120
_OAUTH_REGISTRATION_MAX_TRACKED_SOURCES = 2048
_oauth_registration_source_attempts: _OrderedDict[str, _deque[float]] = _OrderedDict()
_oauth_registration_global_attempts: _deque[float] = _deque()
_oauth_registration_rate_lock = _threading.RLock()
_oauth_client_state_lock = _threading.RLock()
_oauth_grant_state_lock = _threading.RLock()


class OAuthPersistenceError(RuntimeError):
    """A grant mutation could not be committed to private storage."""


def _oauth_required_from_config() -> bool:
    """Snapshot the effective MCP auth mode used for this server process.

    OAuth 在 oauth 与 hybrid 模式可用；只有纯 token 模式会让 discovery、
    register、authorize、token 路由经 _oauth_not_found() 返回 404。
    """
    return (
        parse_bool(sh.config.get("mcp_require_auth", True), default=True)
        and str(sh.config.get("mcp_auth_mode", "oauth")).strip().lower()
        in ("oauth", "hybrid")
    )


def _oauth_not_found() -> Response:
    """Do not advertise an OAuth surface when this MCP server is public."""
    return Response(
        status_code=404,
        headers={"Cache-Control": "no-store"},
    )


def _first_forwarded(value: str) -> str:
    """Return the first proxy header value (RFC 7239 chains are comma-separated)."""
    return (value or "").split(",", 1)[0].strip()


def _public_base_url(
    request: Request, configured_origin: str | None = None
) -> str:
    """Return the externally-visible base URL, honoring Cloudflare/reverse-proxy headers."""
    if configured_origin is None:
        configured_origin = configured_public_origin(sh.config)
    normalized_configured = normalize_public_origin(configured_origin)
    if normalized_configured:
        return normalized_configured
    proto = sh._trusted_forwarded_value(request, "x-forwarded-proto").lower()
    if proto not in ("http", "https"):
        proto = request.url.scheme
    host = sh._trusted_forwarded_value(request, "x-forwarded-host")
    if not host:
        host = _first_forwarded(
            request.headers.get("host") or request.url.netloc
        )
    if (
        not host
        or len(host) > 255
        or any(char.isspace() or char in "/\\#" for char in host)
    ):
        host = request.url.netloc
    candidate = normalize_public_origin(f"{proto}://{host}")
    if candidate:
        return candidate
    return normalize_public_origin(
        f"{request.url.scheme}://{request.url.netloc}"
    )


def _reserve_oauth_registration(request: Request) -> int:
    """Atomically reserve one public dynamic-registration attempt.

    Returns zero when admitted, otherwise the number of seconds the caller
    should wait.  A thread lock is intentional: FastMCP can be mounted from
    more than one event loop, while an asyncio lock would protect only one.
    """
    now = _time_mod.time()
    window = max(1, int(_OAUTH_REGISTRATION_WINDOW_SECONDS))
    cutoff = now - window
    source = sh._client_key(request)

    with _oauth_registration_rate_lock:
        while (
            _oauth_registration_global_attempts
            and _oauth_registration_global_attempts[0] <= cutoff
        ):
            _oauth_registration_global_attempts.popleft()

        # Entries are kept in last-seen order.  Pruning from the front avoids
        # walking every attacker-created source on each request.
        while _oauth_registration_source_attempts:
            oldest_source = next(iter(_oauth_registration_source_attempts))
            oldest_attempts = _oauth_registration_source_attempts[oldest_source]
            if oldest_attempts and oldest_attempts[-1] > cutoff:
                break
            _oauth_registration_source_attempts.popitem(last=False)

        source_attempts = _oauth_registration_source_attempts.get(source)
        if source_attempts is None:
            source_attempts = _deque()
        else:
            while source_attempts and source_attempts[0] <= cutoff:
                source_attempts.popleft()

        source_limit = max(1, int(_OAUTH_REGISTRATION_SOURCE_MAX))
        global_limit = max(1, int(_OAUTH_REGISTRATION_GLOBAL_MAX))
        retry_after = 0
        if len(source_attempts) >= source_limit:
            retry_after = max(
                retry_after,
                max(1, int(source_attempts[0] + window - now) + 1),
            )
        if len(_oauth_registration_global_attempts) >= global_limit:
            retry_after = max(
                retry_after,
                max(
                    1,
                    int(
                        _oauth_registration_global_attempts[0]
                        + window
                        - now
                    )
                    + 1,
                ),
            )
        if retry_after:
            if source in _oauth_registration_source_attempts:
                _oauth_registration_source_attempts.move_to_end(source)
            return retry_after

        source_attempts.append(now)
        _oauth_registration_source_attempts[source] = source_attempts
        _oauth_registration_source_attempts.move_to_end(source)
        tracked_limit = max(1, int(_OAUTH_REGISTRATION_MAX_TRACKED_SOURCES))
        while len(_oauth_registration_source_attempts) > tracked_limit:
            _oauth_registration_source_attempts.popitem(last=False)
        _oauth_registration_global_attempts.append(now)
        return 0


def _cleanup_oauth_clients_locked(
    current: float,
    registry: dict[str, dict] | None = None,
) -> bool:
    """Remove invalid/expired clients while the client-state lock is held."""
    clients = _oauth_clients if registry is None else registry
    changed = False
    for client_id, data in list(clients.items()):
        if not isinstance(data, dict) or (
            "expires" in data and data.get("expires", 0) <= current
        ):
            clients.pop(client_id, None)
            changed = True
    return changed


def _evict_oldest_pending_client_locked(
    registry: dict[str, dict] | None = None,
) -> bool:
    """Free one DCR slot without invalidating an authorized client."""
    clients = _oauth_clients if registry is None else registry
    candidates = []
    for client_id, data in clients.items():
        if not isinstance(data, dict) or data.get("activated") is True:
            continue
        created_at = data.get("created_at", data.get("expires", 0))
        if not isinstance(created_at, (int, float)):
            created_at = 0
        candidates.append((float(created_at), client_id))
    if not candidates:
        return False
    _created_at, client_id = min(candidates, key=lambda item: item[0])
    clients.pop(client_id, None)
    return True


def _activate_oauth_client(client_id: str) -> bool:
    """Extend a DCR client only after the user successfully authorizes it."""
    with _oauth_client_state_lock:
        data = _oauth_clients.get(client_id)
        if not isinstance(data, dict):
            return False
        candidate = {
            key: dict(value)
            for key, value in _oauth_clients.items()
            if isinstance(key, str) and isinstance(value, dict)
        }
        candidate_data = candidate[client_id]
        now = _time_mod.time()
        candidate_data["activated"] = True
        candidate_data.setdefault("activated_at", now)
        candidate_data["last_used"] = now
        candidate_data["expires"] = now + _OAUTH_CLIENT_TTL
        _save_oauth_clients(candidate)
        _oauth_clients.clear()
        _oauth_clients.update(candidate)
        return True


def _cleanup_oauth_state(now: float | None = None) -> None:
    """Bound public OAuth state and discard expired entries opportunistically."""
    current = _time_mod.time() if now is None else now
    with _oauth_client_state_lock:
        _cleanup_oauth_clients_locked(current)
    with _oauth_grant_state_lock:
        for code, data in list(_oauth_codes.items()):
            if not isinstance(data, dict) or data.get("expires", 0) <= current:
                _oauth_codes.pop(code, None)
        for token, expiry in list(_mcp_tokens.items()):
            if not isinstance(expiry, (int, float)) or expiry <= current:
                _mcp_tokens.pop(token, None)
                _mcp_token_resources.pop(token, None)
        for token, data in list(_mcp_refresh_tokens.items()):
            if not isinstance(data, dict) or data.get("expires", 0) <= current:
                _mcp_refresh_tokens.pop(token, None)


def _valid_redirect_uri(value: object) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= _MAX_REDIRECT_URI_CHARS:
        return False
    try:
        parsed = _urlparse.urlsplit(value)
    except Exception:
        return False
    scheme = parsed.scheme.lower()
    if not scheme or parsed.fragment or scheme in _FORBIDDEN_REDIRECT_SCHEMES:
        return False
    if scheme == "https":
        return bool(parsed.netloc and parsed.hostname and not parsed.username and not parsed.password)
    if scheme == "http":
        if not parsed.netloc or not parsed.hostname or parsed.username or parsed.password:
            return False
        hostname = parsed.hostname.lower()
        if hostname == "localhost":
            return True
        try:
            return _ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            return False
    # RFC 8252 native clients may use a private-use URI scheme. It still must
    # be absolute and must not be one of the browser-executable schemes above.
    return bool(parsed.netloc or parsed.path)


def _normalize_client_registration(body: object) -> tuple[dict | None, str]:
    if not isinstance(body, dict):
        return None, "registration body must be a JSON object"
    redirect_uris = body.get("redirect_uris")
    if (
        not isinstance(redirect_uris, list)
        or not 1 <= len(redirect_uris) <= _MAX_REDIRECT_URIS
        or any(not _valid_redirect_uri(uri) for uri in redirect_uris)
    ):
        return None, "redirect_uris must contain 1-10 safe absolute callback URIs"
    if sum(len(uri) for uri in redirect_uris) > _MAX_REDIRECT_URIS_TOTAL_CHARS:
        return None, (
            "redirect_uris total length must not exceed "
            f"{_MAX_REDIRECT_URIS_TOTAL_CHARS} characters"
        )
    client_name = body.get("client_name", "MCP Client")
    if not isinstance(client_name, str):
        return None, "client_name must be a string"
    client_name = client_name.strip()[:_MAX_CLIENT_NAME_CHARS] or "MCP Client"
    return {
        "redirect_uris": list(dict.fromkeys(redirect_uris)),
        "client_name": client_name,
    }, ""


def _valid_scope(scope: object) -> bool:
    return isinstance(scope, str) and set(scope.split()) == {_MCP_SCOPE}


def _valid_pkce_value(value: object) -> bool:
    return isinstance(value, str) and bool(_PKCE_PATTERN.fullmatch(value))


def _normalize_resource(resource: str) -> str:
    """Normalize an absolute OAuth resource URI for stable equality checks."""
    return normalize_http_resource(resource)


def _mcp_resource(
    request: Request,
    requested: str = "",
    configured_origin: str | None = None,
) -> tuple[bool, str]:
    """Validate/bind RFC 8707 resource to this server's canonical /mcp endpoint."""
    base = _public_base_url(request, configured_origin)
    canonical = f"{base}/mcp"
    if not requested:
        return True, canonical
    if not isinstance(requested, str) or len(requested) > _MAX_OAUTH_RESOURCE_CHARS:
        return False, canonical
    normalized = _normalize_resource(requested)
    if normalized in (_normalize_resource(base), _normalize_resource(canonical)):
        return True, canonical
    return False, canonical


def _mcp_tokens_file() -> str:
    return os.path.join(sh.config["buckets_dir"], ".dashboard_mcp_tokens.json")


def _oauth_clients_file() -> str:
    return os.path.join(sh.config["buckets_dir"], ".oauth_clients.json")


def _load_oauth_clients() -> None:
    """Restore active, validated dynamic-client registrations from disk."""
    try:
        path = _oauth_clients_file()
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as handle:
            raw = _json_lib.load(handle)
        if not isinstance(raw, dict):
            raise ValueError("oauth client registry must be a JSON object")

        now = _time_mod.time()
        granted_client_ids = {
            str(data.get("client_id", ""))
            for data in _mcp_refresh_tokens.values()
            if isinstance(data, dict) and data.get("client_id")
        }
        restored: list[tuple[float, str, dict]] = []
        for client_id, data in raw.items():
            if not isinstance(client_id, str) or not isinstance(data, dict):
                continue
            expires = data.get("expires")
            registration, _ = _normalize_client_registration(data)
            if (
                registration is None
                or not isinstance(expires, (int, float))
                or expires <= now
            ):
                continue
            activated = data.get("activated") is True or client_id in granted_client_ids
            created_at = data.get("created_at")
            if not isinstance(created_at, (int, float)) or created_at <= 0:
                created_at = now
            effective_expiry = float(expires)
            if not activated:
                # Registrations written before this field existed are not
                # assumed to have user consent.  A matching persisted refresh
                # grant above safely upgrades genuinely authorized clients.
                effective_expiry = min(
                    effective_expiry,
                    float(created_at) + _OAUTH_CLIENT_PENDING_TTL,
                )
            if effective_expiry <= now:
                continue
            restored_data = {
                **registration,
                "created_at": float(created_at),
                "activated": activated,
                "expires": effective_expiry,
            }
            for timestamp_field in ("activated_at", "last_used"):
                timestamp = data.get(timestamp_field)
                if isinstance(timestamp, (int, float)) and timestamp > 0:
                    restored_data[timestamp_field] = float(timestamp)
            restored.append((effective_expiry, client_id, restored_data))

        # Prefer the registrations that remain valid longest if a corrupt or
        # hand-edited file exceeds the in-memory safety bound.
        restored.sort(reverse=True)
        with _oauth_client_state_lock:
            candidate = {
                client_id: data
                for _, client_id, data in restored[:_MAX_OAUTH_CLIENTS]
            }
            _oauth_clients.clear()
            _oauth_clients.update(candidate)
    except Exception as e:
        logger.warning(f"[oauth] failed to load oauth clients: {e}")


def _persist_oauth_client_state(clients: dict[str, dict]) -> None:
    """Durably persist one DCR candidate before publishing it in memory."""
    try:
        now = _time_mod.time()
        active = {
            client_id: dict(data)
            for client_id, data in clients.items()
            if isinstance(client_id, str)
            and isinstance(data, dict)
            and isinstance(data.get("expires"), (int, float))
            and data["expires"] > now
        }
        sh._atomic_write_private_json(_oauth_clients_file(), active)
    except Exception as e:
        raise OAuthPersistenceError(
            "failed to persist OAuth client registrations"
        ) from e


def _save_oauth_clients(clients: dict[str, dict] | None = None) -> None:
    """Persist a DCR candidate or the current registry; never swallow errors."""
    with _oauth_client_state_lock:
        _persist_oauth_client_state(
            _oauth_clients if clients is None else clients
        )


def _load_mcp_tokens() -> None:
    try:
        path = _mcp_tokens_file()
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            raw = _json_lib.load(f)
        now = _time_mod.time()
        if isinstance(raw, dict) and (
            "access_tokens" in raw or "refresh_tokens" in raw
        ):
            access_raw = raw.get("access_tokens", {})
            refresh_raw = raw.get("refresh_tokens", {})
        else:
            access_raw = raw
            refresh_raw = {}

        needs_migration = not (
            isinstance(raw, dict)
            and "access_tokens" in raw
            and "refresh_tokens" in raw
        )
        loaded_access: dict[str, float] = {}
        loaded_resources: dict[str, str] = {}
        loaded_callers: dict[str, str] = {}
        for tok, data in access_raw.items():
            if isinstance(data, (int, float)):
                exp = data
                resource = ""
                caller = ""
                needs_migration = True
            elif isinstance(data, dict):
                exp = data.get("expires")
                resource = str(data.get("resource", ""))
                caller = _identity.normalize_caller(data.get("caller"))
                if "resource" not in data or "caller" not in data:
                    needs_migration = True
            else:
                continue
            if isinstance(exp, (int, float)) and exp > now:
                loaded_access[tok] = exp
                if resource:
                    loaded_resources[tok] = resource
                if caller:
                    loaded_callers[tok] = caller
        loaded_refresh: dict[str, dict] = {}
        for tok, data in refresh_raw.items():
            if isinstance(data, (int, float)):
                exp = data
                client_id = ""
                needs_migration = True
            elif isinstance(data, dict):
                exp = data.get("expires")
                client_id = str(data.get("client_id", ""))
                if "resource" not in data or "caller" not in data:
                    needs_migration = True
            else:
                continue
            if isinstance(exp, (int, float)) and exp > now:
                loaded_refresh[tok] = {
                    "expires": exp,
                    "client_id": client_id,
                    "resource": str(data.get("resource", "")) if isinstance(data, dict) else "",
                    "caller": _identity.normalize_caller(data.get("caller")) if isinstance(data, dict) else "",
                }
        with _oauth_grant_state_lock:
            _replace_grant_state_locked(
                loaded_access, loaded_resources, loaded_callers, loaded_refresh
            )
        if needs_migration:
            _save_mcp_tokens()
    except Exception as e:
        logger.warning(f"[oauth] failed to load mcp tokens: {e}")


def _save_mcp_tokens() -> None:
    """Persist the current grant registry or raise; never report false success."""
    with sh._credential_state_guard():
        with _oauth_grant_state_lock:
            _persist_mcp_token_state(
                _mcp_tokens,
                _mcp_token_resources,
                _mcp_token_callers,
                _mcp_refresh_tokens,
            )


def _persist_mcp_token_state(
    access_tokens: dict[str, float],
    token_resources: dict[str, str],
    token_callers: dict[str, str],
    refresh_tokens: dict[str, dict],
) -> None:
    """Durably write one candidate grant state before it becomes visible."""
    try:
        path = _mcp_tokens_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        now = _time_mod.time()
        active = {
            tok: {
                "expires": exp,
                "resource": token_resources.get(tok, ""),
                "caller": _identity.normalize_caller(token_callers.get(tok, "")),
            }
            for tok, exp in access_tokens.items()
            if exp > now
        }
        active_refresh = {
            tok: dict(data) for tok, data in refresh_tokens.items()
            if isinstance(data, dict)
            and isinstance(data.get("expires"), (int, float))
            and data["expires"] > now
        }
        sh._atomic_write_private_json(
            path,
            {
                "access_tokens": active,
                "refresh_tokens": active_refresh,
            },
        )
    except Exception as e:
        raise OAuthPersistenceError("failed to persist OAuth grants") from e


def _replace_grant_state_locked(
    access_tokens: dict[str, float],
    token_resources: dict[str, str],
    token_callers: dict[str, str],
    refresh_tokens: dict[str, dict],
) -> None:
    _mcp_tokens.clear()
    _mcp_tokens.update(access_tokens)
    _mcp_token_resources.clear()
    _mcp_token_resources.update(token_resources)
    _mcp_token_callers.clear()
    _mcp_token_callers.update(token_callers)
    _mcp_refresh_tokens.clear()
    _mcp_refresh_tokens.update(refresh_tokens)


def _commit_authorization_code_exchange(
    code: str,
    expected_code: dict,
    token_resource: str,
) -> tuple[str, str] | None:
    """Atomically consume one code and commit its access/refresh grants."""
    with sh._credential_state_guard():
        with _oauth_grant_state_lock:
            current = _oauth_codes.get(code)
            if (
                not isinstance(current, dict)
                or current != expected_code
                or current.get("expires", 0) <= _time_mod.time()
            ):
                return None
            code_generation = current.get("credential_generation")
            if (
                isinstance(code_generation, int)
                and code_generation != sh._credential_generation_snapshot()
            ):
                _oauth_codes.pop(code, None)
                return None

            access_token = secrets.token_urlsafe(32)
            refresh_token = secrets.token_urlsafe(32)
            access_candidate = dict(_mcp_tokens)
            resource_candidate = dict(_mcp_token_resources)
            caller_candidate = dict(_mcp_token_callers)
            refresh_candidate = {
                token: dict(data)
                for token, data in _mcp_refresh_tokens.items()
                if isinstance(data, dict)
            }
            access_candidate[access_token] = _time_mod.time() + _MCP_TOKEN_TTL
            if token_resource:
                resource_candidate[access_token] = token_resource
            caller = _identity.normalize_caller(current.get("caller"))
            if caller:
                caller_candidate[access_token] = caller
            refresh_candidate[refresh_token] = {
                "expires": _time_mod.time() + _MCP_REFRESH_TOKEN_TTL,
                "client_id": str(current.get("client_id", "")),
                "resource": token_resource,
                "caller": caller,
            }
            _persist_mcp_token_state(
                access_candidate,
                resource_candidate,
                caller_candidate,
                refresh_candidate,
            )
            _replace_grant_state_locked(
                access_candidate,
                resource_candidate,
                caller_candidate,
                refresh_candidate,
            )
            _oauth_codes.pop(code, None)
            return access_token, refresh_token


def _commit_refresh_token_rotation(
    refresh_token: str,
    expected_refresh: dict,
    token_resource: str,
) -> tuple[str, str] | None:
    """Atomically rotate a refresh token and issue one access token."""
    with sh._credential_state_guard():
        with _oauth_grant_state_lock:
            current = _mcp_refresh_tokens.get(refresh_token)
            if (
                not isinstance(current, dict)
                or current != expected_refresh
                or current.get("expires", 0) <= _time_mod.time()
            ):
                return None

            access_token = secrets.token_urlsafe(32)
            replacement_refresh = secrets.token_urlsafe(32)
            access_candidate = dict(_mcp_tokens)
            resource_candidate = dict(_mcp_token_resources)
            caller_candidate = dict(_mcp_token_callers)
            refresh_candidate = {
                token: dict(data)
                for token, data in _mcp_refresh_tokens.items()
                if isinstance(data, dict)
            }
            refresh_candidate.pop(refresh_token, None)
            access_candidate[access_token] = _time_mod.time() + _MCP_TOKEN_TTL
            if token_resource:
                resource_candidate[access_token] = token_resource
            caller = _identity.normalize_caller(current.get("caller"))
            if caller:
                caller_candidate[access_token] = caller
            refresh_candidate[replacement_refresh] = {
                "expires": _time_mod.time() + _MCP_REFRESH_TOKEN_TTL,
                "client_id": str(current.get("client_id", "")),
                "resource": token_resource,
                "caller": caller,
            }
            _persist_mcp_token_state(
                access_candidate,
                resource_candidate,
                caller_candidate,
                refresh_candidate,
            )
            _replace_grant_state_locked(
                access_candidate,
                resource_candidate,
                caller_candidate,
                refresh_candidate,
            )
            return access_token, replacement_refresh


def _oauth_grant_generation_snapshot() -> int:
    return sh._credential_generation_snapshot()


def _store_authorization_code(
    code: str,
    code_data: dict,
    expected_generation: int | sh.CredentialProof,
) -> bool:
    """Publish a code only if no credential revocation raced its KDF."""
    with sh._credential_state_guard():
        if isinstance(expected_generation, sh.CredentialProof):
            if not sh._credential_proof_matches_locked(expected_generation):
                return False
            generation = expected_generation.generation
        else:
            generation = expected_generation
            if sh._credential_generation_snapshot() != generation:
                return False
        with _oauth_grant_state_lock:
            now = _time_mod.time()
            for existing_code, data in list(_oauth_codes.items()):
                if not isinstance(data, dict) or data.get("expires", 0) <= now:
                    _oauth_codes.pop(existing_code, None)
            if len(_oauth_codes) >= _MAX_OAUTH_CODES:
                return False
            stored = dict(code_data)
            stored["credential_generation"] = generation
            _oauth_codes[code] = stored
            return True


def revoke_all_mcp_grants() -> None:
    """Durably revoke persisted grants or raise without claiming success."""
    with sh._credential_state_guard():
        # Invalidate KDF proofs before attempting persistence.  Even when the
        # disk write fails, no authorization begun before this revocation may
        # publish a late code using a stale credential.
        sh._advance_credential_generation_locked()
        with _oauth_grant_state_lock:
            _oauth_codes.clear()
            _persist_mcp_token_state({}, {}, {}, {})
            _replace_grant_state_locked({}, {}, {}, {})


def _verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    if not _valid_pkce_value(code_verifier) or not _valid_pkce_value(code_challenge):
        return False
    digest = _hashlib_oauth.sha256(code_verifier.encode()).digest()
    computed = _base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return _hmac.compare_digest(computed, code_challenge)


def _is_valid_mcp_token(token: str, resource: str = "") -> bool:
    with sh._credential_state_guard():
        with _oauth_grant_state_lock:
            expiry = _mcp_tokens.get(token)
            if expiry is None:
                return False
            if _time_mod.time() > expiry:
                del _mcp_tokens[token]
                _mcp_token_resources.pop(token, None)
                _mcp_token_callers.pop(token, None)
                return False
            bound_resource = _mcp_token_resources.get(token, "")
            if resource and bound_resource:
                return _normalize_resource(resource) == _normalize_resource(
                    bound_resource
                )
            return True


def _caller_for_mcp_token(token: str, resource: str = "") -> str:
    """Return the caller bound to a valid OAuth token, if one exists."""
    if not _is_valid_mcp_token(token, resource=resource):
        return ""
    with _oauth_grant_state_lock:
        return _identity.normalize_caller(_mcp_token_callers.get(token))


def _mcp_token_identity(token: str, resource: str = "") -> tuple[bool, str]:
    """Return ``(valid, caller)`` while preserving identityless legacy grants."""
    if not _is_valid_mcp_token(token, resource=resource):
        return False, ""
    with _oauth_grant_state_lock:
        caller = _identity.normalize_caller(_mcp_token_callers.get(token))
    return True, caller


def _resolve_mcp_caller(scope: dict) -> tuple[str, str, str]:
    """Resolve caller as ``(canonical caller, source, raw value)``.

    A valid OAuth bearer grant is authoritative even when it predates caller
    binding.  That prevents a header or query parameter from upgrading an
    identityless legacy grant.
    """
    headers = {key.lower(): value for key, value in scope.get("headers", [])}
    auth = headers.get(b"authorization", b"").decode("latin-1", "replace")
    if auth.startswith("Bearer "):
        valid, caller = _mcp_token_identity(auth[7:])
        if valid:
            return caller, "token" if caller else "legacy_token", caller

    raw_header = headers.get(b"x-ob-caller", b"").decode("utf-8", "replace")
    if raw_header:
        return _identity.normalize_caller(raw_header), "header", raw_header

    try:
        query = _urlparse.parse_qs(
            scope.get("query_string", b"").decode("latin-1")
        )
        raw_query = (query.get("caller") or [""])[0]
    except Exception:
        raw_query = ""
    if raw_query:
        return _identity.normalize_caller(raw_query), "query", raw_query
    return "", "none", ""


def _is_valid_static_mcp_token(token: str, resource: str = "") -> bool:
    """根据 mcp_auth_mode=token/hybrid 的静态密钥校验 Token。

    每次调用都重新读取 sh.config / 环境变量，不使用启动快照，因此在
    Dashboard 重新生成 Token 后无需重启进程即可生效。resource 参数仅为兼容
    TokenValidator 签名而保留；静态 Token 不绑定单个资源。
    """
    if not token:
        return False
    configured = (
        os.environ.get("OMBRE_MCP_TOKEN", "").strip()
        or str(sh.config.get("mcp_token", "") or "").strip()
    )
    if not configured:
        return False
    # compare_digest(str, str) 遇非 ASCII 会抛 TypeError。先变成固定长度摘要，
    # 既兼容 Unicode 错误配置，也不通过长度差异提前返回。
    supplied_digest = _hashlib_oauth.sha256(token.encode("utf-8")).digest()
    configured_digest = _hashlib_oauth.sha256(configured.encode("utf-8")).digest()
    return _hmac.compare_digest(supplied_digest, configured_digest)


def _oauth_log_field(value: object, max_chars: int) -> str:
    """移除日志字段中的控制字符并限制长度。"""
    if max_chars <= 0:
        return ""
    return "".join(char for char in str(value) if char.isprintable())[:max_chars]


def _issue_mcp_access_token(resource: str = "", caller: str = "") -> str:
    # Pre-identity releases accepted caller as the sole positional argument.
    # Preserve that internal/test API without confusing canonical HTTP(S)
    # resource identifiers with local caller names.
    if not caller:
        positional_caller = _identity.normalize_caller(resource)
        if positional_caller:
            caller = positional_caller
            resource = ""
    _cleanup_oauth_state()
    with sh._credential_state_guard():
        with _oauth_grant_state_lock:
            token = secrets.token_urlsafe(32)
            _mcp_tokens[token] = _time_mod.time() + _MCP_TOKEN_TTL
            if resource:
                _mcp_token_resources[token] = resource
            normalized_caller = _identity.normalize_caller(caller)
            if normalized_caller:
                _mcp_token_callers[token] = normalized_caller
            return token


def _issue_mcp_refresh_token(
    client_id: str, resource: str = "", caller: str = ""
) -> str:
    _cleanup_oauth_state()
    with sh._credential_state_guard():
        with _oauth_grant_state_lock:
            refresh_token = secrets.token_urlsafe(32)
            _mcp_refresh_tokens[refresh_token] = {
                "expires": _time_mod.time() + _MCP_REFRESH_TOKEN_TTL,
                "client_id": client_id,
                "resource": resource,
                "caller": _identity.normalize_caller(caller),
            }
            return refresh_token


def _token_response(access_token: str, *, refresh_token: str | None = None) -> dict:
    payload = {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": _MCP_TOKEN_TTL,
        "scope": _MCP_SCOPE,
    }
    if refresh_token:
        with _oauth_grant_state_lock:
            refresh_data = dict(_mcp_refresh_tokens.get(refresh_token, {}))
        refresh_exp = refresh_data.get("expires")
        if isinstance(refresh_exp, (int, float)):
            payload["refresh_expires_in"] = max(0, int(refresh_exp - _time_mod.time()))
        payload["refresh_token"] = refresh_token
    return payload


def _validate_authorize_redirect(client_id: str, redirect_uri: str) -> tuple[bool, str]:
    """Validate OAuth dynamic client and exact redirect_uri before asking for a password."""
    _cleanup_oauth_state()
    if not client_id:
        return False, "missing client_id"
    if not isinstance(client_id, str) or len(client_id) > _MAX_CLIENT_ID_CHARS:
        return False, "invalid client_id"
    if not redirect_uri:
        return False, "missing redirect_uri"
    if not _valid_redirect_uri(redirect_uri):
        return False, "invalid redirect_uri"
    with _oauth_client_state_lock:
        client_info = _oauth_clients.get(client_id)
        if isinstance(client_info, dict):
            client_info = dict(client_info)
    if not client_info:
        return False, "unknown client_id"
    if redirect_uri not in (client_info.get("redirect_uris") or []):
        return False, "redirect_uri mismatch"
    return True, ""


def _oauth_authorize_html(
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    resource: str = "",
    scope: str = _MCP_SCOPE,
    caller: str = "",
    error: str = "",
) -> str:
    # This renderer also serves validation errors.  Bound every reflected
    # request field here so a future caller cannot turn an oversized public GET
    # into a much larger HTML response even if route-level validation regresses.
    client_id = client_id if isinstance(client_id, str) and len(client_id) <= _MAX_CLIENT_ID_CHARS else ""
    redirect_uri = (
        redirect_uri
        if isinstance(redirect_uri, str) and len(redirect_uri) <= _MAX_REDIRECT_URI_CHARS
        else ""
    )
    state = state if isinstance(state, str) and len(state) <= _MAX_OAUTH_STATE_CHARS else ""
    code_challenge = (
        code_challenge
        if isinstance(code_challenge, str) and len(code_challenge) <= 128
        else ""
    )
    resource = (
        resource
        if isinstance(resource, str) and len(resource) <= _MAX_OAUTH_RESOURCE_CHARS
        else ""
    )
    scope = scope if isinstance(scope, str) and len(scope) <= _MAX_OAUTH_SCOPE_CHARS else ""
    e = _html_escape.escape
    try:
        from utils import get_ai_name  # type: ignore
    except ImportError:  # pragma: no cover
        from ..utils import get_ai_name  # type: ignore
    ai_name = e(get_ai_name())
    with _oauth_client_state_lock:
        stored_client = _oauth_clients.get(client_id, {})
        client_info = dict(stored_client) if isinstance(stored_client, dict) else {}
    client_name = e(str(client_info.get("client_name") or "MCP Client"))
    callback = e(redirect_uri[:240])
    normalized_caller = _identity.normalize_caller(caller)
    caller_options = [
        ("", "不绑定本地身份（Handoff 将拒绝）"),
        ("cheng", "澄（cheng）"),
        ("huaiyin", "怀音（huaiyin）"),
        ("huaiyin_cc", "怀音 CC（huaiyin_cc）"),
    ]
    options_html = "".join(
        f'<option value="{e(value)}"'
        f'{" selected" if value == normalized_caller else ""}>{e(label)}</option>'
        for value, label in caller_options
    )
    trace_id = secrets.token_hex(6)
    err_html = (
        f'<p class="error" id="form-error" role="alert">{e(error)}</p>'
        if error
        else ""
    )
    caller_error = error == "请选择列表中的身份，或选择“不绑定本地身份”"
    password_error = error in {"密码格式无效", "密码错误，请重试"}
    field_error = caller_error or password_error
    form_description = (
        ' aria-describedby="form-error"' if error and not field_error else ""
    )
    caller_describedby = "caller-help form-error" if caller_error else "caller-help"
    caller_invalid = ' aria-invalid="true"' if caller_error else ""
    caller_autofocus = " autofocus" if caller_error else ""
    password_describedby = ' aria-describedby="form-error"' if password_error else ""
    password_invalid = ' aria-invalid="true"' if password_error else ""
    password_autofocus = " autofocus" if password_error else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ombre Brain · 授权 MCP</title>
<style>
*{{box-sizing:border-box}}
body{{font-family:-apple-system,system-ui,sans-serif;background:#0f0f0f;color:#e0e0e0;
  display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
.card{{background:#1a1a1a;border:1px solid #333;border-radius:16px;padding:40px 36px;
  max-width:380px;width:90%;text-align:center}}
h1{{color:#c9a96e;font-family:Georgia,serif;font-size:24px;margin:0 0 6px}}
.sub{{color:#888;font-size:13px;margin:0 0 24px}}
label{{display:block;text-align:left;font-size:13px;margin:12px 0 6px}}
select,input[type=password]{{display:block;width:100%;padding:11px 14px;background:#111;
  border:1px solid #6b6b6b;border-radius:8px;color:#e0e0e0;font-size:14px;margin-bottom:14px}}
select:focus-visible,input:focus-visible,button:focus-visible{{outline:3px solid #f2ce8c;outline-offset:2px}}
button{{width:100%;padding:12px;background:#c9a96e;color:#0f0f0f;border:none;
  border-radius:8px;font-size:14px;font-weight:600;cursor:pointer}}
button:hover{{background:#d4b87a}}
button[aria-disabled=true]{{opacity:.65;cursor:wait}}
.submit-status{{display:none;color:#c9a96e;font-size:12px;margin-top:12px;line-height:1.5}}
.note{{color:#aaa;font-size:11px;margin-top:16px;line-height:1.6}}
.field-help{{color:#aaa;font-size:12px;text-align:left;margin:-8px 0 14px;line-height:1.5}}
.error{{color:#ff8a8a;font-size:13px;margin-top:12px}}
</style></head>
<body><div class="card">
<h1>◐ Ombre Brain</h1>
<p class="sub">授权 {ai_name} 连接 MCP</p>
<p class="note">请求方：{client_name}<br>回调：{callback}</p>
<form method="POST" id="oauth-form"{form_description}>
<input type="hidden" name="client_id" value="{e(client_id)}">
<input type="hidden" name="redirect_uri" value="{e(redirect_uri)}">
<input type="hidden" name="state" value="{e(state)}">
<input type="hidden" name="code_challenge" value="{e(code_challenge)}">
<input type="hidden" name="resource" value="{e(resource)}">
<input type="hidden" name="scope" value="{e(scope)}">
<input type="hidden" name="trace_id" value="{trace_id}">
<label for="caller">本地身份</label>
<select id="caller" name="caller" aria-describedby="{caller_describedby}"{caller_invalid}{caller_autofocus}>{options_html}</select>
<p class="field-help" id="caller-help">身份只绑定到本次签发的 OAuth Token，用于隔离 Handoff 记忆。</p>
<label for="password">Dashboard 密码</label>
<input type="password" id="password" name="password" autocomplete="current-password"{password_describedby}{password_invalid}{password_autofocus}>
<button type="submit" id="oauth-submit">授权并连接</button>
</form>
<p class="submit-status" id="submit-status" role="status" aria-live="polite"></p>
{err_html}
<p class="note">授权后 {ai_name} 将可使用 MCP 工具读写记忆。<br>Token 长期有效，并支持自动续期。<br>若工具调用失败，请在客户端断开重连，再重新点击此页授权即可。<br>诊断编号：{trace_id}</p>
</div>
<script>
(() => {{
  const form = document.getElementById('oauth-form');
  const button = document.getElementById('oauth-submit');
  const status = document.getElementById('submit-status');
  let submitting = false;
  form.addEventListener('submit', (event) => {{
    if (submitting) {{
      event.preventDefault();
      return;
    }}
    submitting = true;
    button.setAttribute('aria-disabled', 'true');
    button.textContent = '正在验证…';
    status.style.display = 'block';
    status.textContent = '正在验证密码并生成授权码，请勿关闭此页。';
    window.setTimeout(() => {{
      submitting = false;
      button.removeAttribute('aria-disabled');
      button.textContent = '重试授权';
      status.textContent = '等待超过 30 秒。请记下诊断编号 {trace_id}，再重试或查看服务端日志。';
    }}, 30000);
  }});
}})();
</script></body></html>"""


def register(mcp) -> None:
    """注册 /.well-known/* 与 /oauth/* 路由，并在装配时载入持久化 token。"""
    # Keep discovery aligned with the start-time middleware snapshot. Dashboard
    # config edits require a restart, so they must not change metadata early.
    oauth_required = _oauth_required_from_config()
    # OAuth routes and MCPAuthMiddleware both use startup config snapshots.
    # Saving a new public URL therefore cannot split token issuance from token
    # validation in the interval before the documented process restart.
    oauth_public_origin = configured_public_origin(sh.config)
    if oauth_required:
        _load_mcp_tokens()   # Docker 重启后恢复 token，不强制重新 OAuth
        _load_oauth_clients()

    @mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
    @mcp.custom_route("/.well-known/oauth-protected-resource/{resource_path:path}", methods=["GET"])
    async def oauth_protected_resource(request: Request) -> Response:
        from starlette.responses import JSONResponse
        if not oauth_required:
            return _oauth_not_found()

        base = _public_base_url(request, oauth_public_origin)
        # Ombre exposes one MCP endpoint. Do not let retired or invented paths
        # complete OAuth discovery and appear connected before failing at use.
        sub = str(request.path_params.get("resource_path", "") or "").strip("/")
        if sub and sub != "mcp":
            return _oauth_not_found()
        # The root discovery URL still describes the only real MCP resource;
        # it must never advertise the web origin itself as a protected MCP
        # endpoint.  Path-scoped discovery accepts /mcp only (checked above).
        resource = f"{base}/mcp"
        return JSONResponse({
            "resource": resource,
            "authorization_servers": [base],
            "bearer_methods_supported": ["header"],
            "scopes_supported": [_MCP_SCOPE],
        }, headers={"Cache-Control": "no-store"})

    @mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
    async def oauth_authorization_server(request: Request) -> Response:
        from starlette.responses import JSONResponse
        if not oauth_required:
            return _oauth_not_found()

        base = _public_base_url(request, oauth_public_origin)
        return JSONResponse({
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": ["mcp"],
        })

    @mcp.custom_route("/oauth/register", methods=["POST"])
    async def oauth_register(request: Request) -> Response:
        from starlette.responses import JSONResponse
        if not oauth_required:
            return _oauth_not_found()

        retry_after = _reserve_oauth_registration(request)
        if retry_after:
            return JSONResponse(
                {"error": "temporarily_unavailable"},
                status_code=429,
                headers={
                    "Retry-After": str(retry_after),
                    "Cache-Control": "no-store",
                },
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"error": "invalid_client_metadata", "error_description": "invalid JSON"},
                status_code=400,
            )
        registration, registration_error = _normalize_client_registration(body)
        if registration is None:
            return JSONResponse(
                {
                    "error": "invalid_client_metadata",
                    "error_description": registration_error,
                },
                status_code=400,
        )
        now = _time_mod.time()
        with _oauth_client_state_lock:
            candidate = {
                key: dict(value)
                for key, value in _oauth_clients.items()
                if isinstance(key, str) and isinstance(value, dict)
            }
            _cleanup_oauth_clients_locked(now, candidate)
            pending_count = sum(
                1
                for data in candidate.values()
                if data.get("activated") is not True
            )
            if pending_count >= max(1, int(_MAX_PENDING_OAUTH_CLIENTS)):
                return JSONResponse(
                    {"error": "temporarily_unavailable"},
                    status_code=429,
                    headers={
                        "Retry-After": "60",
                        "Cache-Control": "no-store",
                    },
                )
            if len(candidate) >= max(1, int(_MAX_OAUTH_CLIENTS)):
                if not _evict_oldest_pending_client_locked(candidate):
                    return JSONResponse(
                        {"error": "temporarily_unavailable"},
                        status_code=429,
                        headers={
                            "Retry-After": "60",
                            "Cache-Control": "no-store",
                        },
                    )
            client_id = secrets.token_urlsafe(16)
            candidate[client_id] = {
                **registration,
                "created_at": now,
                "activated": False,
                "expires": now + _OAUTH_CLIENT_PENDING_TTL,
            }
            try:
                _save_oauth_clients(candidate)
            except OAuthPersistenceError:
                return JSONResponse(
                    {"error": "temporarily_unavailable"},
                    status_code=503,
                    headers={
                        "Retry-After": "5",
                        "Cache-Control": "no-store",
                    },
                )
            _oauth_clients.clear()
            _oauth_clients.update(candidate)
        return JSONResponse({
            "client_id": client_id,
            "client_id_issued_at": int(now),
            "redirect_uris": registration["redirect_uris"],
            "client_name": registration["client_name"],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        }, status_code=201, headers={"Cache-Control": "no-store"})

    @mcp.custom_route("/oauth/authorize", methods=["GET", "POST"])
    async def oauth_authorize(request: Request) -> Response:
        from starlette.responses import HTMLResponse, RedirectResponse
        if not oauth_required:
            return _oauth_not_found()

        if request.method == "GET":
            p = dict(request.query_params)
            ok, err = _validate_authorize_redirect(
                p.get("client_id", ""), p.get("redirect_uri", "")
            )
            resource_ok, resource = _mcp_resource(
                request, p.get("resource", ""), oauth_public_origin
            )
            if ok and not resource_ok:
                ok, err = False, "resource 与当前 MCP 地址不匹配"
            if ok and p.get("response_type", "code") != "code":
                ok, err = False, "unsupported response_type"
            if ok and not _valid_scope(p.get("scope", _MCP_SCOPE)):
                ok, err = False, "unsupported scope"
            if ok and len(p.get("state", "")) > _MAX_OAUTH_STATE_CHARS:
                ok, err = False, "state is too long"
            if ok and not _valid_pkce_value(p.get("code_challenge")):
                ok, err = False, "invalid PKCE code_challenge"
            if ok and p.get("code_challenge_method", "S256") != "S256":
                ok, err = False, "仅支持 PKCE S256"
            if ok and sh._is_setup_needed():
                ok, err = False, "尚未设置 Dashboard 密码，请先打开 Dashboard 完成初始化"
            return HTMLResponse(_oauth_authorize_html(
                p.get("client_id", ""), p.get("redirect_uri", ""),
                p.get("state", ""), p.get("code_challenge", ""),
                resource=resource,
                scope=p.get("scope", _MCP_SCOPE),
                error=err,
            ), status_code=200 if ok else (503 if sh._is_setup_needed() else 400))
        # POST
        try:
            form = await request.form()
        except Exception:
            return HTMLResponse("Invalid authorization request", status_code=400)
        password     = str(form.get("password", ""))
        client_id    = str(form.get("client_id", ""))
        redirect_uri = str(form.get("redirect_uri", ""))
        state        = str(form.get("state", ""))
        code_challenge = str(form.get("code_challenge", ""))
        requested_resource = str(form.get("resource", ""))
        scope = str(form.get("scope", _MCP_SCOPE)) or _MCP_SCOPE
        raw_caller = str(form.get("caller", "")).strip()
        caller = _identity.normalize_caller(raw_caller)
        trace_id = _oauth_log_field(form.get("trace_id", ""), 32) or secrets.token_hex(6)
        logged_client_id = _oauth_log_field(client_id, 24)
        sh.logger.info(
            "op=oauth_authorize phase=post trace_id=%s client_id=%s",
            trace_id,
            logged_client_id,
        )

        ok, err = _validate_authorize_redirect(client_id, redirect_uri)
        resource_ok, resource = _mcp_resource(
            request, requested_resource, oauth_public_origin
        )
        if ok and not resource_ok:
            ok, err = False, "resource 与当前 MCP 地址不匹配"
        if ok and not _valid_scope(scope):
            ok, err = False, "unsupported scope"
        if ok and len(state) > _MAX_OAUTH_STATE_CHARS:
            ok, err = False, "state is too long"
        if ok and not _valid_pkce_value(code_challenge):
            ok, err = False, "invalid PKCE code_challenge"
        if ok and raw_caller and not caller:
            ok, err = False, "请选择列表中的身份，或选择“不绑定本地身份”"
        if not ok:
            return HTMLResponse(_oauth_authorize_html(
                client_id, redirect_uri, state, code_challenge,
                resource=resource, scope=scope, caller=caller, error=err
            ), status_code=400)
        if sh._is_setup_needed():
            return HTMLResponse(_oauth_authorize_html(
                client_id, redirect_uri, state, code_challenge,
                resource=resource, scope=scope, caller=caller,
                error="尚未设置 Dashboard 密码，请先打开 Dashboard 完成初始化",
            ), status_code=503)
        retry = sh._login_retry_after(request)
        if retry:
            return HTMLResponse(
                _oauth_authorize_html(
                    client_id, redirect_uri, state, code_challenge,
                    resource=resource, scope=scope, caller=caller,
                    error=f"尝试过于频繁，请 {retry} 秒后再试",
                ),
                status_code=429,
                headers={"Retry-After": str(retry)},
            )
        if len(password) > 1024:
            sh._record_login_failure(request)
            return HTMLResponse(
                _oauth_authorize_html(
                    client_id, redirect_uri, state, code_challenge,
                    resource=resource, scope=scope, caller=caller,
                    error="密码格式无效",
                ),
                status_code=400,
            )
        global_retry = sh._reserve_global_login_attempt()
        if global_retry:
            return HTMLResponse(
                _oauth_authorize_html(
                    client_id, redirect_uri, state, code_challenge,
                    resource=resource, scope=scope, caller=caller,
                    error=f"登录服务繁忙，请 {global_retry} 秒后重试",
                ),
                status_code=429,
                headers={"Retry-After": str(global_retry)},
            )
        verified, queued_retry = await _run_public_password_verification(
            request, sh._verify_password_for_rotation, password
        )
        if queued_retry:
            return HTMLResponse(
                _oauth_authorize_html(
                    client_id, redirect_uri, state, code_challenge,
                    resource=resource, scope=scope, caller=caller,
                    error=f"尝试过于频繁，请 {queued_retry} 秒后再试",
                ),
                status_code=429,
                headers={"Retry-After": str(queued_retry)},
            )
        if not verified:
            sh._record_login_failure(request)
            sh.logger.warning(
                "op=oauth_authorize phase=password_failed trace_id=%s client_id=%s",
                trace_id,
                logged_client_id,
            )
            return HTMLResponse(_oauth_authorize_html(
                client_id, redirect_uri, state, code_challenge,
                resource=resource, scope=scope, caller=caller,
                error="密码错误，请重试"
            ), status_code=401)

        sh._record_login_success(request)
        code = secrets.token_urlsafe(32)
        code_data = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "resource": resource,
            "scope": scope,
            "caller": caller,
            "expires": _time_mod.time() + _OAUTH_CODE_TTL,
        }
        if not _store_authorization_code(code, code_data, verified):
            return HTMLResponse(
                _oauth_authorize_html(
                    client_id, redirect_uri, state, code_challenge,
                    resource=resource, scope=scope, caller=caller,
                    error="授权状态已变化，请重新发起连接",
                ),
                status_code=409,
            )
        try:
            activated = _activate_oauth_client(client_id)
        except OAuthPersistenceError:
            with _oauth_grant_state_lock:
                _oauth_codes.pop(code, None)
            return HTMLResponse(
                _oauth_authorize_html(
                    client_id,
                    redirect_uri,
                    state,
                    code_challenge,
                    resource=resource,
                    scope=scope,
                    caller=caller,
                    error="授权状态无法持久化，请稍后重试",
                ),
                status_code=503,
                headers={
                    "Retry-After": "5",
                    "Cache-Control": "no-store",
                },
            )
        if not activated:
            with _oauth_grant_state_lock:
                _oauth_codes.pop(code, None)
            return HTMLResponse(
                _oauth_authorize_html(
                    client_id,
                    redirect_uri,
                    state,
                    code_challenge,
                    resource=resource,
                    scope=scope,
                    caller=caller,
                    error="客户端注册已失效，请重新连接",
                ),
                status_code=409,
                headers={"Cache-Control": "no-store"},
            )
        sep = "&" if "?" in redirect_uri else "?"
        location = f"{redirect_uri}{sep}code={_urlparse.quote(code)}"
        if state:
            location += f"&state={_urlparse.quote(state)}"
        sh.logger.info(
            "op=oauth_authorize phase=redirect trace_id=%s client_id=%s",
            trace_id,
            logged_client_id,
        )
        return RedirectResponse(location, status_code=302)

    @mcp.custom_route("/oauth/token", methods=["POST"])
    async def oauth_token(request: Request) -> Response:
        from starlette.responses import JSONResponse
        if not oauth_required:
            return _oauth_not_found()

        content_type = request.headers.get("content-type", "")
        try:
            if "json" in content_type:
                body = await request.json()
            else:
                form = await request.form()
                body = dict(form)
        except Exception:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        _cleanup_oauth_state()

        grant_type = body.get("grant_type")
        if grant_type not in ("authorization_code", "refresh_token"):
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

        if grant_type == "refresh_token":
            refresh_token = str(body.get("refresh_token", ""))
            if len(refresh_token) > 256:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            with _oauth_grant_state_lock:
                stored_refresh = _mcp_refresh_tokens.get(refresh_token)
                refresh_data = (
                    dict(stored_refresh)
                    if isinstance(stored_refresh, dict)
                    else None
                )
            now = _time_mod.time()
            if not isinstance(refresh_data, dict):
                return JSONResponse({"error": "invalid_grant", "error_description": "unknown refresh token"}, status_code=400)
            if refresh_data.get("expires", 0) < now:
                with _oauth_grant_state_lock:
                    if _mcp_refresh_tokens.get(refresh_token) == refresh_data:
                        _mcp_refresh_tokens.pop(refresh_token, None)
                return JSONResponse({"error": "invalid_grant", "error_description": "refresh token expired"}, status_code=400)
            client_id = str(body.get("client_id", ""))
            stored_client_id = str(refresh_data.get("client_id", ""))
            if client_id and stored_client_id and client_id != stored_client_id:
                return JSONResponse({"error": "invalid_grant", "error_description": "client_id mismatch"}, status_code=400)
            stored_resource = str(refresh_data.get("resource", ""))
            requested_resource = str(body.get("resource", ""))
            resource_ok, canonical_resource = _mcp_resource(
                request, requested_resource, oauth_public_origin
            )
            if requested_resource and not resource_ok:
                return JSONResponse({"error": "invalid_target", "error_description": "resource mismatch"}, status_code=400)
            # A public-origin change invalidates an existing resource-bound
            # refresh grant.  Never return HTTP 200 with a fresh access token
            # that the MCP middleware must immediately reject; invalid_grant
            # tells the client to begin a new authorization flow instead.
            if stored_resource and (
                _normalize_resource(canonical_resource)
                != _normalize_resource(stored_resource)
            ):
                return JSONResponse(
                    {
                        "error": "invalid_grant",
                        "error_description": (
                            "refresh token belongs to a previous MCP public URL; "
                            "reauthorization required"
                        ),
                    },
                    status_code=400,
                    headers={"Cache-Control": "no-store"},
                )

            try:
                rotated = _commit_refresh_token_rotation(
                    refresh_token,
                    refresh_data,
                    stored_resource or canonical_resource,
                )
            except OAuthPersistenceError:
                return JSONResponse(
                    {"error": "temporarily_unavailable"},
                    status_code=503,
                    headers={"Retry-After": "5", "Cache-Control": "no-store"},
                )
            if rotated is None:
                return JSONResponse(
                    {
                        "error": "invalid_grant",
                        "error_description": "refresh token already used or revoked",
                    },
                    status_code=400,
                )
            token, replacement_refresh = rotated
            return JSONResponse(
                _token_response(token, refresh_token=replacement_refresh),
                headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            )

        code = str(body.get("code", ""))
        code_verifier = str(body.get("code_verifier", ""))
        with _oauth_grant_state_lock:
            stored_code = _oauth_codes.get(code)
            code_data = dict(stored_code) if isinstance(stored_code, dict) else None
        if not code_data:
            return JSONResponse({"error": "invalid_grant", "error_description": "unknown or expired code"}, status_code=400)
        if code_data["expires"] < _time_mod.time():
            with _oauth_grant_state_lock:
                if _oauth_codes.get(code) == code_data:
                    _oauth_codes.pop(code, None)
            return JSONResponse({"error": "invalid_grant", "error_description": "code expired"}, status_code=400)

        client_id = str(body.get("client_id", ""))
        if client_id and client_id != str(code_data.get("client_id", "")):
            return JSONResponse({"error": "invalid_grant", "error_description": "client_id mismatch"}, status_code=400)
        redirect_uri = str(body.get("redirect_uri", ""))
        if redirect_uri and redirect_uri != str(code_data.get("redirect_uri", "")):
            return JSONResponse({"error": "invalid_grant", "error_description": "redirect_uri mismatch"}, status_code=400)
        stored_resource = str(code_data.get("resource", ""))
        requested_resource = str(body.get("resource", ""))
        resource_ok, canonical_resource = _mcp_resource(
            request, requested_resource, oauth_public_origin
        )
        if requested_resource and not resource_ok:
            return JSONResponse({"error": "invalid_target", "error_description": "resource mismatch"}, status_code=400)
        if stored_resource and (
            _normalize_resource(canonical_resource)
            != _normalize_resource(stored_resource)
        ):
            return JSONResponse(
                {
                    "error": "invalid_grant",
                    "error_description": (
                        "authorization code belongs to a different MCP public URL"
                    ),
                },
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )

        if code_data.get("code_challenge"):
            if not code_verifier or not _verify_pkce(code_verifier, code_data["code_challenge"]):
                with _oauth_grant_state_lock:
                    if _oauth_codes.get(code) == code_data:
                        _oauth_codes.pop(code, None)
                return JSONResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"}, status_code=400)

        token_resource = stored_resource or canonical_resource
        try:
            exchanged = _commit_authorization_code_exchange(
                code, code_data, token_resource
            )
        except OAuthPersistenceError:
            return JSONResponse(
                {"error": "temporarily_unavailable"},
                status_code=503,
                headers={"Retry-After": "5", "Cache-Control": "no-store"},
            )
        if exchanged is None:
            return JSONResponse(
                {
                    "error": "invalid_grant",
                    "error_description": "authorization code already used or revoked",
                },
                status_code=400,
            )
        token, refresh_token = exchanged
        return JSONResponse(
            _token_response(token, refresh_token=refresh_token),
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )
