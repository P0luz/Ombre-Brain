"""
========================================
web/auth.py — Dashboard 鉴权相关 HTTP 路由
========================================

承载 /auth/* 这一组 cookie 会话鉴权接口（状态/首启设密/登录/登出/改密/安全问题急救）。
真正的会话/密码逻辑在 web/_shared.py，本文件做 HTTP 入口与参数校验；另有一个启动期
入口：环境变量口令换了就撤销旧的会话和 MCP 授权（这里只编排撤销顺序，指纹的存取在
web/_shared.py，令牌的撤销在 web/oauth.py）。

对外暴露：
- register(mcp) —— server.py 启动装配时调用，把下列路由挂到主 mcp 实例。
- reconcile_environment_password_at_startup(host, port) —— server.py 在 HTTP 进程开始接
  请求前调用一次（4.1.4）。
========================================
"""

import asyncio
import errno
import hashlib
import ipaddress
import math
import os
import socket
import sqlite3
import threading
import time
from contextlib import closing

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh

_MAX_PASSWORD_CHARS = 1024
_MAX_SECURITY_QUESTION_CHARS = 500
_MAX_SECURITY_ANSWER_CHARS = 1024
_PASSWORD_WORK_MAX_CONCURRENCY = 2
_RECOVERY_WINDOW_SECONDS = 900
_RECOVERY_SOURCE_ATTEMPTS = 5
_RECOVERY_TOTAL_ATTEMPTS = 20


def _reserve_recovery_attempt(request) -> int:
    """事务预占恢复额度；重启、并发和客户端断开都不能返还已用额度。"""
    path = os.path.join(sh.config["buckets_dir"], ".recovery_limits.sqlite3")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    source = hashlib.sha256(sh._client_key(request).encode("utf-8")).hexdigest()
    with closing(sqlite3.connect(path, timeout=2)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS attempts (source TEXT NOT NULL, at REAL NOT NULL)")
        now = time.time()
        db.execute("DELETE FROM attempts WHERE at <= ?", (now - _RECOVERY_WINDOW_SECONDS,))
        rows = db.execute("SELECT source, at FROM attempts ORDER BY at").fetchall()
        own = [at for key, at in rows if key == source]
        waits = []
        if len(own) >= _RECOVERY_SOURCE_ATTEMPTS:
            waits.append(own[-_RECOVERY_SOURCE_ATTEMPTS] + _RECOVERY_WINDOW_SECONDS - now)
        if len(rows) >= _RECOVERY_TOTAL_ATTEMPTS:
            waits.append(rows[-_RECOVERY_TOTAL_ATTEMPTS][1] + _RECOVERY_WINDOW_SECONDS - now)
        if waits:
            return max(1, math.ceil(max(waits)))
        db.execute("INSERT INTO attempts VALUES (?, ?)", (source, now))
    return 0


class _CrossLoopSemaphore:
    """Small async context manager backed by a process-wide thread semaphore.

    FastMCP can serve requests from more than one event loop.  An
    ``asyncio.Semaphore`` binds contended waiters to one loop and therefore is
    not a process-wide KDF ceiling.  Non-blocking polling keeps acquisition off
    the executor, so queued login attempts cannot occupy every worker thread
    while the PBKDF2 jobs holding the slots wait behind them.
    """

    def __init__(self, value: int):
        self._semaphore = threading.BoundedSemaphore(value)

    async def __aenter__(self):
        while not self._semaphore.acquire(blocking=False):
            await asyncio.sleep(0.01)
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        self._semaphore.release()


_setup_lock = _CrossLoopSemaphore(1)
_password_work_semaphore = _CrossLoopSemaphore(_PASSWORD_WORK_MAX_CONCURRENCY)


async def _await_password_worker(func, *args, **kwargs):
    worker = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # A task may receive more than one cancellation while the executor
        # thread is still running. Keep shielding until it truly finishes;
        # otherwise each repeated disconnect could release another slot.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
        try:
            worker.result()
        except BaseException:
            pass
        raise


async def _run_password_work(func, *args, **kwargs):
    """Run a password KDF off-loop while enforcing one process-wide ceiling.

    ``asyncio.to_thread`` alone keeps the event loop responsive but its default
    executor can create many simultaneous PBKDF2 jobs. The semaphore bounds CPU
    pressure and remains held until a cancelled worker thread really stops.
    """
    async with _password_work_semaphore:
        return await _await_password_worker(func, *args, **kwargs)


async def _run_public_password_verification(request, verifier, secret):
    """Recheck per-source lockout after waiting for a global KDF slot."""
    async with _password_work_semaphore:
        retry = sh._login_retry_after(request)
        if retry:
            return False, retry
        verified = await _await_password_worker(verifier, secret)
        return verified, 0


def _json_object(body) -> dict | None:
    return body if isinstance(body, dict) else None


def _is_explicit_loopback_host(host: object) -> bool:
    """Accept only unambiguous loopback names and address literals."""
    value = str(host or "").strip().lower()
    if value in ("localhost", "localhost."):
        return True
    if not value or "%" in value:
        return False
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        # This deliberately rejects DNS aliases and legacy numeric IPv4 forms
        # such as 127.1, 2130706433, or 0177.0.0.1. Browsers can interpret
        # those differently from Python, which would reopen DNS rebinding.
        return False


def _loopback_host_authority(authority: object) -> bool:
    """Validate a Host header as a loopback literal/name with optional port."""
    value = str(authority or "").strip()
    if not value or any(char.isspace() or char == "\\" for char in value):
        return False

    if value.startswith("["):
        close = value.find("]")
        if close <= 1:
            return False
        host = value[1:close]
        suffix = value[close + 1 :]
        if suffix and not suffix.startswith(":"):
            return False
        port_text = suffix[1:] if suffix else None
    else:
        if "[" in value or "]" in value or value.count(":") > 1:
            return False
        if ":" in value:
            host, port_text = value.rsplit(":", 1)
        else:
            host, port_text = value, None

    if port_text is not None:
        if (
            not port_text
            or not port_text.isascii()
            or not port_text.isdecimal()
        ):
            return False
        port = int(port_text)
        if not 1 <= port <= 65535:
            return False
    return _is_explicit_loopback_host(host)


def _request_has_one_loopback_host(request: Request) -> bool:
    headers = request.headers
    getlist = getattr(headers, "getlist", None)
    if callable(getlist):
        values = list(getlist("host"))
    else:
        value = headers.get("Host")
        if value is None:
            value = headers.get("host")
        values = [] if value is None else [value]
    return len(values) == 1 and _loopback_host_authority(values[0])


def _setup_request_allowed(request: Request) -> bool:
    """Allow password bootstrap locally, or remotely with an explicit secret."""
    configured_token = os.environ.get("OMBRE_SETUP_TOKEN", "").strip()
    supplied_token = request.headers.get("X-Ombre-Setup-Token", "").strip()
    if configured_token and supplied_token and sh._constant_time_text_equal(
        configured_token, supplied_token
    ):
        return True
    # Forwarding headers mean the browser is not the direct loopback peer.
    # Treat them conservatively even if the reverse proxy itself is local.
    if any(
        request.headers.get(name)
        for name in (
            "Forwarded",
            "X-Forwarded-For",
            "X-Forwarded-Host",
            "X-Forwarded-Proto",
        )
    ):
        return False
    client = getattr(request, "client", None)
    host = str(getattr(client, "host", "") or "").strip()
    return _is_explicit_loopback_host(host) and _request_has_one_loopback_host(
        request
    )


def _revoke_mcp_grants() -> None:
    # Delayed import avoids an auth<->oauth module initialization cycle.
    from .oauth import revoke_all_mcp_grants

    revoke_all_mcp_grants()


def _commit_password_rotation(
    proof: sh.CredentialProof,
    password_hash: str,
) -> str | None:
    """Commit one password rotation in a crash-safe, stale-proof-safe order.

    Persisted sessions are revoked first, then OAuth grants, and only then is
    the new password published.  Therefore an OAuth persistence failure can
    never leave a new password paired with old access/refresh grants.  The
    credential guard also prevents code/token issuance from entering between
    these file commits.
    """
    with sh._credential_state_guard():
        if not sh._credential_proof_matches_locked(proof):
            return None
        sh._revoke_all_sessions()
        _revoke_mcp_grants()
        committed = sh._save_prehashed_password(
            password_hash,
            keep_qa=True,
            advance_generation=False,
        )
        if not committed:  # pragma: no cover - guard makes this unreachable
            raise sh.CredentialChangedError("credential changed during rotation")
        return sh._create_session()


# --- 环境变量口令换了：启动时撤销旧授权 ---
# OMBRE_DASHBOARD_PASSWORD 在进程运行中改不了（热更新 execv 原样继承环境变量，
# /api/env-config 也不写它），只会随真正的重启变化。所以 HTTP 进程开始接请求前
# 比一次指纹就够了，不必在每个认授权的入口各查一遍。


def _listen_port_unavailable(host: str, port: int) -> bool:
    """本进程要监听的地址现在绑不上（多半是旧进程还占着端口）就返回 True。

    旧进程还活着时如果先撤销、记下新指纹，旧进程下一次写令牌或会话文件就会把
    内存里的旧授权整份写回去，而指纹已经对得上，之后再也不会撤。这里照 asyncio
    create_server（uvicorn 实际的监听方式）的做法逐个地址试绑再立刻关掉：POSIX 上设
    SO_REUSEADDR、IPv6 设 IPV6_V6ONLY；建不了 socket 或绑定报 EADDRNOTAVAIL 的地址族
    跳过（Python 3.12 的 asyncio 同样跳过，例如 localhost 解析出 ::1 而系统关了 IPv6），
    一个地址都绑不上才算绑不上。其他绑定错误（主要是 EADDRINUSE）会让 uvicorn 同样
    启动失败，跳过核对不会漏掉什么。

    只看得见同一个网络命名空间里的同一个监听地址：Docker / K8s 的新容器有自己的网络，
    看不见旧容器；同时改了端口或监听地址时也看不见旧进程。
    """
    try:
        infos = socket.getaddrinfo(
            host or None,
            int(port),
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            flags=socket.AI_PASSIVE,
        )
    except (OSError, ValueError, OverflowError):
        return True
    bound = 0
    for family, kind, proto, _canonname, address in infos:
        try:
            probe = socket.socket(family, kind, proto)
        except OSError:
            continue
        with probe:
            try:
                if os.name == "posix":
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if family == socket.AF_INET6 and hasattr(socket, "IPPROTO_IPV6"):
                    probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                probe.bind(address)
            except OSError as e:
                if e.errno == errno.EADDRNOTAVAIL:
                    continue
                return True
            except (ValueError, OverflowError):
                return True
        bound += 1
    return bound == 0


def _persist_oauth_clients_before_revoke() -> None:
    # 延迟导入，理由同 _revoke_mcp_grants。
    from .oauth import persist_loaded_oauth_clients

    try:
        persist_loaded_oauth_clients()
    except Exception as e:
        sh.logger.warning("[auth] 撤销前保存 OAuth 客户端注册失败：%s", e)


def _discard_mcp_grants_in_memory() -> None:
    from .oauth import discard_mcp_grants_in_memory

    discard_mcp_grants_in_memory()


def _clear_static_mcp_token_in(save_config: dict) -> None:
    save_config["mcp_token"] = ""


def _invalidate_config_static_mcp_token() -> bool:
    """作废 config.yaml 里的静态 MCP Token；读写失败返回 False（内存里照样清掉）。

    config.yaml 的 mcp_token 多半是 Dashboard「重新生成」写进去的：拿着旧口令登录过的人
    可以事先生成一把、再把鉴权模式存成 token/hybrid，等主人换口令重启时一起生效。所以
    它和会话、OAuth 令牌一起作废，不分是 Dashboard 生成的还是手写的。环境变量
    OMBRE_MCP_TOKEN 是部署者在环境变量里自己管的钥匙（Dashboard 改不了），不动，只提醒。
    config.yaml 里没有这把 Token 时不写文件。
    """
    from utils import atomic_update_config_yaml, read_config_yaml

    durable = True
    cleared = False
    try:
        if str(read_config_yaml().get("mcp_token") or "").strip():
            atomic_update_config_yaml(_clear_static_mcp_token_in)
            cleared = True
    except Exception as e:
        durable = False
        sh.logger.error(
            "[auth] 作废 config.yaml 里的静态 MCP Token 时读写失败，只清了内存：%s", e
        )
    env_token = os.environ.get("OMBRE_MCP_TOKEN", "").strip()
    if not env_token and str(sh.config.get("mcp_token") or "").strip():
        sh.config["mcp_token"] = ""
        cleared = True
    if cleared:
        sh.logger.warning(
            "[auth] config.yaml 里的静态 MCP Token（mcp_token）已一并作废。还要用 Token 或"
            "共存模式的话，请登录 Dashboard 到设置页重新生成，再更新 MCP 客户端（当前鉴权"
            "模式：%s）。",
            sh.config.get("mcp_auth_mode", "oauth"),
        )
    if env_token:
        sh.logger.warning(
            "[auth] 环境变量 OMBRE_MCP_TOKEN 设置的静态 Token 没有撤销（它不归 Dashboard "
            "管）；怀疑它和口令一起泄露时，请一并更换。"
        )
    return durable


def _revoke_all_grants_for_credential_change() -> bool:
    """撤销全部 Dashboard 会话和 MCP 授权；调用方须持 _credential_state_guard。

    顺序与 _commit_password_rotation 相同：先会话，再 OAuth；之后作废 config.yaml 里的
    静态 MCP Token。某一步写盘失败时打 ERROR，但仍把本进程内存里的会话、令牌、待兑换
    code 和静态 Token 清掉，本进程不再认旧授权；返回 False，调用方据此不记新指纹，下次
    启动再撤一次。OAuth 客户端注册不撤（公开客户端，没有密钥）。
    """
    durable = True
    _persist_oauth_clients_before_revoke()
    try:
        sh._revoke_all_sessions()
    except Exception as e:
        durable = False
        sh.logger.error("[auth] 撤销 Dashboard 会话时写盘失败，只清了内存：%s", e)
        sh._discard_sessions_in_memory()
    try:
        _revoke_mcp_grants()
    except Exception as e:
        durable = False
        sh.logger.error("[auth] 撤销 MCP 授权时写盘失败，只清了内存：%s", e)
        _discard_mcp_grants_in_memory()
    if not _invalidate_config_static_mcp_token():
        durable = False
    return durable


def _active_grant_counts() -> tuple[int, int]:
    from . import oauth

    with sh._session_state_lock:
        sessions = len(sh._sessions)
    with oauth._oauth_grant_state_lock:
        grants = len(oauth._mcp_tokens) + len(oauth._mcp_refresh_tokens)
    return sessions, grants


def _record_first_environment_password_state(env_password: str) -> str:
    """刚升级上来、从没记过：只记录当前状态，不撤销（没有依据判断口令换没换）。"""
    record = (
        sh._derive_environment_password_fingerprint(env_password)
        if env_password
        else None
    )
    try:
        sh._write_environment_password_record_locked(record)
    except Exception as e:
        sh.logger.error(
            "[auth] 记录口令指纹失败，下次启动再记；在 .dashboard_auth.json 能写之前，每次"
            "启动都按第一次处理、只记录不撤销，改 OMBRE_DASHBOARD_PASSWORD 不会自动撤销"
            "旧授权：%s",
            e,
        )
        return "record_failed"
    sessions, grants = _active_grant_counts()
    if env_password and (sessions or grants):
        sh.logger.warning(
            "[auth] 第一次记录 OMBRE_DASHBOARD_PASSWORD 的指纹，没有撤销现有的 %d 个 "
            "Dashboard 会话和 %d 个 MCP 令牌。如果这次升级同时换了口令，请停掉服务，"
            "删除数据目录里的 .dashboard_sessions.json 和 .dashboard_mcp_tokens.json "
            "后再启动，用静态 Token 的再到设置页重新生成；以后改这个环境变量再重启会自动撤销。",
            sessions,
            grants,
        )
    else:
        sh.logger.info("[auth] 已记录当前口令来源，之后换环境变量口令重启会自动撤销旧授权")
    return "recorded"


def _reconcile_with_unreadable_auth_file(env_password: str, error: Exception) -> str:
    """.dashboard_auth.json 读不了时怎么办；调用方须持 _credential_state_guard。

    环境变量口令模式下登录不读这个文件，跳过的话除了一行日志没有任何症状，换口令也
    就悄悄不撤了，所以按换过处理（偏向关闭），修好之前每次启动都撤。没设环境变量口令
    时登录本身就要读这个文件，问题会直接暴露，这里不撤。
    """
    if not env_password:
        sh.logger.error(
            "[auth] 读不了 .dashboard_auth.json，本次跳过口令变更核对；修好或移走该文件"
            "后重启：%s",
            error,
        )
        return "skipped_unreadable"
    sessions, grants = _active_grant_counts()
    durable = _revoke_all_grants_for_credential_change()
    sh.logger.error(
        "[auth] 读不了 .dashboard_auth.json，无从判断 OMBRE_DASHBOARD_PASSWORD 换没换，"
        "已按换过处理，撤销 %d 个 Dashboard 会话和 %d 个 MCP 令牌；修好或移走该文件之前，"
        "每次启动都会这样撤销：%s",
        sessions,
        grants,
        error,
    )
    return "revoked_unreadable" if durable else "revoke_not_durable"


def reconcile_environment_password_at_startup(host: str, port: int) -> str:
    """HTTP 进程开始接请求前调用一次，永远不抛异常。

    环境变量口令换了值、或者在环境变量口令和文件口令（含未设密）之间切换，就撤销
    全部 Dashboard 会话和 MCP 授权（含 config.yaml 里的静态 Token），再记下新指纹。
    返回值给日志和测试用：
    - "skipped_port_busy"：监听地址绑不上（可能旧进程还没退），不撤不记；
    - "revoked_unreadable"：设了环境变量口令而 .dashboard_auth.json 读不了，无从判断，按
      换过处理并撤销，不记指纹（修好之前每次启动都撤）；
    - "skipped_unreadable"：没设环境变量口令而 .dashboard_auth.json 读不了，不撤不记；
    - "recorded" / "record_failed"：从没记过，只记录不撤销；
    - "unchanged"：没变；
    - "revoked"：撤销并记下新指纹；
    - "revoked_unrecorded"：撤销已写盘，新指纹没写进去，下次启动会再撤一次；
    - "revoke_not_durable"：撤销写盘失败，只清了内存，不记新指纹，下次启动再撤；
    - "error"：其他意外，已打 ERROR。
    """
    try:
        if _listen_port_unavailable(host, port):
            sh.logger.error(
                "[auth] 监听端口 %s 现在绑不上（可能是旧进程还没退出），本次跳过口令变更"
                "核对；旧进程退出后重新启动即可完成核对。",
                port,
            )
            return "skipped_port_busy"
        # 与 _verify_password_for_rotation 同一口径：原样取值，非空就算设了。
        env_password = os.environ.get("OMBRE_DASHBOARD_PASSWORD", "")
        with sh._credential_state_guard():
            try:
                record = sh._read_environment_password_record_locked()
            except sh.AuthPersistenceError as e:
                return _reconcile_with_unreadable_auth_file(env_password, e)
            if record is sh._ENV_PASSWORD_FINGERPRINT_MISSING:
                return _record_first_environment_password_state(env_password)
            if env_password:
                if sh._environment_password_fingerprint_matches(env_password, record):
                    if sh._needs_rehash(str(record)):
                        try:
                            sh._write_environment_password_record_locked(
                                sh._derive_environment_password_fingerprint(env_password)
                            )
                        except Exception as e:
                            sh.logger.warning("[auth] 升级口令指纹失败：%s", e)
                    return "unchanged"
                new_record = sh._derive_environment_password_fingerprint(env_password)
            else:
                if record is None:
                    return "unchanged"
                new_record = None
            sessions, grants = _active_grant_counts()
            if not _revoke_all_grants_for_credential_change():
                return "revoke_not_durable"
            sh.logger.warning(
                "[auth] Dashboard 口令（环境变量）变了，已撤销 %d 个 Dashboard 会话和 "
                "%d 个 MCP 令牌，需要重新登录、MCP 客户端需要重新授权。如果同一个数据"
                "目录被两个口令不同的进程轮流使用，每次启动都会这样撤销。",
                sessions,
                grants,
            )
            try:
                sh._write_environment_password_record_locked(new_record)
            except Exception as e:
                sh.logger.error(
                    "[auth] 撤销已完成，但新口令指纹没写进去，下次启动会再撤销一次，"
                    "直到 .dashboard_auth.json 可写：%s",
                    e,
                )
                return "revoked_unrecorded"
            return "revoked"
    except Exception as e:
        sh.logger.error("[auth] 口令变更核对失败：%s", e)
        return "error"


def _persistence_failure_response(action: str, error: Exception) -> Response:
    """Return an explicit fail-closed result without leaking private details."""
    from starlette.responses import JSONResponse

    sh.logger.error("[auth] %s could not be committed: %s", action, error)
    return JSONResponse(
        {"error": "安全状态无法持久化，请检查存储后重试"},
        status_code=503,
        headers={"Retry-After": "5", "Cache-Control": "no-store"},
    )


def register(mcp) -> None:
    """把 /auth/* 路由注册到传入的 FastMCP 实例。"""

    @mcp.custom_route("/auth/status", methods=["GET"])
    async def auth_status(request: Request) -> Response:
        """Return auth state (authenticated, setup_needed)."""
        from starlette.responses import JSONResponse
        return JSONResponse(
            {
                "authenticated": sh._is_authenticated(request),
                "setup_needed": sh._is_setup_needed(),
            },
            headers={"Cache-Control": "no-store"},
        )

    @mcp.custom_route("/auth/setup", methods=["POST"])
    async def auth_setup_endpoint(request: Request) -> Response:
        """Initial password setup (only when no password is configured)."""
        from starlette.responses import JSONResponse
        if not sh._is_setup_needed():
            return JSONResponse({"error": "Already configured"}, status_code=400)
        if not _setup_request_allowed(request):
            return JSONResponse(
                {
                    "error": (
                        "Initial password setup is local-only. Set "
                        "OMBRE_DASHBOARD_PASSWORD before public deployment, or "
                        "supply X-Ombre-Setup-Token matching OMBRE_SETUP_TOKEN."
                    )
                },
                status_code=403,
            )
        retry = sh._login_retry_after(request)
        if retry:
            return JSONResponse(
                {"error": f"尝试过于频繁，请 {retry} 秒后再试"},
                status_code=429,
                headers={"Retry-After": str(retry)},
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        body = _json_object(body)
        if body is None:
            return JSONResponse({"error": "JSON body must be an object"}, status_code=400)
        password = body.get("password", "")
        if not isinstance(password, str):
            return JSONResponse({"error": "password must be a string"}, status_code=400)
        password = password.strip()
        if not 6 <= len(password) <= _MAX_PASSWORD_CHARS:
            return JSONResponse({"error": "密码长度必须在 6-1024 位之间"}, status_code=400)
        # Two public first-run requests can both pass the optimistic check
        # above while either one is awaiting/parsing its body.  Recheck and
        # initialize under one process-wide lock so exactly one password and
        # one administrator session can win the bootstrap race.
        async with _setup_lock:
            if not sh._is_setup_needed():
                return JSONResponse({"error": "Already configured"}, status_code=400)
            try:
                sh._revoke_all_sessions()
                _revoke_mcp_grants()
                await _run_password_work(sh._save_password_hash, password)
                token = sh._create_session()
            except Exception as e:
                return _persistence_failure_response("initial setup", e)
        resp = JSONResponse({"ok": True})
        sh._set_session_cookie(resp, token, request)
        return resp

    @mcp.custom_route("/auth/login", methods=["POST"])
    async def auth_login(request: Request) -> Response:
        """Login with password."""
        from starlette.responses import JSONResponse
        retry = sh._login_retry_after(request)
        if retry:
            return JSONResponse(
                {"error": f"尝试过于频繁，请 {retry} 秒后再试"},
                status_code=429,
                headers={"Retry-After": str(retry)},
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        body = _json_object(body)
        if body is None:
            sh._record_login_failure(request)
            return JSONResponse({"error": "JSON body must be an object"}, status_code=400)
        password = body.get("password", "")
        if not isinstance(password, str) or len(password) > _MAX_PASSWORD_CHARS:
            sh._record_login_failure(request)
            return JSONResponse({"error": "密码格式无效"}, status_code=400)
        global_retry = sh._reserve_global_login_attempt()
        if global_retry:
            return JSONResponse(
                {"error": f"登录服务繁忙，请 {global_retry} 秒后重试"},
                status_code=429,
                headers={"Retry-After": str(global_retry)},
            )
        verified, queued_retry = await _run_public_password_verification(
            request, sh._verify_password_for_rotation, password
        )
        if queued_retry:
            return JSONResponse(
                {"error": f"尝试过于频繁，请 {queued_retry} 秒后再试"},
                status_code=429,
                headers={"Retry-After": str(queued_retry)},
            )
        if verified:
            sh._record_login_success(request)
            try:
                token = sh._create_session_for_credential(verified)
            except Exception as e:
                return _persistence_failure_response("login session", e)
            if token is None:
                return JSONResponse(
                    {"error": "密码已变更，请重新登录"},
                    status_code=409,
                    headers={"Cache-Control": "no-store"},
                )
            resp = JSONResponse({"ok": True})
            sh._set_session_cookie(resp, token, request)
            return resp
        sh._record_login_failure(request)
        return JSONResponse({"error": "密码错误"}, status_code=401)

    @mcp.custom_route("/auth/logout", methods=["POST"])
    async def auth_logout(request: Request) -> Response:
        """Invalidate session."""
        from starlette.responses import JSONResponse
        token = request.cookies.get("ombre_session")
        if token:
            try:
                sh._revoke_session(token)
            except Exception as e:
                return _persistence_failure_response("logout", e)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie("ombre_session")
        return resp

    @mcp.custom_route("/auth/change-password", methods=["POST"])
    async def auth_change_password(request: Request) -> Response:
        """Change dashboard password (requires current password)."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        if os.environ.get("OMBRE_DASHBOARD_PASSWORD", ""):
            return JSONResponse({"error": "当前使用环境变量密码，请直接修改 OMBRE_DASHBOARD_PASSWORD；改完重启后，旧的登录和 MCP 授权会自动失效"}, status_code=400)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        body = _json_object(body)
        if body is None:
            return JSONResponse({"error": "JSON body must be an object"}, status_code=400)
        current = body.get("current", "")
        new_pwd = body.get("new", "")
        if not isinstance(current, str) or not isinstance(new_pwd, str):
            return JSONResponse({"error": "密码格式无效"}, status_code=400)
        new_pwd = new_pwd.strip()
        if len(current) > _MAX_PASSWORD_CHARS:
            return JSONResponse({"error": "当前密码格式无效"}, status_code=400)
        proof = await _run_password_work(
            sh._verify_password_for_rotation, current
        )
        if proof is None:
            return JSONResponse({"error": "当前密码错误"}, status_code=401)
        if not 6 <= len(new_pwd) <= _MAX_PASSWORD_CHARS:
            return JSONResponse({"error": "新密码长度必须在 6-1024 位之间"}, status_code=400)
        try:
            password_hash = await _run_password_work(sh._hash_secret, new_pwd)
            token = _commit_password_rotation(proof, password_hash)
        except Exception as e:
            return _persistence_failure_response("password change", e)
        if token is None:
            return JSONResponse(
                {"error": "凭据已变更，请重试"},
                status_code=409,
                headers={"Cache-Control": "no-store"},
            )
        resp = JSONResponse({"ok": True})
        sh._set_session_cookie(resp, token, request)
        return resp

    @mcp.custom_route("/auth/recovery-question", methods=["GET"])
    async def auth_recovery_question(request: Request) -> Response:
        """Return the configured security question (public, no auth needed)."""
        from starlette.responses import JSONResponse
        q = sh._load_auth_data().get("security_question", "")
        return JSONResponse({"question": q or None})

    @mcp.custom_route("/auth/recover", methods=["POST"])
    async def auth_recover(request: Request) -> Response:
        """Reset password via security question answer."""
        from starlette.responses import JSONResponse
        if os.environ.get("OMBRE_DASHBOARD_PASSWORD", ""):
            return JSONResponse({"error": "当前使用环境变量密码，无法通过安全问题重置"}, status_code=400)
        if not sh._load_auth_data().get("security_answer_hash"):
            return JSONResponse({"error": "未设置安全问题，无法使用急救模式"}, status_code=400)
        try:
            retry = await asyncio.to_thread(_reserve_recovery_attempt, request)
        except Exception as e:
            return _persistence_failure_response("recovery rate limit", e)
        if retry:
            return JSONResponse(
                {"error": f"尝试过于频繁，请 {retry} 秒后再试"},
                status_code=429,
                headers={"Retry-After": str(retry)},
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        body = _json_object(body)
        if body is None:
            return JSONResponse({"error": "JSON body must be an object"}, status_code=400)
        answer = body.get("answer", "")
        new_pwd = body.get("new_password", "")
        if not isinstance(answer, str) or not isinstance(new_pwd, str):
            return JSONResponse({"error": "恢复参数格式无效"}, status_code=400)
        new_pwd = new_pwd.strip()
        if len(answer) > _MAX_SECURITY_ANSWER_CHARS:
            return JSONResponse({"error": "答案格式无效"}, status_code=400)
        verified = await _run_password_work(
            sh._verify_security_answer_for_rotation, answer
        )
        if not verified:
            return JSONResponse({"error": "答案不正确"}, status_code=401)
        if not 6 <= len(new_pwd) <= _MAX_PASSWORD_CHARS:
            return JSONResponse({"error": "新密码长度必须在 6-1024 位之间"}, status_code=400)
        try:
            password_hash = await _run_password_work(sh._hash_secret, new_pwd)
            token = _commit_password_rotation(verified, password_hash)
        except Exception as e:
            return _persistence_failure_response("password recovery", e)
        if token is None:
            return JSONResponse(
                {"error": "恢复凭据已变更，请重试"},
                status_code=409,
                headers={"Cache-Control": "no-store"},
            )
        resp = JSONResponse({"ok": True})
        sh._set_session_cookie(resp, token, request)
        return resp

    @mcp.custom_route("/auth/security-question", methods=["POST"])
    async def auth_set_security_question(request: Request) -> Response:
        """Set or update the security question (requires login)."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        body = _json_object(body)
        if body is None:
            return JSONResponse({"error": "JSON body must be an object"}, status_code=400)
        question = body.get("question", "")
        answer = body.get("answer", "")
        if not isinstance(question, str) or not isinstance(answer, str):
            return JSONResponse({"error": "问题和答案必须是字符串"}, status_code=400)
        question = question.strip()
        answer = answer.strip()
        if not question or not answer:
            return JSONResponse({"error": "问题和答案不能为空"}, status_code=400)
        if (
            len(question) > _MAX_SECURITY_QUESTION_CHARS
            or len(answer) > _MAX_SECURITY_ANSWER_CHARS
        ):
            return JSONResponse({"error": "问题或答案过长"}, status_code=400)
        generation = sh._authenticated_credential_generation(request)
        if generation is None:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        current = body.get("current", "")
        if not isinstance(current, str) or not current or len(current) > _MAX_PASSWORD_CHARS:
            return JSONResponse({"error": "请提供当前密码"}, status_code=400)
        retry = sh._login_retry_after(request) or sh._reserve_global_login_attempt()
        if retry:
            return JSONResponse({"error": "尝试过于频繁，请稍后再试"}, status_code=429,
                                headers={"Retry-After": str(retry)})
        proof, retry = await _run_public_password_verification(
            request, sh._verify_password_for_rotation, current
        )
        if retry:
            return JSONResponse({"error": "尝试过于频繁，请稍后再试"}, status_code=429,
                                headers={"Retry-After": str(retry)})
        if not proof:
            sh._record_login_failure(request)
            return JSONResponse({"error": "当前密码错误"}, status_code=401)
        if proof.generation != generation:
            return JSONResponse({"error": "凭据已变更，请重试"}, status_code=409)
        try:
            saved = await _run_password_work(
                sh._save_security_qa,
                question,
                answer,
                expected_generation=generation,
            )
        except Exception as e:
            return _persistence_failure_response("security question update", e)
        if not saved:
            return JSONResponse(
                {"error": "凭据已变更，请重试"},
                status_code=409,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse({"ok": True})
