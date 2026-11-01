"""
========================================
utils.py — 整个项目共享的小工具集合
========================================

配置加载、日志初始化、路径安全校验、ID 生成、token 估算、时间格式化——
所有「跨模块要用、又不属于任何业务逻辑」的小函数都在这里。

关键行为：
- load_config()：读 config.yaml，处理环境变量覆盖（OMBRE_VAULT_DIR 等），mkdir 必要目录
- setup_logger()：统一日志格式，控制台 + 可选文件
- safe_path()：禁止路径穿越（OWASP）
- generate_bucket_id()：12 位 hex，碰撞概率忽略
- count_tokens_approx()：按字符数粗估 token，离线用
- now_iso() / parse_iso()：统一时间字符串

不做什么（边界）：
- 不依赖任何业务模块（被所有模块依赖，不能反向 import）
- 不做 LLM / 网络调用
- 不做记忆桶相关业务逻辑

对外暴露：上述所有函数
========================================
"""

import errno
import os
import re
import sys
import uuid
import json
import yaml
import logging
import math
import tempfile
import threading
import unicodedata
from pathlib import Path
from datetime import date, datetime
from typing import Callable, Optional


# ============================================================
# 常量 / Named constants
# ------------------------------------------------------------
# rule.md §⑩：禁止裸魔法数字。下面这几个值原本散在函数体内，
# 抽到这里是为了：① 一眼能看清"调参面板"；② 改一处全文生效。
# ============================================================

# count_tokens_approx() 用的粗估系数。
# 经验值，不追求精确——只为判断"是否需要脱水压缩"。
_TOKEN_RATIO_PER_CN_CHAR = 1.5   # 每个中文字 ≈ 1.5 token
_TOKEN_RATIO_PER_EN_WORD = 1.3   # 每个英文词 ≈ 1.3 token
_TOKEN_RATIO_PER_CHAR = 0.05     # 标点/空格等其它字符的兜底贡献

# setup_logging() 文件日志轮转配置。
_LOG_FILE_MAX_BYTES = 1_000_000  # 单个日志文件 1 MB 后轮转
_LOG_FILE_BACKUP_COUNT = 3       # 保留 3 个历史文件
_LOG_FALLBACK_DIR = os.path.join(tempfile.gettempdir(), "ombre_logs")

# sanitize_name() 桶名最大长度（防止文件名过长导致 OS 报错）。
_BUCKET_NAME_MAX_LEN = 80
MEMORY_TITLE_MAX_CHARS = 120

_BOOL_TRUE = frozenset({"1", "true", "yes", "on"})
_BOOL_FALSE = frozenset({"0", "false", "no", "off"})

# 进程启动那一刻就被「真实 OS / 平台」注入的可配置环境变量名集合（值非空才算）。
# 在任何 dashboard 保存动作 mutate os.environ 之前快照——这是「平台级 env」与
# 「运行时被 dashboard 写进 os.environ 的值」唯一可靠的区分依据。
# 用途：dashboard 据此提示「这些字段由平台环境变量提供，重启会覆盖你这里保存的值」，
# 修复「config.yaml 存了 Gemini，但平台 OMBRE_COMPRESS_BASE_URL=DeepSeek 每次重启盖回」的坑。
BOOT_ENV_CONFIG: frozenset[str] = frozenset(
    k for k, v in os.environ.items()
    if (k.startswith("OMBRE_") or k == "AI_NAME") and str(v).strip()
)
def _project_root() -> str:
    """Return absolute path to the project root (parent of src/ where utils.py lives).
    项目根目录（src/ 的上一层）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


_config_yaml_lock = threading.RLock()


def _migrate_legacy_render_config(legacy_path: str, persistent_path: str) -> None:
    """Copy an old Render cwd config into the data disk without overwriting it.

    Render services created before ``OMBRE_CONFIG_PATH`` was added already have
    ``OMBRE_BUCKETS_DIR`` pointing at the persistent disk, but Dashboard hot
    updates do not apply new ``render.yaml`` environment variables.  Those
    instances therefore kept writing ``<cwd>/config.yaml`` on Render's
    ephemeral code filesystem.  Validate the legacy YAML, then publish a copy
    through a same-directory temporary file so an interrupted migration cannot
    leave a partial persistent config.  The legacy file is intentionally kept
    as a rollback copy.
    """
    legacy_abs = os.path.abspath(legacy_path)
    persistent_abs = os.path.abspath(persistent_path)
    if os.path.normcase(legacy_abs) == os.path.normcase(persistent_abs):
        return

    tmp = ""
    with _config_yaml_lock:
        if os.path.exists(persistent_abs) or not os.path.isfile(legacy_abs):
            return
        try:
            with open(legacy_abs, "r", encoding="utf-8") as source:
                legacy_config = yaml.safe_load(source) or {}
            if not isinstance(legacy_config, dict):
                raise ValueError("legacy config.yaml top level is not a mapping")

            parent = os.path.dirname(persistent_abs)
            os.makedirs(parent, exist_ok=True)
            descriptor, tmp = tempfile.mkstemp(
                prefix=f".{os.path.basename(persistent_abs)}.migrate.",
                dir=parent,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
                yaml.safe_dump(
                    legacy_config,
                    target,
                    allow_unicode=True,
                    default_flow_style=False,
                )
                target.flush()
                os.fsync(target.fileno())

            # Publish with true no-clobber semantics.  ``exists`` followed by
            # ``replace`` has a TOCTOU window and can overwrite a config another
            # worker creates between those calls.  The temp file lives in the
            # same directory/filesystem, so link(2) atomically either creates
            # the target name or raises FileExistsError without touching it.
            try:
                os.link(tmp, persistent_abs)
            except FileExistsError:
                pass
        except Exception as exc:
            logging.warning(
                "Failed to migrate Render config.yaml from %s to %s: %s",
                legacy_abs,
                persistent_abs,
                exc,
            )
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


def config_file_path() -> str:
    """config.yaml 的绝对路径 —— 读 / 写 / entrypoint 三方共用的单一真相。

    顺序：
      1. $OMBRE_CONFIG_PATH —— 显式指定即采纳，**即便文件尚不存在**
         （entrypoint 会在服务启动前据此创建；Dashboard 写配置时也据此落盘）。
      2. Render 旧实例未设 OMBRE_CONFIG_PATH 时，跟随已有的
         OMBRE_BUCKETS_DIR / OMBRE_VAULT_DIR 落到持久盘，并安全复制旧 cwd 配置。
      3. <cwd>/config.yaml —— 存在才用。
      4. <project_root>/config.yaml —— 兜底默认。

    为什么独立成函数：load_config 读、Dashboard（config_api/buckets/github/
    embedding）写、entrypoint 初始化——以前各处都硬编码 <repo_root>/config.yaml。
    一旦把 config 挪进数据目录（修 Docker 单文件 bind mount 在 Windows 被建成
    目录、容器崩溃重启的坑），读和写就会分叉到不同路径、Dashboard 存的 key 重启即丢。
    统一到这里，OMBRE_CONFIG_PATH 一处生效、读写永远同一个文件。"""
    env_cfg = os.environ.get("OMBRE_CONFIG_PATH", "").strip()
    if env_cfg:
        return env_cfg

    is_render = str(os.environ.get("RENDER", "")).strip().lower() in _BOOL_TRUE
    render_data_dir = (
        os.environ.get("OMBRE_BUCKETS_DIR", "").strip()
        or os.environ.get("OMBRE_VAULT_DIR", "").strip()
    )
    if is_render and render_data_dir:
        persistent_cfg = os.path.join(
            os.path.abspath(os.path.expanduser(render_data_dir)),
            "config.yaml",
        )
        _migrate_legacy_render_config(
            os.path.join(os.getcwd(), "config.yaml"),
            persistent_cfg,
        )
        return persistent_cfg

    cwd_cfg = os.path.join(os.getcwd(), "config.yaml")
    if os.path.exists(cwd_cfg):
        return cwd_cfg
    return os.path.join(_project_root(), "config.yaml")


# 所有往 config.yaml 写东西的 Dashboard 接口（github/tunnel/config_api/buckets……）
# 共用这一把锁 + 同一套原子写：谁都不能绕开它自己再开 open(path, "w") 整份覆盖。
# 背景：github 备份配置曾经用「open(w) 直接整份覆盖、写失败只记 warning 但仍回 200」
# 这种不安全写法，写失败时用户会看到「保存成功」、下次重启却发现配置又清空了。


_MOUNTINFO_ESCAPES = {
    "040": " ",
    "011": "\t",
    "012": "\n",
    "134": "\\",
}


def _decode_mountinfo_path(value: str) -> str:
    return re.sub(
        r"\\(040|011|012|134)",
        lambda match: _MOUNTINFO_ESCAPES[match.group(1)],
        value,
    )


def _is_exact_linux_mount_point(path: str) -> bool:
    """Return whether ``path`` is a regular-file mount point on Linux."""
    if not sys.platform.startswith("linux") or not os.path.isfile(path):
        return False
    target = os.path.realpath(os.path.abspath(path))
    try:
        with open("/proc/self/mountinfo", "r", encoding="utf-8") as mountinfo:
            for line in mountinfo:
                fields = line.split()
                if len(fields) > 4:
                    mounted_at = _decode_mountinfo_path(fields[4])
                    if os.path.realpath(mounted_at) == target:
                        return True
    except OSError:
        return False
    return False


def _write_bytes_and_sync(path: str, payload: bytes) -> None:
    with open(path, "wb") as target:
        target.write(payload)
        target.flush()
        os.fsync(target.fileno())


def _overwrite_mounted_config(tmp: str, config_path: str) -> bytes:
    """Overwrite an unreplaceable file mount and return bytes for rollback."""
    with open(config_path, "rb") as current:
        previous_payload = current.read()
    with open(tmp, "rb") as source:
        next_payload = source.read()
    try:
        _write_bytes_and_sync(config_path, next_payload)
    except Exception as write_error:
        try:
            _write_bytes_and_sync(config_path, previous_payload)
        except Exception as restore_error:
            raise OSError(
                "config.yaml bind-mount write failed and restoring the previous "
                f"file also failed: {restore_error}"
            ) from write_error
        raise
    return previous_payload


def read_config_yaml() -> dict:
    """Read the persisted config under the same lock used by atomic writers.

    A normal ``os.replace`` reader is already protected from partial files, but
    Docker's single-file bind-mount fallback must overwrite the mounted inode
    in place.  Sharing ``_config_yaml_lock`` prevents Dashboard readers from
    observing that short write window and gives every route one desired-config
    snapshot instead of stale per-route caches.
    """
    config_path = config_file_path()
    with _config_yaml_lock:
        if not os.path.exists(config_path):
            return {}
        with open(config_path, "r", encoding="utf-8") as handle:
            persisted = yaml.safe_load(handle) or {}
        if not isinstance(persisted, dict):
            raise ValueError("config.yaml top level must be a mapping")
        return persisted


def atomic_update_config_yaml(mutate: Callable[[dict], None]) -> dict:
    """线程安全地读改写 config.yaml：加锁读现有内容，交给 ``mutate`` 原地 patch。

    普通文件通过临时文件 + ``os.replace`` 原子落盘。Docker 的旧式单文件
    bind mount 是一个不可替换的挂载点，Linux 会对 ``os.replace`` 返回
    ``EBUSY``；这种情况下退回到锁内覆盖、flush + fsync，并继续执行同一套
    回读校验。降级路径不具备崩溃原子性，但能兼容无法 rename 的挂载点，且
    仍由全局锁避免应用内部的并发读改写互相覆盖。

    任何一步失败都直接抛异常——调用方必须把异常转成对用户如实的错误响应，
    不能吞掉后仍然回「保存成功」，那样用户会以为配置在，其实只在内存里，
    下次进程重启（崩溃/热更新/手动重启按钮）就会被磁盘上没写成功的旧内容盖掉。

    返回值是写入后的完整 config dict（等价于重新读盘一次）。"""
    config_path = config_file_path()
    tmp = ""
    with _config_yaml_lock:
        save_config: dict = {}
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                save_config = yaml.safe_load(f) or {}
        if not isinstance(save_config, dict):
            save_config = {}
        mutate(save_config)
        try:
            parent = os.path.dirname(os.path.abspath(config_path))
            os.makedirs(parent, exist_ok=True)
            descriptor, tmp = tempfile.mkstemp(
                prefix=f".{os.path.basename(config_path)}.tmp.",
                dir=parent,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as f:
                yaml.dump(save_config, f, allow_unicode=True, default_flow_style=False)
                f.flush()
                os.fsync(f.fileno())
            fallback_backup: bytes | None = None
            try:
                os.replace(tmp, config_path)
            except OSError as e:
                if (
                    e.errno != errno.EBUSY
                    or not _is_exact_linux_mount_point(config_path)
                ):
                    raise
                # A bind-mounted file is itself a mount point and cannot be
                # be replaced with rename(2).  It remains writable, so perform
                # a lock-protected overwrite while keeping the old bytes for
                # rollback if the write or verification fails.
                fallback_backup = _overwrite_mounted_config(tmp, config_path)
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    persisted = yaml.safe_load(f) or {}
                if persisted != save_config:
                    raise OSError("config.yaml verification failed after write")
            except Exception as verify_error:
                if fallback_backup is not None:
                    try:
                        _write_bytes_and_sync(config_path, fallback_backup)
                    except Exception as restore_error:
                        raise OSError(
                            "config.yaml verification failed and restoring the "
                            f"previous bind-mounted file also failed: {restore_error}"
                        ) from verify_error
                raise
        finally:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except OSError:
                pass
        return save_config


def parse_bool(value, *, default=...) -> bool:
    """Parse an explicit boolean without Python's ``bool('false')`` trap.

    JSON/YAML callers may supply booleans, 0/1, or common textual forms. Other
    values are rejected unless a default is supplied. This keeps public API
    boundaries predictable while still accepting environment-style strings.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _BOOL_TRUE:
            return True
        if normalized in _BOOL_FALSE:
            return False
    # Ellipsis is a process-wide singleton, so this check remains valid even
    # if a hot-update/test reloads utils while callers retain the old function.
    if default is not ...:
        return bool(default)
    raise ValueError(f"expected boolean value, got {value!r}")


def parse_iso_datetime(value) -> datetime:
    """Parse ISO/date metadata into a timezone-compatible local datetime.

    OB historically stores naive local timestamps, while imported/frontmatter
    data may contain ``Z`` or explicit offsets. Converting aware values to local
    time and dropping ``tzinfo`` lets existing ``datetime.now()`` comparisons
    remain correct instead of treating valid timestamps as corrupt data.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    else:
        raw = str(value or "").strip()
        if not raw:
            raise ValueError("empty datetime")
        if raw[-1:].lower() == "z":
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def load_config(config_path: Optional[str] = None) -> dict:
    """
    Load configuration file.
    加载配置文件。

    Priority: environment variables > config.yaml > built-in defaults.
    优先级：环境变量 > config.yaml > 内置默认值。
    """
    project_root = _project_root()
    # --- Built-in defaults (fallback so it runs even without config.yaml) ---
    # --- 内置默认配置（兜底，保证即使没有 config.yaml 也能跑）---
    defaults = {
        "transport": "stdio",
        "log_level": "INFO",
        "mcp_require_auth": True,
        # 只有 mcp_require_auth=true 时才生效：OAuth、静态 Token 或两者共存。
        "mcp_auth_mode": "oauth",
        "mcp_token": "",
        "buckets_dir": os.path.join(project_root, "buckets"),
        "merge_threshold": 75,
        "dehydration": {
            "model": "gemini-2.0-flash",
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "api_key": "",
            "max_tokens": 4096,
            "temperature": 0.1,
            "timeout_seconds": 60,
        },
        "decay": {
            "lambda": 0.05,
            "threshold": 0.3,
            "check_interval_hours": 24,
            "emotion_weights": {
                "base": 1.0,
                "arousal_boost": 0.8,
            },
        },
        "matching": {
            "fuzzy_threshold": 50,
            "max_results": 5,
        },
        "storage": {
            "external_change_poll_seconds": 1.0,
        },
        "embedding": {
            "enabled": True,
            "background_indexing": True,
            "retry_base_seconds": 5,
            "retry_max_seconds": 300,
            "circuit_failure_threshold": 3,
            "circuit_base_seconds": 30,
            "circuit_max_seconds": 600,
        },
    }

    # --- Load user config from YAML file ---
    # --- 从 YAML 文件加载她/他的自定义配置 ---
    if config_path is None:
        # 读写共用同一解析逻辑（config_file_path）：$OMBRE_CONFIG_PATH > cwd > project_root。
        config_path = config_file_path()

    config = defaults.copy()
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                file_config = yaml.safe_load(f) or {}
            if isinstance(file_config, dict):
                config = _deep_merge(defaults, file_config)
            else:
                logging.warning(
                    f"Config file is not a valid YAML dict, using defaults / "
                    f"配置文件不是有效的 YAML 字典，使用默认配置: {config_path}"
                )
        except yaml.YAMLError as e:
            logging.warning(
                f"Failed to parse config file, using defaults / "
                f"配置文件解析失败，使用默认配置: {e}"
            )

    # Normalize YAML booleans before environment overrides. Quoted values such
    # as mcp_require_auth: "false" must not become truthy via bool("false").
    config["mcp_require_auth"] = parse_bool(
        config.get("mcp_require_auth", True), default=True
    )

    # --- Environment variable overrides (highest priority) ---
    # --- 环境变量覆盖敏感/运行时配置（优先级最高）---
    # 这里曾经有 6 段几乎一模一样的 if-block，每段都在做同一件事：
    #   "若环境变量非空 → 写到 config 的某个嵌套 key 上"
    # 现在统一走 _apply_env_override()，新增一项只要加一行表项。

    # v1.x 兼容：旧变量不得因重构而静默失效。新变量显式设置时始终优先。
    legacy_api_key = os.environ.get("OMBRE_API_KEY", "").strip()
    legacy_base_url = os.environ.get("OMBRE_BASE_URL", "").strip()
    if legacy_api_key and not os.environ.get("OMBRE_COMPRESS_API_KEY", "").strip():
        config.setdefault("dehydration", {})["api_key"] = legacy_api_key
        logging.warning(
            "OMBRE_API_KEY 是兼容变量；请迁移到 OMBRE_COMPRESS_API_KEY，旧名仍会继续生效。"
        )
    if legacy_base_url and not os.environ.get("OMBRE_COMPRESS_BASE_URL", "").strip():
        config.setdefault("dehydration", {})["base_url"] = legacy_base_url
        logging.warning(
            "OMBRE_BASE_URL 是兼容变量；请迁移到 OMBRE_COMPRESS_BASE_URL，旧名仍会继续生效。"
        )

    # v1.3 Zeabur 模板曾使用通用 PASSWORD；只在正式变量缺失时兼容映射。
    legacy_password = os.environ.get("PASSWORD", "").strip()
    if legacy_password and not os.environ.get("OMBRE_DASHBOARD_PASSWORD", "").strip():
        os.environ["OMBRE_DASHBOARD_PASSWORD"] = legacy_password
        logging.warning(
            "PASSWORD 是兼容变量；请迁移到 OMBRE_DASHBOARD_PASSWORD，旧名仍会继续生效。"
        )

    # 压缩组（脱水/打标/合并）—— 写到 config["dehydration"][*]
    _apply_env_override(config, "OMBRE_COMPRESS_API_KEY", "dehydration", "api_key")
    _apply_env_override(config, "OMBRE_COMPRESS_BASE_URL", "dehydration", "base_url")
    _apply_env_override(config, "OMBRE_COMPRESS_MODEL", "dehydration", "model")
    # Accept both names: OMBRE_COMPRESS_FORMAT (dashboard) and OMBRE_COMPRESS_API_FORMAT (legacy)
    _apply_env_override(config, "OMBRE_COMPRESS_FORMAT", "dehydration", "api_format")
    _apply_env_override(config, "OMBRE_COMPRESS_API_FORMAT", "dehydration", "api_format")
    _apply_env_float_override(config, "OMBRE_COMPRESS_TIMEOUT_SECONDS", "dehydration", "timeout_seconds")

    # 向量化组（embedding）—— 写到 config["embedding"][*]
    _apply_env_override(config, "OMBRE_EMBED_API_KEY", "embedding", "api_key")
    _apply_env_override(config, "OMBRE_EMBED_BASE_URL", "embedding", "base_url")
    _apply_env_override(config, "OMBRE_EMBED_MODEL", "embedding", "model")
    _apply_env_override(config, "OMBRE_EMBED_FORMAT", "embedding", "api_format")
    _apply_env_float_override(config, "OMBRE_EMBED_TIMEOUT_SECONDS", "embedding", "timeout_seconds")

    # Obsidian / Git / manual Markdown edits cache poll interval.
    _apply_env_float_override(
        config,
        "OMBRE_EXTERNAL_CHANGE_POLL_SECONDS",
        "storage",
        "external_change_poll_seconds",
    )

    # 顶层运行时
    _apply_env_override(config, "OMBRE_TRANSPORT", "transport")
    # transport 名归一化 —— 单一真源，让 server.py / 诊断接口拿到的都是规范值。
    # 背景：远程接入（Operit / 安卓 / 自建前端等）该填 "streamable-http"，但很多人凭
    # 直觉写成 "http" / "streamable_http" / "streamablehttp" 等变体；server.py 的入口用
    # `transport == "streamable-http"` 精确匹配，写错就悄悄退回 stdio ——
    # 于是根本不开 HTTP 服务、客户端一直连不上（Operit 表现为黄灯）。这里把所有等价写法
    # 收敛成规范的 "streamable-http"，避免因一个连字符/下划线的差异排查半天。
    # 只收敛已知别名；不认识的值原样保留，交给 server.py 入口的显式分支报明确的错。
    # 2026-08-09 起 legacy SSE transport（"sse"）已下线，不再是已知别名——传这个值
    # 会原样落到 server.py 入口的拒绝分支，退出并给出清晰报错，不会被 FastMCP 自带的
    # mcp.run(transport="sse") 悄悄接住（那条路径不受 Ombre Brain 的鉴权/CORS 中间件保护）。
    _raw_transport = str(config.get("transport", "stdio")).strip().lower()
    _transport_aliases = {
        "http": "streamable-http",
        "streamable": "streamable-http",
        "streamable_http": "streamable-http",
        "streamablehttp": "streamable-http",
        "streamable-http": "streamable-http",
        "http-stream": "streamable-http",
        "streaming": "streamable-http",
        "stdio": "stdio",
    }
    config["transport"] = _transport_aliases.get(_raw_transport, _raw_transport)
    _apply_env_override(config, "OMBRE_BUCKETS_DIR", "buckets_dir")
    env_buckets_dir = os.environ.get("OMBRE_BUCKETS_DIR", "")

    # MCP 鉴权开关（布尔，单独处理）—— OMBRE_MCP_REQUIRE_AUTH
    # 不能走 _apply_env_override：它只写字符串，而鉴权中间件和诊断接口都要求
    # 配置中保存真正的 bool；否则字符串 "false" 仍可能被普通真值判断误当成开启。
    # 用途：把 OB 接进同机自有前端等不走 OAuth 的客户端时，可在明确回环边界关闭鉴权；
    # 局域网/NAS/公网应改用 OAuth 或静态 Token，启动期网络门禁还会统一复核真实边界。
    # 仅在显式设置为可识别的值时才覆盖；不设 / 设成乱七八糟的值都保持默认（安全：默认开启）。
    _env_mcp_auth = os.environ.get("OMBRE_MCP_REQUIRE_AUTH", "").strip()
    if _env_mcp_auth:
        config["mcp_require_auth"] = parse_bool(
            _env_mcp_auth, default=config["mcp_require_auth"]
        )

    # MCP 鉴权模式（枚举，仅 mcp_require_auth=true 时生效）—— mcp_auth_mode / OMBRE_MCP_AUTH_MODE
    # "oauth"（默认）沿用 OAuth 2.1 + PKCE；"token" 只走静态密钥；"hybrid" 同时接受两者。
    # 只有纯 token 模式会关闭 OAuth 的 discovery/register/authorize/token 路由（见 web/oauth.py）。
    # 不能走 _apply_env_override：这里需要做枚举校验，非法值一律回退默认 "oauth"。
    _raw_auth_mode = str(config.get("mcp_auth_mode", "oauth")).strip().lower()
    config["mcp_auth_mode"] = (
        _raw_auth_mode
        if _raw_auth_mode in ("oauth", "token", "hybrid")
        else "oauth"
    )
    _env_mcp_auth_mode = os.environ.get("OMBRE_MCP_AUTH_MODE", "").strip().lower()
    if _env_mcp_auth_mode in ("oauth", "token", "hybrid"):
        config["mcp_auth_mode"] = _env_mcp_auth_mode

    _apply_env_override(config, "OMBRE_MCP_TOKEN", "mcp_token")

    # 安全兜底：选了 token 模式却没配密钥——宁可继续用更强的 OAuth 兜底，也不要让用户
    # 误以为已经开了保护、实际上 /mcp 会因校验函数拿不到密钥而被意外锁死或裸奔。
    if config["mcp_auth_mode"] == "token" and not str(config.get("mcp_token") or "").strip():
        logging.warning(
            "mcp_auth_mode=token 但未配置 mcp_token / OMBRE_MCP_TOKEN，已自动回退为 oauth 模式 / "
            "mcp_auth_mode=token but no mcp_token/OMBRE_MCP_TOKEN configured — falling back to oauth"
        )
        config["mcp_auth_mode"] = "oauth"
    elif config["mcp_auth_mode"] == "hybrid" and not str(config.get("mcp_token") or "").strip():
        logging.warning(
            "mcp_auth_mode=hybrid 但尚未配置静态 Token；OAuth 仍可用，静态 Token 入口将在配置密钥后生效 / "
            "mcp_auth_mode=hybrid has no static token yet; OAuth remains available"
        )

    # iter 1.9 F: 统一推荐 OMBRE_VAULT_DIR；老变量 OMBRE_BUCKETS_DIR 仍兼容
    # Priority: OMBRE_BUCKETS_DIR (legacy explicit) > OMBRE_VAULT_DIR > config.yaml.buckets_dir
    # We keep BUCKETS_DIR with higher priority than VAULT_DIR for two reasons:
    #   1) Existing tests use monkeypatch.setenv("OMBRE_BUCKETS_DIR", ...) extensively;
    #      flipping priority would break them when conftest also sets VAULT_DIR globally.
    #   2) Anyone who already had BUCKETS_DIR working should keep working unchanged.
    # New users / new docs should prefer OMBRE_VAULT_DIR; both names map to the same path.
    env_vault_dir = os.environ.get("OMBRE_VAULT_DIR", "")
    if env_vault_dir and not env_buckets_dir:
        config["buckets_dir"] = env_vault_dir
    elif env_buckets_dir and not env_vault_dir:
        # Only legacy var set — emit one INFO hint so users know about the new name.
        try:
            import logging as _logging
            _logging.getLogger(__name__).info(
                "OMBRE_BUCKETS_DIR is the legacy name; OMBRE_VAULT_DIR is preferred "
                "/ 旧变量 OMBRE_BUCKETS_DIR 仍可用，但建议改用 OMBRE_VAULT_DIR"
            )
        except Exception:
            pass

    # 空白的 buckets_dir 会让下面的 join 落到当前工作目录：记忆库看起来是空的，
    # 新记忆散进 cwd。环境变量那条路早就把空字符串当"未设置"了
    # （见 _apply_env_override），config.yaml 里写 buckets_dir: "" 走的是另一条
    # 路，一直没这条规矩。统一成同一个约定。
    if not str(config.get("buckets_dir") or "").strip():
        config["buckets_dir"] = defaults["buckets_dir"]
        logging.getLogger(__name__).warning(
            "buckets_dir 是空的，已退回默认值 %s（空值会让记忆散进当前工作目录）",
            config["buckets_dir"],
        )

    # 媒体必须和记忆一起落在持久卷；默认使用数据目录下独立的 _media。
    # OMBRE_MEDIA_DIR 仅在确实挂载了另一块持久盘时覆盖。
    media_dir = os.environ.get("OMBRE_MEDIA_DIR", "").strip()
    config["media_dir"] = media_dir or os.path.join(str(config["buckets_dir"]), "_media")
    try:
        config["media_max_bytes"] = max(
            1,
            int(os.environ.get("OMBRE_MEDIA_MAX_BYTES", 25 * 1024 * 1024)),
        )
    except (TypeError, ValueError, OverflowError):
        config["media_max_bytes"] = 25 * 1024 * 1024

    # --- Ensure bucket storage directories exist ---
    # --- 确保记忆桶存储目录存在 ---
    buckets_dir: str = str(config["buckets_dir"])
    for subdir in ["permanent", "dynamic", "archive"]:
        os.makedirs(os.path.join(buckets_dir, subdir), exist_ok=True)
    os.makedirs(str(config["media_dir"]), exist_ok=True)

    return config


def _deep_merge(base: dict, override: dict) -> dict:
    """
    Deep-merge two dicts; override values take precedence.
    深度合并两个字典，override 的值覆盖 base。
    """
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _apply_env_override(config: dict, env_name: str, *path: str) -> None:
    """把单个环境变量按 path 写入嵌套 dict（仅当值非空）。

    设计原因：load_config() 里曾有 6 段几乎一模一样的覆盖代码——
        env = os.environ.get("XXX", "")
        if env:
            config["a"]["b"] = env
    长度膨胀且新增一项就要再抄一遍。统一抽出后：
      * 新增覆盖只要写一行 `_apply_env_override(config, "OMBRE_FOO", "a", "b")`
      * 行为一致：空字符串视为"未设置"，绝不覆盖默认值
      * 自动 setdefault 中间层 dict，避免 KeyError

    参数：
        config   ：被修改的配置字典（in-place）
        env_name ：环境变量名
        *path    ：嵌套 key 路径。一层 key 传 1 个，两层传 2 个。
                   例如 ("dehydration", "api_key") 会写到
                   config["dehydration"]["api_key"]。

    边界（rule.md §⑨ 防御式编程）：
      * 环境变量为空 / 未设置 → 直接 return，不动 config
      * path 为空 → 直接 return（调用方写错路径不应静默覆盖整个 config）
    """
    value = os.environ.get(env_name, "").strip()
    if not value or not path:
        return
    # 走到倒数第二层，逐层 setdefault 出嵌套 dict
    cursor = config
    for key in path[:-1]:
        cursor = cursor.setdefault(key, {})
    cursor[path[-1]] = value


def positive_float(value, default: float) -> float:
    """Parse a positive numeric config value, falling back to default."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(parsed) or parsed <= 0:
        return float(default)
    return parsed


def _apply_env_float_override(config: dict, env_name: str, *path: str) -> None:
    value = os.environ.get(env_name, "").strip()
    if not value or not path:
        return
    try:
        parsed = float(value)
    except ValueError:
        return
    if not math.isfinite(parsed) or parsed <= 0:
        return
    cursor = config
    for key in path[:-1]:
        cursor = cursor.setdefault(key, {})
    cursor[path[-1]] = int(parsed) if parsed.is_integer() else parsed


def clean_llm_json(raw: str) -> str:
    """Return the first complete JSON value from an LLM response.

    Models sometimes wrap JSON in Markdown fences or add a short sentence before
    or after it. Keep strict JSON validation in callers, but make the extraction
    step tolerant enough to recover the balanced array/object.
    """
    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    decoder = json.JSONDecoder()
    for idx, ch in enumerate(cleaned):
        if ch not in "[{":
            continue
        try:
            _value, end = decoder.raw_decode(cleaned[idx:])
        except json.JSONDecodeError:
            continue
        return cleaned[idx:idx + end].strip()
    return cleaned


def _resolve_log_dir(explicit: str | None) -> str:
    """决定 server.log 落到哪个目录。

    优先级（rule.md §1.13 + iter 1.6 §3）：
        explicit 参数 > $OMBRE_LOG_DIR > <buckets_dir>/.logs > /tmp 兜底

    抽出来的原因：原 setup_logging() 内联了 4 段 if-fallback，逻辑分支
    挤在一起读不清。独立后单元测试可以直接打它，且改优先级不必动
    setup_logging 主体。
    """
    if explicit:
        return explicit
    env_dir = os.environ.get("OMBRE_LOG_DIR", "").strip()
    if env_dir:
        return env_dir
    bd = os.environ.get("OMBRE_BUCKETS_DIR", "").strip()
    if bd:
        return os.path.join(bd, ".logs")
    return _LOG_FALLBACK_DIR


def setup_logging(level: str = "INFO", log_dir: str | None = None) -> None:
    """
    Initialize logging system.
    初始化日志系统。

    Note: In MCP stdio mode, stdout is occupied by the protocol;
    logs must go to stderr.
    注意：MCP stdio 模式下 stdout 被协议占用，日志只能走 stderr。

    iter 1.6 §3：除 stderr 外，同时写一份 ``server.log``（RotatingFileHandler）。
    Dashboard 的「日志」标签页通过 ``/api/logs`` 读取这个文件，方便她/他在网页上
    直接看 ERROR/WARNING。日志路径优先级：
        log_dir 参数 > 环境变量 OMBRE_LOG_DIR > <buckets_dir>/.logs > /tmp/ombre_logs
    """
    log_level = getattr(logging, level.upper(), None)
    if not isinstance(log_level, int):
        log_level = logging.INFO

    handlers: list[logging.Handler] = [logging.StreamHandler()]  # 默认 stderr

    # ---- 文件日志（按需开启，失败时静默降级到仅 stderr）----
    chosen_dir = _resolve_log_dir(log_dir)

    try:
        from logging.handlers import RotatingFileHandler
        os.makedirs(chosen_dir, exist_ok=True)
        log_path = os.path.join(chosen_dir, "server.log")
        fh = RotatingFileHandler(
            log_path,
            maxBytes=_LOG_FILE_MAX_BYTES,
            backupCount=_LOG_FILE_BACKUP_COUNT,
            encoding="utf-8",
        )
        fh.setLevel(log_level)
        handlers.append(fh)
        # 暴露给 server.py，供 /api/logs 读取
        os.environ["OMBRE_LOG_FILE"] = log_path
    except Exception as e:
        # 文件日志失败不应阻塞服务启动
        sys.stderr.write(f"[setup_logging] file handler disabled: {e}\n")

    logging.basicConfig(
        level=log_level,
        format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )

    # 接入统一错误体系的 in-memory log buffer，给 E 级报错附 tail
    try:
        try:
            from errors import attach_log_buffer_handler  # type: ignore
        except ImportError:
            from .errors import attach_log_buffer_handler  # type: ignore
        attach_log_buffer_handler(level=log_level)
    except Exception as _e:
        sys.stderr.write(f"[setup_logging] buffer handler attach failed: {_e}\n")


def generate_bucket_id() -> str:
    """
    Generate a unique bucket ID (12-char short UUID for readability).
    生成唯一的记忆桶 ID（12 位短 UUID，方便人类阅读）。
    """
    return uuid.uuid4().hex[:12]


def strip_wikilinks(text: str) -> str:
    """
    Remove Obsidian wikilink brackets: [[word]] → word
    去除 Obsidian 双链括号
    """
    return re.sub(r"\[\[([^\]]+)\]\]", r"\1", text) if text else text


# ===============================================================
# Wikilinks / 双链解析（iter 1.7 §F1）
# ---------------------------------------------------------------
# 设计：Obsidian 用 `[[目标桶名]]` 写双向链接，可带 alias 和 section：
#   [[Memory]]                 → target = "Memory"
#   [[Memory#section]]         → target = "Memory"     (# 后是段落锚)
#   [[Memory|这件事]]          → target = "Memory"     (| 后是显示别名)
# 正则只抓「第一段」目标名；遇到 # 或 | 就停止。
# Python 小知识：
#   * re.compile 把正则预编译，反复用时比 re.findall 每次现编译快
#   * 字符类里 `[^\]\|#]+` 表示「不是 ] 不是 | 不是 # 的连续字符」
#   * (?:...)  非捕获分组，只为分支选择，不占 group 编号
# ===============================================================
_WIKILINK_RE = re.compile(r"\[\[([^\]\|#]+)(?:[#\|][^\]]*)?\]\]")


def extract_wikilinks(text: str) -> list[str]:
    """Extract Obsidian-style [[wikilinks]] target names from text.

    抽取正文里所有 `[[xxx]]` 的目标名，去重保序，去掉 `|alias` 和 `#section`。
    返回 list[str]（不是 set，因为下游希望保持出现顺序）。

    Example / 例：
        >>> extract_wikilinks("see [[A]] and [[B|别名]] also [[A]]")
        ['A', 'B']
    """
    # 防御：传 None 或空串直接返回空列表，避免下游 for 循环崩
    if not text:
        return []
    # 用 list + 手工查重而不是 set()，是为了保留首次出现顺序
    # （Python 3.7+ 的 dict 也保序，用 dict.fromkeys 也行，这里写法更直观）
    seen: list[str] = []
    for m in _WIKILINK_RE.finditer(text):
        target = m.group(1).strip()  # group(1) = 第一个括号 ([^\]\|#]+) 抓到的内容
        if target and target not in seen:
            seen.append(target)
    return seen


def get_version() -> str:
    """Read project version from `<repo_root>/VERSION`.

    存在两份 VERSION：src/VERSION 与根目录 VERSION。读取顺序：src/VERSION 优先。
    任何路径都读不到时返回 "0.0.0+unknown"，方便排查。

    ⚠️ 为什么是 src 优先（别再改成根目录优先）：
      热更新（web/meta.py do-update）解压时只覆盖 src/ 和 frontend/，所以 src/VERSION
      一定被刷新，而很多用户的根目录 VERSION 是历史安装遗留的老版本（从没人读、也没人更）。
      若改成根目录优先，用户一更新就会读到那个尘封的旧根 VERSION → 版本号当场倒退
      （2.3.10 真踩过：有人从 2.3.8 更新后显示成 2.1.3）。
      一致性由 do-update「强制把 zip 的根 VERSION 同写到两处」保证；这里只管读那个
      最可靠新鲜的 src/VERSION。
      发版请同时 bump 两个 VERSION（根 + src/）。

    Python 小知识：
      * `with open(...) as f:` 是「上下文管理器」，离开 with 块自动关文件
        即使中途抛异常也会关——比 try/finally 干净
      * `OSError` 涵盖文件不存在、权限不够、磁盘错误等所有 IO 异常
        比裸 `except:` 安全，比 `except FileNotFoundError` 全面
    """
    candidates = [
        # 优先：src/ 旁的副本——热更新一定会刷新它，最可靠新鲜
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION"),
        # fallback：项目根目录 VERSION（Docker 里由 Dockerfile COPY 进 /app/VERSION）
        os.path.join(_project_root(), "VERSION"),
    ]
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as f:
                v = f.read().strip()
                if v:
                    return v
        except OSError:
            # 这一条候选路径读不到就试下一条，不打日志（启动期无日志器）
            continue
    return "0.0.0+unknown"


# (config_path, mtime, value)。get_ai_name() 在一次 letter 请求里会被调用多次，
# 每次都读盘 + 抢 _config_yaml_lock 不划算；按 mtime 缓存，配置改了自动失效。
_ai_name_cache: tuple[str, float, str] | None = None


def _config_ai_name() -> str:
    """从 config.yaml 读 `ai_name`；读不到、为空或配置损坏都返回空串。

    这里绝不能抛异常：署名逻辑遍布 letter / prompt / Dashboard，
    配置文件坏掉时应该退回环境变量，而不是让整条链路崩掉。
    """
    global _ai_name_cache
    try:
        config_path = config_file_path()
        try:
            mtime = os.path.getmtime(config_path)
        except OSError:
            return ""
        cached = _ai_name_cache
        if cached is not None and cached[0] == config_path and cached[1] == mtime:
            return cached[2]
        value = str(read_config_yaml().get("ai_name") or "").strip()
        _ai_name_cache = (config_path, mtime, value)
        return value
    except Exception:
        return ""


# 未配置时的默认时区。选东八区而不是 UTC：OB 的使用者在中国大陆，
# 「2027-01-01 解锁」按本地日期理解才符合直觉。
# 时区这东西一碰就头疼。写完这段去泡了杯茶，回来忘了自己写到哪。
# 其实青柑普洱挺好喝的
_DEFAULT_TIMEZONE = "Asia/Shanghai"
_DEFAULT_UTC_OFFSET_HOURS = 8
_timezone_cache: tuple[str, float, str] | None = None


def get_timezone_name() -> str:
    """config.yaml 的 `timezone`；未配置或读不到时回退 Asia/Shanghai。

    与 ai_name 同一套 mtime 缓存策略：它会在每次解析用户给的日期时被调用，
    不适合每次都读盘 + 抢配置锁。
    """
    global _timezone_cache
    try:
        config_path = config_file_path()
        try:
            mtime = os.path.getmtime(config_path)
        except OSError:
            return _DEFAULT_TIMEZONE
        cached = _timezone_cache
        if cached is not None and cached[0] == config_path and cached[1] == mtime:
            return cached[2]
        value = str(read_config_yaml().get("timezone") or "").strip() or _DEFAULT_TIMEZONE
        _timezone_cache = (config_path, mtime, value)
        return value
    except Exception:
        return _DEFAULT_TIMEZONE


def get_tzinfo():
    """把配置的时区名解析成 tzinfo。

    时区名非法、或运行环境缺少 IANA tzdata 时，回退到固定的 +08:00——
    宁可用一个明确的偏移，也不要让「解析一个日期」这种小事抛异常。
    """
    # 想养蛇，python 算蛇吗但是…算了。
    from datetime import timedelta, timezone as _timezone

    name = get_timezone_name()
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        return _timezone(timedelta(hours=_DEFAULT_UTC_OFFSET_HOURS))


def get_ai_name() -> str:
    """AI 一方的显示名 / display name for the AI side.

    优先级：`config.yaml` 的 `ai_name` → 环境变量 `AI_NAME` → 回退 "AI"。

    config 排在环境变量前面，是因为它跟着 vault 一起持久化：Docker 下
    config.yaml 在挂载目录里，**容器重建/重启都不会丢**；而环境变量得靠
    compose 逐个透传，漏一个这边就静默退回默认名。配置里默认留空，
    表示「没配」，此时行为与改动前完全一致。

    面向用户的文本（prompt / UI / 错误信息）、letter 署名都用它，
    避免硬编码具体模型名。
    """
    return _config_ai_name() or os.environ.get("AI_NAME", "").strip() or "AI"


def get_owner_name() -> str:
    """当前实例记忆归属者的显示名 / display name of this instance's memory owner.

    多人共用一套 OB 时，每个人跑一个独立实例（独立数据目录 + 端口），实例通过
    环境变量 `OMBRE_OWNER_NAME` 标明「这份记忆是谁的」，供 Dashboard 顶部归属徽标
    显示。未设置时回退空串（前端配合 owner_count 决定是否显示）。
    只从进程环境读取，绝不写入共享的 .env——否则同码多实例会互相串名。
    Read from the `OMBRE_OWNER_NAME` env var; empty when unset.
    """
    return os.environ.get("OMBRE_OWNER_NAME", "").strip()


def get_owner_count() -> int:
    """共用这套 OB 的总人数 / total number of people sharing this OB.

    由启动器按配置的人数注入 `OMBRE_OWNER_COUNT`（手动部署时自行设置）。前端据此
    决定是否显示归属徽标：`>= 2` 才显示（单人不打扰）。非法 / 未设置回退 1。
    Read from the `OMBRE_OWNER_COUNT` env var; falls back to 1 when unset/invalid.
    """
    raw = os.environ.get("OMBRE_OWNER_COUNT", "").strip()
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


def sanitize_name(name: str) -> str:
    """
    Sanitize bucket name, keeping only safe characters.
    Prevents path traversal attacks (e.g. ../../etc/passwd).
    清洗桶名称，只保留安全字符。防止路径遍历攻击。
    """
    if not isinstance(name, str):
        return "unnamed"
    cleaned = re.sub(r"[^\w\s\u4e00-\u9fff-]", "", name, flags=re.UNICODE)
    cleaned = cleaned.strip()[:_BUCKET_NAME_MAX_LEN]
    return cleaned if cleaned else "unnamed"


def normalize_memory_title(value: object) -> str:
    """统一显式记忆标题：NFC、单行、不静默截断。"""

    title = " ".join(unicodedata.normalize("NFC", str(value or "")).split())
    if len(title) > MEMORY_TITLE_MAX_CHARS:
        raise ValueError(
            f"title 超过 {MEMORY_TITLE_MAX_CHARS} 字符上限"
        )
    return title


def safe_path(base_dir: str, filename: str) -> Path:
    """
    Construct a safe file path, ensuring it stays within base_dir.
    Prevents directory traversal.
    构造安全的文件路径，确保最终路径始终在 base_dir 内部。
    """
    base = Path(base_dir).resolve()
    target = (base / filename).resolve()
    # 用 is_relative_to 而不是 startswith，避免前缀混淆：
    # 例如 base=/data/buckets，target=/data/buckets_evil/f.md，
    # str 前缀检查会误判为安全，is_relative_to 不会。
    if not target.is_relative_to(base):
        raise ValueError(
            f"Path safety check failed / 路径安全检查失败: "
            f"{target} is not inside / 不在 {base} 内"
        )
    return target


def _win_long_path(path: Path) -> str:
    """Prefix an absolute path with ``\\\\?\\`` on Windows to bypass the 260-char
    MAX_PATH limit. Domain names can sanitize down to 80 chars each, and nested
    under a deep install/data dir the combined bucket path can exceed it. No-op
    on other platforms."""
    if os.name != "nt":
        return str(path)
    resolved = os.path.abspath(str(path))
    if resolved.startswith("\\\\?\\"):
        return resolved
    if resolved.startswith("\\\\"):
        return "\\\\?\\UNC\\" + resolved[2:]
    return "\\\\?\\" + resolved


def atomic_write_text(path: str | Path, text: str) -> None:
    """Atomically replace a UTF-8 text file after flushing it to disk."""
    target = Path(path)
    os.makedirs(_win_long_path(target.parent), exist_ok=True)
    temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
    temporary_long = _win_long_path(temporary)
    try:
        with open(temporary_long, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_long, _win_long_path(target))
    except Exception:
        try:
            os.remove(temporary_long)
        except OSError:
            pass
        raise


def publish_new_file(temporary: str, target: str, text: str) -> None:
    """把已经写好的 ``temporary`` 发布成 ``target``；目标已存在就抛 FileExistsError。

    首选硬链接：`link(2)` 原子地要么创建目标名、要么失败，不会覆盖别人。
    `os.replace` 做不到这一点——它会静默覆盖，那是「更新」的语义，不是「创建」。

    **但有些文件系统压根不支持硬链接**：Termux/Android 的 FUSE 挂载、部分
    NAS/SMB 卷。真机上 `hold` 就是这么炸的（[OB-E004]）。这时退回
    `O_CREAT|O_EXCL`——它同样是原子的「要么占到这个名字、要么 FileExistsError」，
    只是不需要硬链接。

    退回时会重写一遍正文而不是直接 `os.replace(temporary, target)`：后者一旦在
    另一个进程刚创建同名文件之后执行，就把人家的文件盖掉了，而这个函数的全部
    意义就是不许发生这件事。代价是这条路上「写入」不再是一次原子发布，读者有
    可能看到半截文件——在不支持硬链接的文件系统上，这是能保住 no-clobber 的
    最好结果。

    `ombrebrain/storage/source_store.py` 早就为同一个原因做了兜底（那边靠一个
    旁路租约），这里补上另外两处一直漏掉的。
    """
    link = getattr(os, "link", None)
    if link is not None:
        try:
            link(temporary, target)
            return
        except FileExistsError:
            raise
        except OSError:
            # 不支持硬链接。不按 errno 挑：不同平台报 EPERM / ENOSYS /
            # EOPNOTSUPP / EXDEV 都有，而下面这条路本身是安全的——真是别的毛病，
            # 它会用更清楚的错误再失败一次。
            pass

    descriptor = os.open(
        target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        # 这条路上「创建」和「写完」不是同一步，中间炸了（盘满、配额、SMB 断连）
        # 就会在真实路径上留下一个半截文件。它 frontmatter 解析得通、_load_bucket
        # 读得出来，于是调用方收到「失败」，库里却多了一条被截断的记忆。
        # O_EXCL 保证这个名字是我们刚占下的，删掉不会碰到别人的文件。
        try:
            os.unlink(target)
        except OSError:
            pass
        raise


def count_tokens_approx(text: str) -> int:
    """
    Rough token count estimate.
    粗略估算 token 数。

    Chinese ≈ 1 char = 1.5 tokens, English ≈ 1 word = 1.3 tokens.
    Used to decide whether dehydration is needed; precision not required.
    中文 ≈ 1字=1.5token，英文 ≈ 1词=1.3token。
    用于判断是否需要脱水压缩，不追求精确。
    """
    if not text:
        return 0
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    english_words = len(re.findall(r"[a-zA-Z]+", text))
    return int(
        chinese_chars * _TOKEN_RATIO_PER_CN_CHAR
        + english_words * _TOKEN_RATIO_PER_EN_WORD
        + len(text) * _TOKEN_RATIO_PER_CHAR
    )


def now_iso() -> str:
    """当前时间的 ISO 串，带本地时区偏移。

    以前这里是裸的本地墙上时间，没有偏移也没有 Z。单机单时区时没问题，
    但只要库被跨时区的机器写过、或者赶上夏令时回拨的那一小时，字符串里就
    **不存在**能还原真相的信息了——而且 OB 进程外的消费者（导出的 JSON、
    读桶算衰减的下游工具）连「写这条的机器在哪」都无从得知。

    带上偏移之后，旧的 naive 值不受影响：parse_iso_datetime 两种都吃，
    aware 的会被归一成本地 naive，进程内的比较行为一个字都不变。
    """
    return datetime.now().astimezone().isoformat(timespec="seconds")
