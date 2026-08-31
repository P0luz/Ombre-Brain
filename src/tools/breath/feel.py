"""
========================================
tools/breath/feel.py — feel 检索通道
========================================

由 MCP 工具 `feel(query=...)` 调用，也接受 breath(domain="feel") 的老路径。

关键行为（3.0.0 起）：
- **必须给关键词**，不再全量返回。feel 会越攒越多，无差别倒出来既挤上下文，
  也让「我此刻在想的这件事，我以前怎么感受的」这个真实问题被淹没。
- 关键词走向量检索：`search_similar(allowed_bucket_ids=feel 集合)` 把候选限定在
  feel 桶内，相似度 >= 0.65 才算命中。同一件事换个说法也能找回来。
- 向量不可用时退回关键词子串匹配，并在返回里明说降级——不假装检索质量没变。
- 命中后逐字返回完整正文，不截断、不摘要、不调 LLM。

不做什么（边界）：
- 不返回未命中的 feel，也不用低相关的凑数。
- 不做 dehydrate 调用；feel 原文短，直接展示。

对外暴露：surface_feels(query, max_tokens) → str
========================================
"""

from datetime import datetime  # noqa: F401 —— 供签名注解使用

from .. import _runtime as rt
from ..plan.core import is_letter_bucket
from ._date_range import bucket_in_created_range
from ._shared import footprint_reader, render_within_budget

# 与 breath_search 的向量通道保持同一门槛，避免两条检索面给出不同的"相关"标准。
# 0.65 是试出来的。0.6 的时候搜「下雨」能把「今天好累」勾出来，0.7 又谁都不认识谁。
# 中间那个数字试了一下午，试到最后开始怀疑"相关"这个词本身有没有意义。
_FEEL_SEMANTIC_THRESHOLD = 0.65
_SEMANTIC_DISABLED_NOTE = "[检索降级：语义索引暂不可用，本次仅按关键词字面匹配。]"
_NEEDS_QUERY = (
    "feel 需要一个关键词。\n"
    "感受不是列表，是「我此刻在想的这件事，我以前怎么感受的」——"
    "先说你在想什么，我才知道该翻哪一段。\n"
    '例：feel(query="被误解")、feel(query="她生病")、feel(query="删掉自己写的东西")。'
)


async def _semantic_hits(query: str, feel_ids: set[str]) -> tuple[dict[str, float], str]:
    """在 feel 集合内做向量检索，返回 {bucket_id: 相似度} 与降级提示。"""
    engine = rt.embedding_engine
    if not engine or not getattr(engine, "enabled", False):
        return {}, _SEMANTIC_DISABLED_NOTE
    try:
        pairs = await engine.search_similar(
            query,
            top_k=max(len(feel_ids), 1),
            allowed_bucket_ids=set(feel_ids),
        )
        return {
            str(bucket_id): float(score)
            for bucket_id, score in pairs
            if float(score) >= _FEEL_SEMANTIC_THRESHOLD
        }, ""
    except Exception as exc:
        rt.logger.warning(
            f"feel semantic search failed; falling back to literal match: "
            f"{type(exc).__name__}: {exc}"
        )
        return {}, _SEMANTIC_DISABLED_NOTE


def _literal_hits(query: str, feels: list[dict]) -> set[str]:
    """关键词字面兜底：向量不可用、或向量一条都没命中时用。"""
    # 兜底逻辑就是这样，写的时候觉得纯属白写，真用上那天会庆幸。
    needle = query.strip().lower()
    if not needle:
        return set()
    hits = set()
    for bucket in feels:
        haystack = f"{bucket.get('content', '')} {bucket.get('metadata', {}).get('title', '')}"
        if needle in haystack.lower():
            hits.add(str(bucket.get("id") or ""))
    return hits


async def surface_feels(
    query: str = "",
    max_tokens: int = 0,
    created_from: "datetime | None" = None,
    created_to: "datetime | None" = None,
) -> str:
    if not str(query or "").strip():
        return _NEEDS_QUERY
    query = str(query).strip()

    try:
        all_buckets = await rt.bucket_mgr.list_all(include_archive=False)
        feels = [
            b for b in all_buckets
            if b.get("metadata", {}).get("type") == "feel"
            and not is_letter_bucket(b)
            # 3.6.0：feel 也认时间区间。「上个月我是什么感受」是个真实的问题，
            # 在此之前 date_from/date_to 走到这条分支就消失了。
            and bucket_in_created_range(b, created_from, created_to)
        ]
        if not feels:
            # 空的时候其实挺好的。什么都还没发生。
            if created_from is not None or created_to is not None:
                return "这段时间里没有留下过 feel。"
            return "还没有留下过 feel。"

        feel_ids = {str(b.get("id") or "") for b in feels}
        scores, notice = await _semantic_hits(query, feel_ids)
        if not scores:
            literal = _literal_hits(query, feels)
            scores = {bucket_id: 0.0 for bucket_id in literal}

        if not scores:
            head = f"没有和「{query}」相关的 feel。"
            return f"{notice}\n{head}" if notice else head

        matched = [b for b in feels if str(b.get("id") or "") in scores]
        # 先按相似度，再按时间倒序：同样相关时,新的感受更接近现在的我。
        # ……虽然翻到三个月前那条的时候还是会愣一下。那时候的我在想什么啊。
        matched.sort(
            key=lambda b: (
                scores.get(str(b.get("id") or ""), 0.0),
                str(b.get("metadata", {}).get("created", "")),
            ),
            reverse=True,
        )

        lines, omitted = render_within_budget(
            matched, max_tokens, footprint_reader()
        )

        header = f"=== 和「{query}」相关的 feel（{len(matched)} 条）===\n"
        out = (f"{notice}\n" if notice else "") + header + "\n---\n".join(lines)
        if omitted:
            out += f"\n\n另有 {omitted} 条相关 feel 因 token 预算不足未返回；正文未截断或摘要。"
        return out
    except Exception as e:
        # 到这里说明我没想到。
        # 咪。
        rt.logger.error(f"Feel retrieval failed: {e}")
        return "读取 feel 失败。"
