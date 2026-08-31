"""
========================================
tools/grow/shortpath.py — grow 短内容快速路径
========================================

短内容（<30 字，剥空白后）跳过 dehydrator.digest，直接走 analyze +
merge_or_create，省一次 LLM 拆分调用。

关键行为：
- 调 analyze 拿 domain/valence/arousal/tags/suggested_name
- 用 raw_merge=True 与 hold 对齐：保留原文不压缩（修了 2.0 之前
  短日记被 LLM 偷偷压缩的 bug）
- 写完 fire-and-forget：plan 完成建议 + 新桶疑似重复扫描

不做什么（边界）：
- 不拆分：短到这种程度本就该是单条
- 不做 importance 范围裁剪：尊重 analyze 的输出（默认 5）

对外暴露：grow_shortpath(content) → str
========================================
"""

import asyncio
import uuid

try:
    from errors import llm_step_failed_error
except ImportError:  # pragma: no cover - 包内导入兜底
    from ...errors import llm_step_failed_error  # type: ignore

from .. import _runtime as rt
from .._common import merge_or_create, check_duplicate_for, check_plan_resolution


async def grow_shortpath(content: str, test_data: bool = False) -> str:
    rt.logger.info(f"grow short-content fast path: {len(content.strip())} chars")
    try:
        analysis = await rt.dehydrator.analyze(content, include_why=True)
    except Exception as e:
        rt.logger.error(
            "grow short analysis failed: err_type=%s detail=hidden",
            type(e).__name__,
        )
        raise llm_step_failed_error(
            "打标",
            api_available=getattr(rt.dehydrator, "api_available", True),
        ) from e
    importance = analysis.get("importance", 5) if isinstance(analysis.get("importance"), int) else 5
    raw_why_remembered = analysis.get("why_remembered")
    why_remembered = (
        raw_why_remembered.strip()
        if isinstance(raw_why_remembered, str)
        else ""
    )
    # iter 2.0：短路径也是一次 grow 调用 → 仍生成 batch_id，便于 dashboard 聚合，
    # 即使 batch 里只有一条记录也保留字段，schema 一致。
    batch_id = f"g_{uuid.uuid4().hex[:12]}"
    result_name, is_merged, embed_warn = await merge_or_create(
        content=content.strip(),
        tags=analysis.get("tags", []),
        importance=importance,
        domain=analysis.get("domain", ["未分类"]),
        valence=analysis.get("valence", 0.5),
        arousal=analysis.get("arousal", 0.3),
        name=analysis.get("suggested_name", ""),
        raw_merge=True,
        why_remembered=why_remembered,
        merge_why_remembered=why_remembered,
        source_tool="grow",
        grow_batch_id=batch_id,
        test_data=test_data,
    )
    action = "合并" if is_merged else "新建"
    asyncio.create_task(check_plan_resolution(content, source_bucket_id=result_name))
    if not is_merged:
        asyncio.create_task(check_duplicate_for(result_name, content.strip()))
    result = (
        "短内容已按 hold 路径保存为单条记忆，没有拆分。\n"
        f"{action} → {result_name} | "
        f"{','.join(analysis.get('domain', []))} "
        f"V{analysis.get('valence', 0.5):.1f}/A{analysis.get('arousal', 0.3):.1f}"
    )
    if embed_warn:
        result += f"\n⚠️ {embed_warn}"
    return result
