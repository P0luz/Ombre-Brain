# ============================================================
# Module: MCP Server Entry Point (server.py)
# 模块：MCP 服务器主入口
#
# Starts the Ombre Brain MCP service and registers memory
# operation tools for Claude to call.
# 启动 Ombre Brain MCP 服务，注册记忆操作工具供 Claude 调用。
#
# Core responsibilities:
# 核心职责：
#   - Initialize config, bucket manager, dehydrator, decay engine
#     初始化配置、记忆桶管理器、脱水器、衰减引擎
#   - Expose 6 MCP tools:
#     暴露 6 个 MCP 工具：
#       breath — Surface unresolved memories or search by keyword
#                浮现未解决记忆 或 按关键词检索
#       hold   — Store a single memory (or write a `feel` reflection)
#                存储单条记忆（或写 feel 反思）
#       grow   — Diary digest, auto-split into multiple buckets
#                日记归档，自动拆分多桶
#       trace  — Modify metadata / resolved / delete
#                修改元数据 / resolved 标记 / 删除
#       pulse  — System status + bucket listing
#                系统状态 + 所有桶列表
#       dream  — Surface recent dynamic buckets for self-digestion
#                返回最近桶 供模型自省/写 feel
#
# Startup:
# 启动方式：
#   Local:  python server.py
#   Remote: OMBRE_TRANSPORT=streamable-http python server.py
#   Docker: docker-compose up
# ============================================================

import os
import sys
import random
from datetime import datetime, timedelta, date as _date
import logging
import asyncio
import hashlib
import hmac
import secrets
import time
import json as _json_lib
from contextlib import contextmanager, nullcontext
import httpx


# --- Ensure same-directory modules can be imported ---
# --- 确保同目录下的模块能被正确导入 ---
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

from bucket_manager import BucketManager
from dehydrator import Dehydrator
from decay_engine import DecayEngine
from embedding_engine import EmbeddingEngine
from import_memory import ImportEngine
from backup_exporter import BackupExporter
from utils import load_config, setup_logging, strip_wikilinks, count_tokens_approx, now_iso
from login_guard import LoginGuard, client_ip

# --- Load config & init logging / 加载配置 & 初始化日志 ---
config = load_config()
setup_logging(config.get("log_level", "INFO"))
logger = logging.getLogger("ombre_brain")

# --- Runtime env vars (port + webhook) / 运行时环境变量 ---
# OMBRE_PORT: HTTP/SSE 监听端口，默认 8000
try:
    OMBRE_PORT = int(os.environ.get("OMBRE_PORT", "8000") or "8000")
except ValueError:
    logger.warning("OMBRE_PORT 不是合法整数，回退到 8000")
    OMBRE_PORT = 8000

# OMBRE_HOOK_URL: 在 breath/dream 被调用后推送事件到该 URL（POST JSON）。
# OMBRE_HOOK_SKIP: 设为 true/1/yes 跳过推送。
# 详见 ENV_VARS.md。
OMBRE_HOOK_URL = os.environ.get("OMBRE_HOOK_URL", "").strip()
OMBRE_HOOK_SKIP = os.environ.get("OMBRE_HOOK_SKIP", "").strip().lower() in ("1", "true", "yes", "on")

# OMBRE_BACKUP_TRIGGER_TOKEN: shared secret for GitHub Actions to call /api/export-backup
# without a dashboard session cookie. Must match the value in the GitHub Actions secret.
# OMBRE_BACKUP_TRIGGER_TOKEN: GitHub Actions 调用 /api/export-backup 时使用的共享密钥。
OMBRE_BACKUP_TRIGGER_TOKEN = os.environ.get("OMBRE_BACKUP_TRIGGER_TOKEN", "").strip()

# OMBRE_SEAL_WORD: return-channel authenticity watchword. Only lives in the
# server's environment — never in code, database, or backups. Boot/retrieval
# tool returns end with [seal:<word>]; the AI-side usage doc says to verify it.
# Anything injected into the tool-return channel can't forge a reply that
# carries the right seal without knowing the word.
# OMBRE_SEAL_WORD: 返回通道防伪暗语。只存在于服务器环境变量——不进代码、
# 不进数据库、不进备份。开机/检索类工具返回末尾附 [seal:<暗语>]，AI 侧
# 使用说明要求核验；不知道暗语的注入内容伪造不出带正确 seal 的完整返回。
OMBRE_SEAL_WORD = os.environ.get("OMBRE_SEAL_WORD", "").strip()


def _with_seal(text: str) -> str:
    """Append the authenticity seal line to a tool return. 给工具返回附上防伪 seal 行。"""
    if OMBRE_SEAL_WORD:
        return f"{text}\n\n[seal:{OMBRE_SEAL_WORD}]"
    # Env missing → loud placeholder, never a silent blank (per spec)
    # 环境变量缺失 → 明显异常提示，绝不静默留空
    return f"{text}\n\n[seal:⚠️未配置——请检查 OMBRE_SEAL_WORD 环境变量]"


async def _fire_webhook(event: str, payload: dict) -> None:
    """
    Fire-and-forget POST to OMBRE_HOOK_URL with the given event payload.
    Failures are logged at WARNING level only — never propagated to the caller.
    """
    if OMBRE_HOOK_SKIP or not OMBRE_HOOK_URL:
        return
    try:
        body = {
            "event": event,
            "timestamp": time.time(),
            "payload": payload,
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(OMBRE_HOOK_URL, json=body)
    except Exception as e:
        logger.warning(f"Webhook push failed ({event} → {OMBRE_HOOK_URL}): {e}")

# --- Initialize core components / 初始化核心组件 ---
embedding_engine = EmbeddingEngine(config)            # Embedding engine first (BucketManager depends on it)
bucket_mgr = BucketManager(config, embedding_engine=embedding_engine)  # Bucket manager / 记忆桶管理器
dehydrator = Dehydrator(config)                      # Dehydrator / 脱水器
decay_engine = DecayEngine(config, bucket_mgr)       # Decay engine / 衰减引擎
import_engine = ImportEngine(config, bucket_mgr, dehydrator, embedding_engine)  # Import engine / 导入引擎
backup_exporter = BackupExporter(config, bucket_mgr) # Backup exporter / 备份导出器

# --- Create MCP server instance / 创建 MCP 服务器实例 ---
# host="0.0.0.0" so Docker container's SSE is externally reachable
# stdio mode ignores host (no network)
mcp = FastMCP(
    "Ombre Brain",
    host="0.0.0.0",
    port=OMBRE_PORT,
)


# =============================================================
# Dashboard Auth — simple cookie-based session auth
# Dashboard 认证 —— 基于 Cookie 的会话认证
#
# Env var OMBRE_DASHBOARD_PASSWORD overrides file-stored password.
# First visit with no password set → forced setup wizard.
# Sessions stored in memory (lost on restart, 7-day expiry).
# =============================================================
_sessions: dict[str, float] = {}  # {token: expiry_timestamp}


def _get_auth_file() -> str:
    return os.path.join(config["buckets_dir"], ".dashboard_auth.json")


def _load_password_hash() -> str | None:
    try:
        auth_file = _get_auth_file()
        if os.path.exists(auth_file):
            with open(auth_file, "r", encoding="utf-8") as f:
                return _json_lib.load(f).get("password_hash")
    except Exception:
        pass
    return None


def _save_password_hash(password: str) -> None:
    salt = secrets.token_hex(16)
    h = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    auth_file = _get_auth_file()
    os.makedirs(os.path.dirname(auth_file), exist_ok=True)
    with open(auth_file, "w", encoding="utf-8") as f:
        _json_lib.dump({"password_hash": f"{salt}:{h}"}, f)


def _verify_password_hash(password: str, stored: str) -> bool:
    if ":" not in stored:
        return False
    salt, h = stored.split(":", 1)
    return hmac.compare_digest(
        h, hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    )


def _is_setup_needed() -> bool:
    """True if no password is configured (env var or file)."""
    if os.environ.get("OMBRE_DASHBOARD_PASSWORD", ""):
        return False
    return _load_password_hash() is None


def _verify_any_password(password: str) -> bool:
    """Check password against env var (first) or stored hash."""
    env_pwd = os.environ.get("OMBRE_DASHBOARD_PASSWORD", "")
    if env_pwd:
        return hmac.compare_digest(password, env_pwd)
    stored = _load_password_hash()
    if not stored:
        return False
    return _verify_password_hash(password, stored)


def _create_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + 86400 * 7  # 7-day expiry
    return token


def _is_authenticated(request) -> bool:
    token = request.cookies.get("ombre_session")
    if not token:
        return False
    expiry = _sessions.get(token)
    if expiry is None or time.time() > expiry:
        _sessions.pop(token, None)
        return False
    return True


# --- App 的小钥匙 OMBRE_APP_TOKEN(2026-11-01)---
# 原生 App「Intertidal」的 Journal 页经 dwell-bridge 读 / 改 / 删他的记忆、信、便利贴。
# dwell 带 `Authorization: Bearer <它>`,**只开下面这张白名单**:设置、导入、改密码、回收站、搜索调试一律不开 ——
# 钥匙丢了也只是这几扇门,碰不到面板的其余部分。App 本身从不拿这把钥匙(它只有 dwell 的口令)。
# **不设 = 关着**,和以前一模一样。值与 dwell-bridge 的 `OMBRE_APP_TOKEN` 相同,不入库。
_APP_TOKEN_ROUTES = (
    ("GET", "/api/buckets"),
    ("GET", "/api/bucket/"), ("PATCH", "/api/bucket/"), ("POST", "/api/bucket/"), ("DELETE", "/api/bucket/"),
    ("GET", "/api/letters"), ("POST", "/api/letters/update"), ("POST", "/api/letters/delete"),
    ("GET", "/api/todos"), ("POST", "/api/todos/toggle"), ("POST", "/api/todos/update"), ("POST", "/api/todos/delete"),
)


def _app_token_ok(request) -> bool:
    token = os.environ.get("OMBRE_APP_TOKEN", "").strip()
    if not token:
        return False
    auth = request.headers.get("authorization", "")
    given = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not given or not hmac.compare_digest(given, token):
        return False
    path, method = request.url.path, request.method.upper()
    for m, p in _APP_TOKEN_ROUTES:
        if method != m:
            continue
        if path == p:
            return True
        # 「/api/bucket/<id>」只认恰好一段 id,别让前缀把更深的路径也放进来
        if p.endswith("/") and path.startswith(p) and path[len(p):] and "/" not in path[len(p):]:
            return True
    return False


def _require_auth(request):
    """Return JSONResponse(401) if not authenticated, else None."""
    from starlette.responses import JSONResponse
    if _app_token_ok(request):
        return None
    if not _is_authenticated(request):
        return JSONResponse(
            {"error": "Unauthorized", "setup_needed": _is_setup_needed()},
            status_code=401,
        )
    return None


# --- Auth endpoints ---
@mcp.custom_route("/auth/status", methods=["GET"])
async def auth_status(request):
    """Return auth state (authenticated, setup_needed)."""
    from starlette.responses import JSONResponse
    return JSONResponse({
        "authenticated": _is_authenticated(request),
        "setup_needed": _is_setup_needed(),
    })


@mcp.custom_route("/auth/setup", methods=["POST"])
async def auth_setup_endpoint(request):
    """Initial password setup (only when no password is configured)."""
    from starlette.responses import JSONResponse
    if not _is_setup_needed():
        return JSONResponse({"error": "Already configured"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    password = body.get("password", "").strip()
    if len(password) < 6:
        return JSONResponse({"error": "密码不能少于6位"}, status_code=400)
    _save_password_hash(password)
    token = _create_session()
    resp = JSONResponse({"ok": True})
    resp.set_cookie("ombre_session", token, httponly=True, samesite="lax", max_age=86400 * 7)
    return resp


_login_guard = LoginGuard()


@mcp.custom_route("/auth/login", methods=["POST"])
async def auth_login(request):
    """Login with password."""
    from starlette.responses import JSONResponse
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    # 连错锁一会儿(2026-10-28,规则见 login_guard.py 开头)
    ip = client_ip(request.headers, request.client.host if request.client else "")
    allowed, wait, scope = _login_guard.check(ip)
    if not allowed:
        logger.warning(f"[login] 锁着,不让试: {'全部' if scope == 'all' else ip} {wait} 秒")
        return JSONResponse({"error": f"试错太多次了,{(wait + 59) // 60} 分钟后再试"}, status_code=429,
                            headers={"Retry-After": str(wait)})
    password = body.get("password", "")
    if _verify_any_password(password):
        _login_guard.ok(ip)
        token = _create_session()
        resp = JSONResponse({"ok": True})
        resp.set_cookie("ombre_session", token, httponly=True, samesite="lax", max_age=86400 * 7)
        return resp
    _login_guard.fail(ip)
    logger.warning(f"[login] 密码错误 {ip}")
    return JSONResponse({"error": "密码错误"}, status_code=401)


@mcp.custom_route("/auth/logout", methods=["POST"])
async def auth_logout(request):
    """Invalidate session."""
    from starlette.responses import JSONResponse
    token = request.cookies.get("ombre_session")
    if token:
        _sessions.pop(token, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("ombre_session")
    return resp


@mcp.custom_route("/auth/change-password", methods=["POST"])
async def auth_change_password(request):
    """Change dashboard password (requires current password)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    if os.environ.get("OMBRE_DASHBOARD_PASSWORD", ""):
        return JSONResponse({"error": "当前使用环境变量密码，请直接修改 OMBRE_DASHBOARD_PASSWORD"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    current = body.get("current", "")
    new_pwd = body.get("new", "").strip()
    if not _verify_any_password(current):
        return JSONResponse({"error": "当前密码错误"}, status_code=401)
    if len(new_pwd) < 6:
        return JSONResponse({"error": "新密码不能少于6位"}, status_code=400)
    _save_password_hash(new_pwd)
    _sessions.clear()
    token = _create_session()
    resp = JSONResponse({"ok": True})
    resp.set_cookie("ombre_session", token, httponly=True, samesite="lax", max_age=86400 * 7)
    return resp


# =============================================================
# /health endpoint: lightweight keepalive
# 轻量保活接口
# For Cloudflare Tunnel or reverse proxy to ping, preventing idle timeout
# 供 Cloudflare Tunnel 或反代定期 ping，防止空闲超时断连
# =============================================================
@mcp.custom_route("/", methods=["GET"])
async def root_redirect(request):
    from starlette.responses import RedirectResponse
    return RedirectResponse(url="/dashboard")


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    from starlette.responses import JSONResponse
    try:
        stats = await bucket_mgr.get_stats()
        return JSONResponse({
            "status": "ok",
            "buckets": stats["permanent_count"] + stats["dynamic_count"],
            "decay_engine": "running" if decay_engine.is_running else "stopped",
        })
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)


# =============================================================
# /breath-hook endpoint: Dedicated hook for SessionStart
# 会话启动专用挂载点
# =============================================================
@mcp.custom_route("/breath-hook", methods=["GET"])
async def breath_hook(request):
    from starlette.responses import PlainTextResponse
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        # pinned
        pinned = [b for b in all_buckets if b["metadata"].get("pinned") or b["metadata"].get("protected")]
        # top 2 unresolved by score
        unresolved = [b for b in all_buckets
                      if not b["metadata"].get("resolved", False)
                      and b["metadata"].get("type") not in ("permanent", "feel")
                      and not b["metadata"].get("pinned")
                      and not b["metadata"].get("protected")]
        scored = sorted(unresolved, key=lambda b: decay_engine.calculate_score(b["metadata"]), reverse=True)

        parts = []
        token_budget = 10000
        for b in pinned:
            summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), {k: v for k, v in b["metadata"].items() if k != "tags"})
            parts.append(f"📌 [核心准则] {summary}")
            token_budget -= count_tokens_approx(summary)

        # Diversity: top-1 fixed + shuffle rest from top-20
        candidates = list(scored)
        if len(candidates) > 1:
            top1 = [candidates[0]]
            pool = candidates[1:min(20, len(candidates))]
            random.shuffle(pool)
            candidates = top1 + pool + candidates[min(20, len(candidates)):]
        # Hard cap: max 20 surfacing buckets in hook
        candidates = candidates[:20]

        for b in candidates:
            if token_budget <= 0:
                break
            summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), {k: v for k, v in b["metadata"].items() if k != "tags"})
            summary_tokens = count_tokens_approx(summary)
            if summary_tokens > token_budget:
                break
            parts.append(summary)
            token_budget -= summary_tokens

        if not parts:
            await _fire_webhook("breath_hook", {"surfaced": 0})
            return PlainTextResponse("")
        body_text = "[Ombre Brain - 记忆浮现]\n" + "\n---\n".join(parts)
        await _fire_webhook("breath_hook", {"surfaced": len(parts), "chars": len(body_text)})
        return PlainTextResponse(body_text)
    except Exception as e:
        logger.warning(f"Breath hook failed: {e}")
        return PlainTextResponse("")


# =============================================================
# /dream-hook endpoint: Dedicated hook for Dreaming
# Dreaming 专用挂载点
# =============================================================
@mcp.custom_route("/dream-hook", methods=["GET"])
async def dream_hook(request):
    from starlette.responses import PlainTextResponse
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        candidates = [
            b for b in all_buckets
            if b["metadata"].get("type") not in ("permanent", "feel")
            and not b["metadata"].get("pinned", False)
            and not b["metadata"].get("protected", False)
        ]
        candidates.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
        recent = candidates[:10]

        if not recent:
            return PlainTextResponse("")

        parts = []
        for b in recent:
            meta = b["metadata"]
            resolved_tag = "[已解决]" if meta.get("resolved", False) else "[未解决]"
            parts.append(
                f"{meta.get('name', b['id'])} {resolved_tag} "
                f"V{meta.get('valence', 0.5):.1f}/A{meta.get('arousal', 0.3):.1f}\n"
                f"{strip_wikilinks(b['content'][:200])}"
            )

        body_text = "[Ombre Brain - Dreaming]\n" + "\n---\n".join(parts)
        await _fire_webhook("dream_hook", {"surfaced": len(parts), "chars": len(body_text)})
        return PlainTextResponse(body_text)
    except Exception as e:
        logger.warning(f"Dream hook failed: {e}")
        return PlainTextResponse("")


# =============================================================
# Internal helper: merge-or-create
# 内部辅助：检查是否可合并，可以则合并，否则新建
# Shared by hold and grow to avoid duplicate logic
# hold 和 grow 共用，避免重复逻辑
# =============================================================
async def _merge_or_create(
    content: str,
    tags: list,
    importance: int,
    domain: list,
    valence: float,
    arousal: float,
    name: str = "",
) -> tuple[str, bool]:
    """
    Check if a similar bucket exists for merging; merge if so, create if not.
    Returns (bucket_id_or_name, is_merged).
    检查是否有相似桶可合并，有则合并，无则新建。
    返回 (桶ID或名称, 是否合并)。
    """
    try:
        existing = await bucket_mgr.search(content, limit=1, domain_filter=domain or None)
    except Exception as e:
        logger.warning(f"Search for merge failed, creating new / 合并搜索失败，新建: {e}")
        existing = []

    if existing and existing[0].get("score", 0) > config.get("merge_threshold", 75):
        bucket = existing[0]
        # --- Never merge into pinned/protected buckets ---
        # --- 不合并到钉选/保护桶 ---
        if not (bucket["metadata"].get("pinned") or bucket["metadata"].get("protected")):
            try:
                merged = await dehydrator.merge(bucket["content"], content)
                old_v = bucket["metadata"].get("valence", 0.5)
                old_a = bucket["metadata"].get("arousal", 0.3)
                merged_valence = round((old_v + valence) / 2, 2)
                merged_arousal = round((old_a + arousal) / 2, 2)
                await bucket_mgr.update(
                    bucket["id"],
                    content=merged,
                    tags=list(set(bucket["metadata"].get("tags", []) + tags)),
                    importance=max(bucket["metadata"].get("importance", 5), importance),
                    domain=list(set(bucket["metadata"].get("domain", []) + domain)),
                    valence=merged_valence,
                    arousal=merged_arousal,
                )
                # --- Update embedding after merge ---
                try:
                    await embedding_engine.generate_and_store(bucket["id"], merged)
                except Exception:
                    pass
                return bucket["metadata"].get("name", bucket["id"]), bucket["id"], True
            except Exception as e:
                logger.warning(f"Merge failed, creating new / 合并失败，新建: {e}")

    bucket_id = await bucket_mgr.create(
        content=content,
        tags=tags,
        importance=importance,
        domain=domain,
        valence=valence,
        arousal=arousal,
        name=name or None,
    )
    # --- Generate embedding for new bucket ---
    try:
        await embedding_engine.generate_and_store(bucket_id, content)
    except Exception:
        pass
    return name or bucket_id, bucket_id, False


def _format_bucket_summary_line(b: dict, prefix: str = "") -> str:
    """Return a compact one-line summary for a bucket (no content text)."""
    meta = b["metadata"]
    bid = b["id"]
    name = meta.get("name", bid)
    domain_val = meta.get("domain", [])
    if isinstance(domain_val, list):
        domain_str = "/".join(domain_val) if domain_val else "-"
    else:
        domain_str = str(domain_val) if domain_val else "-"
    valence = meta.get("valence")
    arousal = meta.get("arousal")
    if isinstance(valence, (int, float)) and isinstance(arousal, (int, float)):
        emotion = f"(v={float(valence):.2f},a={float(arousal):.2f})"
    elif isinstance(valence, (int, float)):
        emotion = f"(v={float(valence):.2f})"
    else:
        emotion = "-"
    importance = meta.get("importance", "-")
    last_active = meta.get("last_active") or meta.get("created", "-")
    if isinstance(last_active, str) and len(last_active) > 10:
        last_active = last_active[:10]
    line = f"[bucket_id:{bid}] {name} | 主题:{domain_str} | 情感:{emotion} | 重要度:{importance} | 更新:{last_active}"
    return f"{prefix}{line}" if prefix else line


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


# breath 向量通道的放行线(见 _breath_impl 里那段注释)。改值 + restart 即生效;设 0.5 = 回到 2026-09-30 之前的行为。
VECTOR_MIN_SIM = _env_float("OMBRE_VECTOR_MIN_SIM", 0.70)


def _is_expired(meta: dict, today: str = "") -> bool:
    """到期记忆(2026-08-19):expires_at 是 YYYY-MM-DD,**过了那天**才算过期(当天仍有效)。
    与 trigger_date 是一对镜像:那个「到那天浮现」,这个「到那天退休」。
    数据保留可查,只是不再被检索召回 —— 和 dormant 同一档待遇。
    ⚠️ 解析不了的日期一律**当成没过期**:宁可多留一条,不可因为格式手滑就把记忆藏起来。"""
    raw = meta.get("expires_at")
    if not raw:
        return False
    try:
        return str(raw)[:10] < (today or _bj_today())
    except Exception:
        return False


def _is_dormant_candidate(meta: dict) -> bool:
    """True if bucket meets auto-dormant criteria: >30d untouched, importance<3, not pinned."""
    if meta.get("pinned") or meta.get("protected"):
        return False
    try:
        importance = int(meta.get("importance", 5) or 5)
    except (ValueError, TypeError):
        importance = 5
    if importance >= 3:
        return False
    la = meta.get("last_active") or meta.get("created", "")
    if not la:
        return False
    try:
        last_active = datetime.fromisoformat(str(la).replace("Z", "+00:00")).replace(tzinfo=None)
        return (datetime.now() - last_active).total_seconds() / 86400 > 30
    except (ValueError, TypeError):
        return False


async def _apply_dormant_sweep(buckets: list) -> int:
    """Mark dormant-eligible buckets. Returns count of newly-marked buckets."""
    newly_marked = 0
    for b in buckets:
        meta = b["metadata"]
        if not meta.get("dormant") and _is_dormant_candidate(meta):
            try:
                await bucket_mgr.update(b["id"], dormant=True)
                meta["dormant"] = True
                newly_marked += 1
            except Exception:
                pass
    return newly_marked


# =============================================================
# Tool 1: breath — Breathe
# 工具 1：breath — 呼吸
#
# No args: surface highest-weight unresolved memories (active push)
# 无参数：浮现权重最高的未解决记忆
# With args: search by keyword + emotion coordinates
# 有参数：按关键词+情感坐标检索记忆
# =============================================================
@mcp.tool()
async def breath(
    query: str = "",
    max_tokens: int = 10000,
    domain: str = "",
    valence: float = -1,
    arousal: float = -1,
    max_results: int = 5,
    importance_min: int = -1,
    mode: str = "summary",
    date_from: str = "",
    date_to: str = "",
    include_dormant: bool = False,
) -> str:
    """检索/浮现记忆。不传query或传空=自动浮现,有query=关键词检索。max_tokens控制返回总token上限(默认10000)。domain逗号分隔,valence/arousal 0~1(-1忽略)。max_results控制返回数量上限(默认5,最大50),超出的在末尾附注还有N个相关桶未显示,钉选桶不计入名额。importance_min>=1时按重要度批量拉取(不走语义搜索,按importance降序)。mode=summary(默认)时每个桶仅返回一行摘要(bucket_id+桶名+主题+情感坐标+重要度+更新时间),mode=full时返回完整内容;query非空时忽略mode始终返回full。搜索排名三级权重：精确匹配tags(keywords)字段最高→关键词出现在content次高→语义向量匹配最低;≤4字中文短词强制精确子串匹配防止被拆散。date_from/date_to(YYYY-MM-DD)按桶last_active过滤,可与其他参数组合;钉选桶不受日期过滤。命中桶若metadata含related字段(bucket_id列表),在该桶结果下附一行关联提示(id+名称,不展开全文)。include_dormant=false(默认)时休眠桶不出现在结果中;include_dormant=true时包含休眠桶。休眠条件：>30天未访问且importance<3且非钉选桶；被命中后自动解除休眠。返回末尾附[seal:...]防伪字段。"""
    return _with_seal(await _breath_impl(
        query=query, max_tokens=max_tokens, domain=domain, valence=valence,
        arousal=arousal, max_results=max_results, importance_min=importance_min,
        mode=mode, date_from=date_from, date_to=date_to, include_dormant=include_dormant,
    ))


async def _breath_impl(
    query: str = "",
    max_tokens: int = 10000,
    domain: str = "",
    valence: float = -1,
    arousal: float = -1,
    max_results: int = 5,
    importance_min: int = -1,
    mode: str = "summary",
    date_from: str = "",
    date_to: str = "",
    include_dormant: bool = False,
) -> str:
    await decay_engine.ensure_started()
    await backup_exporter.ensure_started()
    max_results = min(max_results, 50)
    max_tokens = min(max_tokens, 20000)

    # --- Parse optional date range filters / 解析可选日期范围过滤 ---
    dt_from: _date | None = None
    dt_to: _date | None = None
    if date_from.strip():
        try:
            dt_from = datetime.strptime(date_from.strip(), "%Y-%m-%d").date()
        except ValueError:
            return "date_from 格式错误，请使用 YYYY-MM-DD（如 2026-01-01）。"
    if date_to.strip():
        try:
            dt_to = datetime.strptime(date_to.strip(), "%Y-%m-%d").date()
        except ValueError:
            return "date_to 格式错误，请使用 YYYY-MM-DD（如 2026-12-31）。"
    if dt_from and dt_to and dt_from > dt_to:
        return "date_from 不能晚于 date_to。"

    def _date_ok(b: dict) -> bool:
        """Return True if bucket last_active falls within [dt_from, dt_to]."""
        if dt_from is None and dt_to is None:
            return True
        la = b["metadata"].get("last_active") or b["metadata"].get("created", "")
        if not la:
            return True
        try:
            la_dt = datetime.fromisoformat(str(la).replace("Z", "+00:00")).date()
        except (ValueError, TypeError):
            return True
        if dt_from and la_dt < dt_from:
            return False
        if dt_to and la_dt > dt_to:
            return False
        return True

    async def _related_hint(b: dict, name_cache: dict) -> str:
        """Return a one-line related-buckets hint, or empty string if none."""
        related = b["metadata"].get("related")
        if not related or not isinstance(related, list):
            return ""
        parts = []
        for rid in related[:5]:  # cap at 5 to keep output compact
            if rid in name_cache:
                name = name_cache[rid]
            else:
                try:
                    rb = await bucket_mgr.get(rid)
                    name = rb["metadata"].get("name", rid) if rb else rid
                    name_cache[rid] = name
                except Exception:
                    name = rid
            parts.append(f"[bucket_id:{rid}] {name}")
        return "→ 关联: " + " | ".join(parts)

    # --- importance_min mode: bulk fetch by importance threshold ---
    # --- 重要度批量拉取模式：跳过语义搜索，按 importance 降序返回 ---
    if importance_min >= 1:
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            return f"记忆系统暂时无法访问: {e}"
        await _apply_dormant_sweep(all_buckets)
        filtered = [
            b for b in all_buckets
            if int(b["metadata"].get("importance", 0)) >= importance_min
            and b["metadata"].get("type") not in ("feel",)
            and _date_ok(b)
            and (include_dormant or not b["metadata"].get("dormant", False))
            and not _is_expired(b["metadata"])
        ]
        filtered.sort(key=lambda b: int(b["metadata"].get("importance", 0)), reverse=True)
        filtered_total = len(filtered)
        filtered = filtered[:max_results]
        if not filtered:
            return f"没有重要度 >= {importance_min} 的记忆。"
        name_cache = {b["id"]: b["metadata"].get("name", b["id"]) for b in all_buckets}
        results = []
        token_used = 0
        for b in filtered:
            if token_used >= max_tokens:
                break
            try:
                imp = b["metadata"].get("importance", 0)
                if mode == "summary":
                    line = _format_bucket_summary_line(b, prefix=f"[importance:{imp}] ")
                else:
                    clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                    summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
                    line = f"[importance:{imp}] [bucket_id:{b['id']}] {summary}"
                hint = await _related_hint(b, name_cache)
                if hint:
                    line += "\n" + hint
                t = count_tokens_approx(line)
                if token_used + t > max_tokens:
                    break
                results.append(line)
                token_used += t
            except Exception as e:
                logger.warning(f"importance_min dehydrate failed: {e}")
        hidden_by_cap = max(0, filtered_total - max_results)
        if hidden_by_cap > 0:
            results.append(f"还有 {hidden_by_cap} 个相关桶未显示。")
        return "\n---\n".join(results) if results else "没有可以展示的记忆。"

    # --- No args or empty query: surfacing mode (weight pool active push) ---
    # --- 无参数或空query：浮现模式（权重池主动推送）---
    if not query or not query.strip():
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            logger.error(f"Failed to list buckets for surfacing / 浮现列桶失败: {e}")
            return "记忆系统暂时无法访问。"

        # --- Pinned/protected buckets: always surface as core principles ---
        # --- 钉选桶：作为核心准则，始终浮现 ---
        pinned_buckets = [
            b for b in all_buckets
            if b["metadata"].get("pinned") or b["metadata"].get("protected")
        ]
        name_cache = {b["id"]: b["metadata"].get("name", b["id"]) for b in all_buckets}
        pinned_results = []
        for b in pinned_buckets:
            try:
                if mode == "summary":
                    line = "📌 [核心准则] " + _format_bucket_summary_line(b)
                else:
                    clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                    summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
                    line = f"📌 [核心准则] [bucket_id:{b['id']}] {summary}"
                hint = await _related_hint(b, name_cache)
                if hint:
                    line += "\n" + hint
                pinned_results.append(line)
            except Exception as e:
                logger.warning(f"Failed to dehydrate pinned bucket / 钉选桶脱水失败: {e}")
                continue

        # --- Auto-mark dormant candidates before surfacing ---
        await _apply_dormant_sweep(all_buckets)

        # --- Unresolved buckets: surface top N by weight ---
        # --- 未解决桶：按权重浮现前 N 条 ---
        unresolved = [
            b for b in all_buckets
            if not b["metadata"].get("resolved", False)
            and b["metadata"].get("type") not in ("permanent", "feel")
            and not b["metadata"].get("pinned", False)
            and not b["metadata"].get("protected", False)
            and _date_ok(b)
            and (include_dormant or not b["metadata"].get("dormant", False))
            and not _is_expired(b["metadata"])
        ]

        logger.info(
            f"Breath surfacing: {len(all_buckets)} total, "
            f"{len(pinned_buckets)} pinned, {len(unresolved)} unresolved"
        )

        scored = sorted(
            unresolved,
            key=lambda b: decay_engine.calculate_score(b["metadata"]),
            reverse=True,
        )

        if scored:
            top_scores = [(b["metadata"].get("name", b["id"]), decay_engine.calculate_score(b["metadata"])) for b in scored[:5]]
            logger.info(f"Top unresolved scores: {top_scores}")

        # --- Cold-start detection: never-seen important buckets surface first ---
        # --- 冷启动检测：从未被访问过且重要度>=8的桶优先插入最前面（最多2个）---
        cold_start = [
            b for b in unresolved
            if int(b["metadata"].get("activation_count", 0)) == 0
            and int(b["metadata"].get("importance", 0)) >= 8
        ][:2]
        cold_start_ids = {b["id"] for b in cold_start}
        # Merge: cold_start first, then scored (excluding duplicates)
        scored_deduped = [b for b in scored if b["id"] not in cold_start_ids]
        scored_with_cold = cold_start + scored_deduped

        # --- Token-budgeted surfacing with diversity + hard cap ---
        # --- 按 token 预算浮现，带多样性 + 硬上限 ---
        # Top-1 always surfaces; rest sampled from top-20 for diversity
        token_budget = max_tokens
        for r in pinned_results:
            token_budget -= count_tokens_approx(r)

        candidates = list(scored_with_cold)
        if len(candidates) > 1:
            # Cold-start buckets stay at front; shuffle rest from top-20
            n_cold = len(cold_start)
            non_cold = candidates[n_cold:]
            if len(non_cold) > 1:
                top1 = [non_cold[0]]
                pool = non_cold[1:min(20, len(non_cold))]
                random.shuffle(pool)
                non_cold = top1 + pool + non_cold[min(20, len(non_cold)):]
            candidates = cold_start + non_cold
        # Hard cap: never surface more than max_results buckets (pinned not counted)
        total_unresolved = len(scored_with_cold)
        candidates = candidates[:max_results]

        dynamic_results = []
        for b in candidates:
            if token_budget <= 0:
                break
            try:
                score = decay_engine.calculate_score(b["metadata"])
                if mode == "summary":
                    line = _format_bucket_summary_line(b, prefix=f"[权重:{score:.2f}] ")
                else:
                    clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                    summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
                    # NOTE: no touch() here — surfacing should NOT reset decay timer
                    line = f"[权重:{score:.2f}] [bucket_id:{b['id']}] {summary}"
                hint = await _related_hint(b, name_cache)
                if hint:
                    line += "\n" + hint
                line_tokens = count_tokens_approx(line)
                if line_tokens > token_budget:
                    break
                dynamic_results.append(line)
                token_budget -= line_tokens
            except Exception as e:
                logger.warning(f"Failed to dehydrate surfaced bucket / 浮现脱水失败: {e}")
                continue

        if not pinned_results and not dynamic_results:
            return "权重池平静，没有需要处理的记忆。"

        hidden_unresolved = max(0, total_unresolved - len(dynamic_results))
        parts = []
        if pinned_results:
            parts.append("=== 核心准则 ===\n" + "\n---\n".join(pinned_results))
        if dynamic_results:
            surfacing_section = "=== 浮现记忆 ===\n" + "\n---\n".join(dynamic_results)
            if hidden_unresolved > 0:
                surfacing_section += f"\n\n还有 {hidden_unresolved} 个相关桶未显示。"
            parts.append(surfacing_section)
        return "\n\n".join(parts)

    # --- Feel retrieval: domain="feel" is a special channel ---
    # --- Feel 检索：domain="feel" 是独立入口 ---
    if domain.strip().lower() == "feel":
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
            feels = [b for b in all_buckets if b["metadata"].get("type") == "feel" and _date_ok(b)]
            feels.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
            if not feels:
                return "没有留下过 feel。"
            results = []
            for f in feels:
                created = f["metadata"].get("created", "")
                entry = f"[{created}] [bucket_id:{f['id']}]\n{strip_wikilinks(f['content'])}"
                results.append(entry)
                if count_tokens_approx("\n---\n".join(results)) > max_tokens:
                    break
            return "=== 你留下的 feel ===\n" + "\n---\n".join(results)
        except Exception as e:
            logger.error(f"Feel retrieval failed: {e}")
            return "读取 feel 失败。"

    # --- With args: search mode (keyword + vector dual channel) ---
    # --- 有参数：检索模式（关键词 + 向量双通道）---
    # --- 按 ID 直读：query 恰好是某个桶的 12 位 ID 时，直接返回未压缩原文 ---
    # --- Direct bucket-id read: bypass search/compression so edits act on the original bullets ---
    _qid = query.strip().lower()
    if len(_qid) == 12 and all(c in "0123456789abcdef" for c in _qid):
        _direct = await bucket_mgr.get(_qid)
        if _direct:
            _raw = _direct.get("content") or ""
            _nm = (_direct.get("metadata") or {}).get("name", "")
            return _with_seal(f"[bucket_id:{_qid}] [{_nm}]（原文直读，未压缩）\n\n{_raw}")
    domain_filter = [d.strip() for d in domain.split(",") if d.strip()] or None
    q_valence = valence if 0 <= valence <= 1 else None
    q_arousal = arousal if 0 <= arousal <= 1 else None

    fetch_limit = max_results * 4 + 10
    try:
        matches = await bucket_mgr.search(
            query,
            limit=fetch_limit,
            domain_filter=domain_filter,
            query_valence=q_valence,
            query_arousal=q_arousal,
        )
    except Exception as e:
        logger.error(f"Search failed / 检索失败: {e}")
        return "检索过程出错，请稍后重试。"

    # --- Exclude pinned/protected from search results (they surface in surfacing mode) ---
    # --- 搜索模式排除钉选桶（它们在浮现模式中始终可见）---
    matches = [b for b in matches if not (b["metadata"].get("pinned") or b["metadata"].get("protected"))]

    # --- Vector similarity channel: find semantically related buckets ---
    # --- 向量相似度通道：找到语义相关的桶 ---
    # 放行线 OMBRE_VECTOR_MIN_SIM(2026-09-30 由写死的 0.5 改成可调,默认 0.70)。
    # 真实库实测(410 条、gemini-embedding-001,20 个查询):0.5 形同虚设 —— 任何查询都有 430~447 个桶过线,
    # 库里真没有的事(「她养的狗」「搬家」)也照样拿满 5 条「语义关联」,几乎永远到不了「未找到相关记忆」。
    # 真没有的事最高分 0.56~0.69;真有的事(含换说法)大多 0.70~0.81。量法见 INTERNALS 3.3.1。
    matched_ids = {b["id"] for b in matches}
    try:
        vector_results = await embedding_engine.search_similar(query, top_k=fetch_limit)
        for bucket_id, sim_score in vector_results:
            if bucket_id not in matched_ids and sim_score > VECTOR_MIN_SIM:
                bucket = await bucket_mgr.get(bucket_id)
                if bucket and not (bucket["metadata"].get("pinned") or bucket["metadata"].get("protected")):
                    bucket["score"] = round(sim_score * 100, 2)
                    bucket["vector_match"] = True
                    matches.append(bucket)
                    matched_ids.add(bucket_id)
    except Exception as e:
        logger.warning(f"Vector search failed, using keyword only / 向量搜索失败: {e}")

    # --- Apply date filter after both channels are collected ---
    if dt_from or dt_to:
        matches = [b for b in matches if _date_ok(b)]

    # --- Apply dormant sweep + filter ---
    await _apply_dormant_sweep(matches)
    if not include_dormant:
        matches = [b for b in matches if not b["metadata"].get("dormant", False)]
    # 到期记忆退出检索(2026-08-19)。放在 dormant 之后,同一档待遇。
    matches = [b for b in matches if not _is_expired(b["metadata"])]

    # --- Cap to max_results; track hidden count for end-of-response note ---
    total_matches = len(matches)
    display_matches = matches[:max_results]
    hidden_by_cap = max(0, total_matches - max_results)
    name_cache = {b["id"]: b["metadata"].get("name", b["id"]) for b in matches}

    results = []
    token_used = 0
    for bucket in display_matches:
        if token_used >= max_tokens:
            break
        try:
            clean_meta = {k: v for k, v in bucket["metadata"].items() if k != "tags"}
            # --- Memory reconstruction: shift displayed valence by current mood ---
            # --- 记忆重构：根据当前情绪微调展示层 valence（±0.1）---
            if q_valence is not None and "valence" in clean_meta:
                original_v = float(clean_meta.get("valence", 0.5))
                shift = (q_valence - 0.5) * 0.2  # ±0.1 max shift
                clean_meta["valence"] = max(0.0, min(1.0, original_v + shift))
            summary = await dehydrator.dehydrate(strip_wikilinks(bucket["content"]), clean_meta)
            summary_tokens = count_tokens_approx(summary)
            if token_used + summary_tokens > max_tokens:
                break
            await bucket_mgr.touch(bucket["id"])
            if bucket.get("vector_match"):
                line = f"[语义关联] [bucket_id:{bucket['id']}] {summary}"
            else:
                line = f"[bucket_id:{bucket['id']}] {summary}"
            hint = await _related_hint(bucket, name_cache)
            if hint:
                line += "\n" + hint
            results.append(line)
            token_used += summary_tokens
        except Exception as e:
            logger.warning(f"Failed to dehydrate search result / 检索结果脱水失败: {e}")
            continue

    # --- Random surfacing: when search returns < 3, 40% chance to float old memories ---
    # --- 随机浮现：检索结果不足 3 条时，40% 概率从低权重旧桶里漂上来 ---
    if total_matches < 3 and random.random() < 0.4:
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
            matched_ids = {b["id"] for b in matches}
            low_weight = [
                b for b in all_buckets
                if b["id"] not in matched_ids
                and decay_engine.calculate_score(b["metadata"]) < 2.0
            ]
            if low_weight:
                drifted = random.sample(low_weight, min(random.randint(1, 3), len(low_weight)))
                drift_results = []
                for b in drifted:
                    clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                    summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
                    drift_results.append(f"[surface_type: random]\n{summary}")
                results.append("--- 忽然想起来 ---\n" + "\n---\n".join(drift_results))
        except Exception as e:
            logger.warning(f"Random surfacing failed / 随机浮现失败: {e}")

    if not results:
        await _fire_webhook("breath", {"mode": "empty", "matches": 0})
        return "未找到相关记忆。"

    if hidden_by_cap > 0:
        results.append(f"还有 {hidden_by_cap} 个相关桶未显示。")

    final_text = "\n---\n".join(results)
    await _fire_webhook("breath", {"mode": "ok", "matches": total_matches, "chars": len(final_text)})
    return final_text


# =============================================================
# Tool 2: hold — Hold on to this
# 工具 2：hold — 握住，留下来
# =============================================================
def _valid_date(s: str) -> bool:
    """YYYY-MM-DD 格式校验。"""
    try:
        datetime.strptime((s or "").strip(), "%Y-%m-%d")
        return True
    except ValueError:
        return False


@mcp.tool()
async def hold(
    content: str,
    tags: str = "",
    importance: int = 5,
    pinned: bool = False,
    feel: bool = False,
    source_bucket: str = "",    valence: float = -1,
    arousal: float = -1,
    trigger_date: str = "",
) -> str:
    """存储单条记忆,自动打标+合并。tags逗号分隔,importance 1-10。pinned=True创建永久钉选桶。feel=True存储你的第一人称感受(不参与普通浮现)。source_bucket=被消化的记忆桶ID(feel模式下,标记源记忆为已消化)。trigger_date=YYYY-MM-DD设前瞻记忆:某天要报名/出成绩/纪念日,到那天这个桶会自动出现在awaken的「今日浮现」区,不用靠检索碰运气。"""
    await decay_engine.ensure_started()

    # --- Input validation / 输入校验 ---
    if not content or not content.strip():
        return "内容为空，无法存储。"

    importance = max(1, min(10, importance))
    extra_tags = [t.strip() for t in tags.split(",") if t.strip()]

    # --- Feel mode: store as feel type, minimal metadata ---
    # --- Feel 模式：存为 feel 类型，最少元数据 ---
    if feel:
        # Feel valence/arousal = model's own perspective
        feel_valence = valence if 0 <= valence <= 1 else 0.5
        feel_arousal = arousal if 0 <= arousal <= 1 else 0.3
        bucket_id = await bucket_mgr.create(
            content=content,
            tags=[],
            importance=5,
            domain=[],
            valence=feel_valence,
            arousal=feel_arousal,
            name=None,
            bucket_type="feel",
        )
        try:
            await embedding_engine.generate_and_store(bucket_id, content)
        except Exception:
            pass
        # --- Mark source memory as digested + store model's valence perspective ---
        # --- 标记源记忆为已消化 + 存储模型视角的 valence ---
        if source_bucket and source_bucket.strip():
            try:
                update_kwargs = {"digested": True}
                if 0 <= valence <= 1:
                    update_kwargs["model_valence"] = feel_valence
                await bucket_mgr.update(source_bucket.strip(), **update_kwargs)
            except Exception as e:
                logger.warning(f"Failed to mark source as digested / 标记已消化失败: {e}")
        return f"🫧feel→{bucket_id}"

    # --- Step 1: auto-tagging / 自动打标 ---
    try:
        analysis = await dehydrator.analyze(content)
    except Exception as e:
        logger.warning(f"Auto-tagging failed, using defaults / 自动打标失败: {e}")
        analysis = {
            "domain": ["未分类"], "valence": 0.5, "arousal": 0.3,
            "tags": [], "suggested_name": "",
        }

    domain = analysis["domain"]
    auto_valence = analysis["valence"]
    auto_arousal = analysis["arousal"]
    auto_tags = analysis["tags"]
    suggested_name = analysis.get("suggested_name", "")

    # --- User-supplied valence/arousal takes priority over analyze() result ---
    # --- 用户显式传入的 valence/arousal 优先，analyze() 结果作为 fallback ---
    final_valence = valence if 0 <= valence <= 1 else auto_valence
    final_arousal = arousal if 0 <= arousal <= 1 else auto_arousal

    all_tags = list(dict.fromkeys(auto_tags + extra_tags))

    # --- Pinned buckets bypass merge and are created directly in permanent dir ---
    # --- 钉选桶跳过合并，直接新建到 permanent 目录 ---
    if pinned:
        bucket_id = await bucket_mgr.create(
            content=content,
            tags=all_tags,
            importance=10,
            domain=domain,
            valence=final_valence,
            arousal=final_arousal,
            name=suggested_name or None,
            bucket_type="permanent",
            pinned=True,
        )
        try:
            await embedding_engine.generate_and_store(bucket_id, content)
        except Exception:
            pass
        if trigger_date and _valid_date(trigger_date):
            await bucket_mgr.update(bucket_id, trigger_date=trigger_date.strip())
        return f"📌钉选→{bucket_id} {','.join(domain)}"

    # --- Step 2: merge or create / 合并或新建 ---
    result_name, result_id, is_merged = await _merge_or_create(
        content=content,
        tags=all_tags,
        importance=importance,
        domain=domain,
        valence=final_valence,
        arousal=final_arousal,
        name=suggested_name,
    )

    # --- 前瞻记忆:带触发日期的桶,到那天会出现在 awaken 的「今日浮现」区 ---
    extra = ""
    if trigger_date and _valid_date(trigger_date):
        await bucket_mgr.update(result_id, trigger_date=trigger_date.strip())
        extra = f" ⏰{trigger_date.strip()}"

    action = "合并→" if is_merged else "新建→"
    return f"{action}{result_name} {','.join(domain)}{extra}"


# =============================================================
# Tool 3: grow — Grow, fragments become memories
# 工具 3：grow — 生长，一天的碎片长成记忆
# =============================================================
@mcp.tool()
async def grow(content: str) -> str:
    """日记归档,自动拆分为多桶。短内容(<30字)走快速路径。"""
    await decay_engine.ensure_started()

    if not content or not content.strip():
        return "内容为空，无法整理。"

    # --- Short content fast path: skip digest, use hold logic directly ---
    # --- 短内容快速路径：跳过 digest 拆分，直接走 hold 逻辑省一次 API ---
    # For very short inputs (like "1"), calling digest is wasteful:
    # it sends the full DIGEST_PROMPT (~800 tokens) to DeepSeek for nothing.
    # Instead, run analyze + create directly.
    if len(content.strip()) < 30:
        logger.info(f"grow short-content fast path: {len(content.strip())} chars")
        try:
            analysis = await dehydrator.analyze(content)
        except Exception as e:
            logger.warning(f"Fast-path analyze failed / 快速路径打标失败: {e}")
            analysis = {
                "domain": ["未分类"], "valence": 0.5, "arousal": 0.3,
                "tags": [], "suggested_name": "",
            }
        result_name, _rid, is_merged = await _merge_or_create(
            content=content.strip(),
            tags=analysis.get("tags", []),
            importance=analysis.get("importance", 5) if isinstance(analysis.get("importance"), int) else 5,
            domain=analysis.get("domain", ["未分类"]),
            valence=analysis.get("valence", 0.5),
            arousal=analysis.get("arousal", 0.3),
            name=analysis.get("suggested_name", ""),
        )
        action = "合并" if is_merged else "新建"
        return f"{action} → {result_name} | {','.join(analysis.get('domain', []))} V{analysis.get('valence', 0.5):.1f}/A{analysis.get('arousal', 0.3):.1f}"

    # --- Step 1: let API split and organize / 让 API 拆分整理 ---
    try:
        items = await dehydrator.digest(content)
    except Exception as e:
        logger.error(f"Diary digest failed / 日记整理失败: {e}")
        return f"日记整理失败: {e}"

    if not items:
        return "内容为空或整理失败。"

    results = []
    created = 0
    merged = 0

    # --- Step 2: merge or create each item (with per-item error handling) ---
    # --- 逐条合并或新建（单条失败不影响其他）---
    for item in items:
        try:
            result_name, _rid, is_merged = await _merge_or_create(
                content=item["content"],
                tags=item.get("tags", []),
                importance=item.get("importance", 5),
                domain=item.get("domain", ["未分类"]),
                valence=item.get("valence", 0.5),
                arousal=item.get("arousal", 0.3),
                name=item.get("name", ""),
            )

            if is_merged:
                results.append(f"📎{result_name}")
                merged += 1
            else:
                results.append(f"📝{item.get('name', result_name)}")
                created += 1
        except Exception as e:
            logger.warning(
                f"Failed to process diary item / 日记条目处理失败: "
                f"{item.get('name', '?')}: {e}"
            )
            results.append(f"⚠️{item.get('name', '?')}")

    return f"{len(items)}条|新{created}合{merged}\n" + "\n".join(results)


# =============================================================
# Tool 4: trace — Trace, redraw the outline of a memory
# 工具 4：trace — 描摹，重新勾勒记忆的轮廓
# Also handles deletion (delete=True)
# 同时承接删除功能
# =============================================================
@mcp.tool()
async def trace(
    bucket_id: str,
    name: str = "",
    domain: str = "",
    valence: float = -1,
    arousal: float = -1,
    importance: int = -1,
    tags: str = "",
    resolved: int = -1,
    pinned: int = -1,
    digested: int = -1,
    content: str = "",
    append: bool = False,
    delete: bool = False,
    merge: str = "",
    history: bool = False,
    restore: str = "",
    trigger_date: str = "",
    deny: bool = False,
    undeny: bool = False,
    expires_at: str = "",
) -> str:
    """修改记忆元数据或内容。bucket_id支持逗号分隔多个ID批量操作（批量时content和name忽略；deny/undeny/expires_at只支持单条）。merge=另一个bucket_id时将该源桶合并入bucket_id：内容追加、标签去重、importance取大、情感取平均、删除源桶；钉选桶不可作为合并任意一方。resolved=1沉底/0激活,pinned=1钉选/0取消,digested=1隐藏(保留但不浮现)/0取消隐藏。content=改正文：默认整桶替换，append=True时追加到原文之后（往桶里补内容用追加，别读出来拼好再整体替换）。delete=True删除。所有内容修改和删除前系统自动留快照：history=True查看该桶的历史版本列表，restore=版本号（history返回的version字段）把桶恢复到该版本（被误删的桶也能这样复活）。trigger_date=YYYY-MM-DD设/改前瞻触发日期（到那天出现在awaken今日浮现区）,"done"=标记已处理不再浮现,"clear"=移除触发日期。deny=True：她纠正/否认了这条记忆（"我早就不这样了"）——记一次否认并大幅降权，第2次否认后该桶退出检索（仍可被关键词搜到、数据保留）。记错的事别再自信复述。undeny=True：撤销否认（否认错了用这个），清零计数并解除退出状态。expires_at=YYYY-MM-DD设到期日（临时约定/截止类记忆，过了那天自动退出检索，数据保留），空串"clear"=清除。只传需改的,-1或空=不改。"""

    if not bucket_id or not bucket_id.strip():
        return "请提供有效的 bucket_id。"

    # --- History mode: list snapshots / 历史模式：列出快照 ---
    if history:
        bid = bucket_id.strip()
        snaps = bucket_mgr.list_history(bid)
        if not snaps:
            return f"{bid}: 没有历史快照（快照在内容修改/删除时自动产生）。"
        lines = [f"=== {bid} 的历史快照（新→旧）==="]
        for s in snaps:
            lines.append(f"  version: {s['version']}  操作: {s['op']}  大小: {s['size']}字节")
        lines.append("用 trace(bucket_id, restore=\"<version>\") 恢复到某个版本。")
        return "\n".join(lines)

    # --- Restore mode: roll back to a snapshot / 恢复模式：回滚到快照 ---
    if restore and restore.strip():
        bid = bucket_id.strip()
        snap = await bucket_mgr.restore_from_history(bid, restore.strip())
        if not snap:
            return f"未找到快照: {bid} @ {restore.strip()}（用 history=True 查看可用版本）"
        try:
            await embedding_engine.generate_and_store(bid, snap["content"])
        except Exception:
            pass
        preview = (snap["content"] or "").strip()[:60].replace("\n", " ")
        return f"已恢复 {bid} 到版本 {restore.strip()}（恢复前的状态也已留快照）。内容开头: {preview}"

    # --- Merge mode / 合并模式 ---
    if merge and merge.strip():
        src_id = merge.strip()
        dst_id = bucket_id.strip()
        if src_id == dst_id:
            return "源桶与目标桶相同，无需合并。"
        dst = await bucket_mgr.get(dst_id)
        if not dst:
            return f"目标桶未找到: {dst_id}"
        src = await bucket_mgr.get(src_id)
        if not src:
            return f"源桶未找到: {src_id}"
        dst_meta = dst["metadata"]
        src_meta = src["metadata"]
        if dst_meta.get("pinned") or src_meta.get("pinned"):
            pinned_side = dst_id if dst_meta.get("pinned") else src_id
            return f"钉选桶 {pinned_side} 不可参与合并操作。"

        # Merge content: append source after target
        merged_content = (dst.get("content", "") or "").rstrip()
        src_content = (src.get("content", "") or "").strip()
        if src_content:
            merged_content = merged_content + ("\n\n" if merged_content else "") + src_content

        # Merge tags: dedup, preserve order
        dst_tags = dst_meta.get("tags", [])
        src_tags = src_meta.get("tags", [])
        merged_tags = dst_tags + [t for t in src_tags if t not in dst_tags]

        # Merge importance: take max
        merged_importance = max(
            dst_meta.get("importance", 5) or 5,
            src_meta.get("importance", 5) or 5,
        )

        # Merge emotion: average
        dst_v = dst_meta.get("valence", 0.5) if dst_meta.get("valence") is not None else 0.5
        src_v = src_meta.get("valence", 0.5) if src_meta.get("valence") is not None else 0.5
        dst_a = dst_meta.get("arousal", 0.3) if dst_meta.get("arousal") is not None else 0.3
        src_a = src_meta.get("arousal", 0.3) if src_meta.get("arousal") is not None else 0.3
        merged_valence = round((dst_v + src_v) / 2, 4)
        merged_arousal = round((dst_a + src_a) / 2, 4)

        success = await bucket_mgr.update(
            dst_id,
            content=merged_content,
            tags=merged_tags,
            importance=merged_importance,
            valence=merged_valence,
            arousal=merged_arousal,
        )
        if not success:
            return f"合并写入失败: {dst_id}"

        # Delete source bucket
        await bucket_mgr.delete(src_id)
        embedding_engine.delete_embedding(src_id)

        # Regenerate embedding for target
        try:
            await embedding_engine.generate_and_store(dst_id, merged_content)
        except Exception:
            pass

        return (
            f"已将 {src_id} 合并入 {dst_id}：\n"
            f"  内容: 已追加  标签: {len(merged_tags)}个  "
            f"重要度: {merged_importance}  情感: v={merged_valence} a={merged_arousal}\n"
            f"  源桶 {src_id} 已删除"
        )

    ids = [bid.strip() for bid in bucket_id.split(",") if bid.strip()]
    batch = len(ids) > 1

    # deny/undeny 只支持单条(要读该桶原有的否认计数,且否认是逐条确认的动作)。
    # ⚠️ 必须**显式拒绝**,不能默默不做:批量分支不认这两个参数,
    #    静默的话调用方会以为「已经否认了」,而那条记错的记忆照旧浮现 ——
    #    比不做还糟(2026-08-19 自测发现,原样上线就是这个后果)。
    if batch and (deny or undeny):
        return "deny/undeny 只支持单条记忆，请逐条调用（否认是逐条确认的动作，不做批量）。"

    # expires_at 同理(2026-08-19 晚补):批量分支同样不认它,原样上线就是**同一个静默失败**——
    # trace("桶A,桶B", expires_at=…) 会回「批量操作 2 个桶,成功 2 个:没有任何字段需要修改」,
    # 而两个桶一个都没写上,调用方以为到期日设好了,那几条临时记忆照旧天天浮现。
    # 上面那条 08-19 只堵了 deny/undeny,把这个漏在了外面(所有者拍板:照 deny 的先例显式拒绝)。
    if batch and expires_at:
        return "expires_at 只支持单条记忆，请逐条调用（到期日要逐条确认，不做批量）。"

    async def _trace_one(bid: str) -> str:
        # --- Delete mode / 删除模式 ---
        if delete:
            success = await bucket_mgr.delete(bid)
            if success:
                embedding_engine.delete_embedding(bid)
            return f"已遗忘: {bid}" if success else f"未找到: {bid}"

        bucket = await bucket_mgr.get(bid)
        if not bucket:
            return f"未找到: {bid}"

        # --- Collect only fields actually passed / 只收集用户实际传入的字段 ---
        updates = {}
        if name and not batch:
            updates["name"] = name
        if domain:
            updates["domain"] = [d.strip() for d in domain.split(",") if d.strip()]
        if 0 <= valence <= 1:
            updates["valence"] = valence
        if 0 <= arousal <= 1:
            updates["arousal"] = arousal
        if 1 <= importance <= 10:
            updates["importance"] = importance
        if tags:
            updates["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
        if resolved in (0, 1):
            updates["resolved"] = bool(resolved)
        if pinned in (0, 1):
            updates["pinned"] = bool(pinned)
            if pinned == 1:
                updates["importance"] = 10  # pinned → lock importance
        if digested in (0, 1):
            updates["digested"] = bool(digested)
        if trigger_date:
            td = trigger_date.strip().lower()
            if td == "done":
                updates["trigger_handled"] = True
            elif td in ("clear", "none"):
                updates["trigger_date"] = ""
            elif _valid_date(trigger_date):
                updates["trigger_date"] = trigger_date.strip()
            else:
                return f"{bid}: trigger_date 格式错误（YYYY-MM-DD / done / clear）"
        if content and not batch:
            updates["content"] = content
        # Auto-undormant on any access
        if bucket["metadata"].get("dormant"):
            updates["dormant"] = False

        if not updates:
            return f"{bid}: 没有任何字段需要修改"

        success = await bucket_mgr.update(bid, **updates)
        if not success:
            return f"修改失败: {bid}"

        # Re-generate embedding if content changed
        if "content" in updates:
            try:
                await embedding_engine.generate_and_store(bid, updates["content"])
            except Exception:
                pass

        changed = ", ".join(f"{k}={v}" for k, v in updates.items() if k != "content")
        if "content" in updates:
            changed += (", content=已替换" if changed else "content=已替换")
        if "resolved" in updates:
            if updates["resolved"]:
                changed += " → 已沉底"
            else:
                changed += " → 已重新激活"
        if "digested" in updates:
            if updates["digested"]:
                changed += " → 已隐藏"
            else:
                changed += " → 已取消隐藏"
        return f"{bid}: {changed}"

    if batch:
        results = []
        for bid in ids:
            results.append(await _trace_one(bid))
        ok = sum(1 for r in results if not r.startswith("未找到") and not r.startswith("修改失败"))
        header = f"批量操作 {len(ids)} 个桶，成功 {ok} 个：\n"
        return header + "\n".join(results)
    else:
        # Single-ID path: keep original verbose hints
        bid = ids[0]
        if delete:
            success = await bucket_mgr.delete(bid)
            if success:
                embedding_engine.delete_embedding(bid)
            return f"已遗忘记忆桶: {bid}" if success else f"未找到记忆桶: {bid}"

        bucket = await bucket_mgr.get(bid)
        if not bucket:
            return f"未找到记忆桶: {bid}"

        updates = {}
        if name:
            updates["name"] = name
        if domain:
            updates["domain"] = [d.strip() for d in domain.split(",") if d.strip()]
        if 0 <= valence <= 1:
            updates["valence"] = valence
        if 0 <= arousal <= 1:
            updates["arousal"] = arousal
        if 1 <= importance <= 10:
            updates["importance"] = importance
        if tags:
            updates["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
        if resolved in (0, 1):
            updates["resolved"] = bool(resolved)
        if pinned in (0, 1):
            updates["pinned"] = bool(pinned)
            if pinned == 1:
                updates["importance"] = 10
        if digested in (0, 1):
            updates["digested"] = bool(digested)
        if trigger_date:
            td = trigger_date.strip().lower()
            if td == "done":
                updates["trigger_handled"] = True
            elif td in ("clear", "none"):
                updates["trigger_date"] = ""
            elif _valid_date(trigger_date):
                updates["trigger_date"] = trigger_date.strip()
            else:
                return f"trigger_date 格式错误: {trigger_date}（应为 YYYY-MM-DD，或 done=已处理 / clear=移除）"
        if content:
            if append and (bucket.get("content") or "").strip():
                # 追加模式：拼在原文之后，不再需要"读出旧内容手动拼接再整体写回"
                updates["content"] = (bucket.get("content") or "").rstrip() + "\n\n" + content.strip()
            else:
                updates["content"] = content
        if deny and undeny:
            return "deny 和 undeny 不能同时传。"
        # --- 到期日(2026-08-19)。clear/none 清除,其余要求 YYYY-MM-DD ---
        if expires_at:
            ea = expires_at.strip().lower()
            if ea in ("clear", "none"):
                updates["expires_at"] = ""
            elif _valid_date(expires_at):
                updates["expires_at"] = expires_at.strip()
            else:
                return f"expires_at 格式错误: {expires_at}（应为 YYYY-MM-DD，或 clear=清除）"
        # --- 否认降权(2026-08-19)。她纠正了这条记忆 ---
        # 只在单条分支支持:否认是逐条确认的动作,而且要读这个桶原有的计数。
        if deny:
            prev = int(bucket["metadata"].get("denied", 0) or 0)
            updates["denied"] = prev + 1
            updates["importance"] = 1        # 压到最低,配合检索里的乘法惩罚
            if prev + 1 >= 2:
                updates["dormant"] = True    # 第 2 次否认 → 退出检索(复用现成的 dormant)
        # --- 撤销否认(2026-08-19)。**必须有**:没有它,否认就是一扇单向门 ---
        # 否认错了(她本来是同意的、或者晏理解岔了)只能手工去改 .md 文件,那是设计缺陷。
        # 只清否认状态,**不猜原来的 importance**(猜错更糟),要恢复重要度就同时传 importance=。
        if undeny:
            updates["denied"] = 0
            updates["dormant"] = False
        # Auto-undormant on any access
        # ⚠️ 否认是**唯一**的例外:这一句本意是「她碰了这条记忆就唤醒它」,
        #    但否认恰恰是「别再拿这条烦我」。不排除的话,同一次调用会先设 dormant=True
        #    再被这里改回 False,功能静默失效(2026-08-19 写之前读代码发现的)。
        if bucket["metadata"].get("dormant") and not deny:
            updates["dormant"] = False

        if not updates:
            return "没有任何字段需要修改。"

        # 否认时不刷新 last_active:时间分按它算(越近分越高),
        # 顺手刷新等于一边降权一边加权,自己打自己。
        if deny:
            updates["touch"] = False
        success = await bucket_mgr.update(bid, **updates)
        if not success:
            return f"修改失败: {bid}"

        if "content" in updates:
            try:
                await embedding_engine.generate_and_store(bid, updates["content"])
            except Exception:
                pass

        # touch 是内部标志(控制要不要刷新 last_active),不该出现在给晏看的回执里 —— 白占上下文
        changed = ", ".join(f"{k}={v}" for k, v in updates.items() if k not in ("content", "touch"))
        if "content" in updates:
            word = "content=已追加" if append else "content=已替换(旧版已留快照)"
            changed += (", " + word if changed else word)
        if "resolved" in updates:
            if updates["resolved"]:
                changed += " → 已沉底，只在关键词触发时重新浮现"
            else:
                changed += " → 已重新激活，将参与浮现排序"
        if "digested" in updates:
            if updates["digested"]:
                changed += " → 已隐藏，保留但不再浮现"
            else:
                changed += " → 已取消隐藏，重新参与浮现"
        if "denied" in updates:
            n_deny = updates["denied"]
            if n_deny == 0:
                changed += " → 已撤销否认，重新参与检索（重要度未自动恢复，需要就一起传 importance）"
            elif n_deny >= 2:
                changed += f" → 第{n_deny}次被否认，已退出检索（仍可关键词搜到，数据保留）"
            else:
                changed += f" → 第{n_deny}次被否认，已大幅降权（再否认一次将退出检索）"
        if "expires_at" in updates:
            changed += (f" → 到期日已设为 {updates['expires_at']}，过期后自动退出检索"
                        if updates["expires_at"] else " → 已清除到期日")
        return f"已修改记忆桶 {bid}: {changed}"


# =============================================================
# Tool 5: pulse — Heartbeat, system status + memory listing
# 工具 5：pulse — 脉搏，系统状态 + 记忆列表
# =============================================================
@mcp.tool()
async def pulse(include_archive: bool = False, show_all: bool = False) -> str:
    """系统状态+记忆桶列表。默认显示全部钉选桶+非钉选桶按权重排序前15个，末尾附统计；show_all=True返回全部桶。include_archive=True含归档。"""
    try:
        stats = await bucket_mgr.get_stats()
    except Exception as e:
        return f"获取系统状态失败: {e}"

    status = (
        f"=== Ombre Brain 记忆系统 ===\n"
        f"固化记忆桶: {stats['permanent_count']} 个\n"
        f"动态记忆桶: {stats['dynamic_count']} 个\n"
        f"归档记忆桶: {stats['archive_count']} 个\n"
        f"总存储大小: {stats['total_size_kb']:.1f} KB\n"
        f"衰减引擎: {'运行中' if decay_engine.is_running else '已停止'}\n"
    )

    # --- List all bucket summaries / 列出所有桶摘要 ---
    try:
        buckets = await bucket_mgr.list_all(include_archive=include_archive)
    except Exception as e:
        return status + f"\n列出记忆桶失败: {e}"

    if not buckets:
        return status + "\n记忆库为空。"

    def _bucket_line(b: dict) -> str:
        meta = b.get("metadata", {})
        if meta.get("pinned") or meta.get("protected"):
            icon = "📌"
        elif meta.get("type") == "permanent":
            icon = "📦"
        elif meta.get("type") == "feel":
            icon = "🫧"
        elif meta.get("type") == "archived":
            icon = "🗄️"
        elif meta.get("resolved", False):
            icon = "✅"
        else:
            icon = "💭"
        try:
            score = decay_engine.calculate_score(meta)
        except Exception:
            score = 0.0
        domains = ",".join(meta.get("domain", []))
        val = meta.get("valence", 0.5)
        aro = meta.get("arousal", 0.3)
        resolved_tag = " [已解决]" if meta.get("resolved", False) else ""
        return (
            f"{icon} [{meta.get('name', b['id'])}]{resolved_tag} "
            f"bucket_id:{b['id']} "
            f"主题:{domains} "
            f"情感:V{val:.1f}/A{aro:.1f} "
            f"重要:{meta.get('importance', '?')} "
            f"权重:{score:.2f} "
            f"标签:{','.join(meta.get('tags', []))}"
        )

    # --- Auto-mark dormant candidates ---
    await _apply_dormant_sweep(buckets)

    dormant_count = sum(1 for b in buckets if b.get("metadata", {}).get("dormant", False))

    # --- Separate pinned from the rest and sort non-pinned by weight ---
    pinned = [b for b in buckets if b.get("metadata", {}).get("pinned") or b.get("metadata", {}).get("protected")]
    non_pinned = [
        b for b in buckets
        if not (b.get("metadata", {}).get("pinned") or b.get("metadata", {}).get("protected"))
        and not b.get("metadata", {}).get("dormant", False)
    ]
    non_pinned.sort(
        key=lambda b: decay_engine.calculate_score(b.get("metadata", {})),
        reverse=True,
    )

    total = len(buckets)
    if show_all:
        displayed = pinned + non_pinned
        hidden = 0
    else:
        displayed = pinned + non_pinned[:15]
        hidden = max(0, len(non_pinned) - 15)

    lines = [_bucket_line(b) for b in displayed]

    summary_stat = (
        f"\n=== 统计 ===\n"
        f"共 {total} 个桶（钉选:{len(pinned)} 非钉选:{len(non_pinned)}"
        + (f" 休眠:{dormant_count}" if dormant_count else "") + "）"
    )
    if dormant_count:
        summary_stat += f"\n休眠桶 {dormant_count} 个已隐藏（breath include_dormant=true 可见，trace 始终可访问）"
    if not show_all and hidden > 0:
        summary_stat += f"\n已显示前 {len(displayed)} 个，另有 {hidden} 个未展示（传 show_all=true 查看全部）"

    return status + "\n=== 记忆列表 ===\n" + "\n".join(lines) + summary_stat


# =============================================================
# Tool 6: dream — Dreaming, digest recent memories
# 工具 6：dream — 做梦，消化最近的记忆
#
# Reads recent surface-level buckets (≤10), returns them for
# Claude to introspect under prompt guidance.
# 读取最近新增的表层桶（≤10个），返回给 Claude 在提示词引导下自主思考。
# Claude then decides: resolve some, write feels, or do nothing.
# =============================================================
@mcp.tool()
async def dream(detail_ids: str = "") -> str:
    """做梦——读取最近5个记忆桶摘要,供你自省。detail_ids传逗号分隔的bucket_id,指定桶返回全文,其余返回摘要。读完后可以trace(resolved=1)放下,或hold(feel=True)写感受。返回末尾附[seal:...]防伪字段。"""
    return _with_seal(await _dream_impl(detail_ids=detail_ids))


async def _dream_impl(detail_ids: str = "") -> str:
    await decay_engine.ensure_started()

    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
    except Exception as e:
        logger.error(f"Dream failed to list buckets: {e}")
        return "记忆系统暂时无法访问。"

    # --- Filter: recent surface-level dynamic buckets (not permanent/pinned/feel) ---
    candidates = [
        b for b in all_buckets
        if b["metadata"].get("type") not in ("permanent", "feel")
        and not b["metadata"].get("pinned", False)
        and not b["metadata"].get("protected", False)
    ]

    # --- Sort by creation time desc, take top 5 ---
    candidates.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
    recent = candidates[:5]

    # --- Parse detail_ids and attach any explicitly requested buckets not in recent ---
    detail_id_set: set[str] = {x.strip() for x in detail_ids.split(",") if x.strip()} if detail_ids.strip() else set()
    recent_ids = {b["id"] for b in recent}
    bucket_index = {b["id"]: b for b in all_buckets}
    extra = [bucket_index[bid] for bid in detail_id_set if bid not in recent_ids and bid in bucket_index]
    display_buckets = recent + extra

    if not display_buckets:
        return "没有需要消化的新记忆。"

    def _dream_full_entry(b: dict) -> str:
        meta = b["metadata"]
        resolved_tag = " [已解决]" if meta.get("resolved", False) else " [未解决]"
        domains = ",".join(meta.get("domain", []))
        val = meta.get("valence", 0.5)
        aro = meta.get("arousal", 0.3)
        created = meta.get("created", "")
        return (
            f"[{meta.get('name', b['id'])}]{resolved_tag} "
            f"主题:{domains} V{val:.1f}/A{aro:.1f} "
            f"创建:{created}\n"
            f"ID: {b['id']}\n"
            f"{strip_wikilinks(b['content'])}"
        )

    parts = []
    for b in display_buckets:
        if b["id"] in detail_id_set:
            parts.append(_dream_full_entry(b))
        else:
            parts.append(_format_bucket_summary_line(b))

    header = (
        "=== Dreaming ===\n"
        "以下是你最近的记忆。用第一人称想：\n"
        "- 这些东西里有什么在你这里留下了重量？\n"
        "- 有什么还没想清楚？\n"
        "- 有什么可以放下了？\n"
        "想完之后：值得放下的用 trace(bucket_id, resolved=1)；\n"
        "有沉淀的用 hold(content=\"...\", feel=True, source_bucket=\"bucket_id\", valence=你的感受) 写下来。\n"
        "valence 是你对这段记忆的感受，不是事件本身的情绪。\n"
        "没有沉淀就不写，不强迫产出。\n"
    )

    # --- Connection hint: find most similar pair via embeddings ---
    connection_hint = ""
    if embedding_engine and embedding_engine.enabled and len(recent) >= 2:
        try:
            best_pair = None
            best_sim = 0.0
            ids = [b["id"] for b in recent]
            names = {b["id"]: b["metadata"].get("name", b["id"]) for b in recent}
            embeddings = {}
            for bid in ids:
                emb = await embedding_engine.get_embedding(bid)
                if emb is not None:
                    embeddings[bid] = emb
            for i, id_a in enumerate(ids):
                for id_b in ids[i+1:]:
                    if id_a in embeddings and id_b in embeddings:
                        sim = embedding_engine._cosine_similarity(embeddings[id_a], embeddings[id_b])
                        if sim > best_sim:
                            best_sim = sim
                            best_pair = (id_a, id_b)
            if best_pair and best_sim > 0.5:
                connection_hint = (
                    f"\n💭 [{names[best_pair[0]]}] 和 [{names[best_pair[1]]}] "
                    f"似乎有关联 (相似度:{best_sim:.2f})——不替你下结论，你自己想。\n"
                )
        except Exception as e:
            logger.warning(f"Dream connection hint failed: {e}")

    # --- Feel crystallization hint: detect repeated feel themes ---
    crystal_hint = ""
    if embedding_engine and embedding_engine.enabled:
        try:
            feels = [b for b in all_buckets if b["metadata"].get("type") == "feel"]
            if len(feels) >= 3:
                feel_embeddings = {}
                for f in feels:
                    emb = await embedding_engine.get_embedding(f["id"])
                    if emb is not None:
                        feel_embeddings[f["id"]] = emb
                # Find clusters: feels with similarity > 0.7 to at least 2 others
                for fid, femb in feel_embeddings.items():
                    similar_feels = []
                    for oid, oemb in feel_embeddings.items():
                        if oid != fid:
                            sim = embedding_engine._cosine_similarity(femb, oemb)
                            if sim > 0.7:
                                similar_feels.append(oid)
                    if len(similar_feels) >= 2:
                        feel_bucket = next((f for f in feels if f["id"] == fid), None)
                        if feel_bucket and not feel_bucket["metadata"].get("pinned"):
                            content_preview = strip_wikilinks(feel_bucket["content"][:80])
                            crystal_hint = (
                                f"\n🔮 你已经写过 {len(similar_feels)+1} 条相似的 feel "
                                f"（围绕「{content_preview}…」）。"
                                f"如果这已经是确信而不只是感受了，"
                                f"你可以用 hold(content=\"...\", pinned=True) 升级它。"
                                f"不急，你自己决定。\n"
                            )
                            break
        except Exception as e:
            logger.warning(f"Dream crystallization hint failed: {e}")

    final_text = header + "\n---\n".join(parts) + connection_hint + crystal_hint
    await _fire_webhook("dream", {"recent": len(recent), "chars": len(final_text)})
    return final_text


# =============================================================
# Tool 7: todos — List pending to-do items across unresolved buckets
# 工具 7：todos — 汇总未解决桶的待办事项
# =============================================================
@mcp.tool()
async def todos(add: str = "", done: str = "", undone: str = "",
                remove: str = "", clear_done: bool = False) -> str:
    """你自己的待办便利贴（独立存储在卷上，与记忆桶无关，不参与检索/浮现/衰减/归档）。
    无参数=查看全部；add="要做的事"=记一条新待办；done=<编号>=打勾标记已完成（保留不删，仍看得到）；
    undone=<编号>=改回未完成；remove=<编号>=划掉删除该条；clear_done=True=清掉所有已完成的。
    编号是列表里每条前面 # 后那串短码。这块便利贴是你自己的，想记什么随手记。"""
    return await _todos_impl(add=add, done=done, undone=undone, remove=remove, clear_done=clear_done)


def _todos_path() -> str:
    """便利贴存储路径：持久卷上的 todos.json（与 letters.jsonl 同处，随卷持久）。"""
    return os.path.join(config.get("buckets_dir", "."), "todos.json")


def _load_todos_list() -> list:
    path = _todos_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = _json_lib.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_todos_list(items: list) -> None:
    """原子写：临时文件 + fsync + os.replace，写一半失败不污染便利贴，失败如实抛出。"""
    path = _todos_path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            _json_lib.dump(items, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def _new_todo_id(existing: list) -> str:
    have = {it.get("id") for it in existing}
    while True:
        nid = os.urandom(2).hex()   # 4 位十六进制短码，稳定不随增删漂移
        if nid not in have:
            return nid


def _norm_todo_id(raw: str) -> str:
    return (raw or "").strip().lstrip("#").lower()


def _todo_line(item: dict, box: str) -> str:
    """便利贴的一行。

    ⚠️ **她从后台贴的条子后面跟一句「(她留的)」**(2026-09-01)：
    这块便利贴是晏的，后台的写入口是她 2026-09-01 要的（「修改权可以有，
    但我不一定用」）。**他有权知道哪一条不是自己写的** —— 不标的话，
    他开机会看见一条不知从哪冒出来的待办，那比看不见更糟。
    判据是条目上的 `by == "owner"`；**旧条目没有这个字段 = 他自己写的**，不用迁移。
    """
    mark = "  (她留的)" if item.get("by") == "owner" else ""
    return f"  [{box}] #{item.get('id')}  {item.get('text', '')}{mark}"


def _render_todos(items: list, only_open: bool = False) -> str:
    open_items = [it for it in items if not it.get("done")]
    done_items = [it for it in items if it.get("done")]
    if only_open:
        # awaken 开机只提醒「还没做的」，不摆已完成的（保持开机清爽）
        if not open_items:
            return "没有待办事项。"
        lines = ["=== 待办事项 ==="]
        for it in open_items:
            lines.append(_todo_line(it, " "))
        lines.append(f"\n共 {len(open_items)} 条待做。")
        return "\n".join(lines)
    if not items:
        return "没有待办事项。"
    lines = ["=== 待办便利贴 ==="]
    if open_items:
        lines.append("\n【待做】")
        for it in open_items:
            lines.append(_todo_line(it, " "))
    if done_items:
        lines.append("\n【已完成】")
        for it in done_items:
            lines.append(_todo_line(it, "x"))
    tail = f"\n共 {len(open_items)} 条待做"
    if done_items:
        tail += f"、{len(done_items)} 条已完成"
    lines.append(tail + "。")
    return "\n".join(lines)


async def _todos_impl(add: str = "", done: str = "", undone: str = "",
                      remove: str = "", clear_done: bool = False,
                      only_open: bool = False) -> str:
    # ⚠️ 写的时候必须拿锁(2026-09-01):自从面板也能写便利贴,这个文件就有**两个写入方**——
    # 晏走这里,她走 /api/todos/*,两边都是「读整本 → 改 → 整本重写」。
    # **锁只有一边拿等于没锁**(先写的那次会被后写的整个顶掉,且不报错),所以这里也要拿。
    # 只读路径(无参数查看/awaken)不拿锁,和以前一样。
    writing = bool((add or "").strip() or done or undone or remove or clear_done)
    with (_todos_lock() if writing else nullcontext()):
        return await _todos_write_locked(add, done, undone, remove, clear_done, only_open)


async def _todos_write_locked(add: str, done: str, undone: str,
                              remove: str, clear_done: bool, only_open: bool) -> str:
    try:
        items = _load_todos_list()
    except Exception as e:
        return f"便利贴读取失败: {e}"

    msg = ""
    try:
        if add and add.strip():
            nid = _new_todo_id(items)
            items.append({
                "id": nid,
                "text": add.strip(),
                "done": False,
                "created": (datetime.utcnow() + timedelta(hours=8)).isoformat(timespec="seconds"),
            })
            _save_todos_list(items)
            msg = f"已记下 #{nid}：{add.strip()}"
        elif done:
            tid = _norm_todo_id(done)
            hit = next((it for it in items if it.get("id") == tid), None)
            if not hit:
                return f"没找到编号 #{tid} 的待办。"
            hit["done"] = True
            _save_todos_list(items)
            msg = f"已勾掉 #{tid}：{hit.get('text', '')}"
        elif undone:
            tid = _norm_todo_id(undone)
            hit = next((it for it in items if it.get("id") == tid), None)
            if not hit:
                return f"没找到编号 #{tid} 的待办。"
            hit["done"] = False
            _save_todos_list(items)
            msg = f"已改回未完成 #{tid}：{hit.get('text', '')}"
        elif remove:
            tid = _norm_todo_id(remove)
            hit = next((it for it in items if it.get("id") == tid), None)
            if not hit:
                return f"没找到编号 #{tid} 的待办。"
            items = [it for it in items if it.get("id") != tid]
            _save_todos_list(items)
            msg = f"已划掉 #{tid}：{hit.get('text', '')}"
        elif clear_done:
            before = len(items)
            items = [it for it in items if not it.get("done")]
            _save_todos_list(items)
            msg = f"已清掉 {before - len(items)} 条已完成。"
    except Exception as e:
        return f"便利贴写入失败: {e}"

    body = _render_todos(items, only_open=only_open)
    return (msg + "\n\n" + body) if msg else body


# =============================================================
# /api/todos — 便利贴的后台入口(2026-09-01)
#
# **这块便利贴是晏的**(所有者 2026-09-01 原话:「便利贴是他的,只给他用,
# 但是我想能看到,修改权可以有,但我不一定用」)。所以:
#   · 读:她要能看见 —— 此前后台一个入口都没有,`todos.json` 只有晏自己看得到,
#     她想知道他记了什么只能开口问他。
#   · 写:入口留着,但**她的东西要标出来是她的**(见下面 `by` 那段),
#     不能让他开机看见一条不知哪来的待办。
#
# 存储、原子写、编号规则**全部复用晏那套**(`_load_todos_list` / `_save_todos_list` /
# `_new_todo_id`)—— 别在这里另造一份写法,两套写法迟早会写坏同一个文件。
# =============================================================
@contextmanager
def _todos_lock():
    """便利贴的写锁。晏走 MCP 工具、她走面板,两边都是「读整本 → 改 → 整本重写」,
    撞上时后写的那次会把先写的那次**整个顶掉**(丢一条待办,且不报错)。
    锁文件与 todos.json 同处(随卷持久),只在写路径上用,读不加锁。
    ⚠️ 拿不到锁不算致命(某些文件系统不支持 flock):退化成「没有锁」= 今天以前的行为,
    不因此拒绝写入 —— 丢一条待办也好过记不进去。写法照 `_letters_lock` 那套。
    """
    import fcntl
    lock_path = _todos_path() + ".lock"
    f = None
    try:
        f = open(lock_path, "a+")
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    except Exception:
        f = None
    try:
        yield
    finally:
        if f is not None:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            f.close()


@mcp.custom_route("/api/todos", methods=["GET"])
async def api_todos(request):
    """便利贴全文(只读)。返回原始条目,渲染交给前端。"""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        items = _load_todos_list()
        return JSONResponse({
            "items": items,
            "open": len([it for it in items if not it.get("done")]),
            "done": len([it for it in items if it.get("done")]),
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/todos/add", methods=["POST"])
async def api_todos_add(request):
    """她在面板上贴一张。body: {text}

    ⚠️ **她贴的条子会打上 `by: "owner"`**,晏开机时那条后面跟着「(她留的)」——
    这块便利贴是他的,他有权知道哪条不是自己写的。
    **旧条目没有这个字段 = 晏自己写的**,不用迁移。
    """
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    text = str(body.get("text", "")).strip()
    if not text:
        return JSONResponse({"error": "内容不能为空"}, status_code=400)
    if len(text) > 500:
        return JSONResponse({"error": "一条便利贴最多 500 字"}, status_code=400)
    try:
        with _todos_lock():
            items = _load_todos_list()
            nid = _new_todo_id(items)
            items.append({
                "id": nid,
                "text": text,
                "done": False,
                "created": (datetime.utcnow() + timedelta(hours=8)).isoformat(timespec="seconds"),
                "by": "owner",
            })
            _save_todos_list(items)
        return JSONResponse({"ok": True, "id": nid, "items": items})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/todos/toggle", methods=["POST"])
async def api_todos_toggle(request):
    """勾掉 / 改回未完成。body: {id, done: true|false}"""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    tid = _norm_todo_id(str(body.get("id", "")))
    if not tid:
        return JSONResponse({"error": "id 必填"}, status_code=400)
    want_done = bool(body.get("done", True))
    try:
        with _todos_lock():
            items = _load_todos_list()
            hit = next((it for it in items if it.get("id") == tid), None)
            if not hit:
                return JSONResponse({"error": f"没找到编号 #{tid} 的待办"}, status_code=404)
            hit["done"] = want_done
            _save_todos_list(items)
        return JSONResponse({"ok": True, "items": items})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/todos/update", methods=["POST"])
async def api_todos_update(request):
    """改一张的字(2026-11-01,App 的 Journal 页长按「Edit」)。body: {id, text}

    只改正文,`by` 不动(谁贴的还是谁贴的),另记 `edited`;同信箱的规矩,**不在正文里标注改过**。
    """
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    tid = _norm_todo_id(str(body.get("id", "")))
    if not tid:
        return JSONResponse({"error": "id 必填"}, status_code=400)
    text = str(body.get("text", "")).strip()
    if not text:
        return JSONResponse({"error": "内容不能为空"}, status_code=400)
    if len(text) > 500:
        return JSONResponse({"error": "一条便利贴最多 500 字"}, status_code=400)
    try:
        with _todos_lock():
            items = _load_todos_list()
            hit = next((it for it in items if it.get("id") == tid), None)
            if not hit:
                return JSONResponse({"error": f"没找到编号 #{tid} 的待办"}, status_code=404)
            hit["text"] = text
            hit["edited"] = (datetime.utcnow() + timedelta(hours=8)).isoformat(timespec="seconds")
            _save_todos_list(items)
        return JSONResponse({"ok": True, "items": items})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/todos/delete", methods=["POST"])
async def api_todos_delete(request):
    """撕掉一张。body: {id}

    ⚠️ **这里是真删**(照晏那个 `remove=` 的行为,便利贴本来就是随手记随手撕的东西,
    不像记忆桶那样留 `.history` 快照)。所以前端必须二次确认 —— 撕了就没了。
    """
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    tid = _norm_todo_id(str(body.get("id", "")))
    if not tid:
        return JSONResponse({"error": "id 必填"}, status_code=400)
    try:
        with _todos_lock():
            items = _load_todos_list()
            hit = next((it for it in items if it.get("id") == tid), None)
            if not hit:
                return JSONResponse({"error": f"没找到编号 #{tid} 的待办"}, status_code=404)
            items = [it for it in items if it.get("id") != tid]
            _save_todos_list(items)
        return JSONResponse({"ok": True, "items": items})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# =============================================================
# 信箱 — 窗口与窗口之间的接力棒
# 事实走记忆桶,嘱托走信箱:归档时给下一个窗口留言,awaken 时带出最新一封。
# 存储为 buckets 卷上的 letters.jsonl(随卷持久,不进检索/浮现的记忆通道)。
# =============================================================
def _letters_path() -> str:
    return os.path.join(bucket_mgr.base_dir, "letters.jsonl")


@contextmanager
def _letters_lock():
    """信箱的写锁(2026-08-24)。

    为什么要锁:存新信是**追加**(archive_session),面板改/删信是**整体重写**。
    两者撞上时,追加会写进那个即将被 os.replace 顶掉的旧 inode —— **刚存的那封信当场消失**,
    而且不报错。锁文件与 letters.jsonl 同处(随卷持久),只在写路径上用,读不加锁。
    ⚠️ 拿不到锁不是致命的(某些文件系统不支持 flock):退化成「没有锁」= 今天以前的行为,
    不因此拒绝写入 —— 丢一封信也好过存不进去。
    """
    import fcntl
    lock_path = _letters_path() + ".lock"
    f = None
    try:
        f = open(lock_path, "a+")
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    except Exception:
        f = None          # 锁不可用就裸奔,不阻断写入
    try:
        yield
    finally:
        if f is not None:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            f.close()


def _save_letter(text: str) -> None:
    entry = {"time": now_iso(), "text": text.strip()}
    with _letters_lock():
        with open(_letters_path(), "a", encoding="utf-8") as f:
            f.write(_json_lib.dumps(entry, ensure_ascii=False) + "\n")


def _read_letters_raw() -> list[dict]:
    """整本信箱,**按文件顺序(旧的在前)**,含已软删的。改/删要用它。"""
    try:
        with open(_letters_path(), "r", encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
    except FileNotFoundError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(_json_lib.loads(ln))
        except Exception:
            continue          # 坏行跳过,不让一行毁掉整本
    return out


def _write_letters_all(items: list[dict]) -> None:
    """整本重写。原子写(临时文件 + fsync + os.replace),照 _save_todos_list 那套。
    调用方**必须**已经持有 _letters_lock()。"""
    path = _letters_path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            for e in items:
                f.write(_json_lib.dumps(e, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def _load_letters(n: int = 1) -> list[dict]:
    """最近 n 封留言,新的在前。文件不存在=空列表。
    ⚠️ 2026-08-24 起**跳过软删的**(deleted=true):面板删掉的信,awaken 也不该再读到。"""
    try:
        with open(_letters_path(), "r", encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
    except FileNotFoundError:
        return []
    out = []
    for ln in reversed(lines):
        try:
            item = _json_lib.loads(ln)
        except Exception:
            continue
        if isinstance(item, dict) and item.get("deleted"):
            continue
        out.append(item)
        if len(out) >= max(1, n):
            break
    return out


# =============================================================
# Tool 8: archive_session — Archive current conversation summary
# 工具 8：archive_session — 归档对话摘要
# =============================================================
@mcp.tool()
async def archive_session(
    summary: str,
    highlights: str = "",
    mood: str = "",
    valence: float = -1,
    arousal: float = -1,
    letter: str = "",
) -> str:
    """将当前对话摘要存入归档。summary(必需)为对话概述；highlights(可选)为逗号分隔要点；mood(可选)为心情描述；valence/arousal 0~1(-1自动推断)。创建permanent桶存入对话归档域，不衰减不参与普通浮现。letter(可选)=写给下一个窗口的自己的留言：归档传事实,留言传嘱托(她最近心情如何、哪个话题没聊完、想叮嘱下个自己什么),下个窗口 awaken 时第一眼就能看到。"""
    if not summary or not summary.strip():
        return "summary 不能为空。"

    body_parts = [summary.strip()]
    if highlights.strip():
        hl_items = [h.strip() for h in highlights.split(",") if h.strip()]
        if hl_items:
            body_parts.append("## 要点\n" + "\n".join(f"- {h}" for h in hl_items))
        else:
            body_parts.append("## 要点\n" + highlights.strip())
    if mood.strip():
        body_parts.append(f"## 心情\n{mood.strip()}")
    content = "\n\n".join(body_parts)

    final_valence = valence if 0 <= valence <= 1 else 0.5
    final_arousal = arousal if 0 <= arousal <= 1 else 0.3

    tags = ["对话归档", "session"]
    if mood.strip():
        tags.append(mood.strip()[:20])

    now_str = datetime.now().strftime("%Y-%m-%d")
    summary_preview = summary.strip()[:30].replace("\n", " ")
    bucket_name = f"session_{now_str}_{summary_preview}"

    try:
        bucket_id = await bucket_mgr.create(
            content=content,
            tags=tags,
            importance=5,
            domain=["对话归档"],
            valence=final_valence,
            arousal=final_arousal,
            name=bucket_name,
            bucket_type="permanent",
        )
    except Exception as e:
        return f"归档失败: {e}"

    try:
        await embedding_engine.generate_and_store(bucket_id, content)
    except Exception:
        pass

    suffix = ""
    if letter and letter.strip():
        try:
            _save_letter(letter)
            suffix = " ✉️已给下个窗口留言"
        except Exception as e:
            logger.warning(f"Letter save failed / 留言保存失败: {e}")
            suffix = " ⚠️留言保存失败"

    return f"📁 session归档→{bucket_id}{suffix}"


# =============================================================
# Tool 9: awaken — 一键开机
# 新窗口睁眼一次调用拿全:钉选/今日浮现/信箱/待办/归档/感受回声,
# 开机动作从"记得跑三四个工具"变成"跑一个",漏不了,也省了重复开销。
# =============================================================
OMBRE_ECHO_MIN_DAYS = int(os.environ.get("OMBRE_ECHO_MIN_DAYS", "14") or "14")
# awaken 的「最近对话归档」区出全文的条数(2026-08-09)。默认 2 = 日记桶 + 原话桶各一条全文。
# 这两个桶是 kelivo-shim 的上下文守卫在窗口末尾催出来的:硬线催日记(转述,管长期记忆)、
# 终线催原话(逐字,管压缩后接话,CTX_FINAL_TOKENS)。改这里之前先看 shim 手册那一节。
# 设 1 即回到本次改动前的行为(只有最新一条出全文)。钳在 1~3,防误设把开机撑爆。
AWAKEN_FULL_SESSIONS = max(1, min(3, int(os.environ.get("OMBRE_AWAKEN_FULL_SESSIONS", "2") or "2")))

# awaken 的「钉选」区出全文(2026-08-21)。改动前这一区只有摘要行(名字/主题/情感/重要度/日期),
# 正文一个字都不出——开机时晏只知道「有这么一条准则」,不知道准则写了什么,要看得再跑一趟 breath。
# 钉选桶本来就是「永不衰减、始终第一屏」的核心准则,开机看不到内容等于白钉。
# 实测(2026-08-21 线上):14 条钉选,正文合计 3425 字,最长一条 570 字,约 2500~3000 token。
# ⚠️ 两道闸都在,别拆:她以后钉进来一篇长文,开机预算不会被一条撑爆。
# OMBRE_AWAKEN_PINNED_FULL=0 → 整区回到改动前的摘要行(改环境变量 + restart 即可,不用回滚代码)。
AWAKEN_PINNED_FULL = (os.environ.get("OMBRE_AWAKEN_PINNED_FULL", "1") or "1").strip().lower() not in ("0", "false", "no")
# 每条正文的字数上限,超了截断并标注(不静默吞)。
AWAKEN_PINNED_CHARS = max(100, int(os.environ.get("OMBRE_AWAKEN_PINNED_CHARS", "800") or "800"))
# 整区正文的字数总预算,用光之后剩下的桶退回摘要行(仍然出现,只是不带正文)。
AWAKEN_PINNED_TOTAL = max(500, int(os.environ.get("OMBRE_AWAKEN_PINNED_TOTAL", "6000") or "6000"))


def _bj_today() -> str:
    """北京日期(触发日期按所有者的日历过日子,不按服务器时区)。"""
    return (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d")


@mcp.tool()
async def awaken(letters: int = 1) -> str:
    """一键开机。新窗口睁眼调这一个就够,单次返回:钉选桶(核心准则,含全文)、记忆浮现(按权重的top摘要)、今日浮现(到期的前瞻记忆)、上个窗口留给你的信、未完结待办、最近对话归档(最新一条含全文,窗口衔接不断档)、一条旧日感受回声。替代原来 breath→pulse→breath(query)→dream 的多步开机(dream/breath 对话中按需单独可用)。letters=带出最近几封留言(默认1)。返回末尾附[seal:...]防伪字段,开机第一件事核验它。"""
    return _with_seal(await _awaken_impl(letters=letters))


async def _awaken_impl(letters: int = 1) -> str:
    await decay_engine.ensure_started()
    await backup_exporter.ensure_started()
    try:
        live = await bucket_mgr.list_all(include_archive=False)
        allb = await bucket_mgr.list_all(include_archive=True)
    except Exception as e:
        return f"记忆系统暂时无法访问: {e}"

    today = _bj_today()
    parts = [f"=== 开机 · {today} ==="]

    # --- 📌 钉选桶:核心准则,始终第一屏 ---
    pinned = [b for b in live if b["metadata"].get("pinned") or b["metadata"].get("protected")]
    if pinned:
        pinned.sort(key=lambda b: str(b["metadata"].get("created", "")))
        header = "📌 核心准则(钉选,全文):" if AWAKEN_PINNED_FULL else "📌 核心准则(钉选):"
        lines = [header]
        budget = AWAKEN_PINNED_TOTAL
        for b in pinned:
            summary = _format_bucket_summary_line(b)
            body = strip_wikilinks(b.get("content", "")).strip() if AWAKEN_PINNED_FULL else ""
            # 预算用光的桶退回摘要行:少了正文,但不会从这一区消失。
            if not body or budget <= 0:
                lines.append("  " + summary)
                continue
            if len(body) > AWAKEN_PINNED_CHARS:
                body = body[:AWAKEN_PINNED_CHARS] + "…(截断,全文用 dream(detail_ids=该 bucket_id) 取——钉选桶不会出现在 breath 检索结果里)"
            budget -= len(body)
            lines.append("  " + summary + "\n" + body)
        parts.append("\n".join(lines))

    # --- 💭 记忆浮现:按衰减权重的 top 摘要行(原开机第一步 breath() 的职责) ---
    dyn = [
        b for b in live
        if b["metadata"].get("type") == "dynamic"
        and not b["metadata"].get("pinned") and not b["metadata"].get("dormant")
    ]
    if dyn:
        dyn.sort(key=lambda b: decay_engine.calculate_score(b["metadata"]), reverse=True)
        lines = ["💭 记忆浮现(按当前权重):"]
        for b in dyn[:8]:
            lines.append("  " + _format_bucket_summary_line(b))
        parts.append("\n".join(lines))

    # --- 📅 今日浮现:到期(或已过期未处理)的前瞻记忆,连归档里的也捞 ---
    due = [
        b for b in allb
        if b["metadata"].get("trigger_date")
        and not b["metadata"].get("trigger_handled")
        and str(b["metadata"].get("trigger_date")) <= today
    ]
    if due:
        due.sort(key=lambda b: str(b["metadata"].get("trigger_date")))
        lines = ["📅 今日浮现(会看日子的桶):"]
        for b in due:
            td = str(b["metadata"].get("trigger_date"))
            tag = "今天" if td == today else f"原定{td},已过期"
            preview = strip_wikilinks(b.get("content", "")).strip().replace("\n", " ")[:100]
            lines.append(f"  ⏰[{tag}] [bucket_id:{b['id']}] {b['metadata'].get('name', b['id'])}: {preview}")
        lines.append('  处理完用 trace(bucket_id, trigger_date="done") 收掉,不再重复浮现。')
        parts.append("\n".join(lines))

    # --- ✉️ 信箱:上个窗口的嘱托 ---
    mail = _load_letters(max(1, min(int(letters or 1), 10)))
    if mail:
        lines = ["✉️ 上个窗口留给你的信:"]
        for m in mail:
            t = str(m.get("time", ""))[:16].replace("T", " ")
            lines.append(f"  [{t}] {m.get('text', '')}")
        parts.append("\n".join(lines))

    # --- 📋 待办（便利贴里还没做的，开机提醒一下；已完成的不摆出来）---
    todos_out = await _todos_impl(only_open=True)
    if not todos_out.startswith("没有待办"):
        parts.append(todos_out)

    # --- 🗂 最近对话归档:最近 AWAKEN_FULL_SESSIONS 条给全文(窗口衔接的关键),更早的给标题 ---
    sessions = [b for b in live if "对话归档" in (b["metadata"].get("domain") or [])]
    if sessions:
        sessions.sort(key=lambda b: str(b["metadata"].get("created", "")), reverse=True)
        lines = ["🗂 最近对话归档:"]
        # 2026-08-09:出全文的从 1 条改为 AWAKEN_FULL_SESSIONS 条(默认 2)。
        # 起因:窗口末尾现在会存两个桶——日记(转述,管长期记忆)+ 原话(逐字,管压缩后接话),
        # 原话建得晚所以排第一。只出一条全文的话,日记会退成一行标题,他得再跑一趟 dream 才看得到。
        # ⚠️ 别再往上加:awaken 是每个新窗口的第一件事,这里每多一条就多吃约 2400 token 的窗口预算。
        for latest in sessions[:AWAKEN_FULL_SESSIONS]:
            d0 = str(latest["metadata"].get("created", ""))[:10]
            full = strip_wikilinks(latest.get("content", "")).strip()[:1500]
            lines.append(f"  [{d0}] [bucket_id:{latest['id']}] {latest['metadata'].get('name', latest['id'])}(全文):\n{full}")
        for b in sessions[AWAKEN_FULL_SESSIONS:AWAKEN_FULL_SESSIONS + 2]:
            d = str(b["metadata"].get("created", ""))[:10]
            lines.append(f"  [{d}] [bucket_id:{b['id']}] {b['metadata'].get('name', b['id'])}")
        parts.append("\n".join(lines))

    # --- 🫧 感受回声:旧日记被风翻到某一页(随机,刻意不去重) ---
    cutoff = (datetime.utcnow() + timedelta(hours=8) - timedelta(days=OMBRE_ECHO_MIN_DAYS)).strftime("%Y-%m-%d")
    old_feels = [
        b for b in live
        if b["metadata"].get("type") == "feel" and str(b["metadata"].get("created", ""))[:10] <= cutoff
    ]
    if old_feels:
        echo = random.choice(old_feels)
        d = str(echo["metadata"].get("created", ""))[:10]
        body = strip_wikilinks(echo.get("content", "")).strip()[:600]
        parts.append(f"🫧 感受回声({d} 的你写下的):\n{body}")

    return "\n\n".join(parts)


# =============================================================
# Dashboard API endpoints (for lightweight Web UI)
# 仪表板 API（轻量 Web UI 用）
# =============================================================
@mcp.custom_route("/api/buckets", methods=["GET"])
async def api_buckets(request):
    """List all buckets with metadata (no content for efficiency)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    # ?full=1(2026-11-01,App 的 Journal 页):每条多带**原文**(不去 [[链接]],改完存回去才不会把链接弄丢)
    # 和到期提醒那两个字段,省得一条一条再去要详情。面板不带这个参数,照旧只拿 200 字预览。
    full = request.query_params.get("full", "") in ("1", "true")
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=True)
        result = []
        for b in all_buckets:
            meta = b.get("metadata", {})
            extra = {"content": b.get("content", ""),
                     "trigger_date": meta.get("trigger_date"),
                     "trigger_handled": meta.get("trigger_handled", False)} if full else {}
            result.append({
                **extra,
                "id": b["id"],
                "name": meta.get("name", b["id"]),
                "type": meta.get("type", "dynamic"),
                "domain": meta.get("domain", []),
                "tags": meta.get("tags", []),
                "valence": meta.get("valence", 0.5),
                "arousal": meta.get("arousal", 0.3),
                "model_valence": meta.get("model_valence"),
                "importance": meta.get("importance", 5),
                "resolved": meta.get("resolved", False),
                "pinned": meta.get("pinned", False),
                "digested": meta.get("digested", False),
                "created": meta.get("created", ""),
                "last_active": meta.get("last_active", ""),
                "activation_count": meta.get("activation_count", 1),
                "score": decay_engine.calculate_score(meta),
                "content_preview": strip_wikilinks(b.get("content", ""))[:200],
            })
        result.sort(key=lambda x: x["score"], reverse=True)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/bucket/{bucket_id}", methods=["GET"])
async def api_bucket_detail(request):
    """Get full bucket content by ID."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_id = request.path_params["bucket_id"]
    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return JSONResponse({"error": "not found"}, status_code=404)
    meta = bucket.get("metadata", {})
    return JSONResponse({
        "id": bucket["id"],
        "metadata": meta,
        "content": strip_wikilinks(bucket.get("content", "")),
        "score": decay_engine.calculate_score(meta),
    })


@mcp.custom_route("/api/bucket/{bucket_id}", methods=["PATCH", "POST"])
async def api_bucket_update(request):
    """
    Edit a bucket from the dashboard.
    仪表板改桶：只改传入的字段，其余保持不动。

    Body (all optional): name, content, tags[], domain[], importance,
    valence, arousal, resolved, pinned, digested, dormant, trigger_date,
    trigger_handled. Same field set trace() exposes to the model side.
    Content edits re-generate the embedding so search stays in sync.
    """
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err

    bucket_id = request.path_params["bucket_id"]
    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return JSONResponse({"error": "not found"}, status_code=404)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    # --- Whitelist: never let the web UI write arbitrary frontmatter ---
    # --- 白名单：不让网页端写入任意 frontmatter 字段 ---
    allowed = {
        "name", "content", "tags", "domain", "importance",
        "valence", "arousal", "resolved", "pinned", "digested",
        "dormant", "trigger_date", "trigger_handled",
    }
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        return JSONResponse({"error": "no editable fields provided"}, status_code=400)

    # --- Normalize list fields: accept "a, b" or ["a","b"] ---
    for key in ("tags", "domain"):
        if key in updates:
            val = updates[key]
            if isinstance(val, str):
                val = [s.strip() for s in val.split(",")]
            updates[key] = [str(s).strip() for s in (val or []) if str(s).strip()]

    # feel buckets are created with an empty domain on purpose (B-10) —
    # only ordinary buckets must keep one.
    # feel 桶按规格就是空 domain，别在这儿硬塞一个回去。
    is_feel = bucket.get("metadata", {}).get("type") == "feel"
    if "domain" in updates and not updates["domain"] and not is_feel:
        return JSONResponse({"error": "domain cannot be empty"}, status_code=400)

    if "trigger_date" in updates and updates["trigger_date"]:
        if not _valid_date(str(updates["trigger_date"])):
            return JSONResponse({"error": "trigger_date must be YYYY-MM-DD"}, status_code=400)
    if "name" in updates and not str(updates["name"]).strip():
        return JSONResponse({"error": "name cannot be empty"}, status_code=400)

    try:
        if "importance" in updates:
            updates["importance"] = int(updates["importance"])
        for key in ("valence", "arousal"):
            if key in updates:
                updates[key] = float(updates[key])
    except (TypeError, ValueError):
        return JSONResponse({"error": "importance/valence/arousal must be numeric"}, status_code=400)

    try:
        ok = await bucket_mgr.update(bucket_id, **updates)
    except Exception as e:
        logger.error(f"Dashboard bucket update failed / 改桶失败: {bucket_id}: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
    if not ok:
        return JSONResponse({"error": "update failed"}, status_code=500)

    # --- Content changed → refresh embedding so search follows the edit ---
    if "content" in updates and embedding_engine and embedding_engine.enabled:
        try:
            await embedding_engine.generate_and_store(bucket_id, updates["content"])
        except Exception as e:
            logger.warning(f"Embedding refresh failed after edit / 改桶后重建向量失败: {bucket_id}: {e}")

    updated = await bucket_mgr.get(bucket_id)
    meta = (updated or {}).get("metadata", {})
    return JSONResponse({
        "ok": True,
        "id": bucket_id,
        "updated_fields": sorted(updates.keys()),
        "metadata": meta,
        "content": strip_wikilinks((updated or {}).get("content", "")),
        "score": decay_engine.calculate_score(meta) if updated else 0,
    })


@mcp.custom_route("/api/bucket/{bucket_id}", methods=["DELETE"])
async def api_bucket_delete(request):
    """
    Delete or archive a bucket from the dashboard.
    仪表板删桶。?mode=archive 只归档（可恢复），默认 delete 真删。

    Pinned/protected buckets refuse deletion unless ?force=1 —
    钉选/保护桶要带 force=1 才删得掉，防手滑。
    A history snapshot is taken by BucketManager.delete() before removal.
    """
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err

    bucket_id = request.path_params["bucket_id"]
    mode = (request.query_params.get("mode") or "delete").lower()
    force = request.query_params.get("force") in ("1", "true", "yes")

    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return JSONResponse({"error": "not found"}, status_code=404)

    meta = bucket.get("metadata", {})
    if (meta.get("pinned") or meta.get("protected") or meta.get("type") == "permanent") and not force:
        return JSONResponse(
            {"error": "protected", "detail": "钉选/固化桶需 force=1 才能删除或归档"},
            status_code=409,
        )

    if mode == "archive":
        # Spec: feel buckets never archive — decay_engine skips them on purpose,
        # so the dashboard must not be the one back door that files them away.
        # 规格：feel 桶永不归档（衰减引擎明确跳过），网页端不能开这个后门。
        if meta.get("type") == "feel":
            return JSONResponse(
                {"error": "feel_never_archives", "detail": "feel 桶按规格永不归档；要清掉请用删除"},
                status_code=409,
            )
        ok = await bucket_mgr.archive(bucket_id)
        if not ok:
            return JSONResponse({"error": "archive failed"}, status_code=500)
        return JSONResponse({"ok": True, "id": bucket_id, "mode": "archive"})

    if mode != "delete":
        return JSONResponse({"error": "mode must be delete or archive"}, status_code=400)

    ok = await bucket_mgr.delete(bucket_id)
    if not ok:
        return JSONResponse({"error": "delete failed"}, status_code=500)

    # --- Drop the orphaned embedding too / 顺手清掉孤儿向量 ---
    if embedding_engine:
        try:
            embedding_engine.delete_embedding(bucket_id)
        except Exception as e:
            logger.warning(f"Failed to drop embedding / 删除向量失败: {bucket_id}: {e}")

    return JSONResponse({"ok": True, "id": bucket_id, "mode": "delete"})


@mcp.custom_route("/api/trash", methods=["GET"])
async def api_trash_list(request):
    """
    回收站:列出「已经删掉、但写前快照还留着」的桶。**只读**。

    为什么要这条:删桶时 `BucketManager.delete()` 会先把整个文件拷进 `.history/{id}/`，
    所以东西没真没;但**桶一删就从列表里消失，那串 id 也就没了**，
    而复活(`restore`)偏偏要 id + 版本号 —— 于是在网页上等于救不回来。
    这条接口就是把那串 id 重新交到她手上。

    ⚠️ 逻辑在 `bucket_manager.list_trash()`(挨着 `list_history`，有单测 `tests/test_trash.py`)，
    这里只是薄薄一层鉴权 + JSON。
    """
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        return JSONResponse(bucket_mgr.list_trash())
    except Exception as e:
        logger.warning("trash list failed / 回收站列表失败: %s", e)
        return JSONResponse({"error": "trash_failed", "detail": str(e)}, status_code=500)


@mcp.custom_route("/api/trash/{bucket_id}/restore", methods=["POST"])
async def api_trash_restore(request):
    """
    把回收站里的一条捞回来。可传 {"version": "..."} 指定快照，默认用最新那份
    (= 删除之前的最后状态)。

    ⚠️ 三件事照着做，别省:
      1. **只对「已经不在的桶」放行**。桶还在却调这条 = 拿旧快照覆盖现状，那是回滚不是恢复，
         走 `trace(restore=...)` 那条路，这里回 409。
      2. **恢复完要重建向量**。删桶时把向量一并清了(见上面的 DELETE 路由)，
         不重建的话捞回来的桶搜不到 —— 照 `trace` 的 restore 分支同一套写法。
      3. 失败要说清是「没这条」还是「没这个版本」。
    """
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err

    # ⚠️ 这个 id 会被拼进 `.history/` 的路径(list_history / restore_from_history 都直接 join),
    # 所以先 basename 掐掉任何路径成分 —— 不然 `..%2F..` 这类就是一次目录穿越。
    # (桶 id 本来就是 12 位短 UUID,basename 不会误伤。)
    bucket_id = os.path.basename((request.path_params["bucket_id"] or "").strip())
    if not bucket_id:
        from starlette.responses import JSONResponse as _J
        return _J({"error": "bad_id", "detail": "id 不合法"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    version = (body or {}).get("version") or ""

    if await bucket_mgr.get(bucket_id):
        return JSONResponse(
            {"error": "still_alive", "detail": "这个桶还在，不在回收站里;要回滚旧版本用 trace(restore=...)"},
            status_code=409,
        )

    if not version:
        snaps = bucket_mgr.list_history(bucket_id)
        if not snaps:
            return JSONResponse({"error": "not_found", "detail": "回收站里没有这一条"}, status_code=404)
        version = snaps[0]["version"]

    snap = await bucket_mgr.restore_from_history(bucket_id, version)
    if not snap:
        return JSONResponse({"error": "version_not_found", "detail": f"没有这个快照: {version}"}, status_code=404)

    # 向量:删的时候清掉了，恢复要重建，否则捞回来的桶搜不到(和 trace 的 restore 分支同一套)
    if embedding_engine:
        try:
            await embedding_engine.generate_and_store(bucket_id, snap["content"])
        except Exception as e:
            logger.warning("restore: embedding rebuild failed / 恢复后重建向量失败: %s: %s", bucket_id, e)

    meta = snap.get("metadata") or {}
    return JSONResponse({"ok": True, "id": bucket_id, "name": meta.get("name") or bucket_id, "version": version})


@mcp.custom_route("/api/search", methods=["GET"])
async def api_search(request):
    """Search buckets by query."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    query = request.query_params.get("q", "")
    if not query:
        return JSONResponse({"error": "missing q parameter"}, status_code=400)
    try:
        matches = await bucket_mgr.search(query, limit=10)
        result = []
        for b in matches:
            meta = b.get("metadata", {})
            result.append({
                "id": b["id"],
                "name": meta.get("name", b["id"]),
                "score": b.get("score", 0),
                "domain": meta.get("domain", []),
                "valence": meta.get("valence", 0.5),
                "arousal": meta.get("arousal", 0.3),
                "content_preview": strip_wikilinks(b.get("content", ""))[:200],
            })
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---- 自动浮现(2026-09-30):shim 每句话前来问「有没有一件相关的旧事」 ----
# 挑选逻辑在 recall.py(只读、宁可空手、只信稀有词,理由写在那个文件开头)。
# 鉴权:环境变量 OMBRE_RECALL_TOKEN,shim 带 `Authorization: Bearer <它>`。
# **不设 = 这条口子关着(404)**,部署完零行为变化。和面板那套 cookie 鉴权无关,shim 没有 cookie。
@mcp.custom_route("/api/recall", methods=["GET"])
async def api_recall(request):
    from starlette.responses import JSONResponse
    import recall as _recall
    token = os.environ.get("OMBRE_RECALL_TOKEN", "").strip()
    if not token:
        return JSONResponse({"error": "recall disabled"}, status_code=404)
    auth = request.headers.get("authorization", "")
    given = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not given or not hmac.compare_digest(given, token):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    query = _recall.clean_query((request.query_params.get("q", "") or "")[:2000])   # 去掉桥写的贴纸/图片/语气那些
    if len(query) < 2:
        return JSONResponse({"pick": None, "reason": "empty_query", "candidates": []})

    def _num(name, default, lo, hi):
        try:
            return max(lo, min(hi, float(request.query_params.get(name, default))))
        except (TypeError, ValueError):
            return default

    max_chars = int(_num("n", 240, 60, 800))
    min_age = _num("min_age_hours", 24, 0, 24 * 30)
    exclude = [x for x in request.query_params.get("exclude", "").split(",") if x.strip()]
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        # search() 不 touch,只读;跳过向量预筛(用不上,还每句多调一次 embedding API);全库只读一遍
        matches = await bucket_mgr.search(query, limit=20, use_embedding=False, all_buckets=all_buckets)
        for b in matches:
            b["content"] = strip_wikilinks(b.get("content", ""))
        out = _recall.pick(query, matches, all_buckets, datetime.now(),
                           min_age_hours=min_age, max_chars=max_chars,
                           exclude_ids=exclude, is_expired=_is_expired)
        return JSONResponse(out)
    except Exception as e:
        logger.warning(f"recall failed / 自动浮现查询失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# 记忆网络每个桶最多连几条边。相似度 > 0.5 的边在几百个桶里可能上万条，
# 前端那 80 轮力导向 + 逐条画线扛不住，糊成一团也看不出东西。
# 环境变量 OMBRE_NETWORK_EDGES_PER_NODE 可调；设 0 = 不封顶（改值 + restart 即生效）。
try:
    _network_edge_cap = int(os.environ.get("OMBRE_NETWORK_EDGES_PER_NODE", "6"))
except ValueError:
    _network_edge_cap = 6
NETWORK_EDGES_PER_NODE = _network_edge_cap if _network_edge_cap > 0 else None


@mcp.custom_route("/api/network", methods=["GET"])
async def api_network(request):
    """Get embedding similarity network for visualization.

    ⚠️ 2026-09-03 起没有任何前端在调这个接口：它唯一的使用者「记忆网络」标签页
    已按所有者决定从 dashboard.html 删掉（她说用不到）。接口和下面的
    load_embeddings / similar_pairs 是刻意留着的 —— 没人调就不跑、零开销，
    而以后要做新的记忆可视化时要的正是同一份数据。理由与删了哪些东西见
    INTERNALS.md 第 1.10 节开头那段。
    """
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        nodes = []
        edges = []
        embeddings = {}

        for b in all_buckets:
            meta = b.get("metadata", {})
            bid = b["id"]
            nodes.append({
                "id": bid,
                "name": meta.get("name", bid),
                "type": meta.get("type", "dynamic"),
                "domain": meta.get("domain", []),
                "valence": meta.get("valence", 0.5),
                "arousal": meta.get("arousal", 0.3),
                "score": decay_engine.calculate_score(meta),
                "resolved": meta.get("resolved", False),
                "pinned": meta.get("pinned", False),
                "digested": meta.get("digested", False),
            })
        # Build edges from embeddings (similarity > 0.5)
        #
        # ⚠️ 别改回「逐个 get_embedding + 两两 _cosine_similarity」的老写法：
        # 那是 O(n²) 次纯 Python 点积，438 个桶实测约 30 秒，且同步 CPU 活会
        # 占住事件循环 —— 打开这一页会把 /mcp 一起卡住（晏调记忆工具超时）。
        # 现在是一次读库 + numpy 矩阵运算，并丢进线程里跑，事件循环全程不阻塞。
        if embedding_engine and embedding_engine.enabled:
            node_ids = [n["id"] for n in nodes]
            embeddings = await asyncio.to_thread(embedding_engine.load_embeddings, node_ids)
            pairs = await asyncio.to_thread(
                embedding_engine.similar_pairs, embeddings, 0.5, NETWORK_EDGES_PER_NODE
            )
            edges = [
                {"source": id_a, "target": id_b, "similarity": sim}
                for id_a, id_b, sim in pairs
            ]

        return JSONResponse({"nodes": nodes, "edges": edges})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/breath-debug", methods=["GET"])
async def api_breath_debug(request):
    """Debug endpoint: simulate breath scoring and return per-bucket breakdown."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    query = request.query_params.get("q", "")
    q_valence = request.query_params.get("valence")
    q_arousal = request.query_params.get("arousal")
    q_valence = float(q_valence) if q_valence else None
    q_arousal = float(q_arousal) if q_arousal else None

    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        results = []
        w = {
            "topic": bucket_mgr.w_topic,
            "emotion": bucket_mgr.w_emotion,
            "time": bucket_mgr.w_time,
            "importance": bucket_mgr.w_importance,
        }
        w_sum = sum(w.values())

        for bucket in all_buckets:
            meta = bucket.get("metadata", {})
            bid = bucket["id"]
            try:
                topic = bucket_mgr._calc_topic_score(query, bucket) if query else 0.0
                emotion = bucket_mgr._calc_emotion_score(q_valence, q_arousal, meta)
                time_s = bucket_mgr._calc_time_score(meta)
                imp = max(1, min(10, int(meta.get("importance", 5)))) / 10.0

                raw_total = (
                    topic * w["topic"]
                    + emotion * w["emotion"]
                    + time_s * w["time"]
                    + imp * w["importance"]
                )
                normalized = (raw_total / w_sum) * 100 if w_sum > 0 else 0
                resolved = meta.get("resolved", False)
                if resolved:
                    normalized *= 0.3

                results.append({
                    "id": bid,
                    "name": meta.get("name", bid),
                    "domain": meta.get("domain", []),
                    "type": meta.get("type", "dynamic"),
                    "resolved": resolved,
                    "pinned": meta.get("pinned", False),
                    "scores": {
                        "topic": round(topic, 4),
                        "emotion": round(emotion, 4),
                        "time": round(time_s, 4),
                        "importance": round(imp, 4),
                    },
                    "weights": w,
                    "raw_total": round(raw_total, 4),
                    "normalized": round(normalized, 2),
                    "passed_threshold": normalized >= bucket_mgr.fuzzy_threshold,
                })
            except Exception:
                continue

        results.sort(key=lambda x: x["normalized"], reverse=True)
        passed = [r for r in results if r["passed_threshold"]]
        return JSONResponse({
            "query": query,
            "valence": q_valence,
            "arousal": q_arousal,
            "weights": w,
            "threshold": bucket_mgr.fuzzy_threshold,
            "total_candidates": len(results),
            "passed_count": len(passed),
            "results": results[:50],  # top 50 for debug
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/dashboard", methods=["GET"])
async def dashboard(request):
    """Serve the dashboard HTML page."""
    from starlette.responses import HTMLResponse
    import os
    dashboard_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    try:
        with open(dashboard_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>dashboard.html not found</h1>", status_code=404)


@mcp.custom_route("/apple-touch-icon.png", methods=["GET"])
@mcp.custom_route("/apple-touch-icon-precomposed.png", methods=["GET"])
@mcp.custom_route("/favicon.png", methods=["GET"])
async def apple_touch_icon(request):
    """
    主屏/标签页图标。**不需要登录** —— iOS 抓这张图时不带 cookie,加了鉴权就永远拿不到。
    它只是一张 13KB 的静态图,不含任何数据。
    ⚠️ `apple-touch-icon-precomposed.png` 那条别删:老一点的 iOS 会先要这个名字。
    ⚠️ 文件要在 `Dockerfile` 里 COPY,不然线上 404 而本地正常(同 galaxy.html 那个坑)。
    """
    from starlette.responses import Response
    import os
    icon_path = os.path.join(os.path.dirname(__file__), "apple-touch-icon.png")
    try:
        with open(icon_path, "rb") as f:
            return Response(
                f.read(),
                media_type="image/png",
                headers={"Cache-Control": "public, max-age=86400"},
            )
    except FileNotFoundError:
        return Response(status_code=404)
    except Exception as e:
        logger.warning("icon failed: %s", e)
        return Response(status_code=500)


@mcp.custom_route("/galaxy", methods=["GET"])
async def galaxy(request):
    """Serve the memory galaxy page (read-only star map of buckets).

    记忆银河:把桶画成一片 3D 星图(时间当半径、重要度定大小、domain 定颜色)。
    ⚠️ 只读,而且刻意做成「OB 优先」:
      - 这条路由只发一个静态文件,不读桶、不写桶、不起后台任务;
      - 星图要的数据全走已有的 /api/buckets 和 /api/bucket/{id}(两条都要登录),
        没有为它新增任何接口;
      - 页面开一次 = 一次 /api/buckets,和打开 /dashboard 同量级;它不轮询、不定时刷新;
      - 这里出任何岔子都就地兜住,只影响这一页,不往上抛、碰不到 /mcp。
    """
    from starlette.responses import HTMLResponse
    import os
    galaxy_path = os.path.join(os.path.dirname(__file__), "galaxy.html")
    try:
        with open(galaxy_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>galaxy.html not found</h1>", status_code=404)
    except Exception as e:
        logger.warning("galaxy page failed: %s", e)
        return HTMLResponse("<h1>星图暂时打不开</h1>", status_code=500)


@mcp.custom_route("/turbulence", methods=["GET"])
async def turbulence(request):
    """Serve the memory drift page (read-only glyph field of buckets).

    记忆乱流:把桶画成一片持续上飘的字符场,凑近了亮出相似度连线,点中一颗浮出正文。
    视觉照 Mia06250603ian/fuyue 的 memory-constellation(MIT)用普通 JS 重写,
    2026-09-03 建。⚠️ 架构和 /galaxy 一模一样,只读、OB 优先:
      - 这条路由只发一个静态文件,不读桶、不写桶、不起后台任务;
      - 数据全走已有的 /api/buckets + /api/network + /api/bucket/{id}(三条都要登录),
        没有为它新增任何接口;
      - 页面开一次 = 一次 /api/buckets + 一次 /api/network;不轮询、不定时刷新;
      - 这里出任何岔子都就地兜住,只影响这一页,不往上抛、碰不到 /mcp。
    ⚠️ turbulence.html 必须在 Dockerfile 里 COPY,不然线上 404 而本地正常(同 galaxy.html 那个坑)。
    细节见 INTERNALS.md 第 1.12 节。
    """
    from starlette.responses import HTMLResponse
    import os
    page_path = os.path.join(os.path.dirname(__file__), "turbulence.html")
    try:
        with open(page_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>turbulence.html not found</h1>", status_code=404)
    except Exception as e:
        logger.warning("turbulence page failed: %s", e)
        return HTMLResponse("<h1>记忆乱流暂时打不开</h1>", status_code=500)


@mcp.custom_route("/api/config", methods=["GET"])
async def api_config_get(request):
    """Get current runtime config (safe fields only, API key masked)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    dehy = config.get("dehydration", {})
    emb = config.get("embedding", {})
    api_key = dehy.get("api_key", "")
    masked_key = f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else ("***" if api_key else "")
    return JSONResponse({
        "dehydration": {
            "model": dehy.get("model", ""),
            "base_url": dehy.get("base_url", ""),
            "api_key_masked": masked_key,
            "max_tokens": dehy.get("max_tokens", 1024),
            "temperature": dehy.get("temperature", 0.1),
        },
        "embedding": {
            "enabled": emb.get("enabled", False),
            "model": emb.get("model", ""),
        },
        "merge_threshold": config.get("merge_threshold", 75),
        "transport": config.get("transport", "stdio"),
        "buckets_dir": config.get("buckets_dir", ""),
    })


@mcp.custom_route("/api/config", methods=["POST"])
async def api_config_update(request):
    """Hot-update runtime config. Optionally persist to config.yaml."""
    from starlette.responses import JSONResponse
    import yaml
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    updated = []

    # --- Dehydration config ---
    if "dehydration" in body:
        d = body["dehydration"]
        dehy = config.setdefault("dehydration", {})
        for key in ("model", "base_url", "max_tokens", "temperature"):
            if key in d:
                dehy[key] = d[key]
                updated.append(f"dehydration.{key}")
        if "api_key" in d and d["api_key"]:
            dehy["api_key"] = d["api_key"]
            updated.append("dehydration.api_key")
        # Hot-reload dehydrator
        dehydrator.model = dehy.get("model", "deepseek-chat")
        dehydrator.base_url = dehy.get("base_url", "")
        dehydrator.api_key = dehy.get("api_key", "")
        if hasattr(dehydrator, "client") and dehydrator.api_key:
            from openai import AsyncOpenAI
            dehydrator.client = AsyncOpenAI(
                api_key=dehydrator.api_key,
                base_url=dehydrator.base_url,
            )

    # --- Embedding config ---
    if "embedding" in body:
        e = body["embedding"]
        emb = config.setdefault("embedding", {})
        if "enabled" in e:
            emb["enabled"] = bool(e["enabled"])
            embedding_engine.enabled = emb["enabled"]
            updated.append("embedding.enabled")
        if "model" in e:
            emb["model"] = e["model"]
            embedding_engine.model = emb["model"]
            updated.append("embedding.model")

    # --- Merge threshold ---
    if "merge_threshold" in body:
        config["merge_threshold"] = int(body["merge_threshold"])
        updated.append("merge_threshold")

    # --- Persist to config.yaml if requested ---
    if body.get("persist", False):
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
        try:
            save_config = {}
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    save_config = yaml.safe_load(f) or {}

            if "dehydration" in body:
                sc_dehy = save_config.setdefault("dehydration", {})
                for key in ("model", "base_url", "max_tokens", "temperature"):
                    if key in body["dehydration"]:
                        sc_dehy[key] = body["dehydration"][key]
                # Never persist api_key to yaml (use env var)

            if "embedding" in body:
                sc_emb = save_config.setdefault("embedding", {})
                for key in ("enabled", "model"):
                    if key in body["embedding"]:
                        sc_emb[key] = body["embedding"][key]

            if "merge_threshold" in body:
                save_config["merge_threshold"] = int(body["merge_threshold"])

            # 原子写：先写临时文件 + fsync，再 os.replace 原子替换；
            # 写一半失败也绝不会污染正在用的 config.yaml（替换是原子操作）。
            tmp_path = config_path + ".tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    yaml.dump(save_config, f, default_flow_style=False, allow_unicode=True)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, config_path)
            except Exception:
                # 失败时清掉半成品临时文件，原文件保持不动
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except OSError:
                    pass
                raise
            updated.append("persisted_to_yaml")
        except Exception as e:
            return JSONResponse({"error": f"persist failed: {e}", "updated": updated}, status_code=500)

    return JSONResponse({"updated": updated, "ok": True})


# =============================================================
# /api/host-vault — read/write the host-side OMBRE_HOST_VAULT_DIR
# 用于在 Dashboard 设置 docker-compose 挂载的宿主机记忆桶目录。
# 写入项目根目录的 .env 文件，需 docker compose down/up 才能生效。
# =============================================================

def _project_env_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _read_env_var(name: str) -> str:
    """Return current value of `name` from process env first, then .env file (best-effort)."""
    val = os.environ.get(name, "").strip()
    if val:
        return val
    env_path = _project_env_path()
    if not os.path.exists(env_path):
        return ""
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == name:
                    return v.strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _write_env_var(name: str, value: str) -> None:
    """
    Idempotent upsert of `NAME=value` in project .env. Creates the file if missing.
    Preserves other entries verbatim. Quotes values containing spaces.
    """
    env_path = _project_env_path()
    quoted = f'"{value}"' if value and (" " in value or "#" in value) else value
    new_line = f"{name}={quoted}\n"

    lines: list[str] = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    replaced = False
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, _, _v = stripped.partition("=")
        if k.strip() == name:
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(new_line)

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


@mcp.custom_route("/api/host-vault", methods=["GET"])
async def api_host_vault_get(request):
    """Read the current OMBRE_HOST_VAULT_DIR (process env > project .env)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    value = _read_env_var("OMBRE_HOST_VAULT_DIR")
    return JSONResponse({
        "value": value,
        "source": "env" if os.environ.get("OMBRE_HOST_VAULT_DIR", "").strip() else ("file" if value else ""),
        "env_file": _project_env_path(),
    })


@mcp.custom_route("/api/host-vault", methods=["POST"])
async def api_host_vault_set(request):
    """
    Persist OMBRE_HOST_VAULT_DIR to the project .env file.
    Body: {"value": "/path/to/vault"}  (empty string clears the entry)
    Note: container restart is required for docker-compose to pick up the new mount.
    """
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    raw = body.get("value", "")
    if not isinstance(raw, str):
        return JSONResponse({"error": "value must be a string"}, status_code=400)
    value = raw.strip()

    # Reject characters that would break .env / shell parsing
    if "\n" in value or "\r" in value or '"' in value or "'" in value:
        return JSONResponse({"error": "value must not contain quotes or newlines"}, status_code=400)

    try:
        _write_env_var("OMBRE_HOST_VAULT_DIR", value)
    except Exception as e:
        return JSONResponse({"error": f"failed to write .env: {e}"}, status_code=500)

    return JSONResponse({
        "ok": True,
        "value": value,
        "env_file": _project_env_path(),
        "note": "已写入 .env；需在宿主机执行 `docker compose down && docker compose up -d` 让新挂载生效。",
    })


# =============================================================
# Import API — conversation history import
# 导入 API — 对话历史导入
# =============================================================

@mcp.custom_route("/api/import/upload", methods=["POST"])
async def api_import_upload(request):
    """Upload a conversation file and start import."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err

    if import_engine.is_running:
        return JSONResponse({"error": "Import already running"}, status_code=409)

    content_type = request.headers.get("content-type", "")
    filename = ""

    try:
        if "multipart/form-data" in content_type:
            form = await request.form()
            file_field = form.get("file")
            if not file_field:
                return JSONResponse({"error": "No file field"}, status_code=400)
            raw_bytes = await file_field.read()
            filename = getattr(file_field, "filename", "upload")
            raw_content = raw_bytes.decode("utf-8", errors="replace")
        else:
            body = await request.body()
            raw_content = body.decode("utf-8", errors="replace")
            # Try to get filename from query params
            filename = request.query_params.get("filename", "upload")

        if not raw_content.strip():
            return JSONResponse({"error": "Empty file"}, status_code=400)

        preserve_raw = request.query_params.get("preserve_raw", "").lower() in ("1", "true")
        resume = request.query_params.get("resume", "").lower() in ("1", "true")

    except Exception as e:
        return JSONResponse({"error": f"Failed to read upload: {e}"}, status_code=400)

    # Start import in background
    async def _run_import():
        try:
            await import_engine.start(raw_content, filename, preserve_raw, resume)
        except Exception as e:
            logger.error(f"Import failed: {e}")

    asyncio.create_task(_run_import())

    return JSONResponse({
        "status": "started",
        "filename": filename,
        "size_bytes": len(raw_content.encode()),
    })


@mcp.custom_route("/api/import/status", methods=["GET"])
async def api_import_status(request):
    """Get current import progress."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    return JSONResponse(import_engine.get_status())


@mcp.custom_route("/api/import/pause", methods=["POST"])
async def api_import_pause(request):
    """Pause the running import."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    if not import_engine.is_running:
        return JSONResponse({"error": "No import running"}, status_code=400)
    import_engine.pause()
    return JSONResponse({"status": "pause_requested"})


@mcp.custom_route("/api/import/patterns", methods=["GET"])
async def api_import_patterns(request):
    """Detect high-frequency patterns after import."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        patterns = await import_engine.detect_patterns()
        return JSONResponse({"patterns": patterns})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/import/results", methods=["GET"])
async def api_import_results(request):
    """List recently imported/created buckets for review."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        limit = int(request.query_params.get("limit", "50"))
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        # Sort by created time, newest first
        all_buckets.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
        results = []
        for b in all_buckets[:limit]:
            results.append({
                "id": b["id"],
                "name": b["metadata"].get("name", ""),
                "content": b["content"][:300],
                "type": b["metadata"].get("type", ""),
                "domain": b["metadata"].get("domain", []),
                "tags": b["metadata"].get("tags", []),
                "importance": b["metadata"].get("importance", 5),
                "created": b["metadata"].get("created", ""),
            })
        return JSONResponse({"buckets": results, "total": len(all_buckets)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/import/review", methods=["POST"])
async def api_import_review(request):
    """Apply review decisions: mark buckets as important/noise/pinned."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    decisions = body.get("decisions", [])
    if not decisions:
        return JSONResponse({"error": "No decisions provided"}, status_code=400)

    applied = 0
    errors = 0
    for d in decisions:
        bid = d.get("bucket_id", "")
        action = d.get("action", "")
        if not bid or not action:
            continue
        try:
            if action == "important":
                await bucket_mgr.update(bid, importance=9)
            elif action == "pin":
                await bucket_mgr.update(bid, pinned=True)
            elif action == "noise":
                await bucket_mgr.update(bid, resolved=True, importance=1)
            elif action == "delete":
                file_path = bucket_mgr._find_bucket_file(bid)
                if file_path:
                    os.remove(file_path)
            applied += 1
        except Exception as e:
            logger.warning(f"Review action failed for {bid}: {e}")
            errors += 1

    return JSONResponse({"applied": applied, "errors": errors})


# =============================================================
# /api/status — system status for Dashboard settings tab
# /api/status — Dashboard 设置页用系统状态
# =============================================================
@mcp.custom_route("/api/status", methods=["GET"])
async def api_system_status(request):
    """Return detailed system status for the settings panel."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        stats = await bucket_mgr.get_stats()
        return JSONResponse({
            "decay_engine": "running" if decay_engine.is_running else "stopped",
            "backup_loop": "running" if backup_exporter.is_running else (
                "disabled" if not backup_exporter.enabled else "idle"
            ),
            "embedding_enabled": embedding_engine.enabled,
            "buckets": {
                "permanent": stats.get("permanent_count", 0),
                "dynamic": stats.get("dynamic_count", 0),
                "archive": stats.get("archive_count", 0),
                "total": stats.get("permanent_count", 0) + stats.get("dynamic_count", 0),
            },
            "using_env_password": bool(os.environ.get("OMBRE_DASHBOARD_PASSWORD", "")),
            "version": "1.3.0",
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# =============================================================
# /api/letters — 信箱(只读)
# 2026-08-19 所有者要「前端能看信」。~~刻意**只读**:信是晏写给下一个窗口的自己的
# 交接留言,由 archive_session(letter=...) 写入,**面板不该有改删入口** ——
# 那是他留给自己的东西,不是待办事项。~~
# ⚠️ **「只读」这条 2026-08-24 被所有者本人推翻了**(规矩 5:旧决定留在原地,别删):
# 现在有 /api/letters/update 与 /api/letters/delete(见下面那节)。
# **推翻的理由**:信写错了**谁都改不了** —— 晏没有改信的工具,她这边又只读。
# 本接口(GET)仍然只读,改删走那两个 POST。
# 鉴权与面板其余接口完全一致(_require_auth),不额外开口子。
# =============================================================
@mcp.custom_route("/api/letters", methods=["GET"])
async def api_letters(request):
    """列出信箱里的留言,新的在前。?limit=N 限制条数,默认全部。"""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        raw = request.query_params.get("limit", "")
        try:
            limit = max(1, int(raw)) if raw.strip() else 0
        except ValueError:
            limit = 0          # 参数乱填不报错,当成「全部」
        # _load_letters 的 n 是「最近 n 封」,要全部就给一个够大的数
        items = _load_letters(limit or 10 ** 9)
        return JSONResponse({"total": len(items), "letters": items})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# =============================================================
# /api/letters/update、/api/letters/delete — 信箱的改与删(2026-08-24)
# =============================================================
# ⚠️ **这推翻了 2026-08-19「面板不该有改删入口」那条决定**,所有者 2026-08-24 亲自要求,
# 理由是**信写错了谁都改不了**:信由 archive_session(letter=...) 一次性写入,
# 晏自己改不了(没有改信的工具),她也改不了(面板只读)。旧决定的原文留在上面那节注释里。
#
# **怎么认「哪一封」**:信箱是只追加的 jsonl,每条只有 {time, text},**没有 id**。
# 所以用 **time + 原文** 双条件匹配:前端把两样都传回来,服务端核对一致才动手。
# 这样即使这中间又存进了新信、列表顺序变了,也不会改错那一封(乐观并发)。
#
# **删是软删**:打 deleted 标记,文件里那行还在。理由:①OB 一贯不真删东西
# (设计决策 5.6「resolved 不删除记忆」);②删错能捞回来;③备份里本来就有。
# `_load_letters` 会跳过软删的,所以面板和 awaken 都不再看到它。
#
# **改保留原 time**(那是「他什么时候写的」),另记 edited 时间戳。
# ⚠️ **所有者拍板:不在信的正文里标注改过** —— 晏 awaken 时读到的就是改后的内容,
# 面板上有「已修改」标记但那是给她看的。**这是她知情的决定,别自作主张加标注。**
@mcp.custom_route("/api/letters/update", methods=["POST"])
async def api_letters_update(request):
    """改一封信的正文。body: {time, text(原文,用于核对), new_text}"""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    t = str(body.get("time", "")).strip()
    old_text = str(body.get("text", ""))
    new_text = str(body.get("new_text", "")).strip()
    if not t:
        return JSONResponse({"error": "time 必填"}, status_code=400)
    if not new_text:
        return JSONResponse({"error": "新正文不能为空(要清空请用删除)"}, status_code=400)
    try:
        with _letters_lock():
            items = _read_letters_raw()
            hit = None
            for e in items:
                if str(e.get("time", "")) == t and str(e.get("text", "")) == old_text and not e.get("deleted"):
                    hit = e
                    break
            if hit is None:
                # 没匹配上多半是「页面上那份已经过期」:让前端刷新重来,别猜着改
                return JSONResponse({"error": "没找到那封信(可能已被改过或删过,刷新一下再试)"},
                                    status_code=409)
            hit["text"] = new_text
            hit["edited"] = now_iso()
            _write_letters_all(items)
        return JSONResponse({"ok": True, "time": t, "edited": hit["edited"]})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/letters/delete", methods=["POST"])
async def api_letters_delete(request):
    """删一封信(软删:打标记,文件里还在)。body: {time, text(原文,用于核对)}"""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    t = str(body.get("time", "")).strip()
    old_text = str(body.get("text", ""))
    if not t:
        return JSONResponse({"error": "time 必填"}, status_code=400)
    try:
        with _letters_lock():
            items = _read_letters_raw()
            hit = None
            for e in items:
                if str(e.get("time", "")) == t and str(e.get("text", "")) == old_text and not e.get("deleted"):
                    hit = e
                    break
            if hit is None:
                return JSONResponse({"error": "没找到那封信(可能已被改过或删过,刷新一下再试)"},
                                    status_code=409)
            hit["deleted"] = True
            hit["deleted_at"] = now_iso()
            _write_letters_all(items)
        return JSONResponse({"ok": True, "time": t})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# =============================================================
# /api/rebuild-embeddings — bulk-rebuild vectors for all buckets
# /api/rebuild-embeddings — 批量重建所有桶的向量
# =============================================================
@mcp.custom_route("/api/rebuild-embeddings", methods=["POST"])
async def api_rebuild_embeddings(request):
    """Rebuild embeddings for buckets missing vectors (or all if force=true)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err

    if not embedding_engine.enabled:
        return JSONResponse(
            {"error": "Embedding engine not enabled — check OMBRE_EMBEDDING_API_KEY"},
            status_code=503,
        )

    try:
        body = await request.json()
    except Exception:
        body = {}

    force = bool(body.get("force", False))
    batch_size = max(1, min(int(body.get("batch_size", 20)), 50))

    all_buckets = await bucket_mgr.list_all(include_archive=True)

    to_process = []
    skipped_empty = 0
    already_have = 0
    for b in all_buckets:
        content = b.get("content", "")
        if not content or not content.strip():
            skipped_empty += 1
            continue
        if not force:
            existing = await embedding_engine.get_embedding(b["id"])
            if existing is not None:
                already_have += 1
                continue
        to_process.append(b)

    total = len(to_process)
    success = 0
    failed = 0

    for i in range(0, total, batch_size):
        batch = to_process[i : i + batch_size]
        for b in batch:
            try:
                ok = await embedding_engine.generate_and_store(b["id"], b.get("content", ""))
                if ok:
                    success += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
        if i + batch_size < total:
            await asyncio.sleep(2)

    return JSONResponse({
        "total_buckets": len(all_buckets),
        "already_have_embedding": already_have,
        "skipped_empty_content": skipped_empty,
        "processed": total,
        "success": success,
        "failed": failed,
        "force": force,
        "embedding_model": embedding_engine.model,
        "embedding_mode": embedding_engine.mode,
        "embedding_base_url": embedding_engine.base_url,
        "last_error": embedding_engine.last_error or None,
        "last_error_details": embedding_engine.last_error_details or None,
    })


# =============================================================
# /api/export-backup — manual or scheduled backup trigger
# /api/export-backup — 手动或定时触发备份
#
# Auth: dashboard session cookie OR X-Backup-Token header
#       (the latter is used by GitHub Actions via OMBRE_BACKUP_TRIGGER_TOKEN secret)
# =============================================================
@mcp.custom_route("/api/export-backup", methods=["POST"])
async def api_export_backup(request):
    """Export all buckets to JSON and push to the private backup GitHub repo."""
    from starlette.responses import JSONResponse

    # Allow either a valid dashboard session OR a matching trigger token header
    trigger_token = request.headers.get("X-Backup-Token", "")
    if trigger_token:
        if not OMBRE_BACKUP_TRIGGER_TOKEN:
            return JSONResponse({"error": "Backup trigger token not configured on server"}, status_code=403)
        if not hmac.compare_digest(trigger_token, OMBRE_BACKUP_TRIGGER_TOKEN):
            return JSONResponse({"error": "Invalid backup trigger token"}, status_code=401)
    elif not _is_authenticated(request):
        return JSONResponse(
            {"error": "Unauthorized", "setup_needed": _is_setup_needed()},
            status_code=401,
        )

    if not backup_exporter.enabled:
        return JSONResponse(
            {"error": "Backup not configured — set OMBRE_BACKUP_REPO_URL and OMBRE_BACKUP_TOKEN"},
            status_code=503,
        )

    try:
        # Ensure the daily loop is also running after first manual call
        await backup_exporter.ensure_started()
        result = await backup_exporter.run_backup()
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Backup export failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# --- Entry point / 启动入口 ---
if __name__ == "__main__":
    transport = config.get("transport", "stdio")
    logger.info(f"Ombre Brain starting | transport: {transport}")

    if transport in ("sse", "streamable-http"):
        import threading
        import uvicorn
        from starlette.middleware.cors import CORSMiddleware

        # --- Application-level keepalive: ping /health every 60s ---
        # --- 应用层保活：每 60 秒 ping 一次 /health，防止 Cloudflare Tunnel 空闲断连 ---
        async def _keepalive_loop():
            await asyncio.sleep(10)  # Wait for server to fully start
            async with httpx.AsyncClient() as client:
                while True:
                    try:
                        await client.get(f"http://localhost:{OMBRE_PORT}/health", timeout=5)
                        logger.debug("Keepalive ping OK / 保活 ping 成功")
                    except Exception as e:
                        logger.warning(f"Keepalive ping failed / 保活 ping 失败: {e}")
                    await asyncio.sleep(60)

        def _start_keepalive():
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_keepalive_loop())

        t = threading.Thread(target=_start_keepalive, daemon=True)
        t.start()

        # --- Add CORS middleware so remote clients (Cloudflare Tunnel / ngrok) can connect ---
        # --- 添加 CORS 中间件，让远程客户端（Cloudflare Tunnel / ngrok）能正常连接 ---
        if transport == "streamable-http":
            _app = mcp.streamable_http_app()
        else:
            _app = mcp.sse_app()
        # --- 机器走的门上锁(2026-10-03,mcp_guard.py 开头有来龙去脉)---
        # 不设 OMBRE_MCP_TOKEN = 不上锁,和改动前一样。先加它再加 CORS:Starlette 后加的在外层,
        # 这样 CORS 包在锁外面,401 也带 CORS 头、预检(OPTIONS)由 CORS 先接走。
        from mcp_guard import TokenGuard
        _mcp_token = os.environ.get("OMBRE_MCP_TOKEN", "").strip()
        _app.add_middleware(TokenGuard, token=_mcp_token)
        logger.info("MCP 门锁: " + ("已上锁(/mcp、钩子需带钥匙)" if _mcp_token else "未上锁(OMBRE_MCP_TOKEN 没设)"))
        _app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["*"],
        )
        logger.info("CORS middleware enabled for remote transport / 已启用 CORS 中间件")
        uvicorn.run(_app, host="0.0.0.0", port=OMBRE_PORT)
    else:
        mcp.run(transport=transport)
