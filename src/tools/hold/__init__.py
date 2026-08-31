"""
========================================
tools/hold/__init__.py — hold 工具入口
========================================

hold 是「我把这件事/这个感受存进我的记忆」。这个文件按入参把请求
路由到三种分支：feel（写第一人称感受）、pinned（钉为永久核心准则）、
core（普通存入 + 自动合并）。

关键行为：
- null-safe 兜底；先做 content / 字节上限校验，再分支
- feel=True / pinned=True 是互斥分支，否则走 core
- core 写完后 fire-and-forget 触发 plan 完成建议 + 疑似重复扫描

不做什么（边界）：
- 不在这里做 LLM 打标，分支模块负责
- 不返回结构化数据，统一返回供模型阅读的中文短句

对外暴露：dispatch(content, tags, importance, pinned, feel, source_bucket,
                   valence, arousal, why_remembered, meaning, media, domain) → str
========================================
"""

from typing import Optional

from errors import ToolInputError, safe_error_detail
from ombrebrain.storage.media_store import MediaPersistenceError
from ombrebrain.storage.quote_store import normalize_quotes
from ombrebrain.storage.source_store import normalize_source_ranges
from utils import normalize_memory_title, parse_bool

from .. import _runtime as rt
from .._common import (
    check_content_size,
    check_metadata_size,
    enforce_pinned_quota,
)
from .feel import store_feel
from .pinned import store_pinned
from .core import store_core


def _normalize_explicit_domain(value: str | list[str] | None) -> list[str] | None:
    if isinstance(value, list):
        parts = [str(item).strip() for item in value if item is not None]
    else:
        parts = [item.strip() for item in str(value or "").split(",")]
    normalized = list(dict.fromkeys(item for item in parts if item))
    return normalized or None


def _prepare_quotes(value: object) -> list[dict] | None:
    """校验本次要原样记住的那几句话。

    超限直接拒绝，不静默截断——截断过的引语已经不是原话了，
    而"原样"正是这个功能存在的全部理由。

    拒绝一律走 ToolInputError：错误信息是给调用方（也就是我自己）看的，
    要说清楚为什么被拒，而且不能被当成一次成功返回。
    """
    if value in (None, "", []):
        return None
    try:
        quotes = normalize_quotes(value)
    except ValueError as exc:
        raise ToolInputError(f"引语无效，未创建任何桶：{safe_error_detail(exc)}") from exc
    return quotes or None


def _prepare_source_refs(
    source_content: object,
    source_ranges: object,
) -> list[dict] | None:
    """把 hold 可选原文挂到与 grow 共用的不可变 SourceStore。

    hold 是单桶写入：调用方提供原文但省略 ranges 时，整份原文默认就是
    该桶的 event 证据。显式 ranges 仍使用与 grow 一致的 1-based 闭区间。
    """
    source_text = "" if source_content is None else str(source_content)
    has_ranges = source_ranges not in (None, "", [])
    if not source_text.strip():
        if has_ranges:
            raise ToolInputError("source_ranges 需要同时提供 source_content，未创建任何桶。")
        return None

    try:
        ranges = normalize_source_ranges(source_ranges)
    except ValueError as exc:
        raise ToolInputError(f"原文范围无效，未创建任何桶：{safe_error_detail(exc)}") from exc

    line_count = len(source_text.splitlines()) or 1
    if not ranges:
        ranges = [[1, line_count]]
    elif any(end > line_count for _start, end in ranges):
        raise ToolInputError(f"source_ranges 超出原文总行数 {line_count}，未创建任何桶。")

    store = getattr(rt, "source_store", None)
    if store is None:
        raise ToolInputError("原文证据存储不可用，未创建任何桶。")
    try:
        ref = store.put(source_text)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ToolInputError(f"原文证据保存失败，未创建任何桶：{safe_error_detail(exc)}") from exc
    return [{"ref": ref, "ranges": ranges}]


async def dispatch(
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
    domain: Optional[str | list[str]] = "",
    source_content: Optional[str] = "",
    source_ranges: Optional[list] = None,
    quotes: Optional[list] = None,
) -> str:
    content = "" if content is None else str(content)
    try:
        title = normalize_memory_title(title)
    except ValueError as exc:
        raise ToolInputError(str(exc)) from exc
    if tags is None:
        tags = ""
    if importance is None:
        importance = 5
    if pinned is None:
        pinned = False
    if feel is None:
        feel = False
    if source_bucket is None:
        source_bucket = ""
    if valence is None:
        valence = -1
    if arousal is None:
        arousal = -1
    if why_remembered is None:
        why_remembered = ""
    why_remembered = str(why_remembered).strip()[:500]
    if meaning is None:
        meaning = ""
    meaning = str(meaning).strip()
    explicit_domain = _normalize_explicit_domain(domain)
    test_data = parse_bool(test_data, default=False)
    if test_data and (pinned or feel):
        raise ToolInputError("测试数据不能创建为 pinned 或 feel；请使用普通测试桶。")
    if feel and explicit_domain:
        raise ToolInputError("feel 的 domain 固定为 feel，不能显式覆盖。")
    try:
        importance = int(importance)
    except (TypeError, ValueError, OverflowError):
        importance = 5
    try:
        valence = float(valence)
    except (TypeError, ValueError, OverflowError):
        valence = -1
    try:
        arousal = float(arousal)
    except (TypeError, ValueError, OverflowError):
        arousal = -1

    metadata_err = check_metadata_size(
        tags=tags,
        title=title,
        source_bucket=source_bucket,
        why_remembered=why_remembered,
        meaning=meaning,
        domain=domain,
    )
    if metadata_err:
        raise ToolInputError(metadata_err)
    if rt.mark_op:
        rt.mark_op("hold")
    rt.record_v3_tool_event("hold", {
        "content_length": len(content or ""),
        "tags": tags,
        "importance": importance,
        "pinned": pinned,
        "feel": feel,
        "source_bucket": source_bucket,
        "valence": valence,
        "arousal": arousal,
        "why_remembered_length": len(why_remembered or ""),
        "source_content_length": len(str(source_content or "")),
        "source_ranges_count": len(source_ranges or []) if isinstance(source_ranges, list) else 0,
        "quotes_count": len(quotes or []) if isinstance(quotes, list) else 0,
    })
    await rt.decay_engine.ensure_started()

    if not content or not content.strip():
        raise ToolInputError("内容为空，无法存储。")

    err = check_content_size(content)
    if err:
        raise ToolInputError(err)

    # importance 越界 clamp 由 bucket_manager 接管（OB-W001 自动 push 到 channel）；
    # 这里仅做一次软 clamp 便于配额判断。
    importance = max(1, min(10, importance))

    # pinned 配额检查（OB-W004 软警告 / OB-I002 自动退出）
    if pinned and not feel:
        pinned = await enforce_pinned_quota(True)

    # 普通桶的 importance 配额在 merge_or_create 的最终 merge/create
    # 事务内检查；这里预检查会在“合并到已占位桶”时产生假降级提示。

    # valence/arousal 越界回退到自动打标（OB-W002 由 bucket_manager 在 clamp 时 push；
    # 这里的 -1 咨兵语义是"她/他未传"，越界则忽略，让 LLM analyze 决定）
    if valence != -1 and not (0 <= valence <= 1):
        try:
            try:
                from errors import push_warning  # type: ignore
            except ImportError:
                from ..errors import push_warning  # type: ignore
            push_warning("OB-W002", f"hold 入参 valence={valence} 越界，已忽略，回退到自动打标")
        except Exception:
            pass
        valence = -1
    if arousal != -1 and not (0 <= arousal <= 1):
        try:
            try:
                from errors import push_warning  # type: ignore
            except ImportError:
                from ..errors import push_warning  # type: ignore
            push_warning("OB-W002", f"hold 入参 arousal={arousal} 越界，已忽略，回退到自动打标")
        except Exception:
            pass
        arousal = -1

    if isinstance(tags, list):
        extra_tags = [str(t).strip() for t in tags if t]
    else:
        extra_tags = [t.strip() for t in str(tags).split(",") if t.strip()]

    if feel and (not source_bucket or not source_bucket.strip()):
        raise ToolInputError(
            "feel 必须指向一条原始记忆（source_bucket 不能为空）。请先用 "
            "breath_search(query=...) 找到那条桶的 bucket_id，再传入 source_bucket=id。"
        )

    # 媒体的持久化在建桶那一步，比下面 _prepare_source_refs 写原文证据晚。
    # 等到那时才失败，原文已经落进 _sources，而错误正文说的是「未创建任何桶」
    # ——调用方据此重试，上一半副作用已经在那了。真机复现过：
    #   hold(source_content="...", media=[{"data_base64": "@@@垃圾@@@"}])
    #     → isError=True、buckets=0，但 _sources 里多了一个 38 字节的文件
    # 所以先把媒体里所有可失败的部分跑一遍，不落盘。
    # 只在真要写原文时才付这个代价（path 类媒体会被读两遍），普通 hold 不受影响。
    if media and source_content and str(source_content).strip():
        try:
            await rt.bucket_mgr.media_store.precheck(media)
        except MediaPersistenceError as exc:
            raise ToolInputError(str(exc)) from exc

    source_refs = _prepare_source_refs(source_content, source_ranges)
    quotes_list = _prepare_quotes(quotes)

    # 所有越界/配额提醒走统一 warnings channel；server.py _with_notice 末尾自动追加。
    # 这里返回值只承载业务正文。

    try:
        return await _store(
            feel=feel,
            pinned=pinned,
            content=content,
            title=title,
            extra_tags=extra_tags,
            importance=importance,
            valence=valence,
            arousal=arousal,
            source_bucket=source_bucket,
            why_remembered=why_remembered,
            meaning=meaning,
            media=media,
            test_data=test_data,
            explicit_domain=explicit_domain,
            source_refs=source_refs,
            quotes_list=quotes_list,
        )
    except MediaPersistenceError as exc:
        # 媒体存不下时，桶一个都没建。存储层给的消息（「请改传 data_base64」）
        # 正是调用方需要的那句话，不翻译就会被 _with_notice 当成未预期异常，
        # 正文整个隐藏掉——调用方只知道失败了，不知道该怎么改。
        #
        # 为什么不让 media_store 直接抛 ToolInputError：ombrebrain/ 这个包
        # 全文零处 import 顶层 errors 模块，那条分层边界比省一层翻译值钱。
        raise ToolInputError(str(exc)) from exc


async def _store(
    *,
    feel: bool,
    pinned: bool,
    content: str,
    title: str,
    extra_tags: list,
    importance: int,
    valence: float,
    arousal: float,
    source_bucket: str,
    why_remembered: str,
    meaning: str,
    media: object,
    test_data: bool,
    explicit_domain: list | None,
    source_refs: list | None,
    quotes_list: list | None,
) -> str:
    """按 feel / pinned / 普通三分支落库。三条路的媒体失败由 dispatch 统一翻译。"""
    if feel:
        result = await store_feel(
            content=content,
            title=title,
            extra_tags=extra_tags,
            valence=valence,
            arousal=arousal,
            source_bucket=source_bucket,
            why_remembered=why_remembered,
            meaning=meaning,
            media=media,
            source_refs=source_refs,
            quotes=quotes_list,
        )
        return result

    if pinned:
        result = await store_pinned(
            content=content,
            title=title,
            extra_tags=extra_tags,
            valence=valence,
            arousal=arousal,
            why_remembered=why_remembered,
            meaning=meaning,
            media=media,
            explicit_domain=explicit_domain,
            source_refs=source_refs,
            quotes=quotes_list,
        )
        return result

    result = await store_core(
        content=content,
        title=title,
        extra_tags=extra_tags,
        importance=importance,
        valence=valence,
        arousal=arousal,
        why_remembered=why_remembered,
        meaning=meaning,
        media=media,
        test_data=test_data,
        explicit_domain=explicit_domain,
        source_refs=source_refs,
        quotes=quotes_list,
    )
    return result
