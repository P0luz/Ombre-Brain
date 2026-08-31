"""dream 里 feel 段的相关性排序（3.2.0）。

**改之前**：取全量 feel，按 created 倒序，在预算内塞。相关性完全没参与——
最新的 feel 未必和这次 dream 在聊的事有关，于是这一段的语义其实是
「我最近写的感受」，而不是「和这件事有关的感受」。

**改之后**：拿当次 dream 的候选桶合并文本当基准，给每条 feel 打分，
只返回最相关的几条。段落语义从"最近"变成"相关"。

打分两路融合：

- **向量 0.7** —— 表达语义，换个说法也认得出
- **关键词 0.3** —— 兜住专有名词（人名、地名、项目名这类向量容易糊掉的）

单字 token 一律丢弃：jieba 切出来的「的」「了」「我」这类虚词会让重合度虚高，
两段毫不相关的文字也能靠虚词凑出高分。

向量不可用时退回纯关键词，并让调用方在输出里明说降级——
不假装检索质量没变。
"""

from __future__ import annotations

from bm25_index import _tokenize

from .. import _runtime as rt

# 融合权重。向量表达语义，关键词兜专有名词。
VECTOR_WEIGHT = 0.7
KEYWORD_WEIGHT = 0.3
# 低于这个融合分不返回。宁可返回 2 条也不用低相关的凑满 5 条。
RELEVANCE_THRESHOLD = 0.5
# 上限。feel 段是"和这件事有关的感受"，不是感受列表。
MAX_FEELS = 5


def _content_tokens(text: str) -> set[str]:
    """分词并丢掉单字 token。

    单字在中文里绝大多数是虚词（的/了/我/是），保留它们会让任意两段文字
    都有可观的重合度——那样关键词这一路就不是在测相关性，是在测文本长度。
    """
    return {token for token in _tokenize(text or "") if len(token) > 1}


def keyword_overlap(feel_text: str, reference_tokens: set[str]) -> float:
    """feel 的实词里有多少出现在这次 dream 聊的事情里，范围 0..1。

    分母取 feel 自己的词数而不是并集：feel 短、reference 长，用 Jaccard
    会让所有 feel 的分数都被 reference 的长度压扁，区分不出谁更相关。
    """
    if not reference_tokens:
        return 0.0
    feel_tokens = _content_tokens(feel_text)
    if not feel_tokens:
        return 0.0
    return len(feel_tokens & reference_tokens) / len(feel_tokens)


async def _vector_scores(reference_text: str, feel_ids: set[str]) -> tuple[dict[str, float], bool]:
    """在 feel 集合内做向量检索，返回 ({bucket_id: 相似度}, 向量是否可用)。"""
    engine = getattr(rt, "embedding_engine", None)
    if not engine or not getattr(engine, "enabled", False) or not feel_ids:
        return {}, False
    try:
        pairs = await engine.search_similar(
            reference_text,
            top_k=max(len(feel_ids), 1),
            allowed_bucket_ids=set(feel_ids),
        )
    except Exception as exc:  # noqa: BLE001 - 降级不该让 dream 整体失败
        rt.logger.warning(
            f"dream feel vector ranking failed; falling back to keywords: "
            f"{type(exc).__name__}: {exc}"
        )
        return {}, False
    return {str(bid): float(score) for bid, score in pairs}, True


async def rank_feels(
    feels: list[dict],
    reference_text: str,
    *,
    max_feels: int = MAX_FEELS,
    threshold: float = RELEVANCE_THRESHOLD,
) -> tuple[list[tuple[dict, float]], bool]:
    """按与 reference_text 的相关性挑出最多 max_feels 条 feel。

    返回 ``([(feel, score), ...], 向量是否可用)``，已按分数降序。
    低于 threshold 的一条都不返回——宁可少给，也不用低相关的凑数。
    """
    if not feels or not str(reference_text or "").strip():
        return [], False

    feel_ids = {str(f.get("id") or "") for f in feels if f.get("id")}
    vector_scores, vector_ok = await _vector_scores(reference_text, feel_ids)
    reference_tokens = _content_tokens(reference_text)

    scored: list[tuple[dict, float]] = []
    for feel in feels:
        fid = str(feel.get("id") or "")
        keyword = keyword_overlap(feel.get("content", ""), reference_tokens)
        if vector_ok:
            vector = vector_scores.get(fid, 0.0)
            score = VECTOR_WEIGHT * vector + KEYWORD_WEIGHT * keyword
        else:
            # 向量不可用：关键词独自承担，不按 0.3 缩放——否则门槛会变成
            # 事实上的 1.67 倍，整段静默消失。
            score = keyword
        if score >= threshold:
            scored.append((feel, score))

    scored.sort(
        key=lambda item: (
            item[1],
            str((item[0].get("metadata") or {}).get("created", "")),
        ),
        reverse=True,
    )
    return scored[:max_feels], vector_ok
