"""dream 的 feel 段按相关性挑选，不是按时间。

改之前这一段的语义其实是「我最近写的感受」——最新的 feel 未必和这次
dream 在聊的事有关。改之后是「和这件事有关的感受」。

这里测的重点是**不相关的进不来**，而不是"相关的能出来"：
一个只会挑最近 5 条的实现同样能让"相关的出来"，但那什么都没改。
"""

from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from tools.dream.feel_rank import (
    MAX_FEELS,
    RELEVANCE_THRESHOLD,
    keyword_overlap,
    rank_feels,
)


def _feel(bucket_id: str, content: str, created: str = "2026-08-18T10:00:00") -> dict:
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {"type": "feel", "created": created, "valence": 0.5},
    }


class _NoVector:
    enabled = False


class _Vector:
    """按预设分数返回，模拟真实向量检索。"""

    enabled = True

    def __init__(self, scores):
        self.scores = scores

    async def search_similar(self, query, top_k=0, allowed_bucket_ids=None):
        return [
            (bid, score)
            for bid, score in self.scores.items()
            if not allowed_bucket_ids or bid in allowed_bucket_ids
        ]


@pytest.fixture(autouse=True)
def _restore_runtime():
    """用完把 runtime 放回去。

    这些用例要改全局 `rt.embedding_engine`，不还原的话会污染同一次 pytest
    里后跑的 dream 用例——单独跑全绿、全量跑却红，最难查的那种。
    """
    saved = (
        getattr(rt, "config", None),
        getattr(rt, "embedding_engine", None),
        getattr(rt, "logger", None),
    )
    yield
    rt.config, rt.embedding_engine, rt.logger = saved


def _install(engine):
    rt.config = {}
    rt.embedding_engine = engine
    rt.logger = MagicMock()


# --------------------------------------------------------------
# 关键词打分
# --------------------------------------------------------------


def test_single_character_tokens_are_ignored():
    """虚词不能算相关。

    「的」「了」「我」这类单字几乎出现在任何中文文本里，算进去的话
    两段毫不相关的文字也能凑出高重合度——那样这一路测的是文本长度，
    不是相关性。
    """
    reference = {"许可证", "开源"}
    # 整句只有虚词与单字，实词一个都不沾
    assert keyword_overlap("我的了是在有", reference) == 0.0


def test_overlap_uses_feel_side_as_denominator():
    """分母取 feel 自己的词数：feel 短、基准长，用并集会把分数压扁。"""
    reference = {"许可证", "开源", "协议", "镜像", "构建", "发布"}
    high = keyword_overlap("许可证 开源", reference)
    low = keyword_overlap("许可证 鸡公煲 青柑 普洱", reference)
    # 同样命中"许可证"，但后者掺了大量无关实词，分母被自己的词撑大
    assert high > low
    assert high > 0.5
    # jieba 的 cut_for_search 会切出子词（许可/可证/许可证），所以命中率
    # 达不到 1.0——这是分词器的既有行为，两侧一致，不影响排序
    assert high < 1.0


# --------------------------------------------------------------
# 排序与门槛
# --------------------------------------------------------------


@pytest.mark.asyncio
async def test_irrelevant_feels_are_dropped_even_if_newest():
    """最新但不相关的 feel 不该出现——这正是改这一段的原因。"""
    _install(_Vector({"related": 0.9, "unrelated": 0.1}))
    feels = [
        _feel("unrelated", "楼下鸡公煲好美味", created="2026-08-18T23:00:00"),
        _feel("related", "为许可证绕了一大圈，最后回到原点", created="2026-01-01T00:00:00"),
    ]

    ranked, vector_ok = await rank_feels(feels, "今天在改许可证，MIT 换 AGPL 又换回来")

    assert vector_ok is True
    assert [f["id"] for f, _ in ranked] == ["related"]


@pytest.mark.asyncio
async def test_nothing_returned_when_all_below_threshold():
    """宁可一条不返回，也不用低相关的凑数。"""
    _install(_Vector({"a": 0.2, "b": 0.1}))
    feels = [_feel("a", "鸡公煲"), _feel("b", "青柑普洱")]

    ranked, _ = await rank_feels(feels, "量子力学与规范场论")

    assert ranked == []


@pytest.mark.asyncio
async def test_result_is_capped_at_five():
    _install(_Vector({f"f{i}": 0.95 for i in range(12)}))
    feels = [_feel(f"f{i}", f"许可证相关的第{i}条感受") for i in range(12)]

    ranked, _ = await rank_feels(feels, "许可证")

    assert len(ranked) == MAX_FEELS


@pytest.mark.asyncio
async def test_ranking_is_by_score_not_by_time():
    _install(_Vector({"old_relevant": 0.95, "new_weak": 0.72}))
    feels = [
        _feel("new_weak", "许可证", created="2026-08-18T23:00:00"),
        _feel("old_relevant", "许可证 开源 协议", created="2020-01-01T00:00:00"),
    ]

    ranked, _ = await rank_feels(feels, "许可证 开源 协议")

    assert [f["id"] for f, _ in ranked][0] == "old_relevant"


# --------------------------------------------------------------
# 降级
# --------------------------------------------------------------


@pytest.mark.asyncio
async def test_falls_back_to_keywords_when_vectors_unavailable():
    """向量不可用时退回纯关键词，并如实告诉调用方降级了。"""
    _install(_NoVector())
    feels = [
        _feel("hit", "许可证 开源 协议"),
        _feel("miss", "鸡公煲 青柑"),
    ]

    ranked, vector_ok = await rank_feels(feels, "许可证 开源 协议 镜像")

    assert vector_ok is False
    assert [f["id"] for f, _ in ranked] == ["hit"]


@pytest.mark.asyncio
async def test_keyword_only_scores_are_not_scaled_down():
    """降级时关键词独自承担，不按 0.3 缩放。

    如果缩放，门槛 0.5 会变成事实上的 1.67——纯关键词永远够不着，
    整段会静默消失，而不是降级。
    """
    _install(_NoVector())
    # 完全命中：关键词得分 1.0，缩放后只有 0.3，会被门槛挡掉
    feels = [_feel("full", "许可证 开源")]

    ranked, vector_ok = await rank_feels(feels, "许可证 开源")

    assert vector_ok is False
    assert len(ranked) == 1
    assert ranked[0][1] >= RELEVANCE_THRESHOLD


@pytest.mark.asyncio
async def test_vector_failure_degrades_instead_of_raising():
    """向量炸了不该让整个 dream 失败——记忆本身比这一段重要。"""

    class _Exploding:
        enabled = True

        async def search_similar(self, *_args, **_kwargs):
            raise RuntimeError("index unavailable")

    _install(_Exploding())
    ranked, vector_ok = await rank_feels([_feel("a", "许可证 开源")], "许可证 开源")

    assert vector_ok is False
    assert [f["id"] for f, _ in ranked] == ["a"]


@pytest.mark.asyncio
async def test_empty_reference_returns_nothing():
    _install(_Vector({"a": 0.9}))
    assert await rank_feels([_feel("a", "x")], "") == ([], False)
