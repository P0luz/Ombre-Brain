"""
Ombre Brain — 统一错误码体系 / Unified Error Code System
==========================================================

设计原则（来自 rule.md §1.5 +  2026-05-02 规范）：
    "在产生与发现错误这件事上，能说出来的绝不静默。"
    "报错要让她/他在前端面板上能看到，也要让 LLM 模型在 MCP 的返回端看到。"

四级严重度：
    F (Fatal)   — 拒绝启动 + 终端输出 + 写 error.log
    E (Error)   — 前端弹窗 + MCP 返回值末尾 + 附最近 15 条 log
    W (Warning) — MCP 返回值末尾追加 + 前端日志面板
    I (Info)    — MCP 返回值末尾追加（轻量提示，例如自动降级）

模块职责：
    1. ERROR_CODES：错误码注册表（含级别、中英文描述、建议操作）
    2. format_error()：标准化字符串渲染
    3. record_error()：写持久化 errors.jsonl + 内存 buffer
    4. recent_errors()：供 /api/errors/recent 端点读取
    5. log_buffer：环形缓冲，存最近 N 条 log（含 stderr 流过的所有 log）
    6. attach_log_buffer_handler()：把 BufferHandler 装到 root logger
    7. warnings_channel（contextvars）：MCP 工具调用期间累积的 W/I 提示，
       由 _with_notice() 在工具返回前 pop 出并 append 到返回值末尾

不引入任何额外依赖，纯标准库实现。
"""
from __future__ import annotations

import collections
import contextvars
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Iterable

logger = logging.getLogger(__name__)


# ============================================================
# 1. 错误码表 / Error Code Registry
# ============================================================

@dataclass(frozen=True)
class ErrorSpec:
    code: str            # e.g. "OB-E001"
    level: str           # "F" | "E" | "W" | "I"
    title_zh: str
    title_en: str
    suggestion_zh: str
    suggestion_en: str = ""


class PublicToolError(RuntimeError):
    """固定文案已确认可安全返回给 MCP 客户端的工具异常。

    禁止把动态供应商异常正文放进 public_message。RuntimeError 基础消息保持
    泛化，避免其他路径意外记录 ``str(exc)`` 时泄露客户端可见正文。
    """

    def __init__(self, public_message: str):
        message = str(public_message).strip()
        if (
            not message
            or len(message) > 500
            or any(ord(char) < 32 for char in message)
        ):
            raise ValueError("公开工具错误文案必须是单行安全文本")
        self.public_message = message
        super().__init__("public tool error")


class ToolInputError(ValueError):
    """入参不合法，工具在任何写入之前就停下了——一个桶都没建。

    为什么需要这个类：MCP 只认异常。工具用 ``return "错误说明"`` 表达失败时，
    客户端拿到的是一次 ``isError=False`` 的正常返回，调用方（通常是模型自己）
    会以为写成功了继续往下走，等下次去翻，那条记忆从来没存在过。

    与 PublicToolError 的分工：那个是"固定安全文案，动态正文一个字都不许进"，
    用于必须收敛话术的失败；这个的正文本来就是要给调用方看的参数校验说明——
    它得靠这句话知道该改哪个参数。带动态正文的场景先过 safe_error_detail()
    脱敏，再传进来。

    边界：判据是"这次调用什么都没写"，不是"错在谁"。所以除了入参不合法，
    写入前的前置条件不成立（原文证据存储不可用、存原文时磁盘失败）同样走这里
    ——调用方一样需要知道那条记忆没存上。反过来，主体已经成功、只是附带信息
    没读到的降级提示（``👣 Footprint：暂时无法读取``）不属于这里，
    那种 isError=False 才对。
    """

    def __init__(self, message: str):
        # 折成单行：MCP 把它拼进 "Error executing tool X: ..."，换行会破坏可读性
        text = " ".join(str(message).split())[:500]
        super().__init__(text or "入参不合法，未做任何写入。")


_SAFE_DETAIL_MAX = 200         # 异常正文对外截断长度（与 import 侧 _CHUNK_ERR_PREVIEW 一致）


def safe_error_detail(exc: BaseException) -> str:
    """把异常正文压成"可以拿给人看"的一行：保留信息，抹掉凭证，限长。

    为什么需要它：工具层大量 `except Exception as e: return f"...: {e}"` 直接把
    异常正文拼进返回给 MCP 客户端的字符串。多数时候那是本地文件系统错误，无害且
    有助排查；但同一段代码也会兜住带 Authorization 头、api_key= 查询串的供应商
    异常，一旦命中就把凭证原样送出去了。

    与 PublicToolError 的分工：PublicToolError 走"固定安全文案"，一个字都不让
    动态正文进去；本函数走另一条路——正文照给，但先脱敏。前者用于必须收敛成
    统一话术的失败，后者用于"说清楚到底哪儿错了"更重要的失败。

    脱敏覆盖三种常见凭证形态：``bearer <token>``、``sk-`` 开头的 key、
    ``api_key=`` / ``token:`` 这类键值对。空正文回落到异常类名，避免返回空串。
    """
    detail = str(exc).strip() or type(exc).__name__
    detail = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[REDACTED]", detail)
    detail = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", detail)
    detail = re.sub(
        r"(?i)((?:api[_-]?key|token)\s*[=:]\s*)[^\s,;]+",
        r"\1[REDACTED]",
        detail,
    )
    return detail[:_SAFE_DETAIL_MAX]


def llm_step_failed_error(step_zh: str, *, api_available: bool) -> PublicToolError:
    """LLM 步骤（日记拆分 / 打标）失败时的公开文案，按"是不是真的没配 key"分岔。

    为什么要分岔：grow 的两条路径原本 `except Exception` 之后一律返回
    "API key 未配置或调用失败……请检查 OMBRE_COMPRESS_API_KEY"。但这条路上绝大
    多数失败与 key 无关——供应商 5xx、超时，或 dehydrator 抛的"API 日记整理返回
    空结果"（模型返回解析后 0 条有效条目）都会撞上同一句话，把人指向错误的排查
    方向：key 明明是好的，失败前一秒调用还是 200。

    api_available 由调用方传入（通常是 dehydrator.api_available，也就是
    ``_require_api()`` 用来判断"配置到底配好没有"的同一个信号）。之所以不在这里
    自己去取，是为了不让 errors 这个底层模块反向依赖 tools._runtime。

    注意 api_available 只回答"配没配"，不回答"配得对不对"：key 填错、过期、
    余额耗尽时它仍然是 True，调用会以 401/402 失败。所以 True 分支的文案**不能**
    反过来打包票说"key 没问题"——那只是把原来的误导换了个方向。这里给的是一组
    并列的可能原因（供应商故障、模型返回为空、key 失效），把判断交回给日志。

    边界：真实异常正文一律不进公开文案——PublicToolError 的契约就是固定安全
    文本，供应商正文只写日志（err_type=…）。需要把正文给出去的场景请用
    safe_error_detail()。
    """
    if not api_available:
        return PublicToolError(
            f"脱水 API 不可用（未配置或配置有误），{step_zh}无法完成，桶未创建。"
            "请检查 OMBRE_COMPRESS_API_KEY 与 config.yaml 的 dehydration 配置。"
        )
    return PublicToolError(
        f"脱水 API 调用失败或返回无法解析的内容，{step_zh}无法完成，桶未创建。"
        "可稍后重试；持续失败请看 server.log 里的 err_type，"
        "再逐一排除供应商故障、模型返回为空、key 失效或余额不足。"
    )


# 注册表 —— 修改/新增请同时同步 rule.md §11
ERROR_CODES: dict[str, ErrorSpec] = {
    # ---- Fatal：拒绝启动 ----
    "OB-F001": ErrorSpec(
        code="OB-F001",
        level="F",
        title_zh="向量化 API Key 缺失",
        title_en="Embedding API key missing",
        suggestion_zh=(
            "设置环境变量 OMBRE_EMBED_API_KEY（或在 config.yaml 中填写 embedding.api_key）。\n"
            "若暂时不需要语义检索，可在 config.yaml 中设置 embedding.enabled=false 跳过。"
        ),
    ),
    "OB-F002": ErrorSpec(
        code="OB-F002",
        level="F",
        title_zh="config.yaml 损坏或缺失",
        title_en="config.yaml missing or malformed",
        suggestion_zh=(
            "检查项目根目录是否存在 config.yaml；如缺失，从 config.example.yaml 复制一份。"
            "如已存在，运行 `python -c \"import yaml; yaml.safe_load(open('config.yaml'))\"` 看是否能解析。"
        ),
    ),
    "OB-F003": ErrorSpec(
        code="OB-F003",
        level="F",
        title_zh="vault 目录不可写",
        title_en="vault (buckets) directory not writable",
        suggestion_zh=(
            "检查 OMBRE_BUCKETS_DIR 指向的目录是否存在且当前用户拥有写权限。"
            "Docker 部署请检查 volume 挂载与 uid/gid 映射。"
        ),
    ),
    "OB-F004": ErrorSpec(
        code="OB-F004",
        level="F",
        title_zh="embedding 后端初始化失败",
        title_en="Embedding backend initialization failed",
        suggestion_zh=(
            "检查 OMBRE_EMBED_API_KEY 是否有效，以及 OMBRE_EMBED_BASE_URL 是否可达。"
        ),
    ),

    # ---- Error：前端弹窗 + MCP 末尾 ----
    "OB-E001": ErrorSpec(
        code="OB-E001",
        level="E",
        title_zh="embedding API 调用失败",
        title_en="Embedding API call failed",
        suggestion_zh=(
            "检查网络可达性、OMBRE_EMBED_API_KEY 是否有效、配额是否耗尽。"
            "本次写入仍会保存到 buckets，向量由后台自动重试；也可调用 "
            "/api/embedding/backfill 手动触发全库对账。"
        ),
    ),
    "OB-E002": ErrorSpec(
        code="OB-E002",
        level="E",
        title_zh="写盘失败",
        title_en="Disk write failed",
        suggestion_zh=(
            "检查磁盘剩余空间、目录权限；确认未被备份/同步软件锁定（iCloud/Dropbox 等）。"
        ),
    ),
    "OB-E003": ErrorSpec(
        code="OB-E003",
        level="E",
        title_zh="并发冲突超时",
        title_en="Concurrency lock timeout",
        suggestion_zh=(
            "同一 content 的 merge_or_create 长时间未释放锁；通常是上一个调用卡死。"
            "稍后重试；若反复出现，重启服务或检查 LLM 提供方是否慢响应。"
        ),
    ),
    "OB-E004": ErrorSpec(
        code="OB-E004",
        level="E",
        title_zh="MCP 工具执行异常",
        title_en="MCP tool execution exception",
        suggestion_zh=(
            "异常正文已隐藏，以避免泄露密钥、本机路径或调用内容。"
            "请在已认证 Dashboard 中按错误码与时间定位；若反复出现，请重试并反馈。"
        ),
    ),

    # ---- Warning：MCP 返回末尾 + 前端日志面板 ----
    "OB-W001": ErrorSpec(
        code="OB-W001",
        level="W",
        title_zh="importance 越界已修正",
        title_en="importance out of range, clamped",
        suggestion_zh="importance 必须在 [1,10]；本次已被修正到边界值。",
    ),
    "OB-W002": ErrorSpec(
        code="OB-W002",
        level="W",
        title_zh="valence/arousal 越界已回退",
        title_en="valence/arousal out of range, clamped",
        suggestion_zh="valence/arousal 必须在 [0.0, 1.0]；本次已被修正到边界值。",
    ),
    "OB-W004": ErrorSpec(
        code="OB-W004",
        level="W",
        title_zh="pinned 配额接近上限",
        title_en="pinned quota near cap",
        suggestion_zh=(
            "pinned 桶接近上限（默认 18/20，硬上限 20，可在 config.limits.max_pinned 调整）。"
            "建议先用 trace(bucket_id, pinned=0) 取消不再核心的钉选，再钉新桶。"
        ),
    ),
    "OB-W005": ErrorSpec(
        code="OB-W005",
        level="W",
        title_zh="embeddings.db 中的模型/维度与当前后端不一致",
        title_en="embeddings.db model/dim mismatch with current backend",
        suggestion_zh=(
            "过往写入的向量与当前模型不同维，搜索会退化为 0 分。"
            "请在 Dashboard 设置页点击「切换模型」，或调用 POST /api/embedding/migrate 重建索引。"
            "迁移期间搜索降级为关键词模式，不会丢文件。"
        ),
    ),
    "OB-W006": ErrorSpec(
        code="OB-W006",
        level="W",
        title_zh="引语超过每桶上限，超出的部分未写入",
        title_en="quotes exceed per-bucket cap; the overflow was not written",
        suggestion_zh=(
            "这条记忆被合并进了一条已有记忆，两边的引语加起来超过了每桶上限"
            "（默认 3 条）。先记住的那几句被保留，本次多出来的没有写入。\n"
            "上限是防止「记住几句重要的话」退化成「存原文」——原文层是只写不读的，"
            "引语不该变成它的替代品。\n"
            "如果这次多出来的那句确实更重要，可以用 trace(bucket_id, ...) "
            "看一眼那条桶现在留着哪几句，再决定要不要换。"
        ),
    ),

    # ---- Info：自动降级 / 轻量提示 ----
    "OB-I002": ErrorSpec(
        code="OB-I002",
        level="I",
        title_zh="pinned 已自动退出（pinned 配额超标）",
        title_en="pinned auto-unset (pinned quota exceeded)",
        suggestion_zh=(
            "★ 这是 OB 自作主张帮你做的事 ★\n"
            "pinned 桶已达硬上限（默认 20，可在 config.limits.max_pinned 调整），本次未钉成功（保留为普通桶）。\n"
            "建议：用 breath 看一遍当前 pinned 列表，把不再属于「永久核心准则」的"
            "用 trace(bucket_id, pinned=0) 取消，再来钉这条。"
        ),
    ),
}

# ============================================================
# 2. 内存日志环形缓冲 / In-memory Log Ring Buffer
# ============================================================

_LOG_BUFFER_MAX = 500     # 总环形缓冲，前端"最近日志"读这里
_LOG_TAIL_FOR_ERROR = 15  # E 级报错附带的最近日志条数（按规范）

_log_buffer: collections.deque[str] = collections.deque(maxlen=_LOG_BUFFER_MAX)
_log_buffer_lock = threading.Lock()


class _BufferHandler(logging.Handler):
    """把 logging 输出顺手存一份到内存 deque，供 E 级报错附带 tail。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            with _log_buffer_lock:
                _log_buffer.append(line)
        except Exception:
            # 日志 handler 自己绝不能抛
            pass


def attach_log_buffer_handler(level: int = logging.INFO) -> None:
    """把 BufferHandler 挂到 root logger；幂等，重复调用无害。"""
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, _BufferHandler):
            return
    h = _BufferHandler()
    h.setLevel(level)
    h.setFormatter(logging.Formatter(
        "[%(asctime)s] %(name)s %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(h)


def get_recent_logs(n: int = _LOG_TAIL_FOR_ERROR) -> list[str]:
    """读取最近 n 条 log（newest last）。"""
    with _log_buffer_lock:
        if n >= len(_log_buffer):
            return list(_log_buffer)
        return list(_log_buffer)[-n:]


# ============================================================
# 3. 持久化错误日志 / Persistent Error Log
# ============================================================

_errors_path: str | None = None
_errors_path_lock = threading.Lock()
_MAX_ERROR_TAIL_SCAN_BYTES = 8 * 1024 * 1024
_TAIL_CHUNK_BYTES = 64 * 1024


def _iter_tail_lines(path: str, *, max_bytes: int):
    """Yield UTF-8 text lines newest-first from a bounded file tail."""

    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        remaining = max(0, int(max_bytes))
        carry = b""
        while position > 0 and remaining > 0:
            chunk_size = min(_TAIL_CHUNK_BYTES, position, remaining)
            position -= chunk_size
            remaining -= chunk_size
            handle.seek(position)
            block = handle.read(chunk_size) + carry
            parts = block.split(b"\n")
            carry = parts.pop(0)
            for raw_line in reversed(parts):
                yield raw_line.decode("utf-8", errors="replace")
        # Only yield carry when it starts at byte zero.  If the scan hit its
        # byte cap, carry is an intentionally incomplete giant/old line.
        if position == 0 and carry:
            yield carry.decode("utf-8", errors="replace")


def configure_errors_path(buckets_dir: str) -> None:
    """由 server 启动时调用：将 errors.jsonl 放在 buckets_dir/.logs/errors.jsonl。"""
    global _errors_path
    log_dir = os.path.join(buckets_dir, ".logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
        _errors_path = os.path.join(log_dir, "errors.jsonl")
    except Exception as e:
        logger.warning(f"[errors] cannot create log dir {log_dir}: {e}")
        _errors_path = None


def _ends_with_newline(path: str) -> bool:
    """文件最后一个字节是不是换行。空文件/读不了都算「是」（不用补）。"""
    try:
        if os.path.getsize(path) == 0:
            return True
        with open(path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            return f.read(1) == b"\n"
    except OSError:
        return True


def _persist_error_record(record: dict) -> None:
    if not _errors_path:
        return
    try:
        with _errors_path_lock:
            # 崩溃会把最后一行截在中间。直接追加会把新记录粘到那半行后面，
            # 于是**两条都变成一行读不出来的东西**——而这个日志正是崩溃之后
            # 要拿来看的，尾行残缺才是常态。先补一个换行，坏的只坏一条。
            # ledger_mirror 早就这么做了，这里一直漏着。
            if not _ends_with_newline(_errors_path):
                with open(_errors_path, "a", encoding="utf-8", newline="\n") as f:
                    f.write("\n")
            with open(_errors_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"[errors] persist failed: {e}")


def recent_errors(limit: int = 50, min_level: str = "W") -> list[dict]:
    """读取最近 limit 条已记录的错误（从 errors.jsonl 末尾倒序取）。"""
    if not _errors_path or not os.path.exists(_errors_path):
        return []
    order = ["I", "W", "E", "F"]
    if min_level not in order:
        min_level = "W"
    min_idx = order.index(min_level)
    out: list[dict] = []
    try:
        with _errors_path_lock:
            for ln in _iter_tail_lines(
                _errors_path,
                max_bytes=_MAX_ERROR_TAIL_SCAN_BYTES,
            ):
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    obj = json.loads(ln)
                except Exception:
                    continue
                lvl = obj.get("level", "W")
                if lvl in order and order.index(lvl) >= min_idx:
                    out.append(obj)
                if len(out) >= limit:
                    break
    except Exception as e:
        logger.warning(f"[errors] read failed: {e}")
        return []
    return out


def clear_errors_log() -> int:
    """清空 errors.jsonl，返回原行数（供 dashboard "已读" 按钮）。"""
    if not _errors_path or not os.path.exists(_errors_path):
        return 0
    try:
        with _errors_path_lock:
            with open(_errors_path, "r", encoding="utf-8") as f:
                n = sum(1 for _ in f)
            open(_errors_path, "w", encoding="utf-8").close()
        return n
    except Exception as e:
        logger.warning(f"[errors] clear failed: {e}")
        return 0


# ============================================================
# 4. 标准格式化 / Standard Formatter
# ============================================================

_LEVEL_PREFIX = {
    "F": "🛑",   # Fatal
    "E": "❌",   # Error
    "W": "⚠️",   # Warning
    "I": "ℹ️",   # Info
}


def format_error(
    code: str,
    detail: str = "",
    *,
    include_logs: bool | None = None,
    extra: dict | None = None,
) -> str:
    """渲染统一格式字符串。

    include_logs=None 时按级别决定：F/E 默认带 tail，W/I 默认不带。
    """
    spec = ERROR_CODES.get(code)
    if not spec:
        # 未知码：仍能渲染，让排错时一眼看到拼错的码
        return (
            f"❌ [{code}] 未注册错误码\n"
            f"详情：{detail}\n"
            f"建议：在 src/errors.py ERROR_CODES 注册该码或修正调用处。"
        )
    prefix = _LEVEL_PREFIX.get(spec.level, "•")
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    parts = [
        f"{prefix} [{spec.code}] {spec.title_zh}",
    ]
    if detail:
        parts.append(f"描述：{detail}")
    parts.append(f"建议：{spec.suggestion_zh}")
    parts.append(f"时间：{ts}")
    if extra:
        for k, v in extra.items():
            parts.append(f"{k}：{v}")

    if include_logs is None:
        include_logs = spec.level in ("F", "E")
    if include_logs:
        tail = get_recent_logs(_LOG_TAIL_FOR_ERROR)
        parts.append("")
        parts.append(f"--- 最近 {len(tail)} 条日志 ---")
        parts.extend(tail if tail else ["(暂无日志)"])
    return "\n".join(parts)


def record_error(
    code: str,
    detail: str = "",
    *,
    extra: dict | None = None,
    log: bool = True,
) -> dict:
    """记录一条错误：写 errors.jsonl + 同步到 logger（按级别）+ 返回结构化 dict。

    上层若需要把它附加到 MCP 返回值，使用 format_error 或 push_warning。
    """
    spec = ERROR_CODES.get(code)
    level = spec.level if spec else "E"
    record = {
        "code": code,
        "level": level,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "title": spec.title_zh if spec else "未注册错误码",
        "detail": detail,
        "extra": extra or {},
    }
    _persist_error_record(record)
    if log:
        msg = f"[{code}] {record['title']} | {detail}"
        if level == "F":
            logger.critical(msg)
        elif level == "E":
            logger.error(msg)
        elif level == "W":
            logger.warning(msg)
        else:
            logger.info(msg)
    return record


# ============================================================
# 5. MCP 返回值警告通道 / MCP Return Suffix Channel
# ============================================================
#
# 设计：MCP 工具调用期间，业务代码（bucket_manager / tools/_common 等）可能在
# 任意层产生 W/I 级提示。这些提示要透传到 MCP 返回值末尾让 AI 能看到。
# 用 contextvars 维护一个 per-task 的列表；server.py 的 _with_notice 包装器
# 在工具返回时 pop 出来 append 到末尾。
# 注意：contextvars 在 asyncio 中按任务隔离，不会跨调用串味。

_warnings_var: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "ob_warnings", default=None
)


def begin_warnings() -> None:
    """在每次 MCP 工具调用入口处调用一次，初始化本调用的 channel。"""
    _warnings_var.set([])


def push_warning(code: str, detail: str = "", *, extra: dict | None = None) -> None:
    """业务代码调用：登记一条 W/I 级提示。

    会同时 record_error（写盘 + 写 logger）。
    """
    record_error(code, detail, extra=extra)
    cur = _warnings_var.get()
    if cur is None:
        # 调用方不在 MCP 工具上下文（例如后台任务），仅持久化即可
        return
    cur.append(format_error(code, detail, extra=extra))


def pop_warnings() -> list[str]:
    """server.py 的 _with_notice 在工具返回前调用，取出本调用累计的提示。"""
    cur = _warnings_var.get()
    if cur is None:
        return []
    _warnings_var.set([])
    return cur


def format_warnings_suffix(warnings: Iterable[str]) -> str:
    items = list(warnings)
    if not items:
        return ""
    return "\n\n" + "\n\n".join(items)


# ============================================================
# 6. 启动期专用异常 / Startup-time Exception
# ============================================================

class OBStartupError(SystemExit):
    """Fatal：拒绝启动。携带错误码，由 server.py 顶层捕获后输出标准格式 + 写 error.log。

    注意：SystemExit 自身有内置 ``.code`` 属性（保存进程退出码），所以本类用
    ``.error_code`` 暴露 OB 错误码；同时也提供 ``.code`` 的兼容别名。
    """

    def __init__(self, code: str, detail: str = "", *, extra: dict | None = None):
        self.error_code = code
        self.detail = detail
        self.extra = extra or {}
        # SystemExit 的 message 即终端最终输出
        msg = format_error(code, detail, extra=extra, include_logs=True)
        super().__init__(msg)


def write_fatal_log(code: str, detail: str, *, buckets_dir: str | None = None) -> None:
    """Fatal 级别专用：直接写 error.log（不走 errors.jsonl 因为可能尚未 configure）。"""
    target_dir = buckets_dir or os.environ.get("OMBRE_BUCKETS_DIR", "").strip() or "."
    try:
        log_dir = os.path.join(target_dir, ".logs")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "error.log"), "a", encoding="utf-8") as f:
            f.write(format_error(code, detail, include_logs=True) + "\n\n")
    except Exception:
        pass


__all__ = [
    "ERROR_CODES",
    "ErrorSpec",
    "format_error",
    "record_error",
    "recent_errors",
    "clear_errors_log",
    "configure_errors_path",
    "get_recent_logs",
    "attach_log_buffer_handler",
    "begin_warnings",
    "push_warning",
    "pop_warnings",
    "format_warnings_suffix",
    "PublicToolError",
    "ToolInputError",
    "OBStartupError",
    "write_fatal_log",
]
