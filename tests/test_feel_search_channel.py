"""feel 检索通道 —— 3.0.0 起必须带关键词，不再全量返回。

为什么改：feel 会越攒越多，无差别倒出来既挤上下文，也让「我此刻在想的这件事，
我以前怎么感受的」这个真实问题被淹没在时间序列里。

关键词走向量检索，候选限定在 feel 桶内（`search_similar(allowed_bucket_ids=...)`），
相似度 >= 0.65 才算命中；向量不可用时退回字面匹配并明确提示降级。
"""

from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from tools.breath.feel import surface_feels


class _NoopDecay:
    is_running = True

    async def ensure_started(self):
        return None


class _ExplodingDehydrator:
    async def dehydrate(self, content, meta=None):
        raise AssertionError("feel 通道不得调用 LLM")


class _VectorEngine:
    """按预设分数回答；同时记录 allowed_bucket_ids，用于验证候选被限定在 feel 内。"""

    enabled = True

    def __init__(self, scores: dict[str, float]):
        self.scores = scores
        self.last_allowed = None

    async def search_similar(self, query, top_k=10, allowed_bucket_ids=None):
        self.last_allowed = allowed_bucket_ids
        pairs = sorted(self.scores.items(), key=lambda kv: kv[1], reverse=True)
        if allowed_bucket_ids is not None:
            pairs = [(b, s) for b, s in pairs if b in allowed_bucket_ids]
        return pairs[:top_k]


class _DisabledEngine:
    enabled = False


def _install(bucket_mgr, engine):
    rt.config = {"surfacing": {}}
    rt.bucket_mgr = bucket_mgr
    rt.decay_engine = _NoopDecay()
    rt.dehydrator = _ExplodingDehydrator()
    rt.embedding_engine = engine
    rt.logger = MagicMock()
    rt.fire_webhook = None
    rt.mark_op = None
    rt.record_v3_tool_event = lambda *a, **k: None


async def _make_feel(bucket_mgr, content: str) -> str:
    return await bucket_mgr.create(
        content=content, tags=[], importance=5, domain=["feel"],
        valence=0.5, arousal=0.3, name=None, bucket_type="feel",
    )


@pytest.mark.asyncio
async def test_missing_query_asks_for_a_keyword_instead_of_dumping_everything(bucket_mgr):
    _install(bucket_mgr, _DisabledEngine())
    await _make_feel(bucket_mgr, "一条不该被无差别倒出来的感受。")

    out = await surface_feels(query="", max_tokens=10000)

    assert "需要一个关键词" in out
    assert "一条不该被无差别倒出来的感受。" not in out


@pytest.mark.asyncio
async def test_semantic_hit_returns_only_related_feels(bucket_mgr):
    related = await _make_feel(bucket_mgr, "被误解的时候，我第一反应是想把话说清楚。")
    unrelated = await _make_feel(bucket_mgr, "看到窗外的雨停了，心里松了一下。")
    engine = _VectorEngine({related: 0.82, unrelated: 0.20})
    _install(bucket_mgr, engine)

    out = await surface_feels(query="被误解", max_tokens=10000)

    assert "被误解的时候" in out
    assert "窗外的雨" not in out, "低于阈值的 feel 不能用来凑数"
    assert engine.last_allowed is not None, "候选必须限定在 feel 桶内"
    assert related in engine.last_allowed


@pytest.mark.asyncio
async def test_scores_below_threshold_are_not_returned(bucket_mgr):
    weak = await _make_feel(bucket_mgr, "一条只有一点点相关的感受。")
    _install(bucket_mgr, _VectorEngine({weak: 0.5}))  # < 0.65

    out = await surface_feels(query="完全不同的话题", max_tokens=10000)

    assert "一条只有一点点相关的感受。" not in out
    assert "没有和" in out


@pytest.mark.asyncio
async def test_vector_unavailable_falls_back_to_literal_and_says_so(bucket_mgr):
    await _make_feel(bucket_mgr, "删掉自己写的东西有点舍不得。")
    await _make_feel(bucket_mgr, "今天的风很大。")
    _install(bucket_mgr, _DisabledEngine())

    out = await surface_feels(query="舍不得", max_tokens=10000)

    assert "检索降级" in out, "降级必须明说，不能假装检索质量没变"
    assert "删掉自己写的东西有点舍不得。" in out
    assert "今天的风很大。" not in out


@pytest.mark.asyncio
async def test_empty_vault_says_no_feel_yet(bucket_mgr):
    _install(bucket_mgr, _DisabledEngine())

    out = await surface_feels(query="任何词", max_tokens=10000)

    assert "还没有留下过 feel" in out


@pytest.mark.asyncio
async def test_body_is_returned_verbatim(bucket_mgr):
    body = "我对这件事的感受是复杂的：既松了一口气，又觉得少了点什么。"
    bid = await _make_feel(bucket_mgr, body)
    _install(bucket_mgr, _VectorEngine({bid: 0.9}))

    out = await surface_feels(query="复杂", max_tokens=10000)

    assert body in out, "正文必须逐字返回，不摘要不截断"
