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


def _prepare_quotes(value: object) -> tuple[list[dict] | None, str]:
    """校验本次要原样记住的那几句话。

    超限直接拒绝，不静默截断——截断过的引语已经不是原话了，
    而"原样"正是这个功能存在的全部理由。

    错误信息是给调用方（也就是我自己）看的，所以要说清楚为什么被拒，
    而不是只丢一个"参数非法"。
    """
    if value in (None, "", []):
        return None, ""
    try:
        quotes = normalize_quotes(value)
    except ValueError as exc:
        return None, f"引语无效，未创建任何桶：{exc}"
    return (quotes or None), ""


def _prepare_source_refs(
    source_content: object,
    source_ranges: object,
) -> tuple[list[dict] | None, str]:
    """把 hold 可选原文挂到与 grow 共用的不可变 SourceStore。

    hold 是单桶写入：调用方提供原文但省略 ranges 时，整份原文默认就是
    该桶的 event 证据。显式 ranges 仍使用与 grow 一致的 1-based 闭区间。
    """
    source_text = "" if source_content is None else str(source_content)
    has_ranges = source_ranges not in (None, "", [])
    if not source_text.strip():
        if has_ranges:
            return None, "source_ranges 需要同时提供 source_content，未创建任何桶。"
        return None, ""

    try:
        ranges = normalize_source_ranges(source_ranges)
    except ValueError as exc:
        return None, f"原文范围无效，未创建任何桶：{exc}"

    line_count = len(source_text.splitlines()) or 1
    if not ranges:
        ranges = [[1, line_count]]
    elif any(end > line_count for _start, end in ranges):
        return None, f"source_ranges 超出原文总行数 {line_count}，未创建任何桶。"

    store = getattr(rt, "source_store", None)
    if store is None:
        return None, "原文证据存储不可用，未创建任何桶。"
    try:
        ref = store.put(source_text)
    except (OSError, UnicodeError, ValueError) as exc:
        return None, f"原文证据保存失败，未创建任何桶：{exc}"
    return [{"ref": ref, "ranges": ranges}], ""


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
    event_time: Optional[str] = "",
    references: Optional[list] = None,
) -> str:
    content = "" if content is None else str(content)
    try:
        title = normalize_memory_title(title)
    except ValueError as exc:
        return str(exc)
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
        return "测试数据不能创建为 pinned 或 feel；请使用普通测试桶。"
    if feel and explicit_domain:
        return "feel 的 domain 固定为 feel，不能显式覆盖。"
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
        return metadata_err
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
        return "内容为空，无法存储。"

    err = check_content_size(content)
    if err:
        return err

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
        return "feel 必须指向一条原始记忆（source_bucket 不能为空）。请先用 breath_search(query=...) 找到那条桶的 bucket_id，再传入 source_bucket=id。"

    source_refs, source_error = _prepare_source_refs(source_content, source_ranges)
    if source_error:
        return source_error

    quotes_list, quotes_error = _prepare_quotes(quotes)
    if quotes_error:
        return quotes_error

    # 所有越界/配额提醒走统一 warnings channel；server.py _with_notice 末尾自动追加。
    # 这里返回值只承载业务正文。

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
            event_time=event_time or "",
            references=references,
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
            event_time=event_time or "",
            references=references,
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
        event_time=event_time or "",
        references=references,
    )
    return result
