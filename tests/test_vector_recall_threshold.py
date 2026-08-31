"""纯语义召回通道 —— 语义够相关就该被找到，哪怕一个关键词都不沾。

背景：3.2.0 之前 `_VECTOR_RECALL_THRESHOLD=0.65`，这条通道**事实上不存在**。
对 917 桶真实记忆扫描发现，9 个宽泛查询一共只有 1 条桶能靠它进来，
「我的工作」「同事」「情绪」全是 0 —— 代码里写着 `text_match or semantic_match`，
但后一支从来不为真。OB 名义上是混合检索，实际是纯关键词检索。

原因：semantic 权重只占 2.5/13.5≈18.5%，一条桶哪怕相似度 0.9，单靠这一维
也只贡献约 16.7 分，离 `fuzzy_threshold=50` 差得远。而「我的工作」这几个字
根本不会字面出现在记忆里。

所以这里测的是**关键词完全不沾的桶能不能靠语义被找回**——那正是宽泛主题
查询的全部指望。
"""

import pytest


@pytest.mark.asyncio
async def test_semantically_relevant_bucket_is_found_without_any_keyword(
    bucket_mgr, monkeypatch
):
    """一个关键词都不匹配、但语义高度相关的桶，必须能被检索到。

    真实例子：「上线成功那一刻的踏实感，记一笔。」——十几个字、没有"工作"
    二字，但任何人都同意它属于「我的工作」。关键词检索永远够不到它。
    """
    bucket_id = await bucket_mgr.create(
        content="上线成功那一刻的踏实感，记一笔。",
        importance=5,
    )

    # 查询与正文没有任何字面重合，只有语义相关
    query = "我的工作"
    monkeypatch.setattr(
        bucket_mgr.embedding_engine,
        "search_similar",
        _fixed_similarity({bucket_id: 0.58}),
        raising=False,
    )

    results = await bucket_mgr.search(query, limit=20)

    assert bucket_id in {b["id"] for b in results}


@pytest.mark.asyncio
async def test_below_threshold_semantic_alone_does_not_enter(bucket_mgr, monkeypatch):
    """低于门槛的纯语义候选不得进入结果池。

    门要真的是门。0.45 那档实测每查询涌进 170 条、双通道印证率只剩 60%，
    那是拿噪音换召回。
    """
    bucket_id = await bucket_mgr.create(
        content="完全无关的一条记忆，只有微弱的语义相似。",
        importance=5,
    )
    monkeypatch.setattr(
        bucket_mgr.embedding_engine,
        "search_similar",
        _fixed_similarity({bucket_id: 0.40}),
        raising=False,
    )

    results = await bucket_mgr.search("我的工作", limit=20)

    assert bucket_id not in {b["id"] for b in results}


@pytest.mark.asyncio
async def test_threshold_is_configurable(test_config, fake_embedding_engine):
    """阈值必须能从 config 调，不用改代码。

    宽泛查询的合适门槛与语料强相关——别人的记忆库不一定是 0.55。
    """
    from bucket_manager import BucketManager

    cfg = dict(test_config)
    cfg["matching"] = {**(cfg.get("matching") or {}), "vector_recall_threshold": 0.72}
    mgr = BucketManager(cfg, embedding_engine=fake_embedding_engine)

    assert mgr.vector_recall_threshold == pytest.approx(0.72)


@pytest.mark.asyncio
async def test_invalid_config_falls_back_to_default(test_config, fake_embedding_engine):
    """配置写坏了要回落到默认值，不能让检索整体失效。"""
    from bucket_manager import BucketManager, _VECTOR_RECALL_THRESHOLD

    cfg = dict(test_config)
    cfg["matching"] = {**(cfg.get("matching") or {}), "vector_recall_threshold": "不是数字"}
    mgr = BucketManager(cfg, embedding_engine=fake_embedding_engine)

    assert mgr.vector_recall_threshold == pytest.approx(_VECTOR_RECALL_THRESHOLD)


def test_default_is_the_scanned_value():
    """默认值就是扫出来的那个数。

    改这个数字之前请重跑 src/bucket_manager.py 注释里记的那份扫描——
    0.55 是拐点，不是随手填的。
    """
    from bucket_manager import _VECTOR_RECALL_THRESHOLD

    assert _VECTOR_RECALL_THRESHOLD == pytest.approx(0.55)


def _fixed_similarity(scores: dict):
    async def _search_similar(query, top_k=0, allowed_bucket_ids=None):
        return [
            (bid, score)
            for bid, score in scores.items()
            if not allowed_bucket_ids or bid in allowed_bucket_ids
        ]

    return _search_similar
