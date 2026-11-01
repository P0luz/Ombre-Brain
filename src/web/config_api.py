"""
========================================
web/config_api.py — Dashboard 配置 / 环境变量 / API Key 测试 / 模型列表
========================================

- /dashboard：重定向到根
- /api/env-vars：环境变量只读概览
- /api/config (GET/POST)：运行期配置读取 / 热更新（含 embedding 热替换）
- /api/test/dehydration、/api/test/embedding：压缩 / 向量化连通性自检
- /api/models：列目标 provider 可用模型
- /api/env-config (GET/POST)：四块 env（compress/embed/webhook/password）热更新；
  embedding 改动会原子替换所有 Web/MCP/写入/迁移运行时引用。
  webhook 不再回写模块全局——_fire_webhook 每次读 os.environ。

对外暴露：register(mcp)。
========================================
"""

import copy
import math
import os
import secrets
import sys
import threading
from collections.abc import Mapping

import httpx

from starlette.requests import Request
from starlette.responses import Response

from ombrebrain.security.deployment_profile import (
    assess_mcp_network_safety,
    current_mcp_network_security,
    mcp_network_safety_issue,
    normalize_public_https_origin,
)
from ombrebrain.security.public_origin import configured_public_origin

from . import _shared as sh

try:
    from dehydrator import chat_completion_token_limit
except ImportError:  # pragma: no cover
    from ..dehydrator import chat_completion_token_limit

try:
    from utils import (  # type: ignore
        get_ai_name as _get_ai_name,
        get_owner_name as _get_owner_name,
        get_owner_count as _get_owner_count,
        get_timezone_name as _get_timezone_name,
        positive_float as _positive_float,
        parse_bool as _parse_bool,
        atomic_update_config_yaml,
        read_config_yaml,
    )
except ImportError:  # pragma: no cover
    from ..utils import (  # type: ignore
        get_ai_name as _get_ai_name,
        get_owner_name as _get_owner_name,
        get_owner_count as _get_owner_count,
        get_timezone_name as _get_timezone_name,
        positive_float as _positive_float,
        parse_bool as _parse_bool,
        atomic_update_config_yaml,
        read_config_yaml,
    )

logger = sh.logger
_MAX_PROVIDER_KEY_CHARS = 8192
_MAX_PROVIDER_URL_CHARS = 2048
_MAX_PROVIDER_FORMAT_CHARS = 64
_MAX_ENV_VALUE_CHARS = 8192


def _bounded_config_int(value, field: str, low: int, high: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer in [{low},{high}]")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            f"{field} must be an integer in [{low},{high}]"
        ) from exc
    if isinstance(value, float) and (
        not math.isfinite(value) or value != parsed
    ):
        raise ValueError(f"{field} must be an integer in [{low},{high}]")
    if not low <= parsed <= high:
        raise ValueError(f"{field} must be in [{low},{high}]")
    return parsed


def _bounded_config_float(value, field: str, low: float, high: float) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite number in [{low},{high}]")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be a finite number in [{low},{high}]") from exc
    if not math.isfinite(parsed) or not low <= parsed <= high:
        raise ValueError(f"{field} must be a finite number in [{low},{high}]")
    return parsed


def _rebuild_embedding_runtime():
    """Rebuild and publish one embedding engine to every runtime holder."""
    try:
        from embedding_engine import EmbeddingEngine  # type: ignore
    except ImportError:  # pragma: no cover
        from ..embedding_engine import EmbeddingEngine  # type: ignore

    engine = EmbeddingEngine(sh.config)
    sh.replace_embedding_engine(engine)
    return engine


def _mcp_auth_mode(config: Mapping[str, object] | object) -> str:
    """规范化一个配置快照中的 MCP 鉴权模式。"""
    raw = (
        str(config.get("mcp_auth_mode", "oauth")).strip().lower()
        if isinstance(config, Mapping)
        else "oauth"
    )
    return raw if raw in ("oauth", "token", "hybrid") else "oauth"


def _current_mcp_token() -> str:
    """Live static MCP token — env wins over config.yaml, same priority as validation."""
    return (
        os.environ.get("OMBRE_MCP_TOKEN", "").strip()
        or str(sh.config.get("mcp_token", "") or "").strip()
    )


def _mask_mcp_token(token: str) -> str | None:
    if not token:
        return None
    if len(token) <= 8:
        return "***"
    return f"{token[:4]}...{token[-4:]}"


def register(mcp) -> None:
    # 该锁只保护本次路由注册实例，不绑定 asyncio 事件循环；这样同一处理器
    # 被测试客户端从多个事件循环调用时，也能串行提交而不会触发跨循环错误。
    mcp_token_commit_lock = threading.Lock()

    # MCP 鉴权在进程启动时绑定到中间件和 OAuth 路由可见性。有效值与期望持久值
    # 必须分开，避免 Dashboard 错称启动期切换已经热生效。
    runtime_mcp_auth_required = _parse_bool(
        sh.config.get("mcp_require_auth", True), default=True
    )
    runtime_mcp_auth_mode = _mcp_auth_mode(sh.config)
    runtime_transport = str(sh.config.get("transport") or "stdio")
    # deployment.public_url 参与 OAuth resource/audience 绑定，同样是启动快照。
    # Dashboard 往返使用独立期望值；重启前发布到 sh.config 会让已绑定的 OAuth
    # 路由与 MCP 中间件看到不同配置。
    runtime_public_url = configured_public_origin(sh.config)

    def _desired_startup_state(persisted: Mapping[str, object]) -> dict[str, object]:
        persisted_deployment = persisted.get("deployment")
        has_persisted_deployment = isinstance(persisted_deployment, Mapping)
        return {
            "transport": str(persisted.get("transport") or runtime_transport)
            if "transport" in persisted
            else runtime_transport,
            "mcp_require_auth": _parse_bool(
                persisted.get("mcp_require_auth"), default=runtime_mcp_auth_required
            )
            if "mcp_require_auth" in persisted
            else runtime_mcp_auth_required,
            "mcp_auth_mode": _mcp_auth_mode(persisted)
            if "mcp_auth_mode" in persisted
            else runtime_mcp_auth_mode,
            "public_url": configured_public_origin(persisted)
            if has_persisted_deployment
            else runtime_public_url,
        }

    def _runtime_network_security(desired_auth_required: object | None = None) -> dict:
        return current_mcp_network_security(
            sh.config,
            desired_auth_required=desired_auth_required,
            environment=os.environ,
            in_docker=sh.in_docker(),
        )

    @mcp.custom_route("/dashboard", methods=["GET"])
    async def dashboard(request: Request) -> Response:
        """Legacy alias: /dashboard 永久跳到根路径。

        我历史上把 dashboard 同时挂在 / 与 /dashboard，但叠加 Cloudflare 边缘
        （或任何 reverse proxy）的 host-rewrite 规则时容易触发回环。统一只在 /
        上提供 HTML，老书签靠 301 软迁移到 /。
        """
        from starlette.responses import RedirectResponse
        return RedirectResponse(url="/", status_code=301)


    @mcp.custom_route("/api/env-vars", methods=["GET"])
    async def api_env_vars(request: Request) -> Response:
        """Return status of all known OMBRE_* env vars (sensitive fields masked)."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err

        # 启动期被平台注入的可配置 env 集合（在任何 dashboard 保存 mutate os.environ 之前快照）。
        # from_boot=True ⇒ 该变量是平台级 env，重启后会覆盖 dashboard 存进 config.yaml 的值。
        from utils import BOOT_ENV_CONFIG

        def _masked(name: str) -> dict:
            return {"set": bool(os.environ.get(name, "").strip()), "value": None,
                    "from_boot": name in BOOT_ENV_CONFIG}

        def _plain(name: str) -> dict:
            v = os.environ.get(name, "").strip()
            return {"set": bool(v), "value": v or None, "from_boot": name in BOOT_ENV_CONFIG}

        vars_data = [
            # LLM 压缩组
            {"name": "OMBRE_COMPRESS_API_KEY", "group": "llm", "label": "压缩 LLM API Key", "sensitive": True, **_masked("OMBRE_COMPRESS_API_KEY")},
            {"name": "OMBRE_COMPRESS_BASE_URL", "group": "llm", "label": "压缩 LLM Base URL", "sensitive": False, **_plain("OMBRE_COMPRESS_BASE_URL")},
            {"name": "OMBRE_COMPRESS_MODEL", "group": "llm", "label": "压缩 LLM 模型", "sensitive": False, **_plain("OMBRE_COMPRESS_MODEL")},
            {"name": "OMBRE_COMPRESS_TIMEOUT_SECONDS", "group": "llm", "label": "压缩 LLM 超时秒数", "sensitive": False, **_plain("OMBRE_COMPRESS_TIMEOUT_SECONDS")},
            # Embedding 组
            {"name": "OMBRE_EMBED_API_KEY", "group": "embed", "label": "向量化 API Key", "sensitive": True, **_masked("OMBRE_EMBED_API_KEY")},
            {"name": "OMBRE_EMBED_BASE_URL", "group": "embed", "label": "向量化 Base URL", "sensitive": False, **_plain("OMBRE_EMBED_BASE_URL")},
            {"name": "OMBRE_EMBED_MODEL", "group": "embed", "label": "向量化模型", "sensitive": False, **_plain("OMBRE_EMBED_MODEL")},
            {"name": "OMBRE_EMBED_TIMEOUT_SECONDS", "group": "embed", "label": "向量化超时秒数", "sensitive": False, **_plain("OMBRE_EMBED_TIMEOUT_SECONDS")},
            # 服务配置组
            {"name": "OMBRE_TRANSPORT", "group": "system", "label": "传输模式", "sensitive": False, **_plain("OMBRE_TRANSPORT")},
            {"name": "OMBRE_PORT", "group": "system", "label": "服务端口", "sensitive": False, **_plain("OMBRE_PORT")},
            {"name": "OMBRE_LOG_FILE", "group": "system", "label": "日志文件路径", "sensitive": False, **_plain("OMBRE_LOG_FILE")},
            {"name": "OMBRE_CONFIG_PATH", "group": "system", "label": "配置文件路径", "sensitive": False, **_plain("OMBRE_CONFIG_PATH")},
            {"name": "OMBRE_MCP_REQUIRE_AUTH", "group": "auth", "label": "MCP 鉴权开关覆盖", "sensitive": False, **_plain("OMBRE_MCP_REQUIRE_AUTH")},
            {"name": "OMBRE_MCP_AUTH_MODE", "group": "auth", "label": "MCP 鉴权模式覆盖 (oauth/token/hybrid)", "sensitive": False, **_plain("OMBRE_MCP_AUTH_MODE")},
            {"name": "OMBRE_MCP_TOKEN", "group": "auth", "label": "MCP 静态 Token", "sensitive": True, **_masked("OMBRE_MCP_TOKEN")},
            {"name": "AI_NAME", "group": "identity", "label": "AI 显示名", "sensitive": False, **_plain("AI_NAME")},
            # 路径组
            {"name": "OMBRE_VAULT_DIR", "group": "paths", "label": "Vault 目录 (推荐)", "sensitive": False, **_plain("OMBRE_VAULT_DIR")},
            {"name": "OMBRE_BUCKETS_DIR", "group": "paths", "label": "桶目录 (旧版兼容)", "sensitive": False, **_plain("OMBRE_BUCKETS_DIR")},
            {"name": "OMBRE_HOST_VAULT_DIR", "group": "paths", "label": "宿主机 Vault 目录 (Docker)", "sensitive": False, **_plain("OMBRE_HOST_VAULT_DIR")},
            # Webhook 组
            {"name": "OMBRE_HOOK_URL", "group": "webhook", "label": "Webhook URL", "sensitive": False, **_plain("OMBRE_HOOK_URL")},
            {"name": "OMBRE_HOOK_SKIP", "group": "webhook", "label": "跳过 Webhook", "sensitive": False,
             "set": bool(os.environ.get("OMBRE_HOOK_SKIP", "").strip()),
             "value": os.environ.get("OMBRE_HOOK_SKIP", "").strip() or None},
            # 鉴权组
            {"name": "OMBRE_DASHBOARD_PASSWORD", "group": "auth", "label": "Dashboard 密码", "sensitive": True, **_masked("OMBRE_DASHBOARD_PASSWORD")},
        ]

        return JSONResponse({"vars": vars_data})


    @mcp.custom_route("/api/config", methods=["GET"])
    async def api_config_get(request: Request) -> Response:
        """Get current runtime config (safe fields only, API key masked)."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            desired = _desired_startup_state(read_config_yaml())
        except (OSError, ValueError) as exc:
            logger.error("读取持久化启动配置失败: %s", exc)
            return JSONResponse(
                {"error": f"failed to read persisted config: {exc}"},
                status_code=500,
            )
        dehy = sh.config.get("dehydration", {})
        emb = sh.config.get("embedding", {})
        runtime_network_security = _runtime_network_security(
            desired["mcp_require_auth"]
        )
        api_key = dehy.get("api_key", "")
        masked_key = f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else ("***" if api_key else "")
        return JSONResponse({
            "dehydration": {
                "model": dehy.get("model", ""),
                "base_url": dehy.get("base_url", ""),
                "api_key_masked": masked_key,
                "max_tokens": dehy.get("max_tokens", 1024),
                "temperature": dehy.get("temperature", 0.1),
                "api_format": dehy.get("api_format", "openai_compat"),
                "timeout_seconds": dehy.get("timeout_seconds", 60),
            },
            "embedding": {
                "enabled": _parse_bool(emb.get("enabled", False), default=False),
                "model": emb.get("model", ""),
                "api_format": emb.get("api_format", "openai_compat"),
                "timeout_seconds": emb.get("timeout_seconds", 30),
                "backend": "api",
                "backend_options": [
                    {"value": "api", "label": "Gemini API（云端）", "note": "需填 OMBRE_EMBED_API_KEY，3072 维质量最高，需联网；客户端几乎不占额外内存"},
                ],
            },
            "surfacing": {
                "breath_max_results": int(sh.config.get("surfacing", {}).get("breath_max_results") or 20),
                "breath_max_tokens": int(sh.config.get("surfacing", {}).get("breath_max_tokens") or 20000),
                "feel_max_tokens": int(sh.config.get("surfacing", {}).get("feel_max_tokens") or 15000),
            },
            "merge_threshold": sh.config.get("merge_threshold", 75),
            # 只给日期不写时区时按它理解（Letter 定时锁等）。前端「设置」可改。
            "timezone": _get_timezone_name(),
            "transport": desired["transport"],
            "transport_effective": runtime_transport,
            "buckets_dir": sh.config.get("buckets_dir", ""),
            # MCP 鉴权开关。默认 true；具体 OAuth/静态 Token 模式由 mcp_auth_mode 决定。
            # 渲染一键开关；关掉后 /mcp 免认证直连（供自有前端 / GPT / GLM 等）。
            "mcp_require_auth": desired["mcp_require_auth"],
            "mcp_require_auth_effective": runtime_mcp_auth_required,
            # 鉴权模式（仅 mcp_require_auth=true 时有意义）：OAuth、静态 Token 或两者共存。
            "mcp_auth_mode": desired["mcp_auth_mode"],
            "mcp_auth_mode_effective": runtime_mcp_auth_mode,
            "mcp_network_security": runtime_network_security,
            # 静态 Token 状态：只回掩码/是否已配置，绝不回明文。
            "mcp_token_configured": bool(_current_mcp_token()),
            "mcp_token_hint": _mask_mcp_token(_current_mcp_token()),
            # Dashboard 的公网 MCP 地址是 OAuth resource/audience 的启动期
            # 配置；同时回传已保存值与本进程实际值，避免假装热切换成功。
            "deployment": {
                "public_url": desired["public_url"],
                "public_url_effective": runtime_public_url,
            },
            "restart_required": (
                (
                    desired["mcp_require_auth"] != runtime_mcp_auth_required
                    and not runtime_network_security.get("guard_active")
                    and not runtime_network_security.get("auth_environment_override")
                )
                or desired["mcp_auth_mode"] != runtime_mcp_auth_mode
                or desired["transport"] != runtime_transport
                or desired["public_url"] != runtime_public_url
            ),
            # 部署信息：数据目录 + 端口 + 是否容器内。前端「系统」区展示，端口可改。
            "host_port": sh.config.get("host_port"),
            "in_docker": sh.in_docker(),
            # AI 一方的显示名（取自环境变量 AI_NAME，回退 "AI"）。前端只读，用于
            # 面向用户的文案（如删除确认、信件署名占位）。
            "ai_name": _get_ai_name(),
            # 记忆归属：多人共用一套 OB 时标明「这份记忆是谁的」。owner_count>=2 时
            # 前端顶部才显示归属徽标（单人不打扰）；owner_name 为徽标文字。均只读。
            "owner_name": _get_owner_name(),
            "owner_count": _get_owner_count(),
        })


    @mcp.custom_route("/api/config", methods=["POST"])
    async def api_config_update(request: Request) -> Response:
        """Hot-update runtime sh.config. Optionally persist to config.yaml."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "JSON body must be an object"}, status_code=400)

        updated = []
        try:
            persist_requested = _parse_bool(body.get("persist", False))
            mcp_auth_value = (
                _parse_bool(body["mcp_require_auth"])
                if "mcp_require_auth" in body
                else None
            )
            mcp_auth_mode_value = None
            if "mcp_auth_mode" in body:
                mcp_auth_mode_value = str(body["mcp_auth_mode"]).strip().lower()
                if mcp_auth_mode_value not in ("oauth", "token", "hybrid"):
                    return JSONResponse(
                        {"error": "mcp_auth_mode must be 'oauth', 'token', or 'hybrid'"},
                        status_code=400,
                    )
            embedding_payload = body.get("embedding")
            if "embedding" in body and not isinstance(embedding_payload, dict):
                return JSONResponse(
                    {"error": "embedding must be an object"}, status_code=400
                )
            if "dehydration" in body and not isinstance(
                body.get("dehydration"), dict
            ):
                return JSONResponse(
                    {"error": "dehydration must be an object"}, status_code=400
                )
            if "surfacing" in body and not isinstance(body.get("surfacing"), dict):
                return JSONResponse(
                    {"error": "surfacing must be an object"}, status_code=400
                )
            dehydration_payload = dict(body.get("dehydration") or {})
            if "extra_body" in dehydration_payload and not isinstance(
                dehydration_payload["extra_body"], dict
            ):
                return JSONResponse(
                    {"error": "dehydration.extra_body must be an object"},
                    status_code=400,
                )
            if "max_tokens" in dehydration_payload:
                dehydration_payload["max_tokens"] = _bounded_config_int(
                    dehydration_payload["max_tokens"],
                    "dehydration.max_tokens",
                    128,
                    8192,
                )
            if "temperature" in dehydration_payload:
                dehydration_payload["temperature"] = _bounded_config_float(
                    dehydration_payload["temperature"],
                    "dehydration.temperature",
                    0.0,
                    2.0,
                )
            if "timeout_seconds" in dehydration_payload:
                dehydration_payload["timeout_seconds"] = _bounded_config_float(
                    dehydration_payload["timeout_seconds"],
                    "dehydration.timeout_seconds",
                    1.0,
                    600.0,
                )

            merge_threshold_value = (
                _bounded_config_int(
                    body["merge_threshold"], "merge_threshold", 0, 100
                )
                if "merge_threshold" in body
                else None
            )
            host_port_value = (
                _bounded_config_int(body["host_port"], "host_port", 1, 65535)
                if "host_port" in body
                else None
            )

            # --- Timezone ---
            # 只给日期不写时区时按它理解。必须当场校验：写进去一个解析不了的
            # 名字，之后每次解析日期都会静默回退 +08:00，用户以为自己设成功了。
            timezone_value = None
            if "timezone" in body:
                raw_tz = str(body.get("timezone") or "").strip()
                if raw_tz:
                    if len(raw_tz) > 64:
                        # 64 是随便定的。世界上最长的 IANA 时区名才 30 出头
                        # （America/Argentina/ComodRivadavia，去查了，真的存在）。
                        # 留一倍余量，剩下的当有人手滑。
                        return JSONResponse(
                            {"error": "timezone 名称过长"}, status_code=400
                        )
                    try:
                        from zoneinfo import ZoneInfo

                        ZoneInfo(raw_tz)
                    except Exception:
                        return JSONResponse(
                            {
                                "error": (
                                    f"无法识别时区「{raw_tz}」。请使用 IANA 时区名，"
                                    "例如 Asia/Shanghai、UTC、America/New_York。"
                                )
                            },
                            status_code=400,
                        )
                timezone_value = raw_tz

            surfacing_values: dict[str, int] = {}
            surfacing_payload = body.get("surfacing") or {}
            for key, low, high in (
                ("breath_max_results", 1, 50),
                ("breath_max_tokens", 500, 40000),
                ("feel_max_tokens", 500, 20000),
            ):
                if key in surfacing_payload:
                    surfacing_values[key] = _bounded_config_int(
                        surfacing_payload[key], f"surfacing.{key}", low, high
                    )
            deployment_payload = body.get("deployment")
            if "deployment" in body and not isinstance(deployment_payload, dict):
                return JSONResponse(
                    {"error": "deployment must be an object"}, status_code=400
                )
            deployment_public_url = None
            if isinstance(deployment_payload, dict) and "public_url" in deployment_payload:
                raw_public_url = str(deployment_payload["public_url"] or "").strip()
                deployment_public_url = ""
                if raw_public_url:
                    deployment_public_url = normalize_public_https_origin(
                        raw_public_url
                    )
                    if not deployment_public_url:
                        return JSONResponse(
                            {
                                "error": (
                                    "deployment.public_url must be an HTTPS domain "
                                    "or complete /mcp URL"
                                )
                            },
                            status_code=400,
                        )
            embedding_enabled = (
                _parse_bool(embedding_payload["enabled"])
                if isinstance(embedding_payload, dict)
                and "enabled" in embedding_payload
                else None
            )
            embedding_backend = None
            if isinstance(embedding_payload, dict) and "backend" in embedding_payload:
                backend_raw = str(embedding_payload["backend"]).strip().lower()
                embedding_backend = (
                    "api" if backend_raw in ("api", "gemini") else backend_raw
                )
                if embedding_backend != "api":
                    return JSONResponse(
                        {"error": f"unsupported embedding backend: {backend_raw}"},
                        status_code=400,
                    )
            sampling_payload = None
            if isinstance(body.get("surfacing"), dict):
                candidate = body["surfacing"].get("sampling")
                if candidate is not None and not isinstance(candidate, dict):
                    return JSONResponse(
                        {"error": "surfacing.sampling must be an object"},
                        status_code=400,
                    )
                sampling_payload = candidate
            sampling_enabled = (
                _parse_bool(sampling_payload["enabled"])
                if isinstance(sampling_payload, dict)
                and "enabled" in sampling_payload
                else None
            )
            sampling_values: dict[str, int | float] = {}
            if isinstance(sampling_payload, dict):
                if "top_k" in sampling_payload:
                    sampling_values["top_k"] = _bounded_config_int(
                        sampling_payload["top_k"],
                        "surfacing.sampling.top_k",
                        1,
                        50,
                    )
                if "sample_k" in sampling_payload:
                    sampling_values["sample_k"] = _bounded_config_int(
                        sampling_payload["sample_k"],
                        "surfacing.sampling.sample_k",
                        1,
                        20,
                    )
                if "temperature" in sampling_payload:
                    sampling_values["temperature"] = _bounded_config_float(
                        sampling_payload["temperature"],
                        "surfacing.sampling.temperature",
                        0.1,
                        5.0,
                    )
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        startup_setting_requested = (
            deployment_public_url is not None
            or mcp_auth_value is not None
            or mcp_auth_mode_value is not None
        )
        if startup_setting_requested and not persist_requested:
            return JSONResponse(
                {
                    "error": (
                        "MCP startup settings require persist=true because "
                        "they only take effect after restart"
                    )
                },
                status_code=400,
            )
        hot_update_keys = {
            "dehydration",
            "embedding",
            "merge_threshold",
            "host_port",
            "surfacing",
            "timezone",
        }
        if startup_setting_requested and hot_update_keys.intersection(body):
            return JSONResponse(
                {
                    "error": (
                        "MCP startup settings cannot be combined with hot runtime "
                        "settings; save them in separate requests"
                    )
                },
                status_code=400,
            )

        mcp_network_security: dict | None = None
        if mcp_auth_value is False:
            # 先于任何热配置变更执行，避免同一请求稍后因危险鉴权设置被拒绝时，
            # 其他字段却已经部分生效；原子写入锁内还会基于最新磁盘配置再检查一次。
            security_candidate = dict(sh.config)
            security_candidate["mcp_require_auth"] = False
            mcp_network_security = assess_mcp_network_safety(
                security_candidate,
                environment=os.environ,
                in_docker=sh.in_docker(),
            )
            security_issue = mcp_network_safety_issue(mcp_network_security)
            if security_issue:
                return JSONResponse({
                    "error": security_issue,
                    "mcp_network_security": mcp_network_security,
                }, status_code=400)

        runtime_config_before = copy.deepcopy(sh.config)
        embedding_before = sh.embedding_engine
        dehydrator_fields = (
            "model",
            "base_url",
            "max_tokens",
            "temperature",
            "timeout_seconds",
            "api_format",
            "extra_body",
            "api_key",
            "api_available",
            "client",
        )
        dehydrator_before = {
            field: getattr(sh.dehydrator, field)
            for field in dehydrator_fields
            if hasattr(sh.dehydrator, field)
        }

        def _rollback_hot_runtime() -> None:
            """在验证或持久化失败时恢复同一份运行态快照。"""
            sh.config.clear()
            sh.config.update(copy.deepcopy(runtime_config_before))
            for field, value in dehydrator_before.items():
                setattr(sh.dehydrator, field, value)
            if sh.embedding_engine is not embedding_before:
                sh.replace_embedding_engine(embedding_before)

        # --- Dehydration config ---
        if "dehydration" in body:
            d = dehydration_payload
            dehy = sh.config.setdefault("dehydration", {})
            for key in ("model", "base_url", "max_tokens", "temperature", "api_format", "timeout_seconds", "extra_body"):
                if key in d:
                    dehy[key] = d[key]
                    updated.append(f"dehydration.{key}")
            if "api_key" in d and d["api_key"]:
                dehy["api_key"] = d["api_key"]
                updated.append("dehydration.api_key")
            # 热重载压缩器：同步所有属性，让 Dashboard 修改立即生效。
            sh.dehydrator.model = dehy.get("model", sh.dehydrator.model)
            sh.dehydrator.base_url = dehy.get("base_url", sh.dehydrator.base_url)
            sh.dehydrator.max_tokens = int(dehy.get("max_tokens") or sh.dehydrator.max_tokens)
            configured_temperature = dehy.get("temperature")
            if configured_temperature is not None:
                sh.dehydrator.temperature = float(configured_temperature)
            sh.dehydrator.timeout_seconds = _positive_float(dehy.get("timeout_seconds"), sh.dehydrator.timeout_seconds)
            sh.dehydrator.api_format = dehy.get("api_format", getattr(sh.dehydrator, "api_format", "openai_compat"))
            sh.dehydrator.extra_body = dict(dehy.get("extra_body") or {})
            if "api_key" in d and d["api_key"]:
                sh.dehydrator.api_key = dehy["api_key"]
            sh.dehydrator.api_available = bool(sh.dehydrator.api_key)
            # 密钥或 URL 改变时重建 OpenAI 兼容客户端。
            if sh.dehydrator.api_available and sh.dehydrator.api_format == "openai_compat":
                from openai import AsyncOpenAI
                try:
                    sh.dehydrator.client = AsyncOpenAI(
                        api_key=sh.dehydrator.api_key,
                        base_url=sh.dehydrator.base_url,
                        timeout=sh.dehydrator.timeout_seconds,
                        max_retries=0,  # 重试归 Dehydrator._chat 管，见 dehydrator.py
                    )
                except Exception as exc:
                    _rollback_hot_runtime()
                    logger.warning(
                        "dehydration reload failed: err_type=%s detail=hidden",
                        type(exc).__name__,
                    )
                    return JSONResponse(
                        {"error": "dehydration reload failed"},
                        status_code=400,
                    )
            else:
                sh.dehydrator.client = None

        # --- Embedding config ---
        if "embedding" in body:
            e = embedding_payload
            emb = sh.config.setdefault("embedding", {})
            rebuild_embedding = False
            if embedding_enabled is not None:
                emb["enabled"] = embedding_enabled
                updated.append("embedding.enabled")
                rebuild_embedding = True
            if "model" in e:
                emb["model"] = e["model"]
                updated.append("embedding.model")
                rebuild_embedding = True
            if "base_url" in e:
                emb["base_url"] = str(e["base_url"]).strip()
                updated.append("embedding.base_url")
                rebuild_embedding = True
            if "timeout_seconds" in e:
                emb["timeout_seconds"] = e["timeout_seconds"]
                updated.append("embedding.timeout_seconds")
                rebuild_embedding = True
            if "api_format" in e:
                emb["api_format"] = str(e["api_format"]).strip()
                updated.append("embedding.api_format")
                rebuild_embedding = True
            if embedding_backend is not None:
                emb["backend"] = embedding_backend
                updated.append("embedding.backend")
                rebuild_embedding = True

            # 一个请求可能修改多个字段；只重建一次，再把同一实例发布给 Web 路由、
            # BucketManager、ImportEngine 和 MCP 工具运行时，避免读写使用不同模型。
            if rebuild_embedding:
                try:
                    _rebuild_embedding_runtime()
                except Exception as e:
                    _rollback_hot_runtime()
                    logger.warning(
                        "embedding reload failed: err_type=%s detail=hidden",
                        type(e).__name__,
                    )
                    return JSONResponse(
                        {"error": "embedding reload failed"},
                        status_code=400,
                    )

        # --- Merge threshold ---
        if merge_threshold_value is not None:
            sh.config["merge_threshold"] = merge_threshold_value
            updated.append("merge_threshold")

        # --- Timezone ---
        if timezone_value is not None:
            sh.config["timezone"] = timezone_value
            updated.append("timezone")

        # MCP 鉴权开关、鉴权模式与公网地址都是启动期快照。它们只写入
        # config.yaml，不能提前发布到 sh.config；否则 OAuth/MCP 中间件仍使用
        # 旧闭包，而诊断与其他路由却会误以为新值已经生效。GET /api/config 会从
        # 持久配置回显 desired 值，并单独返回 effective 值。

        # --- 对外端口（host_port）---
        # 裸机：写 config 后进程自重启即监听新端口（前端「保存并重启」）。
        # Docker：容器内端口由 Dockerfile 固定，host_port 仅供部署脚本读取注入
        # OMBRE_HOST_PORT，须重建容器才生效（前端会提示）。
        if host_port_value is not None:
            sh.config["host_port"] = host_port_value
            updated.append("host_port")

        # --- Surfacing defaults (breath/feel token & result caps) ---
        if "surfacing" in body and isinstance(body["surfacing"], dict):
            sf = sh.config.setdefault("surfacing", {})
            for key, value in surfacing_values.items():
                sf[key] = value
                updated.append(f"surfacing.{key}")

        persisted_after: dict | None = None

        # --- Persist to config.yaml if requested ---
        if persist_requested:
            def _mutate(save_config: dict) -> None:
                if "dehydration" in body:
                    sc_dehy = save_config.setdefault("dehydration", {})
                    if not isinstance(sc_dehy, dict):
                        sc_dehy = {}
                        save_config["dehydration"] = sc_dehy
                    for key in ("model", "base_url", "max_tokens", "temperature", "api_format", "timeout_seconds", "extra_body"):
                        if key in dehydration_payload:
                            sc_dehy[key] = dehydration_payload[key]
                    # Never persist api_key to yaml (use env var)

                if "embedding" in body:
                    sc_emb = save_config.setdefault("embedding", {})
                    if not isinstance(sc_emb, dict):
                        sc_emb = {}
                        save_config["embedding"] = sc_emb
                    for key in ("model", "base_url", "api_format", "timeout_seconds"):
                        if key in body["embedding"]:
                            sc_emb[key] = body["embedding"][key]
                    if embedding_enabled is not None:
                        sc_emb["enabled"] = embedding_enabled
                    if embedding_backend is not None:
                        sc_emb["backend"] = embedding_backend

                if merge_threshold_value is not None:
                    save_config["merge_threshold"] = merge_threshold_value

                if timezone_value is not None:
                    save_config["timezone"] = timezone_value

                if mcp_auth_value is not None:
                    security_candidate = dict(save_config)
                    security_candidate.setdefault("transport", runtime_transport)
                    security_candidate["mcp_require_auth"] = mcp_auth_value
                    latest_security = assess_mcp_network_safety(
                        security_candidate,
                        environment=os.environ,
                        in_docker=sh.in_docker(),
                    )
                    security_issue = mcp_network_safety_issue(latest_security)
                    if security_issue:
                        raise ValueError(security_issue)
                    save_config["mcp_require_auth"] = mcp_auth_value

                if mcp_auth_mode_value is not None:
                    save_config["mcp_auth_mode"] = mcp_auth_mode_value

                if host_port_value is not None:
                    save_config["host_port"] = host_port_value

                if "surfacing" in body and isinstance(body["surfacing"], dict):
                    sc_sf = save_config.setdefault("surfacing", {})
                    if not isinstance(sc_sf, dict):
                        sc_sf = {}
                        save_config["surfacing"] = sc_sf
                    for key, value in surfacing_values.items():
                        sc_sf[key] = value
                    if "sampling" in body["surfacing"] and isinstance(body["surfacing"]["sampling"], dict):
                        sc_samp = sc_sf.setdefault("sampling", {})
                        if not isinstance(sc_samp, dict):
                            sc_samp = {}
                            sc_sf["sampling"] = sc_samp
                        if sampling_enabled is not None:
                            sc_samp["enabled"] = sampling_enabled
                        for key, value in sampling_values.items():
                            sc_samp[key] = value

                if deployment_public_url is not None:
                    sc_deployment = save_config.get("deployment")
                    if not isinstance(sc_deployment, dict):
                        sc_deployment = {}
                        save_config["deployment"] = sc_deployment
                    if deployment_public_url:
                        sc_deployment["public_url"] = deployment_public_url
                    else:
                        sc_deployment.pop("public_url", None)

            try:
                persisted_after = atomic_update_config_yaml(_mutate)
                updated.append("persisted_to_yaml")
                if mcp_auth_value is not None:
                    updated.append("mcp_require_auth")
                if mcp_auth_mode_value is not None:
                    updated.append("mcp_auth_mode")
                if deployment_public_url is not None:
                    updated.append("deployment.public_url")
            except ValueError as e:
                _rollback_hot_runtime()
                return JSONResponse({"error": str(e), "updated": []}, status_code=400)
            except Exception as e:
                _rollback_hot_runtime()
                logger.error(
                    "config persist failed: err_type=%s detail=hidden",
                    type(e).__name__,
                )
                return JSONResponse(
                    {"error": "persist failed", "updated": []},
                    status_code=500,
                )

        desired = _desired_startup_state(
            persisted_after if persisted_after is not None else sh.config
        )
        runtime_network_security = _runtime_network_security(
            desired["mcp_require_auth"]
        )
        auth_environment_conflict = (
            runtime_network_security.get("auth_environment_override")
            and runtime_network_security.get("auth_environment_value")
            != desired["mcp_require_auth"]
        )
        restart_required = (
            (
                desired["mcp_require_auth"] != runtime_mcp_auth_required
                and not runtime_network_security.get("guard_active")
                and not runtime_network_security.get("auth_environment_override")
            )
            or desired["mcp_auth_mode"] != runtime_mcp_auth_mode
            or desired["transport"] != runtime_transport
            or desired["public_url"] != runtime_public_url
        )
        return JSONResponse({
            "updated": updated,
            "ok": True,
            "restart_required": restart_required,
            "mcp_require_auth_effective": runtime_mcp_auth_required,
            "mcp_auth_mode_effective": runtime_mcp_auth_mode,
            "transport": desired["transport"],
            "transport_effective": runtime_transport,
            "mcp_require_auth": desired["mcp_require_auth"],
            "mcp_auth_mode": desired["mcp_auth_mode"],
            "mcp_network_security": runtime_network_security,
            "warnings": (
                (
                    [runtime_network_security["reason"]]
                    if runtime_network_security.get("override_active") else []
                )
                + (
                    [
                        "OMBRE_MCP_REQUIRE_AUTH 仍由平台环境变量控制；"
                        "请在部署平台修改或删除该变量后重建/重启服务。"
                    ]
                    if auth_environment_conflict else []
                )
            ),
            "deployment": {
                "public_url": desired["public_url"],
                "public_url_effective": runtime_public_url,
            },
            "message": (
                "设置已保存；当前配置或环境仍请求免鉴权，安全门禁继续强制鉴权。"
                if runtime_network_security.get("guard_active")
                else (
                    "设置已保存，但 OMBRE_MCP_REQUIRE_AUTH 仍由平台环境变量控制；"
                    "请在部署平台修改或删除该变量后重建/重启服务。"
                    if auth_environment_conflict
                    else (
                        "MCP 启动配置已保存，需要重启服务后生效。"
                        if restart_required else "设置已生效。"
                    )
                )
            ),
        })


    # =============================================================
    # /api/mcp-token/regenerate — 生成/轮换 token/hybrid 模式使用的静态密钥
    # 独立成一个小路由（而不是塞进 POST /api/config）：生成新密钥和改配置项
    # 是两件不同的事，参照 oauth.py 里 token 签发自成一块的做法。
    # =============================================================
    @mcp.custom_route("/api/mcp-token/regenerate", methods=["POST"])
    async def api_mcp_token_regenerate(request: Request) -> Response:
        """(Re)generate the static MCP token and persist it to config.yaml.

        Returns the plaintext token exactly once — GET /api/config only ever
        returns a masked hint, so the Dashboard must capture this response.
        Takes effect immediately when the running process is already in token
        or hybrid mode: _is_valid_static_mcp_token reads sh.config/env fresh on
        every request. A newly selected auth mode still requires a restart.
        """
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err

        new_token = secrets.token_urlsafe(32)

        # 原生锁覆盖“落盘 + 发布”整个提交段，且临界区内没有 await；既避免
        # 并发请求发生磁盘 B、运行态 A 的逆序，也不产生 asyncio 跨循环绑定。
        with mcp_token_commit_lock:
            try:
                atomic_update_config_yaml(
                    lambda save_config: save_config.__setitem__(
                        "mcp_token", new_token
                    )
                )
            except Exception as e:
                return JSONResponse(
                    {"error": f"persist failed: {e}"}, status_code=500
                )

            # 鉴权每次请求都直接读取 sh.config；必须先确认持久化成功，再发布运行态。
            # 否则磁盘写失败时接口虽然返回 500，新 token 却已经即时生效，重启后又
            # 回到旧 token，形成无法从响应判断的临时授权状态。
            sh.config["mcp_token"] = new_token

        env_override = bool(os.environ.get("OMBRE_MCP_TOKEN", "").strip())
        return JSONResponse({
            "ok": True,
            "token": new_token,
            "token_hint": _mask_mcp_token(new_token),
            "env_override": env_override,
            "message": (
                "环境变量 OMBRE_MCP_TOKEN 优先级更高，已生成的新密钥暂不会生效，"
                "请改用该环境变量或先取消设置它。"
                if env_override
                else "新 Token 已生成并保存，请立即复制；刷新页面后不再显示完整值。"
                     "当前进程已处于 Token/共存模式时立即生效；刚切换模式仍需重启。"
            ),
        })


    # =============================================================
    # /api/test/dehydration — 测试脱水 LLM API Key 是否可用
    # =============================================================
    @mcp.custom_route("/api/test/dehydration", methods=["POST"])
    async def api_test_dehydration(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        # Use current runtime config (api_key may have been updated in-memory)
        dehyd = sh.config.get("dehydration", {})
        model = dehyd.get("model", "")
        base_url = dehyd.get("base_url", "")
        api_key = dehyd.get("api_key", "")
        if not api_key:
            return JSONResponse({"ok": False, "error": "未设置 API Key"}, status_code=400)
        try:
            import httpx as _httpx
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
                **chat_completion_token_limit(model, 5),
            }
            async with _httpx.AsyncClient(timeout=15) as client:
                r = await client.post(f"{base_url.rstrip('/')}/chat/completions", json=payload, headers=headers)
            if r.status_code in (200, 201):
                return JSONResponse({"ok": True, "message": "API Key 有效 ✓"})
            else:
                try:
                    detail = r.json().get("error", {})
                    msg = detail.get("message", r.text[:200]) if isinstance(detail, dict) else str(detail)[:200]
                except Exception:
                    msg = r.text[:200]
                return JSONResponse({"ok": False, "error": f"HTTP {r.status_code}: {msg}"})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)[:300]})


    # =============================================================
    # /api/test/embedding — 测试向量化 Embedding 是否真的可用
    # 之前只有脱水(compress)能测，向量化无从验证 → 用户「压缩正常但向量化静默失败」
    # 时完全无感。这里实际发一次 embedding 请求，把成功/失败如实回给前端。(#2/#3)
    # =============================================================
    @mcp.custom_route("/api/test/embedding", methods=["POST"])
    async def api_test_embedding(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        eng = sh.embedding_engine  # 读全局（Fix: env-sh.config 保存后已正确重建）
        if not getattr(eng, "enabled", False) or getattr(eng, "_backend", None) is None:
            return JSONResponse({
                "ok": False,
                "error": "向量化未启用或缺 key（standby）。请填入 Embedding API Key 点「保存」后再测。",
            })
        try:
            vec = await eng._generate_async("connectivity probe / 连接性探针")
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"[:300]})
        if vec:
            model = getattr(eng, "model", "") or (
                eng._backend.model_name() if getattr(eng, "_backend", None) else "?"
            )
            return JSONResponse({
                "ok": True,
                "message": f"向量化连接成功 ✓（模型 {model}，维度 {len(vec)}）",
            })
        return JSONResponse({
            "ok": False,
            "error": "调用返回空向量：检查 model 名 / base_url / key 是否匹配该 provider"
                     "（如硅基流动 base_url=https://api.siliconflow.cn/v1、model=BAAI/bge-m3）。详见错误面板 OB-E001。",
        })


    # =============================================================
    # /api/models — 获取 LLM provider 可用模型列表（供 Dashboard 模型选择器使用）
    # POST Body: {api_key, base_url, api_format}
    # 支持 openai_compat / gemini / anthropic 三种格式
    # =============================================================
    @mcp.custom_route("/api/models", methods=["POST"])
    async def api_list_models(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = await sh._read_json_object(request)
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

        provider_fields = ("api_key", "base_url", "api_format")
        if any(key in body and not isinstance(body[key], str) for key in provider_fields):
            return JSONResponse({"ok": False, "error": "provider fields must be strings"}, status_code=400)
        api_key = str(body.get("api_key", "")).strip()
        base_url = str(body.get("base_url", "")).strip()
        api_format = str(body.get("api_format", "openai_compat")).strip().lower()
        if (
            len(api_key) > _MAX_PROVIDER_KEY_CHARS
            or len(base_url) > _MAX_PROVIDER_URL_CHARS
            or len(api_format) > _MAX_PROVIDER_FORMAT_CHARS
        ):
            return JSONResponse({"ok": False, "error": "provider configuration is too large"}, status_code=400)

        # Sentinel "__use_current__": use server-side key from dehydration config
        if api_key == "__use_current__":
            api_key = sh.config.get("dehydration", {}).get("api_key", "")
            if not base_url:
                base_url = sh.config.get("dehydration", {}).get("base_url", "")
            if not api_format or api_format == "openai_compat":
                api_format = sh.config.get("dehydration", {}).get("api_format", "openai_compat")
        # Sentinel "__use_current_embed__": use server-side key from embedding config
        if api_key == "__use_current_embed__":
            api_key = sh.config.get("embedding", {}).get("api_key", "")
            if not base_url:
                base_url = sh.config.get("embedding", {}).get("base_url", "")

        if not api_key:
            return JSONResponse({"ok": False, "error": "需要 api_key（请先保存 API Key 或在输入框填入）"}, status_code=400)

        try:
            models: list[str] = []
            if api_format in ("gemini", "gemini_embed"):
                # gemini → generateContent models；gemini_embed → embedContent models
                method_filter = "embedContent" if api_format == "gemini_embed" else "generateContent"
                url = "https://generativelanguage.googleapis.com/v1beta/models"
                async with httpx.AsyncClient(timeout=10.0) as c:
                    r = await c.get(
                        url,
                        params={"pageSize": 200},
                        headers={"x-goog-api-key": api_key},
                    )
                r.raise_for_status()
                for m in r.json().get("models", []):
                    if method_filter in m.get("supportedGenerationMethods", []):
                        models.append(m.get("name", "").replace("models/", ""))
            elif api_format == "anthropic":
                ant_base = base_url.rstrip("/") if base_url else "https://api.anthropic.com"
                headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
                async with httpx.AsyncClient(timeout=10.0) as c:
                    r = await c.get(f"{ant_base}/v1/models", headers=headers)
                r.raise_for_status()
                models = [m.get("id", "") for m in r.json().get("data", []) if m.get("id")]
            else:  # openai_compat
                if not base_url:
                    return JSONResponse({"ok": False, "error": "openai_compat 格式需要 base_url"}, status_code=400)
                headers_oai = {"Authorization": f"Bearer {api_key}"}
                async with httpx.AsyncClient(timeout=10.0) as c:
                    r = await c.get(f"{base_url.rstrip('/')}/models", headers=headers_oai)
                r.raise_for_status()
                models = sorted(m.get("id", "") for m in r.json().get("data", []) if m.get("id"))
            return JSONResponse({"ok": True, "models": [m for m in models if m]})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)[:300]})


    # =============================================================
    # /api/env-config — Dashboard 热更新环境变量（四块：Compress / Embed / Password / Webhook）
    # GET  返回当前值（API key 脱敏）
    # POST 批量更新：同时更新进程内 config + 写 .env 文件持久化
    # =============================================================

    # 哪些变量可以从 Dashboard 读写（不能出现在这里之外的变量）
    _ENV_CONFIG_FIELDS: dict[str, dict] = {
        # Compress / 脱水压缩
        "OMBRE_COMPRESS_API_KEY":  {"group": "compress", "sensitive": True,  "in_memory": ("dehydration", "api_key")},
        "OMBRE_COMPRESS_BASE_URL": {"group": "compress", "sensitive": False, "in_memory": ("dehydration", "base_url")},
        "OMBRE_COMPRESS_MODEL":    {"group": "compress", "sensitive": False, "in_memory": ("dehydration", "model")},
        "OMBRE_COMPRESS_FORMAT":   {"group": "compress", "sensitive": False, "in_memory": ("dehydration", "api_format")},
        "OMBRE_COMPRESS_TIMEOUT_SECONDS": {"group": "compress", "sensitive": False, "in_memory": ("dehydration", "timeout_seconds")},
        # Embed / 向量化（backend 切换走 /api/embedding/migrate）
        "OMBRE_EMBED_API_KEY":     {"group": "embed",    "sensitive": True,  "in_memory": ("embedding", "api_key")},
        "OMBRE_EMBED_BASE_URL":    {"group": "embed",    "sensitive": False, "in_memory": ("embedding", "base_url")},
        "OMBRE_EMBED_MODEL":       {"group": "embed",    "sensitive": False, "in_memory": ("embedding", "model")},
        "OMBRE_EMBED_FORMAT":      {"group": "embed",    "sensitive": False, "in_memory": ("embedding", "api_format")},
        "OMBRE_EMBED_TIMEOUT_SECONDS": {"group": "embed", "sensitive": False, "in_memory": ("embedding", "timeout_seconds")},
        # Webhook
        "OMBRE_HOOK_URL":          {"group": "webhook",  "sensitive": False, "in_memory": None},
        "OMBRE_HOOK_SKIP":         {"group": "webhook",  "sensitive": False, "in_memory": None},
        # Identity / display labels
        "AI_NAME":                 {"group": "identity", "sensitive": False, "in_memory": None},
    }

    _ENV_CONFIG_NOTE = {
        "compress": "改完即时生效（进程内 sh.config 已更新），同时写 config.yaml 持久化（重启后仍有效）。",
        "embed": "API key / base_url / model 立即更新进程内 config；backend 切换请用「切换 / 重算所有 embedding…」按钮。",
        "webhook": "改完下次 breath/dream 触发时即生效，无需重启。",
        "identity": "AI 显示名立即生效；若由平台环境变量注入，重启后仍会被平台值覆盖。",
    }


    def _mask(val: str) -> str:
        """对 API key 做脱敏，末 4 位保留供校验。"""
        if not val:
            return ""
        if len(val) > 8:
            return f"{val[:4]}...{val[-4:]}"
        return "***"


    @mcp.custom_route("/api/env-config", methods=["GET"])
    async def api_env_config_get(request: Request) -> Response:
        """
        返回四块配置的当前值（API key 脱敏显示）。
        优先读进程内 sh.config / os.environ，其次读 .env 文件。
        """
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err

        result: dict[str, dict] = {}
        for var, meta in _ENV_CONFIG_FIELDS.items():
            # 优先从 config dict 读（进程内最新）
            raw = ""
            if meta["in_memory"]:
                section, key = meta["in_memory"]
                raw = str(sh.config.get(section, {}).get(key, "")).strip()
            # 进程内为空，则读 os.environ
            if not raw:
                raw = os.environ.get(var, "").strip()
            # 再读 .env 文件
            if not raw:
                raw = sh._read_env_var(var)
            result[var] = {
                "group": meta["group"],
                "sensitive": meta["sensitive"],
                "value": _mask(raw) if meta["sensitive"] else raw,
                "is_set": bool(raw),
            }

        return JSONResponse({
            "ok": True,
            "fields": result,
            "notes": _ENV_CONFIG_NOTE,
        })


    @mcp.custom_route("/api/env-config", methods=["POST"])
    async def api_env_config_set(request: Request) -> Response:
        """
        热更新指定环境变量。

        Body (JSON): {"updates": {"OMBRE_COMPRESS_API_KEY": "sk-...", ...}}
        - 只写传入的字段，未传字段不动。
        - 空字符串 = 清除该变量（.env 里写成 NAME= ，进程内 sh.config 设为 ""）。
        - API key 不支持 "***" 保持不变（应传实际值或空字符串）。

        返回字段：
        - updated：已写入当前进程 sh.config / os.environ 的变量名；若对应
          业务引擎热更新失败，会同时出现在 warnings 中；
        - persisted：已成功落盘、重启后仍会保留的变量名；
        - partial / warnings：运行时已生效但落盘失败，或部分字段未应用。
        """
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err

        try:
            body = await sh._read_json_object(request)
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

        updates: dict = body.get("updates", {})
        if not isinstance(updates, dict) or not updates:
            return JSONResponse({"ok": False, "error": "updates 必须是非空对象"}, status_code=400)
        if len(updates) > len(_ENV_CONFIG_FIELDS):
            return JSONResponse({"ok": False, "error": "updates 字段过多"}, status_code=400)

        accepted: dict[str, str] = {}
        warnings: list[str] = []

        for var, val in updates.items():
            if var not in _ENV_CONFIG_FIELDS:
                warnings.append(f"{var}: 不在白名单里，未应用")
                continue
            if not isinstance(val, str):
                warnings.append(f"{var}: 值必须是字符串，未应用")
                continue
            if len(val) > _MAX_ENV_VALUE_CHARS:
                warnings.append(f"{var}: 值超过 {_MAX_ENV_VALUE_CHARS} 字符，未应用")
                continue
            # 拒绝明显的注入字符
            if "\n" in val or "\r" in val:
                warnings.append(f"{var}: 值不能含换行，未应用")
                continue

            value = val.strip()

            # OMBRE_HOOK_URL 只允许 http/https（防止意外配成 file:// 等非 HTTP scheme）
            if var == "OMBRE_HOOK_URL" and value and not value.startswith(("http://", "https://")):
                warnings.append(f"{var}: 只允许 http:// 或 https:// 开头的 URL，未应用")
                continue

            accepted[var] = value

        # Compress 必须按整批最终配置只重建一次 client。逐字段重建会让请求中的
        # key/base_url/model 顺序影响中间状态，也会在落盘失败时留下旧 client。
        compress_vars = [
            var for var in accepted
            if _ENV_CONFIG_FIELDS[var]["group"] == "compress"
        ]
        if compress_vars:
            current_dehy = sh.dehydrator
            current_cfg = sh.config.get("dehydration", {})
            staged_cfg = dict(current_cfg) if isinstance(current_cfg, dict) else {}
            for var in compress_vars:
                _section, key = _ENV_CONFIG_FIELDS[var]["in_memory"]
                staged_cfg[key] = accepted[var]

            try:
                if current_dehy is None:
                    raise RuntimeError("dehydrator runtime unavailable")
                staged_api_key = staged_cfg.get(
                    "api_key", getattr(current_dehy, "api_key", "")
                )
                staged_base_url = staged_cfg.get(
                    "base_url", getattr(current_dehy, "base_url", "")
                )
                staged_model = staged_cfg.get(
                    "model", getattr(current_dehy, "model", "")
                )
                staged_timeout = _positive_float(
                    staged_cfg.get("timeout_seconds"),
                    getattr(current_dehy, "timeout_seconds", 60.0),
                )
                staged_format = staged_cfg.get(
                    "api_format", getattr(current_dehy, "api_format", "openai_compat")
                )
                staged_available = bool(staged_api_key)
                staged_client = None
                if staged_available and staged_format == "openai_compat":
                    from openai import AsyncOpenAI as _OAI_DH

                    staged_client = _OAI_DH(
                        api_key=staged_api_key,
                        base_url=staged_base_url,
                        timeout=staged_timeout,
                        max_retries=0,  # 重试归 Dehydrator._chat 管，见 dehydrator.py
                    )

                staged_attrs = {
                    "api_key": staged_api_key,
                    "base_url": staged_base_url,
                    "model": staged_model,
                    "timeout_seconds": staged_timeout,
                    "api_format": staged_format,
                    "api_available": staged_available,
                    "client": staged_client,
                }
                previous_attrs = {
                    name: getattr(current_dehy, name) for name in staged_attrs
                }
                try:
                    for name, value in staged_attrs.items():
                        setattr(current_dehy, name, value)
                except Exception:
                    for name, value in previous_attrs.items():
                        try:
                            setattr(current_dehy, name, value)
                        except Exception:
                            pass
                    raise
            except Exception as e:
                failed = ", ".join(compress_vars)
                warnings.append(
                    f"压缩配置热更新失败，未应用 {failed}：{type(e).__name__}: {e}"
                )
                for var in compress_vars:
                    accepted.pop(var, None)

        # 运行时更新与持久化解耦。到这里的字段先全部对当前进程生效；后续落盘
        # 即使失败，也不能阻断 dehydrator/client 已完成的热更新。
        written: list[str] = []
        for var, value in accepted.items():
            meta = _ENV_CONFIG_FIELDS[var]
            if meta["in_memory"]:
                section, key = meta["in_memory"]
                section_cfg = sh.config.get(section)
                if not isinstance(section_cfg, dict):
                    section_cfg = {}
                    sh.config[section] = section_cfg
                section_cfg[key] = value
            if value:
                os.environ[var] = value
            else:
                os.environ.pop(var, None)
            written.append(var)

        # Embed 配置同样等整批字段进入 sh.config 后再重建一次，避免先用旧 URL
        # 建引擎、下一字段才补 model/key。失败时明确报告，不再静默吞掉。
        embed_vars = [
            var for var in written
            if _ENV_CONFIG_FIELDS[var]["group"] == "embed"
        ]
        if embed_vars:
            try:
                if (
                    "OMBRE_EMBED_API_KEY" in embed_vars
                    and not accepted["OMBRE_EMBED_API_KEY"]
                ):
                    sh.embedding_engine._backend = None  # type: ignore[attr-defined]
                    sh.embedding_engine.enabled = False
                    sh.replace_embedding_engine(sh.embedding_engine)
                else:
                    _rebuild_embedding_runtime()
            except Exception as e:
                warnings.append(
                    "向量化配置已写入进程配置，但运行时引擎重建失败："
                    f"{type(e).__name__}: {e}"
                )

        persisted: list[str] = []

        # 没有 config.yaml 映射的字段写项目 .env；失败不撤销已生效的 os.environ。
        for var in written:
            if _ENV_CONFIG_FIELDS[var]["in_memory"]:
                continue
            try:
                sh._write_env_var(var, accepted[var])
                persisted.append(var)
            except Exception as e:
                warnings.append(
                    f"{var}: 运行时已生效，但写 .env 失败，重启后可能丢失：{e}"
                )

        # 所有映射到 config.yaml 的字段一次原子落盘，保证多字段配置不会只写一半。
        yaml_vars = [
            var for var in written if _ENV_CONFIG_FIELDS[var]["in_memory"]
        ]
        if yaml_vars:
            def _persist_batch(save_config: dict) -> None:
                for var in yaml_vars:
                    section, key = _ENV_CONFIG_FIELDS[var]["in_memory"]
                    section_cfg = save_config.get(section)
                    if not isinstance(section_cfg, dict):
                        section_cfg = {}
                        save_config[section] = section_cfg
                    section_cfg[key] = accepted[var]

            try:
                atomic_update_config_yaml(_persist_batch)
                persisted.extend(yaml_vars)
            except Exception as e:
                affected = ", ".join(yaml_vars)
                warnings.append(
                    "运行时已生效，但 config.yaml 持久化失败；重启后可能恢复旧值"
                    f"（{affected}）：{type(e).__name__}: {e}"
                )

        partial = bool(warnings) or len(written) != len(updates)
        response: dict = {
            "ok": bool(written),
            "partial": bool(written) and partial,
            "updated": written,
            "persisted": persisted,
            "env_file": sh._project_env_path(),
            "note": (
                "updated 中字段已写入进程配置；若 warnings 指出引擎重建失败，"
                "则对应业务引擎尚未生效。仅 persisted 中的字段确认已落盘。"
                if partial
                else "当前进程运行时与持久化配置均已更新。"
            ),
        }
        if warnings:
            response["warnings"] = warnings
        if not written:
            response["error"] = warnings[0] if warnings else "没有字段成功更新"
        return JSONResponse(response)


    # --- 传输模式热切换：streamable-http / stdio ---
    # transport 是「启动时绑定」的（server.py 据此起 streamable_http_app / stdio），
    # 运行中无法无缝切换，所以这里的做法是：持久化新值 → 原地自重启（os.execv 继承已改的
    # os.environ，绕过 compose 里硬编码的旧 OMBRE_TRANSPORT）→ 新进程按新 transport 起。
    # 2026-08-09 起 legacy SSE（"sse"）已下线，不再是可选项。
    _TRANSPORT_CHOICES = ("streamable-http", "stdio")

    @mcp.custom_route("/api/transport", methods=["POST"])
    async def api_transport_set(request: Request) -> Response:
        """切换 MCP 传输模式并自重启生效。

        Body (JSON): {"transport": "streamable-http" | "stdio"}

        ⚠️ stdio 没有 HTTP 服务：切到 stdio 后 Dashboard / REST / /mcp(HTTP) 全部消失，
        且无法再从网页切回（需在服务器改 config.yaml / env 恢复）。前端对此二次确认。
        """
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = await sh._read_json_object(request)
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

        new_t = str(body.get("transport") or "").strip()
        if new_t not in _TRANSPORT_CHOICES:
            return JSONResponse(
                {"ok": False, "error": f"transport 必须是 {list(_TRANSPORT_CHOICES)} 之一"},
                status_code=400,
            )

        current = str(sh.config.get("transport", "stdio"))
        if new_t == current:
            return JSONResponse({"ok": True, "transport": new_t, "restarting": False,
                                 "note": "传输模式未变化，无需重启。"})

        # 1. 运行时 config + os.environ（os.execv 自重启会继承 environ，
        #    从而盖过 docker-compose 里硬编码的旧 OMBRE_TRANSPORT）。
        sh.config["transport"] = new_t
        os.environ["OMBRE_TRANSPORT"] = new_t

        # 2. 持久化到项目 .env（compose 若以 ${OMBRE_TRANSPORT} 引用则容器重建也保留）。
        env_persisted = True
        try:
            sh._write_env_var("OMBRE_TRANSPORT", new_t)
        except Exception:
            env_persisted = False

        # 3. 持久化到 config.yaml（裸机 / 无 env 覆盖时的权威来源）。
        try:
            atomic_update_config_yaml(lambda saved: saved.__setitem__("transport", new_t))
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"写 config.yaml 失败：{e}"}, status_code=500)

        # 4. 延迟自重启，让本次响应先回到前端（参照 /api/do-update 的重启节奏）。
        import threading

        def _do_restart() -> None:
            try:
                os.execv(sys.executable, [sys.executable] + sys.argv)
            except Exception:
                os._exit(0)

        threading.Timer(1.0, _do_restart).start()
        logger.info(f"[transport] 切换 {current} → {new_t}，1s 后自重启生效")
        return JSONResponse({
            "ok": True,
            "transport": new_t,
            "previous": current,
            "restarting": True,
            "env_persisted": env_persisted,
            "loses_http": new_t == "stdio",
        })
