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
import hashlib as _hashlib_oauth
import html as _html_escape

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh

logger = sh.logger

_oauth_clients: dict[str, dict] = {}
_oauth_codes: dict[str, dict] = {}    # code -> {client_id, redirect_uri, code_challenge, expires}
_mcp_tokens: dict[str, float] = {}    # token -> expiry timestamp
_mcp_token_resources: dict[str, str] = {}  # token -> canonical MCP resource
_mcp_refresh_tokens: dict[str, dict] = {}  # refresh_token -> {expires, client_id, resource}

_OAUTH_CODE_TTL = 300               # 5 min
_MCP_TOKEN_TTL = 86400 * 30         # 30 天；避免 100 年秒数溢出部分客户端的 32-bit duration
_MCP_REFRESH_TOKEN_TTL = 86400 * 365
_MCP_SCOPE = "mcp"


def _first_forwarded(value: str) -> str:
    """Return the first proxy header value (RFC 7239 chains are comma-separated)."""
    return (value or "").split(",", 1)[0].strip()

def _public_base_url(request: Request) -> str:
    """Return the externally-visible base URL, honoring Cloudflare/reverse-proxy headers."""
    proto = _first_forwarded(request.headers.get("x-forwarded-proto") or "").lower()
    if proto not in ("http", "https"):
        proto = request.url.scheme
    host = _first_forwarded(
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    )
    return f"{proto}://{host}".rstrip("/")


def _normalize_resource(resource: str) -> str:
    """Normalize an absolute OAuth resource URI for stable equality checks."""
    try:
        parsed = _urlparse.urlsplit(resource.strip())
    except Exception:
        return ""
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc or parsed.fragment:
        return ""
    path = parsed.path.rstrip("/")
    return _urlparse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


def _mcp_resource(request: Request, requested: str = "") -> tuple[bool, str]:
    """Validate/bind RFC 8707 resource to this server's canonical /mcp endpoint."""
    base = _public_base_url(request)
    canonical = f"{base}/mcp"
    if not requested:
        return True, canonical
    normalized = _normalize_resource(requested)
    if normalized in (_normalize_resource(base), _normalize_resource(canonical)):
        return True, canonical
    return False, canonical


def _mcp_tokens_file() -> str:
    return os.path.join(sh.config["buckets_dir"], ".dashboard_mcp_tokens.json")


def _oauth_clients_file() -> str:
    return os.path.join(sh.config["buckets_dir"], ".oauth_clients.json")


def _load_oauth_clients() -> None:
    """启动时恢复 DCR 客户端注册表。

    注册表若不持久化，容器重启后客户端（Claude Desktop 经 mcp-remote 等）
    缓存的 client_id 会变成 unknown client_id，授权页 400 无限循环，
    用户只能手动清客户端缓存才能恢复。与 token 同策略落盘 buckets 卷。"""
    global _oauth_clients
    try:
        path = _oauth_clients_file()
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            raw = _json_lib.load(f)
        if isinstance(raw, dict):
            _oauth_clients = {
                cid: info for cid, info in raw.items()
                if isinstance(cid, str) and isinstance(info, dict)
            }
    except Exception as e:
        logger.warning(f"[oauth] failed to load oauth clients: {e}")


def _save_oauth_clients() -> None:
    try:
        path = _oauth_clients_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json_lib.dump(_oauth_clients, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"[oauth] failed to save oauth clients: {e}")


def _load_mcp_tokens() -> None:
    global _mcp_tokens, _mcp_token_resources, _mcp_refresh_tokens
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

        _mcp_tokens = {}
        _mcp_token_resources = {}
        for tok, data in access_raw.items():
            if isinstance(data, (int, float)):
                exp = data
                resource = ""
            elif isinstance(data, dict):
                exp = data.get("expires")
                resource = str(data.get("resource", ""))
            else:
                continue
            if isinstance(exp, (int, float)) and exp > now:
                _mcp_tokens[tok] = exp
                if resource:
                    _mcp_token_resources[tok] = resource
        _mcp_refresh_tokens = {}
        for tok, data in refresh_raw.items():
            if isinstance(data, (int, float)):
                exp = data
                client_id = ""
            elif isinstance(data, dict):
                exp = data.get("expires")
                client_id = str(data.get("client_id", ""))
            else:
                continue
            if isinstance(exp, (int, float)) and exp > now:
                _mcp_refresh_tokens[tok] = {
                    "expires": exp,
                    "client_id": client_id,
                    "resource": str(data.get("resource", "")) if isinstance(data, dict) else "",
                }
    except Exception as e:
        logger.warning(f"[oauth] failed to load mcp tokens: {e}")


def _save_mcp_tokens() -> None:
    try:
        path = _mcp_tokens_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        now = _time_mod.time()
        active = {
            tok: {
                "expires": exp,
                "resource": _mcp_token_resources.get(tok, ""),
            }
            for tok, exp in _mcp_tokens.items()
            if exp > now
        }
        active_refresh = {
            tok: data for tok, data in _mcp_refresh_tokens.items()
            if isinstance(data, dict)
            and isinstance(data.get("expires"), (int, float))
            and data["expires"] > now
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json_lib.dump({
                "access_tokens": active,
                "refresh_tokens": active_refresh,
            }, f)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"[oauth] failed to save mcp tokens: {e}")


def _verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    digest = _hashlib_oauth.sha256(code_verifier.encode()).digest()
    computed = _base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return computed == code_challenge


def _is_valid_mcp_token(token: str, resource: str = "") -> bool:
    expiry = _mcp_tokens.get(token)
    if expiry is None:
        return False
    if _time_mod.time() > expiry:
        del _mcp_tokens[token]
        _mcp_token_resources.pop(token, None)
        return False
    bound_resource = _mcp_token_resources.get(token, "")
    if resource and bound_resource:
        return _normalize_resource(resource) == _normalize_resource(bound_resource)
    return True


def _issue_mcp_access_token(resource: str = "") -> str:
    token = secrets.token_urlsafe(32)
    _mcp_tokens[token] = _time_mod.time() + _MCP_TOKEN_TTL
    if resource:
        _mcp_token_resources[token] = resource
    return token


def _issue_mcp_refresh_token(client_id: str, resource: str = "") -> str:
    refresh_token = secrets.token_urlsafe(32)
    _mcp_refresh_tokens[refresh_token] = {
        "expires": _time_mod.time() + _MCP_REFRESH_TOKEN_TTL,
        "client_id": client_id,
        "resource": resource,
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
        refresh_data = _mcp_refresh_tokens.get(refresh_token, {})
        refresh_exp = refresh_data.get("expires")
        if isinstance(refresh_exp, (int, float)):
            payload["refresh_expires_in"] = max(0, int(refresh_exp - _time_mod.time()))
        payload["refresh_token"] = refresh_token
    return payload


def _mcp_auth_check(request: Request):
    """Return True if request has a valid MCP Bearer token."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return _is_valid_mcp_token(auth[7:])
    return False


def _validate_authorize_redirect(client_id: str, redirect_uri: str) -> tuple[bool, str]:
    """Validate OAuth dynamic client and exact redirect_uri before asking for a password."""
    if not client_id:
        return False, "missing client_id"
    if not redirect_uri:
        return False, "missing redirect_uri"
    client_info = _oauth_clients.get(client_id)
    if not client_info:
        return False, "unknown client_id"
    if redirect_uri not in (client_info.get("redirect_uris") or []):
        return False, "redirect_uri mismatch"
    return True, ""


def _oauth_authorize_html(client_id: str, redirect_uri: str, state: str,
                           code_challenge: str, resource: str = "",
                           scope: str = _MCP_SCOPE, error: str = "") -> str:
    e = _html_escape.escape
    try:
        from utils import get_ai_name  # type: ignore
    except ImportError:  # pragma: no cover
        from ..utils import get_ai_name  # type: ignore
    ai_name = e(get_ai_name())
    err_html = f'<p style="color:#ff6b6b;font-size:13px;margin-top:12px;">{e(error)}</p>' if error else ""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ombre Brain · 授权 MCP</title>
<style>
*{{box-sizing:border-box}}
body{{font-family:-apple-system,system-ui,sans-serif;background:#0f0f0f;color:#e0e0e0;
  display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
.card{{background:#1a1a1a;border:1px solid #333;border-radius:16px;padding:40px 36px;
  max-width:380px;width:90%;text-align:center}}
h2{{color:#c9a96e;font-family:Georgia,serif;font-size:24px;margin:0 0 6px}}
.sub{{color:#888;font-size:13px;margin:0 0 24px}}
input[type=password]{{display:block;width:100%;padding:11px 14px;background:#111;
  border:1px solid #444;border-radius:8px;color:#e0e0e0;font-size:14px;margin-bottom:14px}}
button{{width:100%;padding:12px;background:#c9a96e;color:#0f0f0f;border:none;
  border-radius:8px;font-size:14px;font-weight:600;cursor:pointer}}
button:hover{{background:#d4b87a}}
.note{{color:#666;font-size:11px;margin-top:16px;line-height:1.6}}
</style></head>
<body><div class="card">
<h2>◐ Ombre Brain</h2>
<p class="sub">授权 {ai_name} 连接 MCP</p>
<form method="POST">
<input type="hidden" name="client_id" value="{e(client_id)}">
<input type="hidden" name="redirect_uri" value="{e(redirect_uri)}">
<input type="hidden" name="state" value="{e(state)}">
<input type="hidden" name="code_challenge" value="{e(code_challenge)}">
<input type="hidden" name="resource" value="{e(resource)}">
<input type="hidden" name="scope" value="{e(scope)}">
<input type="password" name="password" placeholder="输入 Dashboard 密码" autofocus>
<button type="submit">授权并连接</button>
</form>
{err_html}
<p class="note">授权后 {ai_name} 将可使用 MCP 工具读写记忆。<br>Token 长期有效，并支持自动续期。<br>若工具调用失败，请在客户端断开重连，再重新点击此页授权即可。</p>
</div></body></html>"""


def register(mcp) -> None:
    """注册 /.well-known/* 与 /oauth/* 路由，并在装配时载入持久化 token。"""
    _load_mcp_tokens()   # 启动时恢复持久化 token，Docker 重启不再强制重新 OAuth
    _load_oauth_clients()  # 启动时恢复 DCR 客户端注册表，重启后旧 client_id 依然有效

    @mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
    @mcp.custom_route("/.well-known/oauth-protected-resource/{resource_path:path}", methods=["GET"])
    async def oauth_protected_resource(request: Request) -> Response:
        from starlette.responses import JSONResponse
        base = _public_base_url(request)
        # 带路径时（如 /mcp-extra）按请求路径动态返回对应 resource，
        # 以便 Claude.ai 的严格 resource 匹配通过；无路径时返回根资源。
        sub = request.path_params.get("resource_path", "")
        resource = f"{base}/{sub}" if sub else base
        return JSONResponse({
            "resource": resource,
            "authorization_servers": [base],
            "bearer_methods_supported": ["header"],
            "scopes_supported": [_MCP_SCOPE],
        })

    @mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
    async def oauth_authorization_server(request: Request) -> Response:
        from starlette.responses import JSONResponse
        base = _public_base_url(request)
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
        try:
            body = await request.json()
        except Exception:
            body = {}
        client_id = secrets.token_urlsafe(16)
        _oauth_clients[client_id] = {
            "redirect_uris": body.get("redirect_uris", []),
            "client_name": body.get("client_name", "MCP Client"),
            "registered_at": int(_time_mod.time()),
        }
        _save_oauth_clients()
        return JSONResponse({
            "client_id": client_id,
            "client_id_issued_at": int(_time_mod.time()),
            "redirect_uris": body.get("redirect_uris", []),
            "client_name": body.get("client_name", "MCP Client"),
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        }, status_code=201)

    @mcp.custom_route("/oauth/authorize", methods=["GET", "POST"])
    async def oauth_authorize(request: Request) -> Response:
        from starlette.responses import HTMLResponse, RedirectResponse
        if request.method == "GET":
            p = dict(request.query_params)
            ok, err = _validate_authorize_redirect(
                p.get("client_id", ""), p.get("redirect_uri", "")
            )
            resource_ok, resource = _mcp_resource(request, p.get("resource", ""))
            if ok and not resource_ok:
                ok, err = False, "resource 与当前 MCP 地址不匹配"
            if ok and p.get("response_type", "code") != "code":
                ok, err = False, "unsupported response_type"
            if ok and not p.get("code_challenge"):
                ok, err = False, "missing PKCE code_challenge"
            if ok and p.get("code_challenge_method", "S256") != "S256":
                ok, err = False, "仅支持 PKCE S256"
            if ok and sh._is_setup_needed():
                ok, err = False, "尚未设置 Dashboard 密码，请先打开 Dashboard 完成初始化"
            return HTMLResponse(_oauth_authorize_html(
                p.get("client_id", ""), p.get("redirect_uri", ""),
                p.get("state", ""), p.get("code_challenge", ""),
                resource=resource, scope=p.get("scope", _MCP_SCOPE), error=err,
            ), status_code=200 if ok else (503 if sh._is_setup_needed() else 400))
        # POST
        form = await request.form()
        password     = str(form.get("password", ""))
        client_id    = str(form.get("client_id", ""))
        redirect_uri = str(form.get("redirect_uri", ""))
        state        = str(form.get("state", ""))
        code_challenge = str(form.get("code_challenge", ""))
        requested_resource = str(form.get("resource", ""))
        scope = str(form.get("scope", _MCP_SCOPE)) or _MCP_SCOPE

        ok, err = _validate_authorize_redirect(client_id, redirect_uri)
        resource_ok, resource = _mcp_resource(request, requested_resource)
        if ok and not resource_ok:
            ok, err = False, "resource 与当前 MCP 地址不匹配"
        if ok and not code_challenge:
            ok, err = False, "missing PKCE code_challenge"
        if not ok:
            return HTMLResponse(_oauth_authorize_html(
                client_id, redirect_uri, state, code_challenge,
                resource=resource, scope=scope, error=err
            ), status_code=400)
        if sh._is_setup_needed():
            return HTMLResponse(_oauth_authorize_html(
                client_id, redirect_uri, state, code_challenge,
                resource=resource, scope=scope,
                error="尚未设置 Dashboard 密码，请先打开 Dashboard 完成初始化",
            ), status_code=503)
        if not sh._verify_any_password(password):
            return HTMLResponse(_oauth_authorize_html(
                client_id, redirect_uri, state, code_challenge,
                resource=resource, scope=scope, error="密码错误，请重试"
            ), status_code=401)

        code = secrets.token_urlsafe(32)
        _oauth_codes[code] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "resource": resource,
            "scope": scope,
            "expires": _time_mod.time() + _OAUTH_CODE_TTL,
        }
        sep = "&" if "?" in redirect_uri else "?"
        location = f"{redirect_uri}{sep}code={_urlparse.quote(code)}"
        if state:
            location += f"&state={_urlparse.quote(state)}"
        return RedirectResponse(location, status_code=302)

    @mcp.custom_route("/oauth/token", methods=["POST"])
    async def oauth_token(request: Request) -> Response:
        from starlette.responses import JSONResponse
        content_type = request.headers.get("content-type", "")
        try:
            if "json" in content_type:
                body = await request.json()
            else:
                form = await request.form()
                body = dict(form)
        except Exception:
            return JSONResponse({"error": "invalid_request"}, status_code=400)

        grant_type = body.get("grant_type")
        if grant_type not in ("authorization_code", "refresh_token"):
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

        if grant_type == "refresh_token":
            refresh_token = str(body.get("refresh_token", ""))
            refresh_data = _mcp_refresh_tokens.get(refresh_token)
            now = _time_mod.time()
            if not isinstance(refresh_data, dict):
                return JSONResponse({"error": "invalid_grant", "error_description": "unknown refresh token"}, status_code=400)
            if refresh_data.get("expires", 0) < now:
                _mcp_refresh_tokens.pop(refresh_token, None)
                _save_mcp_tokens()
                return JSONResponse({"error": "invalid_grant", "error_description": "refresh token expired"}, status_code=400)
            client_id = str(body.get("client_id", ""))
            stored_client_id = str(refresh_data.get("client_id", ""))
            if client_id and stored_client_id and client_id != stored_client_id:
                return JSONResponse({"error": "invalid_grant", "error_description": "client_id mismatch"}, status_code=400)
            stored_resource = str(refresh_data.get("resource", ""))
            requested_resource = str(body.get("resource", ""))
            resource_ok, canonical_resource = _mcp_resource(request, requested_resource)
            if requested_resource and not resource_ok:
                return JSONResponse({"error": "invalid_target", "error_description": "resource mismatch"}, status_code=400)
            if requested_resource and stored_resource and (
                _normalize_resource(canonical_resource) != _normalize_resource(stored_resource)
            ):
                return JSONResponse({"error": "invalid_target", "error_description": "resource mismatch"}, status_code=400)

            token = _issue_mcp_access_token(stored_resource or canonical_resource)
            _save_mcp_tokens()
            return JSONResponse(
                _token_response(token, refresh_token=refresh_token),
                headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            )

        code = str(body.get("code", ""))
        code_verifier = str(body.get("code_verifier", ""))
        code_data = _oauth_codes.get(code)
        if not code_data:
            return JSONResponse({"error": "invalid_grant", "error_description": "unknown or expired code"}, status_code=400)
        if code_data["expires"] < _time_mod.time():
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
        resource_ok, canonical_resource = _mcp_resource(request, requested_resource)
        if requested_resource and not resource_ok:
            return JSONResponse({"error": "invalid_target", "error_description": "resource mismatch"}, status_code=400)
        if requested_resource and stored_resource and (
            _normalize_resource(canonical_resource) != _normalize_resource(stored_resource)
        ):
            return JSONResponse({"error": "invalid_target", "error_description": "resource mismatch"}, status_code=400)

        if code_data.get("code_challenge"):
            if not code_verifier or not _verify_pkce(code_verifier, code_data["code_challenge"]):
                return JSONResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"}, status_code=400)

        _oauth_codes.pop(code, None)
        token_resource = stored_resource or canonical_resource
        token = _issue_mcp_access_token(token_resource)
        refresh_token = _issue_mcp_refresh_token(
            str(code_data.get("client_id", "")), token_resource
        )
        _save_mcp_tokens()
        return JSONResponse(
            _token_response(token, refresh_token=refresh_token),
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )
