"""
========================================
tools/grow/core.py — grow 长内容主路径（digest + merge）
========================================

长内容（≥30 字）走这里。先调 dehydrator.digest 把整段拆成 2~6 条
事件项，每条独立尝试 merge_or_create。

关键行为：
- digest 失败（API key 不可用）时直接 RuntimeError，不创建任何桶
- 各 item 之间互不依赖，用 asyncio.gather 并发处理（而不是逐条 await 串行）；
  merge_or_create 内部按内容哈希的租约锁不受影响——同内容仍会正确串行，
  只是不同内容的 item 之间不再互相等待
- iter 2.0：每次 grow 调用生成一个 ``grow_batch_id``，同批次新建桶共享，
  source_tool 一律为 ``grow``；合并到的老桶不改 source_tool
- 单条失败不影响其他；按字节上限校验单条尺寸
- embedding 失败时桶正常创建，返回追加向量化降级警告
- 末尾 fire-and-forget 触发 plan 完成建议（用整段原文做匹配）

不做什么（边界）：
- 不写 feel：grow 是事件归档，不是反思
- 不做 pinned 标记：grow 拆出来的事件桶都是 dynamic
- items 可透传人工 why_remembered；digest 自动理由在首次新建时写入，
  后续合并时仅补空值、不覆盖旧理由

对外暴露：grow_core(content) → str
========================================
"""

from errors import ToolInputError
import asyncio
import uuid

from utils import normalize_memory_title

try:
    from errors import llm_step_failed_error, safe_error_detail
except ImportError:  # pragma: no cover - 包内导入兜底
    from ...errors import llm_step_failed_error, safe_error_detail  # type: ignore

from .. import _runtime as rt
from .._common import (
    merge_or_create,
    check_content_size,
    check_grow_items_payload,
    check_duplicate_for,
    check_plan_resolution,
)


async def grow_core(content: str, test_data: bool = False) -> str:
    try:
        items = await rt.dehydrator.digest(content)
    except Exception as e:
        # 这里原来把 detail 写死成字面量 "hidden"，于是**服务端自己的日志**
        # 也看不到原因——真机上「长内容 grow 一直失败」卡了两天，卡的就是这个。
        # 返回给客户端的仍然是 PublicToolError 的固定文案，一个字都没多给；
        # 但落盘日志该说清楚哪儿错了，safe_error_detail 正是为这件事写的：
        # 正文照给，先抹掉凭证。
        rt.logger.error(
            "Diary digest failed / 日记整理失败: err_type=%s detail=%s",
            type(e).__name__,
            safe_error_detail(e),
        )
        raise llm_step_failed_error(
            "日记拆分",
            api_available=getattr(rt.dehydrator, "api_available", True),
        ) from e

    if not isinstance(items, list) or not items:
        raise ToolInputError("内容为空或整理失败。")
    payload_err = check_grow_items_payload(items)
    if payload_err:
        rt.logger.warning(f"grow digest output rejected: {payload_err}")
        return payload_err

    # iter 2.0 来源追踪：同一次 grow 拆出的所有桶共享同一个 batch_id，
    # dashboard 可按 grow_batch_id 聚合显示「这次日记一共归档了哪些事件」。
    # 用 12 位 hex 与 bucket_id 长度对齐，加 g_ 前缀方便人眼区分。
    # **原文存档。** 自动拆分这条路以前只把 content 喂给 LLM，拆完就丢——
    # 桶里留下的全是 LLM 改写过的话，原话一个字都不在，事后无从核对它记没记岔。
    # （真机验证过：DS 拆出来的 4 个桶，source_ranges、source_id 一个都没有。）
    #
    # items 那条路早就有完整机制（原文存一份 + 每桶行号区间指回），
    # 这里补的是同一套，不新造第二种存法。
    source_ref = ""
    line_count = len(content.splitlines()) or 1
    try:
        from ombrebrain.storage.source_store import normalize_source_ranges

        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                ranges = normalize_source_ranges(item.get("source_ranges"))
            except (TypeError, ValueError):
                ranges = []
            # 越界的行号一律丢弃，不猜、不截断到边界：LLM 报错了行，
            # 与其存一段不相干的原话，不如这条没有原文佐证。
            item["_source_ranges"] = [r for r in ranges if r[1] <= line_count]
        source_ref = rt.source_store.put(content)
    except (OSError, ValueError) as exc:
        # 原文存不下不该让整批记忆丢掉——正文照常入库，只是这批没有佐证。
        rt.logger.warning(
            f"grow source evidence not saved / 原文证据未保存: "
            f"{type(exc).__name__}: {exc}"
        )
        source_ref = ""

    batch_id = f"g_{uuid.uuid4().hex[:12]}"

    async def _process_item(item: dict) -> dict:
        """处理 digest 拆出的一条独立 item，返回结构化结果供 gather 后汇总。"""
        size_err = check_content_size(item.get("content", ""))
        if size_err:
            return {"line": f"⚠️{item.get('name', '?')}（{size_err}）"}
        try:
            why_remembered = item.get("why_remembered") or ""
            # 把这条桶连回它自己那几行原话。行号是 LLM 报的，原话由系统去取——
            # 它碰不到原文，也就没法在这一层压缩或改写。
            source_refs = None
            if source_ref:
                source_refs = [
                    {"ref": source_ref, "ranges": item.get("_source_ranges") or []}
                ]
            result_name, is_merged, embed_warn = await merge_or_create(
                content=item["content"],
                tags=item.get("tags") or [],
                importance=item.get("importance") or 5,
                domain=item.get("domain") or ["未分类"],
                valence=item.get("valence") or 0.5,
                arousal=item.get("arousal") or 0.3,
                name=item.get("name", ""),
                title=normalize_memory_title(item.get("name", "")),
                why_remembered=why_remembered,
                merge_why_remembered=why_remembered,
                source_tool="grow",
                source_refs=source_refs,
                grow_batch_id=batch_id,
                test_data=test_data,
            )
        except Exception as e:
            rt.logger.warning(
                f"Failed to process diary item / 日记条目处理失败: "
                f"{item.get('name', '?')}: {e}"
            )
            return {"line": f"⚠️{item.get('name', '?')}"}
        return {
            "line": (
                f"📎{result_name}" if is_merged
                else f"📝{item.get('name', result_name)}"
            ),
            "merged": is_merged,
            "embed_warn": embed_warn,
            "dup_check": None if is_merged else (result_name, item["content"]),
        }

    outcomes = await asyncio.gather(*(_process_item(item) for item in items))

    results = []
    created = 0
    merged = 0
    embed_warnings = []
    for outcome in outcomes:
        results.append(outcome["line"])
        if outcome.get("merged") is True:
            merged += 1
        elif outcome.get("merged") is False:
            created += 1
        embed_warn = outcome.get("embed_warn")
        if embed_warn and embed_warn not in embed_warnings:
            embed_warnings.append(embed_warn)
        dup_check = outcome.get("dup_check")
        if dup_check:
            asyncio.create_task(check_duplicate_for(*dup_check))

    asyncio.create_task(check_plan_resolution(content))
    summary = f"{len(items)}条|新{created}合{merged} batch:{batch_id}\n" + "\n".join(results)
    if embed_warnings:
        summary += f"\n⚠️ {embed_warnings[0]}"
    return summary


async def grow_items(items: list, source_content: str = "", test_data: bool = False) -> str:
    """预拆分模式：上层 AI 已把长文拆成 N 条最终正文，直接逐字入库。

    与 grow_core 的关键差别（issue 的诉求）：
    - **不调 digest**：跳过廉价 LLM 的二次拆分+改写，正文一字不动（消除第二次失真）；
    - 每条只调 analyze() 打元数据（domain/valence/arousal/tags/name），不碰正文；
    - 合并走 raw_merge=True（原文追加，不 LLM 压缩老+新），消除第三次失真。
    存储沿用 grow 风格：共享 grow_batch_id，source_tool=grow，dashboard 仍可按批展示。
    """
    payload_err = check_grow_items_payload(items)
    if payload_err:
        return payload_err

    # 规整：字典条目会保留人工给出的最终元数据；未给出的字段才由 analyze 补齐。
    clean: list[dict] = []
    for it in items:
        if isinstance(it, str):
            s = it.strip()
            item = {"content": s}
        elif isinstance(it, dict):
            s = it.get("content", "").strip()
            item = dict(it)
            item["content"] = s
            if isinstance(item.get("why_remembered"), str):
                item["why_remembered"] = item["why_remembered"].strip()
        else:
            s = ""
        if s:
            clean.append(item)
    if not clean:
        raise ToolInputError("items 为空或都不合法，未创建任何桶。")
    if not source_content.strip() and any(
        item.get("source_ranges") not in (None, [], "") for item in clean
    ):
        raise ToolInputError("source_ranges 需要同时提供 content 作为原文，未创建任何桶。")

    source_ref = ""
    if source_content and source_content.strip():
        try:
            from ombrebrain.storage.source_store import normalize_source_ranges

            line_count = len(source_content.splitlines()) or 1
            for item in clean:
                ranges = normalize_source_ranges(item.get("source_ranges"))
                if any(end > line_count for _, end in ranges):
                    raise ValueError(f"source_ranges 超出原文总行数 {line_count}")
                item["_source_ranges"] = ranges
            source_ref = rt.source_store.put(source_content)
        except (OSError, ValueError) as exc:
            raise ToolInputError(f"原文证据保存失败，未创建任何桶：{safe_error_detail(exc)}")

    batch_id = f"g_{uuid.uuid4().hex[:12]}"

    async def _process_item(item: dict) -> dict:
        """处理一条预拆分 item：只打标不改写正文，独立于其它 item。"""
        content_str = item["content"]
        size_err = check_content_size(content_str)
        if size_err:
            return {"line": f"⚠️（{size_err}）"}
        try:
            # 只打标，不改写正文；打标失败（如 API key 未配置）不应丢正文——
            # 落回本地中性元数据，与 hold 的降级行为保持一致（见 tools/hold/core.py）。
            needs_analysis = (
                not str(item.get("title") or "").strip()
                or item.get("tags") is None
                or item.get("domain") is None
                or item.get("valence") is None
                or item.get("arousal") is None
                or item.get("importance") is None
            )
            default_analysis = getattr(rt.dehydrator, "_default_analysis", None)
            meta = default_analysis() if callable(default_analysis) else {
                "domain": ["未分类"],
                "valence": 0.5,
                "arousal": 0.3,
                "tags": [],
                "suggested_name": "",
            }
            item_metadata_fallback = False
            if needs_analysis:
                try:
                    meta = await rt.dehydrator.analyze(content_str)
                except Exception as e:
                    item_metadata_fallback = True
                    rt.logger.warning(
                        "grow items metadata analysis failed; preserving raw content with local defaults / "
                        "grow items 打标失败，使用本地默认元数据并原样保存正文: "
                        f"{type(e).__name__}: {e}"
                    )
            explicit_title = normalize_memory_title(item.get("title"))
            explicit_tags = item.get("tags")
            if isinstance(explicit_tags, str):
                explicit_tags = [t.strip() for t in explicit_tags.split(",") if t.strip()]
            if not isinstance(explicit_tags, list):
                explicit_tags = None
            explicit_domain = item.get("domain")
            if isinstance(explicit_domain, str):
                explicit_domain = [explicit_domain.strip()] if explicit_domain.strip() else []
            if not isinstance(explicit_domain, list):
                explicit_domain = None
            try:
                importance = int(
                    item["importance"]
                    if item.get("importance") is not None
                    else meta.get("importance", 5)
                )
            except (TypeError, ValueError, OverflowError):
                importance = 5
            try:
                valence = float(item.get("valence", meta.get("valence", 0.5)))
            except (TypeError, ValueError, OverflowError):
                valence = float(meta.get("valence", 0.5))
            try:
                arousal = float(item.get("arousal", meta.get("arousal", 0.3)))
            except (TypeError, ValueError, OverflowError):
                arousal = float(meta.get("arousal", 0.3))

            source_refs = None
            if source_ref:
                ranges = item.get("_source_ranges") or []
                source_refs = [{"ref": source_ref, "ranges": ranges}]

            inferred_title = normalize_memory_title(meta.get("suggested_name", ""))
            final_title = explicit_title or inferred_title
            why_remembered = str(item.get("why_remembered") or "").strip()
            result_name, is_merged, embed_warn = await merge_or_create(
                content=content_str,
                tags=explicit_tags if explicit_tags is not None else (meta.get("tags") or []),
                importance=importance,
                domain=explicit_domain if explicit_domain is not None else (meta.get("domain") or ["未分类"]),
                valence=valence,
                arousal=arousal,
                name=final_title,
                title=final_title,
                why_remembered=why_remembered,
                merge_why_remembered=why_remembered,
                source_refs=source_refs,
                quotes=item.get("quotes") or None,
                source_tool="grow",
                grow_batch_id=batch_id,
                raw_merge=True,  # 逐字追加，合并不压缩
                test_data=test_data,
            )
        except Exception as e:
            rt.logger.warning(f"grow items 条目处理失败 / verbatim item failed: {e}")
            return {"line": "⚠️"}
        return {
            # 新建时回标题而不是 bucket_id：与 digest 路径保持一致。
            # 之前这里回 12 位 hex，调用方光看返回无法确认存进去的是什么，
            # 还得再查一次目录——同一个工具的两条路径不该一条人能读、一条不能。
            "line": f"📎{result_name}" if is_merged else f"📝{final_title or result_name}",
            "merged": is_merged,
            "embed_warn": embed_warn,
            "dup_check": None if is_merged else (result_name, content_str),
            "metadata_fallback": item_metadata_fallback,
        }

    outcomes = await asyncio.gather(*(_process_item(item) for item in clean))

    results = []
    created = 0
    merged = 0
    embed_warnings = []
    metadata_fallback = False
    for outcome in outcomes:
        results.append(outcome["line"])
        if outcome.get("merged") is True:
            merged += 1
        elif outcome.get("merged") is False:
            created += 1
        embed_warn = outcome.get("embed_warn")
        if embed_warn and embed_warn not in embed_warnings:
            embed_warnings.append(embed_warn)
        if outcome.get("metadata_fallback"):
            metadata_fallback = True
        dup_check = outcome.get("dup_check")
        if dup_check:
            asyncio.create_task(check_duplicate_for(*dup_check))

    asyncio.create_task(check_plan_resolution("\n".join(item["content"] for item in clean)))
    summary = f"{len(clean)}条(预拆分·逐字)|新{created}合{merged} batch:{batch_id}\n" + "\n".join(results)
    if embed_warnings:
        summary += f"\n⚠️ {embed_warnings[0]}"
    if metadata_fallback:
        summary += "\n⚠️ 打标 API 暂不可用：正文已逐字保存，未做任何压缩；元数据暂用本地中性值。"
        if any(not (item.get("title") or "").strip() for item in clean):
            summary += " 无标题的桶需先在 Dashboard 设置标题。"
    return summary
