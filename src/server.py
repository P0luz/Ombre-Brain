"""
========================================
server.py — MCP 服务入口 + 启动装配
========================================

启动整个 Ombre Brain 进程：加载配置、创建 BucketManager / Dehydrator /
DecayEngine / EmbeddingEngine / ImportEngine，把它们注入 tools._runtime 与
web._shared，然后以 @mcp.tool() 注册薄封装（真正的实现在 src/tools/<工具>/ 下面）。

关键行为：
- 启动后在唯一连接器 `/mcp` 上暴露 16 个基础 MCP 工具（含
  letter_write/letter_lock_update/letter_read）；每个入口
  ≤ 10 行，只负责转发。breath 拆成 breath()(0 参数)+breath_search(3 参数)+
  breath_advanced(9 参数) 三级，是因为 claude.ai 按需加载工具时会跳过参数
  复杂的工具，全塞一个 breath() 会导致它常年加载不上（见 issue #17）。
- Dashboard / HTTP 路由全部已拆分到 src/web/<域>.py（每个模块 register(mcp)），
  本文件仅在启动时调用 web.register_all(mcp) 装配；共享依赖见 web/_shared.py
- 仍保留在本文件：进程启动、引擎初始化、GitHub 后台同步循环、Webhook 推送、
  MCP Bearer 鉴权中间件、唯一连接器装配、uvicorn 拉起

不做什么（边界）：
- 不在这里写 hold/breath/dream 等业务逻辑（全在 tools/* 下）
- 不写 HTTP 路由处理（全在 web/* 下）；不写 LLM prompt（dehydrator 负责）
- 不直接读写桶文件（bucket_manager 负责）

对外暴露：唯一连接器 `mcp`，16 个基础工具（含 3.4.0 并回的信件三件套）
          + 可选的 You / Them（各按自己的持久开关动态挂载）；HTTP 路由在 src/web/*
========================================
"""

import os
import sys
import logging
import asyncio
import time
from contextlib import asynccontextmanager
from typing import Optional, Awaitable
import httpx


# --- Ensure same-directory modules can be imported ---
# --- 确保同目录下的模块能被正确导入 ---
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

from bucket_manager import BucketManager
from deletion_requests import DeletionRequestStore
from dehydrator import Dehydrator
from decay_engine import DecayEngine
from embedding_engine import EmbeddingEngine
from ombrebrain.storage.embedding_outbox import EmbeddingOutbox
from ombrebrain.storage.source_store import SourceStore
from ombrebrain.them import ThemService, ThemStore, ThemToolGate
from ombrebrain.you import YouService, YouStore, YouToolGate
from ombrebrain.security.deployment_profile import enforce_mcp_network_guard
from ombrebrain.protocol.strict_schema import harden_registered_tools
from import_memory import ImportEngine
from migrate_engine import MigrateEngine
from utils import get_version, load_config, setup_logging

# --- iter 2.1：MCP 工具实现已按代码路径拆分到 tools/ 子包 ---
# 本文件只保留 MCP 注册 + 路由（HTTP custom_route）+ 共享辅助。
# 真正的工具逻辑在 tools/breath, tools/hold, tools/grow, tools/trace,
# tools/anchor, tools/plan, tools/dream 里，便于单独阅读和修改。
from tools import _runtime as _tools_runtime
from tools import breath as _t_breath
from tools import hold as _t_hold
from tools import grow as _t_grow
from tools import trace as _t_trace
from tools import anchor as _t_anchor
from tools import plan as _t_plan
from tools import dream as _t_dream
from tools import i as _t_i
from tools import them as _t_them
from tools import you as _t_you

# --- Load config & init logging / 加载配置 & 初始化日志 ---
config = load_config()
setup_logging(config.get("log_level", "INFO"))
logger = logging.getLogger("ombre_brain")

# --- Project version (read from <repo_root>/VERSION) / 项目版本号 ---
# get_version() 汇总读文件 + fallback 逻辑。
# 赋给双下划线变量 `__version__` 是 Python 社区约定俗成的模块版本字段名。
__version__ = get_version()
logger.info(f"Ombre Brain v{__version__}")

# --- iter 1.7 §A: legacy path migration check / 老路径迁移检测 ---
# 场景：1.6 早期使用者习惯在项目根跑 `python server.py`；1.7 重组后需要
# `python src/server.py`。这里只做「检测 + 提醒」，不做任何破坏性动作。
# load_config() 里 buckets_dir 默认仍是 <repo_root>/buckets，所以老数据不会丢。
#
# Python 小知识：
#   * 变量名以 `_` 开头是「模块内部」约定，不是语法强制
#   * for/else 这里没用，用了 break 提前退出
#   * `os.path.isdir(p) and any(...)` 是短路：前者 False 就不会跳 listdir
try:
    _bd = config.get("buckets_dir", "")
    if _bd and os.path.isdir(_bd):
        _has_data = False
        # 遍历各个桶目录，任何一个里（含域子目录）有 .md 文件就认定有数据。
        # 必须递归 os.walk：桶按域存在子目录里（permanent/<域>/x.md），
        # 只 os.listdir 顶层只会看到域文件夹、永远判定为空 → 误报 "fresh install"
        # （数据其实都在，breath 也读得到，纯粹是这条日志吓人）。
        for sub in ("permanent", "dynamic", "feel", "plans", "letters"):
            p = os.path.join(_bd, sub)
            if not os.path.isdir(p):
                continue
            if any(
                f.endswith(".md") and not f.startswith(".")
                for _root, _dirs, _files in os.walk(p)
                for f in _files
            ):
                _has_data = True
                break
        if _has_data:
            logger.info(f"[migration] existing buckets detected at {_bd} — zero data loss expected.")
        else:
            logger.info(f"[migration] {_bd} is empty — fresh install assumed.")
except Exception as _e:  # pragma: no cover - defensive / 防御性兑底
    # 启动期任何检测出错都不能阻止服务拉起，记个 warning 就过
    logger.warning(f"[migration] check skipped: {_e}")

# --- Runtime env vars (port + webhook) / 运行时环境变量 ---
# OMBRE_PORT: HTTP/SSE 监听端口，默认 18001
# Docker 部署：compose 显式设 OMBRE_PORT=8000 保持容器内 8000（不动 Cloudflare ingress），
# 由 host 端口映射 18001:8000 对外暴露 18001。裸机：直接监听 18001。
# 端口优先级：env OMBRE_PORT（Docker 由 Dockerfile 固定 8000）> config.yaml host_port
# （裸机前端可改、保存即写 config）> 默认 18001。Docker 下前端改 host_port 不影响容器内
# 监听（仍 8000），由 host 映射 OMBRE_HOST_PORT 决定对外端口（部署脚本读 config 注入）。
try:
    _port_raw = os.environ.get("OMBRE_PORT") or str(config.get("host_port") or "") or "18001"
    OMBRE_PORT = int(_port_raw)
except (ValueError, TypeError):
    logger.warning("端口配置不是合法整数，回退到 18001")
    OMBRE_PORT = 18001

# Docker needs an all-interface default; bare-metal deployments can restrict it
# with OMBRE_BIND_HOST=127.0.0.1.
_BIND_HOST = (os.environ.get("OMBRE_BIND_HOST") or "0.0.0.0").strip() or "0.0.0.0"  # nosec B104

# OMBRE_HOOK_URL: 在 breath/dream 被调用后推送事件到该 URL（POST JSON）。
# OMBRE_HOOK_SKIP: 设为 true/1/yes 跳过推送。详见 ENV_VARS.md。
# _fire_webhook 每次调用直接读 os.environ（不缓存模块常量）——这样 dashboard 的
# /api/env-config 改完（它会写 os.environ）即时生效，无需再回写模块全局，
# 也让该路由能干净地迁出到 web/config_api.py。


# ============================================================
# 调参面板 / Tunable constants
# ------------------------------------------------------------
# rule.md §①：禁裸魔法数字。这里集中所有会调的阁值。
# 与安全、鉴权、性能相关的参数不要在运行时乲变；如需调整请同步跑 pytest。
# ============================================================

# --- Webhook / HTTP 客户端超时 ---
_WEBHOOK_TIMEOUT_SECONDS = 5.0

# --- Dashboard 鉴权 / 会话 / 密码 / 日志&错误面板分页常量 已移至 web/_shared.py、web/system.py ---


async def _fire_webhook(event: str, payload: dict) -> None:
    """
    Fire-and-forget POST to OMBRE_HOOK_URL with the given event payload.
    Failures are logged at WARNING level only — never propagated to the caller.
    """
    hook_url = os.environ.get("OMBRE_HOOK_URL", "").strip()
    hook_skip = os.environ.get("OMBRE_HOOK_SKIP", "").strip().lower() in ("1", "true", "yes", "on")
    if hook_skip or not hook_url:
        return
    if not hook_url.startswith(("http://", "https://")):
        logger.warning("OMBRE_HOOK_URL rejected: only http/https URLs are allowed")
        return
    try:
        body = {
            "event": event,
            "timestamp": time.time(),
            "payload": payload,
        }
        async with httpx.AsyncClient(timeout=_WEBHOOK_TIMEOUT_SECONDS) as client:
            await client.post(hook_url, json=body)
    except Exception as e:
        # Webhook credentials commonly live in the URL path/query.  Never put
        # either the configured URL or httpx's URL-bearing exception text in logs.
        logger.warning("Webhook push failed (%s): %s", event, type(e).__name__)

# --- Initialize core components / 初始化核心组件 ---
# 统一错误码体系（必须在任何业务初始化之前 configure，确保 errors.jsonl 路径生效）
try:
    from errors import (
        configure_errors_path,
        OBStartupError,
        write_fatal_log,
        record_error,
        format_error,
        begin_warnings,
        pop_warnings,
        format_warnings_suffix,
        PublicToolError,
        ToolInputError,
    )
except ImportError:
    from .errors import (  # type: ignore
        configure_errors_path,
        OBStartupError,
        write_fatal_log,
        record_error,
        format_error,
        begin_warnings,
        pop_warnings,
        format_warnings_suffix,
        PublicToolError,
        ToolInputError,
    )
configure_errors_path(config.get("buckets_dir", "buckets"))

try:
    embedding_engine = EmbeddingEngine(config)            # Embedding engine first (BucketManager depends on it)
except OBStartupError as _ob_err:
    # OB-F001 已在 OBStartupError 内格式化好；写 fatal log 后退出
    logger.error(str(_ob_err))
    write_fatal_log(_ob_err.error_code, _ob_err.detail, buckets_dir=config.get("buckets_dir"))
    raise
except RuntimeError as _emb_err:
    # 兼容尚未迁移到 OBStartupError 的旧 raise（应该不再触发）
    logger.error(f"[STARTUP FAILED] {_emb_err}")
    raise SystemExit(f"Ombre Brain 启动中止：{_emb_err}") from _emb_err
bucket_mgr = BucketManager(config, embedding_engine=embedding_engine)  # Bucket manager / 记忆桶管理器
deletion_requests = DeletionRequestStore(
    config["buckets_dir"], bucket_mgr, embedding_engine
)
_source_max_bytes = int(
    (config.get("limits") or {}).get("max_grow_input_bytes", 2 * 1024 * 1024)
)
source_store = SourceStore(
    config.get("buckets_dir", "buckets"),
    max_bytes=_source_max_bytes,
)
embedding_outbox = EmbeddingOutbox(config, bucket_mgr, embedding_engine)
bucket_mgr.attach_embedding_outbox(embedding_outbox)
dehydrator = Dehydrator(config)                      # Dehydrator / 脱水器
decay_engine = DecayEngine(config, bucket_mgr)       # Decay engine / 衰减引擎
import_engine = ImportEngine(config, bucket_mgr, dehydrator, embedding_engine)  # Import engine / 导入引擎
migrate_engine = MigrateEngine(config, bucket_mgr, embedding_engine)              # Migrate engine / 记忆包迁移引擎
you_service = YouService(
    store=YouStore(config.get("buckets_dir", "buckets")),
    bucket_mgr=bucket_mgr,
    dehydrator=dehydrator,
    source_store=source_store,
    logger=logger,
)
them_service = ThemService(
    store=ThemStore(config.get("buckets_dir", "buckets")),
    bucket_mgr=bucket_mgr,
    decay_engine=decay_engine,
    source_store=source_store,
    config=config,
    logger=logger,
)
# 认识由模型显式写入，没有后台抽取，也就没有可观察的桶变动事件。
# bucket_mgr 那套 observer 机制已在 3.5.0 删除：3.4.x 拆掉自动派生流水线时
# 只删了订阅方，发射端空转了一整个大版本，而读代码的人会以为它还能挂钩子。

# --- GitHub Sync / GitHub 同步 ---
from github_sync import GitHubSync  # type: ignore
_gh_cfg = config.get("github_sync", {}) or {}
_gh_token = (os.environ.get("OMBRE_GITHUB_TOKEN") or _gh_cfg.get("token") or "").strip()
github_sync_instance: GitHubSync | None = (
    GitHubSync(
        token=_gh_token,
        repo=_gh_cfg.get("repo", ""),
        branch=_gh_cfg.get("branch", "main"),
        path_prefix=_gh_cfg.get("path_prefix", "ombre"),
        max_source_bytes=_source_max_bytes,
    )
    if _gh_token and _gh_cfg.get("repo")
    else None
)
_github_auto_task: "asyncio.Task | None" = None  # 后台定时同步任务


async def _github_sync_loop(interval_minutes: int) -> None:
    """后台定时 GitHub 同步循环。只在 is_validated=True 后执行实际上传。"""
    import asyncio
    logger.info(f"[github_sync] auto-sync loop started, interval={interval_minutes}min")
    # 首次先做一次验证，确认连接可用
    if _wsh.github_sync_instance and not _wsh.github_sync_instance.is_validated:
        try:
            result = await _wsh.github_sync_instance.validate()
            if not result.get("ok"):
                logger.warning(f"[github_sync] auto-sync: validate failed: {result.get('error')} — loop will retry next cycle")
        except Exception as e:
            logger.warning(f"[github_sync] auto-sync: validate exception: {e}")
    while True:
        await asyncio.sleep(interval_minutes * 60)
        inst = _wsh.github_sync_instance  # 读当前全局引用（config 更新可能替换实例）
        if inst is None:
            logger.info("[github_sync] auto-sync: instance gone, stopping loop")
            return
        if not inst.is_validated:
            # 还没验证通过，先 validate
            try:
                res = await inst.validate()
                if not res.get("ok"):
                    logger.warning(f"[github_sync] auto-sync skipped (not validated): {res.get('error')}")
                    continue
            except Exception as e:
                logger.warning(f"[github_sync] auto-sync validate failed: {e}")
                continue
        buckets_dir = config.get("buckets_dir", "")
        if not buckets_dir:
            continue
        try:
            result = await inst.sync(buckets_dir)
            if result.get("ok"):
                logger.info(f"[github_sync] auto-sync ok: {result.get('uploaded', 0)} files")
            else:
                logger.warning(f"[github_sync] auto-sync failed: {result.get('error')}")
        except Exception as e:
            logger.error(f"[github_sync] auto-sync exception: {e}")


def _restart_github_auto_task(interval_minutes: int) -> None:
    """取消旧任务并按新间隔启动后台同步循环（interval_minutes=0 表示仅取消）。"""
    import asyncio
    global _github_auto_task
    if _github_auto_task and not _github_auto_task.done():
        _github_auto_task.cancel()
        _github_auto_task = None
    if interval_minutes > 0 and _wsh.github_sync_instance is not None:
        try:
            loop = asyncio.get_event_loop()
            _github_auto_task = loop.create_task(_github_sync_loop(interval_minutes))
        except RuntimeError:
            pass  # 没有运行中的 event loop（测试环境），跳过


# 启动时若配置了自动同步间隔，推迟到事件循环就绪后启动（用 lifespan 钩子）
_gh_auto_interval: int = int(_gh_cfg.get("auto_interval_minutes") or 0)


# --- Create MCP server instance / 创建 MCP 服务器实例 ---
# host="0.0.0.0" so Docker container's HTTP endpoint is externally reachable
# stdio mode ignores host (no network)
#
# 唯一连接器 /mcp 直接注册全部 16 个工具（信件三件套 3.4.0 已并回）。
# 不依赖 FastMCP 私有注册表的启动期合并，导入式 ASGI 启动也能稳定暴露
# 完整工具清单。
#
# 远程 Streamable HTTP 固定返回单个 JSON-RPC 对象，并且不要求客户端在
# initialize 后保存/回传 Mcp-Session-Id。Kelivo 等会静默吞掉 tools/list 异常的
# 客户端因此不会再出现“已连接但 0 工具”。stdio 不受这两项影响。

_stdio_runtime_lifecycle = None


@asynccontextmanager
async def _stdio_lifespan(_server):
    lifecycle = _stdio_runtime_lifecycle
    if lifecycle is None:
        yield {}
        return

    await lifecycle.start()
    try:
        yield {}
    finally:
        await lifecycle.stop()


mcp = FastMCP(
    "Ombre Brain",
    host=_BIND_HOST,
    port=OMBRE_PORT,
    json_response=True,
    stateless_http=True,
    lifespan=_stdio_lifespan if config.get("transport", "stdio") == "stdio" else None,
)

# 3.4.0：信件并回主链路，`/mcp-extra` 再次退役。
#
# 3.2.0 把信件拆出去的理由是「没人在大脑里写信」——信有收件人、有时间锁，
# 时间方向和记忆相反，怕模型在该回忆的时候去翻信。理由本身没错，但拆连接器
# 解决不了它：**能不能连上**和**该不该在这时候用**是两件事。真正管住后者的是
# 工具描述，而代价是实打实的——两个 FastMCP 实例要各自维护生命周期、严格
# 参数校验、体积限制和鉴权三处边界（3.2.0 的发布说明里就列了这三处），任何
# 一处漏跟就是一个静默旁路，而 `letter_write` 是能创建记忆的写工具。
#
# 一个连接器一套边界。信件回到 `@mcp.tool()`，主连接器 16 个工具。


# =============================================================
# Dashboard Auth —— 已拆分：会话/密码/鉴权 helper 在 web/_shared.py，
# /auth/* 路由在 web/auth.py。这里注入 config，并把 helper 名字 import 回本模块，
# 让本文件其余尚未迁移的 @mcp.custom_route 路由（大量调用 _require_auth）继续可用；
# 待这些路由也迁出 web/ 后，本段 import 可删除。
# =============================================================
import web as _web
import web._shared as _wsh

# 注册 OAuth 路由和 MCP 中间件之前统一评估真实网络边界，供启动日志与
# Dashboard 诊断使用。风险评估不得覆盖明确的 mcp_require_auth 配置。
_mcp_network_security = enforce_mcp_network_guard(
    config,
    environment=os.environ,
    in_docker=_wsh.in_docker(),
)
if _mcp_network_security["guard_active"]:
    logger.error(
        "=" * 60 + "\n"
        "🛡️  MCP 安全门禁已启用：检测到非回环或无法确认边界的免鉴权配置。\n"
        "    当前进程已在内存中强制开启 MCP 鉴权，config.yaml 原值未被改写。\n"
        "    原因：%s\n"
        "    请改用 OAuth/静态 Token，或把服务明确限制到本机回环地址。\n"
        + "=" * 60,
        _mcp_network_security["reason"],
    )
elif _mcp_network_security["override_active"]:
    logger.critical(
        "=" * 60 + "\n"
        "⚠️  已显式允许非回环免鉴权 MCP：任何能访问该端口的人都可读写记忆。\n"
        "    原因：%s\n"
        "    不再需要时请立即删除 OMBRE_ALLOW_INSECURE_MCP。\n"
        + "=" * 60,
        _mcp_network_security["reason"],
    )
_wsh.init(config)
# 记忆持久性自检：容器里记忆目录若没挂持久卷，重建就全丢。开机就醒目告警，别让用户
# 以为「存住了其实没有」。只提示不阻断（阻断会伤部署）。
try:
    _dp = _wsh.data_dir_persistence(config.get("buckets_dir", ""))
    if not _dp["persistent"]:
        logger.warning(
            "=" * 60 + "\n"
            "⚠️  记忆目录未挂载到持久卷：" + str(config.get("buckets_dir", "")) + "\n"
            "    " + _dp["note"] + "\n"
            "    （记忆比代码金贵：代码能重部署，记忆丢了找不回。请尽快修正挂载。）\n"
            + "=" * 60
        )
    else:
        logger.info(f"记忆目录持久性：{_dp['mode']} — {_dp['note']}")
except Exception as _dpe:
    logger.warning(f"数据目录持久性自检失败（不影响启动）：{_dpe}")
# 注入业务引擎/版本/仓库根目录到 web 层（类比 tools/_runtime）。
# 注意：embedding_engine 会被热重载替换 —— 待 embedding/config 路由迁到 web/ 时，
# 替换处须同时写 _wsh.embedding_engine（目前这些路由仍在本文件、仍走 global）。
_wsh.init_runtime(
    version=__version__,
    repo_root=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    bucket_mgr=bucket_mgr,
    deletion_requests=deletion_requests,
    dehydrator=dehydrator,
    decay_engine=decay_engine,
    embedding_engine=embedding_engine,
    embedding_outbox=embedding_outbox,
    import_engine=import_engine,
    migrate_engine=migrate_engine,
    github_sync_instance=github_sync_instance,
    you_service=you_service,
    them_service=them_service,
    restart_github_auto_task=_restart_github_auto_task,
)
# 启动时把磁盘上的会话装回内存（容器重启不踢登录）。鉴权/会话逻辑全在 web/_shared.py，
# server.py 自身已无 @mcp.custom_route 路由，只需启动时载入一次会话。
from web._shared import _load_sessions
_load_sessions()

# 注册所有 web/ 路由模块（HTTP 层已全部迁出，见 web/__init__.register_all）
_web.register_all(mcp)


# =============================================================
# 根仪表板 / 静态资源 / favicon / /health —— 已拆分到 web/dashboard.py
# =============================================================


# 心跳时间戳 + _mark_op 已移到 web/_shared.py；这里 import 回来供 tools._runtime 注入。
from web._shared import _mark_op  # noqa: F401  (injected into tools._runtime below)


# =============================================================
# 已退役的硬删除通知兼容钩子
# web/_shared.py 仍保留这两个注入位，以免旧扩展导入时报错。
# 当前版本不写入、不消费硬删除通知，也不抹除记忆。
# =============================================================

def _write_deletion_notice(_names: list) -> None:
    """兼容旧注入接口；物理删除能力已退役。"""
    return None


def _pop_deletion_notice() -> str:
    """兼容旧返回值；当前永远没有硬删除通知。"""
    return ""


# 这些 helper 定义在 server.py（读/写 webhook 全局等），但 web/ 的 hooks/buckets 路由要用。
# 在它们都定义好之后注入到 web._shared，供已迁出的路由通过 sh.fire_webhook 等调用。
_wsh.init_runtime(
    fire_webhook=_fire_webhook,
    write_deletion_notice=_write_deletion_notice,
    pop_deletion_notice=_pop_deletion_notice,
)


# =============================================================
# 结构化操作日志 helpers（任务A，2026-05-03）
# 给 MCP 工具入口统一打 entry/ok/err 三段日志，便于排查
# 客户端报 invalid_arguments / 静默错误等问题。
# 输出格式：op=<name> phase=entry|ok|err key=value...
# 所有可能含 PII 的字段（content / 信件正文等）只记 length，不记内容。
# =============================================================
def _fmt_log_val(v: object) -> str:
    """日志 value 的安全格式化：文本只记长度，绝不记录用户原文。"""
    if v is None:
        return "_"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        # query、署名、标题、domain/tag 乃至 bucket_id 都可能含私密内容或
        # CR/ANSI 控制字符。结构化操作日志只需要知道字段是否存在和规模，
        # 不应把文本复制到全局日志，再由另一次失败回传给别的 MCP 客户端。
        return f"str_len:{len(v)}"
    return type(v).__name__


def _fmt_log_args(args: dict) -> str:
    """把 args dict 拼成 `k1=v1 k2=v2` 串。"""
    if not args:
        return ""
    return " ".join(f"{k}={_fmt_log_val(v)}" for k, v in args.items())


def _log_op_entry(op: str, args: dict) -> None:
    logger.info(f"op={op} phase=entry " + _fmt_log_args(args))


def _log_op_ok(op: str, result: object) -> None:
    size = len(result) if isinstance(result, str) else 0
    logger.info(f"op={op} phase=ok bytes={size}")


def _safe_exception_type(exc: BaseException) -> str:
    """只保留可安全写入响应与日志的 ASCII 异常类型名。"""
    raw_type = type(exc).__name__
    safe_type = "".join(
        char
        for char in raw_type
        if char.isascii() and (char.isalnum() or char == "_")
    )[:80]
    return safe_type or "Exception"


def _log_op_err(op: str, exc: BaseException) -> None:
    # 异常正文和 traceback 可能含密钥 URL、本机路径及调用参数，服务日志
    # 只记录安全化类型；详细排障使用同一时间点附近的独立结构化事件。
    logger.error(
        "op=%s phase=err err_type=%s detail=hidden",
        op,
        _safe_exception_type(exc),
    )


def _safe_exception_detail(exc: BaseException) -> str:
    """异常对外或持久化前只保留类型与泛化说明。"""
    if isinstance(exc, PublicToolError):
        return f"{_safe_exception_type(exc)}: {exc.public_message}"
    return (
        f"{_safe_exception_type(exc)}: 工具执行失败；"
        "异常正文已隐藏，以保护密钥、本机路径与调用内容。"
    )


async def _with_notice(coro: Awaitable[str], op: str = "", args: dict | None = None) -> str:
    """所有 MCP 工具调用的包装器。

    职责（统一错误规范）：
    1. 入口：begin_warnings() 初始化本调用的 W/I channel。
    2. 出口：拼接顺序 = [删除通知] + [工具正文] + [本调用产生的 W/I 提示].
    3. 异常：捕获后 record OB-E004，响应、持久错误与日志只保留异常类型和
       泛化说明，不能复制异常正文或 traceback。
    4. 任务A：op 非空时，在 entry/ok/err 三处打结构化日志。
    5. 例外：ToolInputError 原样上抛，见下方注释。
    """
    if op:
        _log_op_entry(op, args or {})
    begin_warnings()
    try:
        result = await coro
    except ToolInputError as e:
        # 唯一放行的异常：入参不合法、且工具在任何写入之前就停了。
        #
        # 这里必须抛出去而不是转成字符串——MCP 只认异常，把它兜住就等于
        # 告诉客户端 isError=False，调用方会以为写成功了继续往下走。
        # 真机复现过：hold(feel=True, domain=...) 返回「domain 固定为 feel」
        # 却报成功，一个桶都没落库。
        #
        # 不记 OB-E004：调用方参数写错不是系统故障，记进去只会淹没真正的错误。
        # 本次累计的 W/I 提示随失败作废；删除通知不 pop，留给下一次成功调用。
        if op:
            _log_op_err(op, e)
        try:
            pop_warnings()
        except Exception:
            pass
        raise
    except Exception as e:
        if op:
            _log_op_err(op, e)
        # OB-E004：MCP 工具执行异常 —— 不静默，给 LLM 一个能看懂的字符串
        try:
            detail = _safe_exception_detail(e)
            record_error("OB-E004", detail)
            err_str = format_error(
                "OB-E004",
                detail,
                include_logs=False,
            )
        except Exception:
            # 错误格式化器本身失效时也不能退回未净化的异常原文。
            # 例如 provider 异常可能含密钥 URL、CRLF 或 ANSI 控制序列。
            try:
                fallback_detail = _safe_exception_detail(e)
            except Exception:
                fallback_detail = "Exception: 工具执行失败；异常正文已隐藏。"
            err_str = f"❌ [OB-E004] MCP 工具执行异常\n{fallback_detail}"
        # 仍把通道里已累计的提示拼上
        try:
            extras = format_warnings_suffix(pop_warnings())
        except Exception:
            extras = ""
        notice = ""
        try:
            notice = _pop_deletion_notice()
        except Exception:
            pass
        return (notice + err_str + extras) if notice else (err_str + extras)
    # 正常路径
    if op:
        _log_op_ok(op, result)
    try:
        extras = format_warnings_suffix(pop_warnings())
    except Exception:
        extras = ""
    notice = _pop_deletion_notice()
    body = (notice + result) if notice else result
    return body + extras if extras else body


# =============================================================
# /api/heartbeat、/api/logs、/api/errors/* —— 已拆分到 web/system.py
# =============================================================


# =============================================================
# /api/embedding/* —— 已拆分到 web/embedding.py
# =============================================================


# =============================================================
# /breath-hook —— 已拆分到 web/hooks.py（/dream-hook 已移除：dream 不是义务，不自动触发）
# =============================================================


# =============================================================
# Wire tools subpackage runtime context
# 把所有共享对象注入 tools._runtime，让 tools/* 子模块可以访问
# =============================================================
_tools_runtime.init(
    config=config,
    bucket_mgr=bucket_mgr,
    deletion_requests=deletion_requests,
    dehydrator=dehydrator,
    decay_engine=decay_engine,
    embedding_engine=embedding_engine,
    embedding_outbox=embedding_outbox,
    import_engine=import_engine,
    source_store=source_store,
    you_service=you_service,
    them_service=them_service,
    logger=logger,
    fire_webhook=_fire_webhook,
    mark_op=_mark_op,
)


# =============================================================
# MCP tools — thin registration wrappers
# MCP 工具 —— 仅注册，实现见 tools/<tool>/
# 每个入口都不超过 10 行，便于一眼看清参数与归属
# =============================================================
@mcp.tool()
async def breath(
    query: Optional[str] = "",
    max_tokens: Optional[int] = 0,
    domain: Optional[str] = "",
    valence: Optional[float] = -1,
    arousal: Optional[float] = -1,
    max_results: Optional[int] = 0,
    importance_min: Optional[int] = -1,
    tags: Optional[str] = "",
    catalog: Optional[bool] = False,
) -> str:
    """无参数,睁眼看看自己记得什么:返回权重最高、未解决且未标记 digested 的记忆 + 置顶核心准则。digested 从默认/被动浮现及 dream 隐藏，仍可由 breath_search(query=...) 显式找回。0 参数是刻意设计——claude.ai 按需加载工具时会跳过参数复杂的工具,拆成 0 参数才能保证每次对话自动浮现,不用手动触发。要按关键词找记忆用 breath_search(query=...);要用 catalog/tags/importance_min/valence/arousal/max_tokens 等高级模式用 breath_advanced(...)。domain=只在某个主题域里找(逗号分隔),max_results=最多返回几条——这两个 breath 也认,但 breath 的本意就是不带参数睁眼看看。"""
    return await _with_notice(
        _t_breath.dispatch(
            query=query, max_tokens=max_tokens, domain=domain,
            valence=valence, arousal=arousal, max_results=max_results,
            importance_min=importance_min, tags=tags, catalog=catalog,
        ),
        op="breath",
        args={
            "query": query, "max_tokens": max_tokens, "domain": domain,
            "valence": valence, "arousal": arousal, "max_results": max_results,
            "importance_min": importance_min, "tags": tags, "catalog": catalog,
        },
    )


# Keep the advertised schema parameter-free so claude.ai still auto-loads the
# default surfacing tool.  The callable deliberately retains the pre-2.6.8
# signature behind that schema: clients which cached the old tool definition
# may keep sending those arguments after an upgrade, and FastMCP otherwise
# silently drops every unknown field before calling a zero-argument function.
try:
    _breath_public_tool = mcp._tool_manager.get_tool("breath")
    if _breath_public_tool is None:
        raise RuntimeError("registered breath tool is missing")
    # Unknown/typoed legacy arguments must fail loudly instead of recreating
    # the original bug by degrading a targeted request into default surfacing.
    _breath_arg_model = _breath_public_tool.fn_metadata.arg_model
    _breath_arg_model.model_config["extra"] = "forbid"
    _breath_arg_model.model_rebuild(force=True)
    _breath_public_tool.parameters = {
        "properties": {},
        "title": "breathArguments",
        "type": "object",
    }
except (AttributeError, RuntimeError, TypeError, ValueError) as _breath_compat_exc:
    logger.warning(
        "breath legacy-argument compatibility adapter unavailable: %s",
        _breath_compat_exc,
    )


@mcp.tool()
async def breath_search(
    query: str,
    domain: Optional[str] = "",
    max_results: Optional[int] = 0,
    date_from: Optional[str] = "",
    date_to: Optional[str] = "",
    quotes: Optional[bool] = False,
    mode: Optional[str] = "manual",
    with_ids: Optional[bool] = False,
) -> str:
    """按关键词/语义检索记忆桶,融合关键词/BM25+语义检索,向量不可用时明确提示并退回关键词检索。命中后逐字返回桶内当前 content，不调用 LLM 摘要/改写。domain 逗号分隔,按主题域预筛。date_from/date_to 按桶的创建时间过滤，支持 YYYY-MM-DD 或 ISO 8601，同日上下界包含当天全日。max_results=返回条数上限(默认 config.surfacing.breath_max_results,fallback 20,最大 50)。需要 tags/importance_min/valence/arousal/max_tokens/catalog 等更多过滤维度用 breath_advanced(...)。quotes=True：如果你发现自己不只想知道当时发生了什么，还想知道当时到底是怎么说的，就要它——命中的桶里如果存过原话，会原样附在正文后面。默认不给，引语平时安静躺着，不占上下文也不打扰你。它给的是写入那一刻挑出来的那几句（每条记忆最多 3 句、每句 100 字），**不是原文**：OB 没有「返回全文」这个入口，没挑出来的话当时就没有留下。

    mode 说明这次检索是谁发起的，默认 "manual"。"manual"=我自己决定要查这件事，行为与以往完全一致。"automatic"=调用方（agent 框架 / hook）每轮自动召回并注入上下文，此时会额外尊重 dont_surface 与 digested 两个标记——它们的含义都是「别主动拿给我」，而每轮自动注入正是它们要安静的那个场合。核心准则、坐标系与受保护记忆不受 mode 影响，任何时候都不会被这个开关拿掉。

    with_ids=True 会在返回文本末尾追加一段 `=== ombre:result-ids ===` 加 json 块，列出本次命中的 bucket_id、条数、mode，以及被 dont_surface/digested 挡掉的条数（omitted_by_policy）。给需要按桶做后处理的调用方用，免得去解析人类可读的渲染文本。默认不追加。"""
    return await _with_notice(
        _t_breath.dispatch(
            query=query, domain=domain, max_results=max_results,
            date_from=date_from, date_to=date_to, quotes=quotes,
            mode=mode, with_ids=with_ids,
        ),
        op="breath_search",
        args={
            "query": query, "domain": domain, "max_results": max_results,
            "date_from": date_from, "date_to": date_to,
            "mode": mode, "with_ids": with_ids,
        },
    )


@mcp.tool()
async def breath_advanced(
    query: Optional[str] = "",
    max_tokens: Optional[int] = 0,
    domain: Optional[str] = "",
    valence: Optional[float] = -1,
    arousal: Optional[float] = -1,
    max_results: Optional[int] = 0,
    importance_min: Optional[int] = -1,
    tags: Optional[str] = "",
    catalog: Optional[bool] = False,
    date_from: Optional[str] = "",
    date_to: Optional[str] = "",
    quotes: Optional[bool] = False,
    mode: Optional[str] = "manual",
    with_ids: Optional[bool] = False,
) -> str:
    """breath 的完整参数版,给需要精细控制的场景用(日常用 breath()/breath_search() 就够了)。不传 query=返回权重最高的未解决记忆;传 query=融合关键词/BM25+语义检索，向量不可用时明确提示并退回关键词检索。命中后逐字返回桶内当前 content，不调用 LLM 摘要/改写；max_tokens 不足时整桶省略，绝不截断正文。catalog=True=目录模式:只返回每桶一行元数据(名称|域|重要度,0 LLM 调用,最省 token),anchor 行带 ⚓ [anchor] 冷参考标记,适合开新对话先看目录再 breath_search(query=...) 精准拉取,并遵守 domain、tags 与 max_results。date_from/date_to 按桶的创建时间过滤，支持 YYYY-MM-DD 或 ISO 8601。max_tokens=单次返回总 token 上限(默认 config.surfacing.breath_max_tokens,fallback 20000)。domain 逗号分隔,valence/arousal 0~1(-1 忽略)。max_results=返回条数上限(默认 config.surfacing.breath_max_results,fallback 20,最大 50)。importance_min>=1=跳过语义检索,按重要度降序返回最多 20 条高重要度记忆。tags 逗号分隔,AND 过滤;tags=\"feel\" 或 \"__feel__\" 等价于 domain=\"feel\",返回所有 feel 类记忆。

    quotes 与 breath_search 同义：命中的桶里如果存过原话，原样附在正文后面。默认不给。老记忆写入时没有引语这一层，读它们不会报错，只是不附加任何东西。

    mode / with_ids 与 breath_search 同义，只在传了 query 走检索道时生效：mode=\"automatic\" 表示这次是调用方每轮自动召回，此时额外尊重 dont_surface 与 digested；with_ids=True 在末尾追加 `=== ombre:result-ids ===` json 块。不传 query 的浮现道本来就尊重这两个标记，不受 mode 影响。"""
    return await _with_notice(
        _t_breath.dispatch(
            query=query, max_tokens=max_tokens, domain=domain,
            valence=valence, arousal=arousal, max_results=max_results,
            importance_min=importance_min, tags=tags, catalog=catalog,
            date_from=date_from, date_to=date_to, quotes=quotes,
            mode=mode, with_ids=with_ids,
        ),
        op="breath_advanced",
        args={
            "query": query, "max_tokens": max_tokens, "domain": domain,
            "valence": valence, "arousal": arousal, "max_results": max_results,
            "importance_min": importance_min, "tags": tags, "catalog": catalog,
            "date_from": date_from, "date_to": date_to,
            "mode": mode, "with_ids": with_ids,
        },
    )


@mcp.tool()
async def hold(
    content: str,
    title: Optional[str] = "",
    tags: Optional[str] = "",
    importance: Optional[int] = 5,
    pinned: Optional[bool] = False,
    feel: Optional[bool] = False,
    source_bucket: Optional[str] = "",
    valence: Optional[float] = -1,
    arousal: Optional[float] = -1,
    why_remembered: Optional[str] = "",
    meaning: Optional[str] = "",
    media: Optional[list | str] = None,
    test_data: Optional[bool] = False,
    domain: Optional[str] = "",
    source_content: Optional[str] = "",
    source_ranges: Optional[list] = None,
    quotes: Optional[list] = None,
) -> str:
    """仅在对话中已明确决定“这段内容值得成为长期记忆”时调用；不要因普通聊天、猜测或工具名称联想而自行调用。content 逐字保存，绝不压缩。正文里凡是**别人**说的话（不是用户、也不是我自己），写成单独一行 `@名字：原话`——这样的行以后会被单独拆成一条带 speaker 的 JSON 返回，不会被读成用户说过的话；混在叙述里写「某某说……」拆不出来。用户和我自己的话照常直接写，不要加 `@`。title 可选；传入时是最终显式标题，优先于打标模型建议。domain 可选、逗号分隔；显式传入时优先于打标模型结果。系统自动补其余元数据，API 不可用时使用本地中性值继续保存。tags 逗号分隔，importance 1-10。pinned=True 标记为永久核心；feel=True 存为感受类记忆且 domain 固定为 feel。source_bucket 是正在消化的原始记忆桶 ID。source_content/source_ranges 是可选原文证据：由调用方自行决定是否提供；原文进入与 grow 共用的不可变原文层，不参与普通 breath。省略 source_ranges 时整份 source_content 默认属于当前 hold 事件；显式 ranges 使用 1-based 闭区间。why_remembered 与 meaning 是可选的第一人称记录原因。media 可传服务器可读路径或 data_base64+filename 列表项。quotes 是这一刻我决定要原样记住的那一两句话——不是记录对话，是说出口的当下就知道自己不想忘的那几句。传字符串列表，或 [{"text":"原话","speaker":"谁说的","at":"什么时候"}]。最多 3 句、每句 100 字；超了会被拒绝而不是截断，因为截断过的话已经不是原话。**默认状态是一句都不放**：3 句是上限不是配额，绝大多数记忆不需要引语，拿不准就别放。不要为了保住上下文把一段话切成几句塞进来，也不要挑「有信息量」的句子——那是在用引语存原文，而原文层正是因为这个原因被删掉的。只放那种漏掉会真的可惜的原话。它平时不出现在任何浮现里，只有以后我自己想知道当时到底怎么说的时候才拿得出来。valence/arousal 可选，0~1：valence 是情绪色调(0 消极/0.5 中性/1 积极)，arousal 是情绪强度(0 平静/1 激动)。不传由系统分析；只在你觉得系统会判错时才自己给。"""
    return await _with_notice(
        _t_hold.dispatch(
            content=content, title=title, tags=tags, importance=importance,
            pinned=pinned, feel=feel, source_bucket=source_bucket,
            valence=valence, arousal=arousal, why_remembered=why_remembered,
            meaning=meaning, media=media, test_data=test_data, domain=domain,
            source_content=source_content, source_ranges=source_ranges,
            quotes=quotes,
        ),
        op="hold",
        args={
            "content_len": len(content or ""), "title_len": len(title or ""), "tags": tags,
            "importance": importance, "pinned": pinned, "feel": feel,
            "source_bucket": source_bucket, "valence": valence, "arousal": arousal,
            "why_len": len(why_remembered or ""), "meaning_len": len(meaning or ""),
            "media_count": len(media or []),
            "test_data": bool(test_data),
            "domain": domain,
            "source_content_len": len(source_content or ""),
            "source_ranges_count": len(source_ranges or []),
            "quotes_count": len(quotes or []),
        },
    )


@mcp.tool()
async def grow(
    content: str = "", items: Optional[list] = None, test_data: Optional[bool] = False
) -> str:
    """仅在对话中已明确要求整理并写入长期记忆时调用，不要根据普通聊天自行推断写入意图。整理一段长文本(如一天的记录/一段日记/一篇总结)存入记忆,系统拆分为 2~6 条独立事件桶并各自尝试合并。短内容(<30 字)走 hold 单条快速路径,不强行拆分。

    进阶(可选):若你已经把长文拆成 N 条最终正文，可传字符串 items，或对象 items=[{"title":"最终标题","content":"最终正文","tags":["中文短标签"],"importance":5,"domain":["恋爱"],"valence":0.8,"arousal":0.4,"why_remembered":"我为什么要留下这条","source_ranges":[[1,20]],"quotes":["当时说出口就知道要记住的那句原话"]}]。quotes 只在 items 这条路上有；content 整段交给系统拆分时不填，因为那些条目是拆出来的，不是我一条条挑的。每条最多 3 句、每句 100 字，超限拒绝不截断；**多数 item 不该有 quotes**——整理长文时尤其容易顺手把原文抄进去，那是在用引语存全文，不是在挑那句不想忘的话。显式字段优先于自动打标，正文逐字入库，合并时也不压缩。人工 why_remembered 与 digest/短内容打标生成的合法理由都会在首次新建时保存；后续合并仅补旧空值，绝不覆盖人工或历史理由。模型漏字段或返回非法理由时仍正常保存正文。同时传 content 时，content 是整批共享的隐藏原文证据，只保存一次；source_ranges 使用 1-based 闭区间把每个桶连回自己的原文片段。"""
    return await _with_notice(
        _t_grow.dispatch(content, items=items, test_data=test_data),
        op="grow",
        args={
            "content_len": len(content or ""), "items": len(items or []),
            "test_data": bool(test_data),
        },
    )


async def _decide_deletion_request(
    bucket_id: str,
    request_id: Optional[str],
    decision: Optional[str],
    ai_reason: Optional[str],
) -> str:
    """审批一条人类发起的删除请求。失败抛 ToolInputError，不返回字符串。

    抛而不是返回，是因为「没审批成功」必须在 MCP 侧表现为 isError=True——
    否则模型会当成审批通过继续往下走，而那条记忆的去留其实一个字都没变。
    """
    result = await deletion_requests.decide(
        request_id or "",
        decision or "",
        ai_reason or "",
        expected_bucket_id=bucket_id,
    )
    if not result.get("ok"):
        raise ToolInputError(
            f"删除请求审批失败：{result.get('error') or '未知原因'}"
        )
    return (
        f"删除请求 {request_id} 已{result['decision']}；桶 {result['bucket_id']}。"
    )


@mcp.tool()
async def trace(
    bucket_id: str,
    name: Optional[str] = "",
    title: Optional[str] = "",
    domain: Optional[str] = "",
    valence: Optional[float] = -1,
    arousal: Optional[float] = -1,
    importance: Optional[int] = -1,
    tags: Optional[str] = "",
    resolved: Optional[int] = -1,
    pinned: Optional[int] = -1,
    protected: Optional[int] = -1,
    digested: Optional[int] = -1,
    content: Optional[str] = "",
    delete: Optional[bool] = False,
    status: Optional[str] = "",
    weight: Optional[float] = -1,
    dont_surface: Optional[int] = -1,
    why_remembered: Optional[str] = "",
    meaning_append: Optional[str] = "",
    meaning_replace: Optional[list] = None,
    media_append: Optional[list | str] = None,
    media_replace: Optional[list | str] = None,
    hard_delete: Optional[bool] = False,
    delete_reason: Optional[str] = "",
    restore: Optional[bool] = False,
    old_str: Optional[str] = "",
    new_str: Optional[str] = None,
    deletion_request_id: Optional[str] = "",
    deletion_decision: Optional[str] = "",
    deletion_ai_reason: Optional[str] = "",
    unlink: Optional[str] = "",
    relink: Optional[str] = "",
    relation_type: Optional[str] = "",
    quotes_replace: Optional[list] = None,
    reinforce: Optional[bool] = False,
) -> str:
    """仅在明确需要修改某条已存在记忆时调用，不要猜测 bucket_id 或自行改写记忆。

    resolved=1 标记已放下；resolved=0 重新激活。pinned=1 标记永久核心并锁定
    importance=10。protected=1 保护记忆不被衰减，但不作为核心准则强制浮现；
    它与 pinned/anchor 互斥且同样锁定 importance=10。解除最后一层
    pinned/protected 保护时，必须在同一次调用显式传入 importance=1..10。
    name 改的是桶名（进文件名、做显示回退）；title 改的是这条记忆自己的标题，
    **信件的标题就存在 title 里**。两个是不同字段，改一个不会动另一个——
    想改信件标题请用 title，用 name 改不到它。
    digested=1 标记已消化并从默认/被动浮现及 dream 隐藏（对 pinned/permanent/anchor 桶不生效——核心准则与坐标系始终在场，要让某条安静请改用 trace(bucket_id, pinned=0)），
    但仍可通过显式 query、importance 审计或目录找回。content 会完整替换正文；
    old_str/new_str 会在完整原文中做唯一、逐字的局部替换（new_str 可为空以删除），
    两种方式都会重建 embedding，且不能同时使用。status/weight 用于 plan；dont_surface 控制日常浮现；
    why_remembered、meaning_append/replace、media_append/replace 更新相应元数据。

    删除边界：delete=True 只会把 Markdown 移入 archive 并标记 deleted_at，不会
    物理抹除。hard_delete=True 仅用于清理创建时明确标记 test_data=True 的测试桶，
    必须单独提供非空 delete_reason；普通记忆和 plan 一律拒绝且不会顺带归档。
    delete 与 hard_delete 不能同时使用。归档记忆只有在反思后决定值得再次回忆时，才单独调用
    trace(bucket_id="...", restore=True) 恢复；若历史归档同时带有 protected/anchor，
    只能用 restore=True、protected=0、importance=1..10 原子解除冲突后恢复。
    检索命中不会自动恢复。只传需要修改的字段，-1 或空串表示不改。

    关系修正：桶间关系由后端在写入时自动建立，模型不需要也无法主动建立它们；
    但发现连错了可以在这里改。unlink="目标id" 双向断开这一对的关联；
    relink="目标id" 配合 relation_type=（caused_by / causes / continuation_of /
    continues / related_to / same_event）把已存在关系改成正确的类型，对侧自动
    取反向类型。改过的关系会被标记为手动关系，此后不再被自动推断改写或挤掉。
    relink 不能凭空建立关系——两条记忆之间没有已存在的关系时会被拒绝。
    这两个参数与其他字段更新互斥，请单独调用。

    引语订正：quotes_replace 整体替换这条记忆的引语，用来订正和删除写入那一刻
    留下的原话。传 [] 删掉全部；只想去掉其中一句，就把要保留的那几句原样传回来。
    格式同 hold(quotes=...)：字符串列表，或 [{"text":"原话","speaker":"谁说的",
    "at":"什么时候"}]。**只能改和删，不能补录**——这条记忆本来没有引语会被拒绝，
    条数也只能持平或减少。「当时说出口就知道不想忘」是写入那一刻的判断；事后
    追认一句话「当时就知道它重要」，那不是引语，是摘要。同样与其他字段更新互斥。

    强化：reinforce=True 刷新这条记忆的活跃时间并累加 activation_count，让它在
    之后的浮现里排得更靠前。**检索本身不再做这件事**——breath_search 命中一条
    不代表它要紧，只代表我在找它；为了核对、debug、反复确认而读的记忆，读多了
    权重就会爬到最高，那不是记忆变重要，是我查得勤。所以强化改成读完之后针对
    **那一条**显式确认：这条确实要紧。整批候选不要一起强化，命中里绝大多数只是
    路过。与其他字段更新互斥，请单独调用。

    元数据字段：domain=主题域(逗号分隔)，tags=标签(逗号分隔)，
    valence/arousal=情绪色调与强度(0~1)。meaning_replace / media_replace 是整段替换，
    对应的 meaning_append / media_append 是追加——替换会丢掉原有内容，拿不准用 append。

    审批删除请求：deletion_request_id=人类发起的那条请求的 ID，deletion_decision=
    "approve" 或 "reject"，deletion_ai_reason=你做这个决定的理由（可选）。
    这是删除某条记忆前征求你意见的入口——人类提请求，你决定。与其他字段更新互斥，
    请单独调用；审批不成立时会报错，不会静默当成通过。
    """
    if deletion_request_id or deletion_decision:
        # 这条分支原先直接 return，绕过了 _with_notice 的全部五条职责，
        # 其中最要紧的是失败没有变成 isError。
        #
        # 仓库自己写过为什么不行（test_mcp_tools_docker_integration.py）：
        # 「用返回字符串表达失败，在客户端是一次正常返回，调用方（通常是模型
        #  自己）会以为成功了继续往下走」。trace 的常规路径由
        # test_trace_failure_is_error.py 守着这条规矩，而这半边一直没人守。
        return await _with_notice(
            _decide_deletion_request(
                bucket_id, deletion_request_id, deletion_decision, deletion_ai_reason
            ),
            op="trace",
            args={
                "bucket_id": bucket_id,
                "deletion_request_id": deletion_request_id,
                "deletion_decision": deletion_decision,
            },
        )
    return await _with_notice(
        _t_trace.dispatch(
            bucket_id=bucket_id, name=name, title=title, domain=domain,
            valence=valence, arousal=arousal, importance=importance,
            tags=tags, resolved=resolved, pinned=pinned,
            protected=protected, digested=digested,
            content=content, delete=delete, status=status, weight=weight,
            dont_surface=dont_surface, why_remembered=why_remembered,
            meaning_append=meaning_append, meaning_replace=meaning_replace,
            media_append=media_append, media_replace=media_replace,
            hard_delete=hard_delete, delete_reason=delete_reason,
            restore=restore,
            old_str=old_str, new_str=new_str,
            unlink=unlink, relink=relink, relation_type=relation_type,
            quotes_replace=quotes_replace,
            reinforce=reinforce,
        ),
        op="trace",
        args={
            "bucket_id": bucket_id, "name": name, "title": title, "domain": domain,
            "valence": valence, "arousal": arousal, "importance": importance,
            "tags": tags, "resolved": resolved, "pinned": pinned,
            "protected": protected, "digested": digested,
            "content_len": len(content or ""), "delete": delete, "status": status,
            "hard_delete": hard_delete,
            "restore": restore,
            "delete_reason_len": len(str(delete_reason or "")),
            "old_str_len": len(str(old_str or "")),
            "new_str_len": len(str(new_str or "")) if new_str is not None else 0,
            "weight": weight, "dont_surface": dont_surface,
            "why_len": len(why_remembered or ""),
            "meaning_append_len": len(meaning_append or ""),
            "meaning_replace_count": len(meaning_replace or []),
            "media_append_count": len(media_append or []),
            "media_replace_count": len(media_replace or []),
            "unlink": unlink, "relink": relink, "relation_type": relation_type,
            "quotes_replace_count": (
                len(quotes_replace) if quotes_replace is not None else -1
            ),
            "reinforce": bool(reinforce),
        },
    )


# Reject misspelled/unknown trace arguments instead of letting Pydantic's
# default extra=ignore silently degrade an intended edit into a bucket-id-only
# no-op.  This is especially important for old_str/new_str patch calls.
try:
    _trace_public_tool = mcp._tool_manager.get_tool("trace")
    if _trace_public_tool is None:
        raise RuntimeError("registered trace tool is missing")
    _trace_arg_model = _trace_public_tool.fn_metadata.arg_model
    _trace_arg_model.model_config["extra"] = "forbid"
    _trace_arg_model.model_rebuild(force=True)
    # FastMCP caches the public input schema when the tool is registered.
    # Keep that cache in sync so clients can discover that unknown arguments
    # are rejected instead of learning only after a failed invocation.
    _trace_public_tool.parameters = _trace_arg_model.model_json_schema()
except (AttributeError, RuntimeError, TypeError, ValueError) as _trace_schema_exc:
    logger.warning(
        "trace strict-argument adapter unavailable: %s",
        _trace_schema_exc,
    )


@mcp.tool()
async def dream(
    window_hours: Optional[int] = 48,
) -> str:
    """读取最近 window_hours（默认 48h）内有变动的所有记忆桶,用于回顾与消化。
    每个桶返回其在窗口内的最新内容（按 last_active 取）,完整正文不截断。
    可据此操作：放下的 → trace(resolved=1) 沉底；有沉淀的 → hold(feel=True, source_bucket=...) 记录；无沉淀则不操作。
    候选桶超过 40 时按 decay_engine.calculate_score() 排序取前 40，避免一次返回过多。"""
    return await _with_notice(
        _t_dream.dispatch(
            window_hours=window_hours,
        ),
        op="dream",
        args={
            "window_hours": window_hours,
        },
    )


@mcp.tool()
async def anchor(bucket_id: str) -> str:
    """把指定桶标记为 anchor(坐标系)。anchor 不主动出现在默认 breath，但 query/domain/emotion 命中时仍返回。硬上限 24，已满时拒绝并提示先 release。"""
    return await _with_notice(
        _t_anchor.anchor_set(bucket_id),
        op="anchor",
        args={"bucket_id": bucket_id},
    )


@mcp.tool()
async def release(bucket_id: str) -> str:
    """解除指定桶的 anchor 标记。桶恢复为普通状态，重新参与默认 breath；pinned 状态保留。"""
    return await _with_notice(
        _t_anchor.anchor_release(bucket_id),
        op="release",
        args={"bucket_id": bucket_id},
    )


@mcp.tool()
async def pulse(include_archive: Optional[bool] = False) -> str:
    """返回记忆系统状态摘要:固化/动态/归档/feel/plan/letter 数量、总占用、衰减引擎运行状态,以及所有桶的摘要列表；anchor 行带独立 ⚓ [anchor] 冷参考标记。include_archive=True 同时返回归档区。"""
    return await _with_notice(
        _t_anchor.pulse(include_archive=include_archive),
        op="pulse",
        args={"include_archive": include_archive},
    )


@mcp.tool()
async def plan(
    content: str,
    status: Optional[str] = "active",
    related_bucket: Optional[str] = "",
    weight: Optional[float] = 0.5,
    why_remembered: Optional[str] = "",
) -> str:
    """登记一个待办/承诺/未闭环事项。status=active(默认)/resolved/abandoned。related_bucket 可选,关联到某个普通记忆桶。weight=承诺重量 0.0-1.0(默认 0.5),与 importance 区分——importance 表示「多重要」、weight 表示「多重」。why_remembered=登记原因(可选、仅展示)。plan 不衰减、不出现在普通 breath,仅在 dream 末尾的 active 段返回;后续 hold/grow 写入新事件时系统只会提示可能已完成,实际关闭必须显式调用 trace(status="resolved")。"""
    return await _with_notice(
        _t_plan.plan_create(
            content=content, status=status, related_bucket=related_bucket,
            weight=weight, why_remembered=why_remembered,
        ),
        op="plan",
        args={
            "content_len": len(content or ""), "status": status,
            "related_bucket": related_bucket, "weight": weight,
            "why_len": len(why_remembered or ""),
        },
    )


@mcp.tool()
async def letter_write(
    author: str,
    content: str,
    user_name: Optional[str] = "",
    title: Optional[str] = "",
    date: Optional[str] = "",
    ai_name: Optional[str] = "",
    lock_type: Optional[str] = "none",
    unlock_date: Optional[str] = "",
) -> str:
    """写下一封信。**想留给对方、或留给以后的自己的话,写在这里**,不要写成普通记忆——
信件原文永久保存,不压缩、不合并、不衰减。

author 必填:\"user\"=用户一方写的,\"ai\"(或等于 ai_name)=AI 一方写的,也可直接传任意署名字符串;
user_name 可选;ai_name 可选(默认取环境变量 AI_NAME,回退 \"AI\");title/date 可选。

**锁**(要「过一段时间才能打开」时才用,默认不锁):
  lock_type=\"none\"       不锁,写完双方都能读(默认)
  lock_type=\"timed\"      到期才能打开,**必须同时给 unlock_date**,且必须是未来
                           unlock_date 写日期(2027-01-01)或完整时刻(2027-01-01T09:00:00+08:00)
  lock_type=\"permanent\"  永久封存,谁都读不到正文
锁只有写信的这一方能改(letter_lock_update),另一方连正文都看不到。
**想留着以后再上锁,现在就得给 ai_name**(或设好环境变量 AI_NAME):事后上锁要用到
写信时记下的实际关系名,当时没记下来,`letter_lock_update` 就再也锁不上这封了。

普通 breath 不返回信件;SessionStart 钩子会带上双方各最新一封。"""
    return await _with_notice(
        _t_plan.letter_write(
            author=author, content=content, user_name=user_name,
            title=title, date=date, ai_name=ai_name,
            lock_type=lock_type, unlock_date=unlock_date,
        ),
        op="letter_write",
        args={
            "author": author, "content_len": len(content or ""),
            "user_name": user_name, "title": title, "date": date,
            "ai_name": ai_name, "lock_type": lock_type,
            "unlock_date": unlock_date,
        },
    )


@mcp.tool()
async def letter_lock_update(
    letter_id: str,
    lock_type: str,
    unlock_date: Optional[str] = "",
) -> str:
    """改一封已有信件的锁。**只动锁,不动标题、正文、署名和创建时间。**

letter_id 从 letter_read 的返回里取——每封信开头方括号里那串就是(如 [a0102c0f44e2])。

lock_type=\"none\" 解锁 / \"timed\" 到期打开(必须同时给未来的 unlock_date,
写日期 2027-01-01 或完整时刻 2027-01-01T09:00:00+08:00) / \"permanent\" 永久封存。

**只有写这封信的一方能改自己的锁**:你改不了用户写的那封,用户也改不了你的。"""
    return await _with_notice(
        _t_plan.letter_lock_update(
            letter_id=letter_id,
            lock_type=lock_type,
            unlock_date=unlock_date,
            caller_side="ai",
        ),
        op="letter_lock_update",
        args={
            "letter_id": letter_id,
            "lock_type": lock_type,
            "unlock_date": unlock_date,
        },
    )


@mcp.tool()
async def letter_read(
    query: Optional[str] = "",
    limit: Optional[int] = 10,
    author: Optional[str] = "",
    date_from: Optional[str] = "",
    date_to: Optional[str] = "",
) -> str:
    """检索历史信件。query=语义检索(可选);author 按署名过滤(\"user\"=用户侧,\"ai\"=AI 侧,也可传具体署名字符串);date_from/date_to=ISO 日期范围(可选)。无 query 时按时间倒序返回最近 limit 封。返回完整原文,不压缩。"""
    return await _with_notice(
        _t_plan.letter_read(
            query=query, limit=limit, author=author,
            date_from=date_from, date_to=date_to,
        ),
        op="letter_read",
        args={
            "query": query, "limit": limit, "author": author,
            "date_from": date_from, "date_to": date_to,
        },
    )


@mcp.tool()
async def feel(
    query: str,
    max_tokens: Optional[int] = 0,
) -> str:
    """按关键词找回我以前留下的感受。query 必填——feel 不是列表，是「我此刻在想的这件事，我以前怎么感受的」，先说在想什么才知道该翻哪一段。关键词走向量检索（候选限定在 feel 桶内，相似度 >= 0.65 才算命中），同一件事换个说法也能找回；向量不可用时退回关键词字面匹配并明确提示降级。命中后逐字返回完整正文，不截断、不摘要、不调 LLM；不返回未命中的 feel，也不用低相关的凑数。写入感受仍用 hold(content=..., feel=True, source_bucket=...)。max_tokens=返回正文的 token 上限,默认够用,只在一次要看很多条时才调大。"""
    return await _with_notice(
        _t_breath.surface_feels(query=query, max_tokens=max_tokens or 10000),
        op="feel",
        args={"query": query, "max_tokens": max_tokens},
    )


@mcp.tool()
async def I(
    content: Optional[str] = "",
    aspect: Optional[str] = "",
    read: Optional[bool] = False,
    limit: Optional[int] = 20,
    promote: Optional[str] = "",
    supersedes: Optional[str] = "",
) -> str:
    """写下或读取自我认知。I 是沉淀物不是日记：content=一个「我觉得……」，先落成一条普通记忆（候选），会浮现也会衰减，每次 dream 都跟相关记忆摆在一起碰撞。aspect=维度:nature(本质)/values(看重的)/patterns(规律)/limits(局限)/becoming(变化方向)/uncertainty(不确定的)/stance(立场)(可选)。read=True 或全空=读正式条目+待沉淀候选。limit=返回条数上限(默认 20)。promote=候选桶ID，被 3 次不同日期的 dream 见证后才能升级成正式条目（可同时传 content 用提炼后的措辞）。supersedes=正式I条目ID，表示这条新认识要取代它：旧条目立刻不再作为当前信念读出去（一个字不删，随时可查，质疑撤了它就回来），而新的仍要照常攒够见证；只能在同一 aspect 内取代。正式条目不参与普通 breath/dream，SessionStart 时自动附最近 3 条。"""
    return await _with_notice(
        _t_i.dispatch(
            content=content, aspect=aspect, read=read, limit=limit,
            promote=promote, supersedes=supersedes,
        ),
        op="I",
        args={
            "content_len": len(content or ""), "aspect": aspect, "read": read,
            "limit": limit, "promote": promote, "supersedes": supersedes,
        },
    )


# Pydantic 默认的 ``extra=ignore`` 会让拼错的 MCP 参数看似调用成功；
# 写工具甚至会在未应用客户端目标字段时仍创建记忆。breath 和 trace
# 已有严格适配层，其余公开工具使用相同边界，并同步 FastMCP
# 的发现 schema 缓存与运行时校验器。
def _forbid_unknown_tool_arguments(tool_name: str) -> None:
    """拒绝未知参数，并同步 FastMCP 的发现 schema。

    3.4.0 信件并回主连接器后只剩一个实例，这里不再需要「工具挂在哪」这个参数。
    3.2.0 拆连接器时它是必需的，也正是那次拆分要付的代价之一：边界得跟着工具走，
    漏跟一处，那个端点就成了参数拼错也照样"成功"的旁路。
    """
    public_tool = mcp._tool_manager.get_tool(tool_name)
    if public_tool is None:
        raise RuntimeError(f"registered {tool_name} tool is missing")
    arg_model = public_tool.fn_metadata.arg_model
    arg_model.model_config["extra"] = "forbid"
    arg_model.model_rebuild(force=True)
    public_tool.parameters = arg_model.model_json_schema()


for _strict_tool_name in (
    "breath_search",
    "breath_advanced",
    "hold",
    "grow",
    "dream",
    "anchor",
    "release",
    "pulse",
    "plan",
    "letter_write",
    "letter_lock_update",
    "letter_read",
    "feel",
    "I",
):
    try:
        _forbid_unknown_tool_arguments(_strict_tool_name)
    except (AttributeError, RuntimeError, TypeError, ValueError) as _schema_exc:
        logger.warning(
            "%s strict-argument adapter unavailable: %s",
            _strict_tool_name,
            _schema_exc,
        )


# 每个属性补上 type：Gemini 的 functionDeclaration 要求 Schema.type 必填，而
# `Optional[X]` 生成的 anyOf 那一层没有它，一个不合格就整批工具被拒。只改对外
# 声明，运行时校验器不动——老客户端照样传得进 null。详见 strict_schema 的模块
# 文档串。必须排在上面那个 strict 循环之后：它会用 model_json_schema() 整个重
# 生成 parameters，放前面会被冲掉。
try:
    _hardened = harden_registered_tools(mcp)
    logger.debug("advertised schema hardened for %d tools", _hardened)
except Exception as _harden_exc:  # noqa: BLE001 - 压不平也要能起服务
    logger.warning("advertised schema hardening unavailable: %s", _harden_exc)


# You 与 Them 是仅有的两个动态工具：各自按持久开关在唯一连接器 /mcp 上
# 挂载或摘除。基础工具固定 16 个（含 3.4.0 并回的信件三件套），只开一个是
# 17，两个都开是 18。
#
# 关掉时必须**完全消失**而不是留一个返回「已关闭」的壳——留着的话，
# 模块开没开就变成了模型能看见的信息。
you_tool_gate = YouToolGate(mcp, _t_you.dispatch)
try:
    you_tool_gate.sync(you_service.status().enabled)
except Exception as _you_gate_exc:
    logger.error("You MCP gate failed closed: %s", type(_you_gate_exc).__name__)
    _you_state = you_service.status()
    if _you_state.enabled:
        try:
            you_service.set_enabled(
                False,
                expected_revision=_you_state.state_revision,
            )
        except Exception:
            logger.error("You persistent fail-closed update failed", exc_info=True)
    try:
        you_tool_gate.sync(False)
    except Exception:
        pass
them_tool_gate = ThemToolGate(mcp, _t_them.dispatch)
try:
    them_tool_gate.sync(them_service.status().enabled)
except Exception as _them_gate_exc:
    logger.error("them MCP gate failed closed: %s", type(_them_gate_exc).__name__)
    _them_state = them_service.status()
    if _them_state.enabled:
        try:
            them_service.set_enabled(False, expected_revision=_them_state.state_revision)
        except Exception:
            logger.error("them persistent fail-closed update failed", exc_info=True)
    try:
        them_tool_gate.sync(False)
    except Exception:
        pass
_wsh.init_runtime(you_tool_gate=you_tool_gate, them_tool_gate=them_tool_gate)
migrate_engine.attach_you_runtime(you_service, you_tool_gate)
migrate_engine.attach_them_runtime(them_service, them_tool_gate)


# =============================================================
# Dashboard API 端点（供轻量 Web UI 使用）
# 仪表板 API（轻量 Web UI 用）
# =============================================================
# =============================================================
# /api/buckets、/api/bucket/*、/api/settings/*、/api/anchors、/api/self
# —— 已拆分到 web/buckets.py
# =============================================================


# =============================================================
# /dashboard、/api/env-vars、/api/config、/api/test/*、/api/models、/api/env-config
# —— 已拆分到 web/config_api.py
# =============================================================




# =============================================================
# /api/host-vault、/api/import/*、/api/bucket/{id}/edit、/api/export、/api/migrate/*
# —— 已拆分到 web/import_api.py
# =============================================================


# =============================================================
# /api/version、/api/update-info、/api/do-update、/api/author、
# /api/onboarding/status、/api/status —— 已拆分到 web/meta.py
# =============================================================


# ============================================================
# OAuth 2.0 — MCP Remote Auth —— 已拆分到 web/oauth.py（路由在其 register 内注册）。
# 这里把启动期 MCP 鉴权中间件要用的两个校验函数 import 回来；hybrid 会同时注入。
# ============================================================
from web.oauth import _is_valid_mcp_token, _is_valid_static_mcp_token  # noqa: F401


# ============================================================
# Cloudflare Tunnel 管理 —— 已拆分到 web/tunnel.py（路由在其 register 内注册）。
# 这里把启动/关停 lifespan 要用的 helper import 回来。
# ============================================================
from web.tunnel import _load_tunnel_config, _start_tunnel, _stop_tunnel  # noqa: F401


# --- Entry point / 启动入口 ---
if __name__ == "__main__":
    transport = config.get("transport", "stdio")
    logger.info(f"Ombre Brain starting | transport: {transport}")

    from server_app import (
        HTTPRuntimeSettings,
        RuntimeLifecycle,
        build_http_app,
    )

    if transport == "streamable-http":
        import uvicorn
        from web import ollama_local as _ollama_local

        _http_settings = HTTPRuntimeSettings.from_config(config)
        _runtime_lifecycle = RuntimeLifecycle(
            logger=logger,
            decay_engine=decay_engine,
            embedding_outbox=embedding_outbox,
            you_service=you_service,
            ensure_ollama_child=_ollama_local.ensure_child_on_boot,
            stop_ollama_child=_ollama_local.stop_child,
            load_tunnel_config=_load_tunnel_config,
            start_tunnel=_start_tunnel,
            stop_tunnel=_stop_tunnel,
            restart_github_auto_task=_restart_github_auto_task,
            github_auto_interval=_gh_auto_interval,
            boot_marker_path=os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                ".boot_fails",
            ),
            # Explicit IPv4 avoids localhost resolving to ::1 in Proot/Termux.
            keepalive_url=f"http://127.0.0.1:{OMBRE_PORT}/health",
        )
        _mcp_token_validator = (
            _is_valid_static_mcp_token
            if _http_settings.auth_mode == "token"
            else _is_valid_mcp_token
        )
        _mcp_static_token_validator = (
            _is_valid_static_mcp_token
            if _http_settings.auth_mode == "hybrid"
            else None
        )
        _app = build_http_app(
            mcp,
            transport,
            settings=_http_settings,
            token_validator=_mcp_token_validator,
            lifecycle=_runtime_lifecycle,
            static_token_validator=_mcp_static_token_validator,
        )
        if transport == "streamable-http":
            logger.info(
                "MCP /mcp：16 个基础工具（单连接器），You / Them 各按独立开关动态显隐"
            )
        logger.info("CORS middleware enabled for remote transport / 已启用 CORS 中间件")
        logger.info(
            "MCP request body limit: %s",
            "disabled"
            if _http_settings.max_request_bytes == 0
            else f"{_http_settings.max_request_bytes} bytes",
        )

        _mcp_auth_required = _http_settings.auth_required
        if _mcp_auth_required and _http_settings.auth_mode == "token":
            logger.info(
                "MCP 静态 Token 鉴权已启用（OAuth 端点已关闭）/ "
                "MCP static-token auth enabled (OAuth endpoints disabled)"
            )
            logger.warning(
                "=" * 60 + "\n"
                "⚠️  MCP 静态 Token 等同万能密钥：拿到它的人能读写你的全部记忆。\n"
                "    该模式与 OAuth 互斥，本进程不再提供 OAuth 授权流程；请勿把本服务\n"
                "    直接暴露到公网，仅在可信内网或自带鉴权的隧道场景使用，并妥善保管、\n"
                "    定期轮换该 Token。\n"
                + "=" * 60
            )
        elif _mcp_auth_required and _http_settings.auth_mode == "hybrid":
            logger.info("MCP OAuth + 静态 Token 共存鉴权已启用")
            logger.warning(
                "=" * 60 + "\n"
                "⚠️  共存模式保留 OAuth，同时接受预置静态 Token；静态 Token 等同万能密钥。\n"
                "    请仅向受信任客户端分发并定期轮换，不要提交到仓库或截图分享。\n"
                + "=" * 60
            )
        elif _mcp_auth_required:
            logger.info("MCP OAuth middleware enabled / MCP OAuth 中间件已启用")
        else:
            # 安全加固 #7：关掉鉴权 = 两个 MCP 端点全裸奔，任何能连到端口的人都能读写全部记忆。
            # 从 info 升级为显著 WARNING，避免用户无意识地把大脑暴露到公网。
            logger.warning(
                "=" * 60 + "\n"
                "⚠️  MCP 认证已关闭 (mcp_require_auth: false)：/mcp 无需任何令牌即可直连，\n"
                "    16 个基础工具及当前已启用的可选工具均对外开放——任何能访问本端口的人都能读写你的全部记忆。\n"
                f"    本服务进程监听 {_BIND_HOST}，若端口暴露到局域网/公网，请务必用反代鉴权、防火墙\n"
                "    或仅绑定 127.0.0.1 保护；免鉴权只建议用于已确认的本机回环连接。\n"
                + "=" * 60
            )
        # 端口口径澄清（用户反馈：Docker 与裸机端口容易混淆）。容器内固定监听 8000，
        # 对外端口由 host 映射（如 18001:8000）决定，改 host_port 不影响容器内监听；
        # 裸机则直接监听本端口（默认 18001）。
        if _wsh.in_docker():
            logger.info(
                f"Listening on :{OMBRE_PORT} INSIDE the container. "
                f"外部访问端口由 host 映射决定（compose 里的 18001:{OMBRE_PORT}），"
                f"改前端 host_port 不影响容器内监听。"
            )
        else:
            logger.info(f"Listening on :{OMBRE_PORT} (bare-metal / 裸机默认 18001)")
        # 明确打印「客户端该怎么连」——给 Operit / 安卓 / 自建前端等非技术用户排障用。
        # 一眼能看清 endpoint 路径、鉴权开关；本机桥接务必用 127.0.0.1（见上方保活注释）。
        _endpoint_path = "/mcp"
        logger.info(
            "MCP endpoint ready | transport=%s | 本机连接 URL: http://127.0.0.1:%s%s "
            "（远程走你的域名/隧道，末尾同样是 %s）| 鉴权: %s",
            transport,
            OMBRE_PORT,
            _endpoint_path,
            _endpoint_path,
            (
                "开启(需静态 Token)" if _http_settings.auth_mode == "token"
                else (
                    "开启(OAuth 或静态 Token)"
                    if _http_settings.auth_mode == "hybrid"
                    else "开启(需 OAuth Bearer)"
                )
            ) if _mcp_auth_required
            else "关闭(免 token 直连，仅限本机回环/显式高风险豁免)",
        )
        # Forwarded headers are validated inside the application against
        # OMBRE_TRUSTED_PROXY_CIDRS.  Uvicorn's default proxy middleware rewrites
        # scope["client"] before our guards run, which discards the immediate
        # proxy address and makes that trust decision impossible.
        uvicorn.run(
            _app,
            host=_BIND_HOST,
            port=OMBRE_PORT,
            proxy_headers=False,
        )
    elif transport == "stdio":
        # stdio：唯一实例提供 16 个基础工具，You / Them 各按独立开关显隐；启动成功边界由
        # FastMCP public lifespan 触发。向量队列必须与 HTTP 一样纳入生命周期，
        # 否则正文落盘后会退回同步索引，让慢 provider 拖住工具回包。
        _stdio_runtime_lifecycle = RuntimeLifecycle(
            logger=logger,
            embedding_outbox=embedding_outbox,
            you_service=you_service,
            boot_marker_path=os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                ".boot_fails",
            ),
        )
        mcp.run(transport=transport)
    else:
        # 2026-08-09 起 legacy SSE transport 已下线。这里必须显式拒绝、不能落到
        # mcp.run(transport=transport) ——FastMCP 自带的 "sse" 字面量仍然合法，会
        # 绕过 build_http_app 里的鉴权/CORS/CSRF/限流中间件，直接起一个不受 Ombre
        # Brain 安全闸门保护的裸 SSE 服务，等于悄悄开一个没有认证的记忆读写口子。
        logger.error(
            f"不支持的 transport：{transport!r}。合法值仅 streamable-http、stdio；"
            "legacy SSE 传输已下线，请改用 streamable-http 并更新 config.yaml / "
            "OMBRE_TRANSPORT。"
        )
        raise SystemExit(1)
