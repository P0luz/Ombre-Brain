import asyncio
import hashlib
from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from tools.breath import dispatch
from tools.breath._verbatim import render_stored_bucket
from tools.breath.importance import surface_by_importance
from tools.breath.search import surface_search
from tools.breath.surface import surface_default
from utils import strip_wikilinks


class ExplodingDehydrator:
    def __init__(self):
        self.calls = 0

    async def dehydrate(self, content, meta=None):
        self.calls += 1
        raise AssertionError("breath return path must not call the LLM")


class DisabledEmbedding:
    enabled = False


class ExplodingEmbedding:
    enabled = True

    async def search_similar_strict(self, query, top_k):
        raise AssertionError("exact bucket-id lookup must not call embedding")


class NoopDecay:
    is_running = True

    async def ensure_started(self):
        return None

    def calculate_score(self, meta):
        return float(meta.get("importance") or 5)


class OrderedBucketManager:
    def __init__(self, buckets):
        self.buckets = list(buckets)
        self.touched = []
        self.search_kwargs = {}

    async def search(self, query, **kwargs):
        self.search_kwargs = dict(kwargs)
        return list(self.buckets)

    async def touch_many(self, bucket_ids, ripple=False):
        self.touched.extend(bucket_ids)

    async def list_all(self, include_archive=False):
        return list(self.buckets)


def _install_runtime(bucket_mgr, dehydrator=None):
    rt.config = {"surfacing": {}}
    rt.bucket_mgr = bucket_mgr
    rt.decay_engine = NoopDecay()
    rt.dehydrator = dehydrator or ExplodingDehydrator()
    rt.embedding_engine = DisabledEmbedding()
    rt.logger = MagicMock()
    rt.fire_webhook = None
    rt.mark_op = None
    rt.record_v3_tool_event = lambda *_args, **_kwargs: None
    return rt.dehydrator


@pytest.mark.asyncio
async def test_dispatch_keeps_default_breath_budget_and_allows_explicit_headroom(
    monkeypatch,
):
    _install_runtime(OrderedBucketManager([]))
    seen = []

    async def capture_default(
        *, max_results, max_tokens, tag_filter, created_from=None, created_to=None
    ):
        seen.append(max_tokens)
        return ""

    monkeypatch.setattr("tools.breath.surface_default", capture_default)

    await dispatch()
    rt.config["surfacing"]["breath_max_tokens"] = 10_000
    await dispatch()
    await dispatch(max_tokens=35_000)
    await dispatch(max_tokens=50_000)

    assert seen == [20_000, 10_000, 35_000, 40_000]


async def _search(query, **overrides):
    params = {
        "query": query,
        "max_results": 10,
        "max_tokens": 10000,
        "domain": "",
        "valence": -1,
        "arousal": -1,
        "tag_filter": [],
    }
    params.update(overrides)
    return await surface_search(**params)


def _returned_body(output: str, bucket_id: str, expected_length: int) -> str:
    marker = f"[bucket_id:{bucket_id}]"
    marker_at = output.index(marker)
    body_start = output.index("\n", marker_at) + 1
    return output[body_start:body_start + expected_length]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.asyncio
async def test_query_single_bucket_returns_stored_content_exactly(bucket_mgr, monkeypatch):
    original = (
        "第二场风暴 你只是claude\n\n"
        "原话中的次数是三次，顺序是先确认、再等待、最后离开。\n"
        "这是一段普通叙述，不是任务清单。"
    )
    bucket_id = await bucket_mgr.create(content=original, domain=["记忆"], importance=8)
    stored_before = (await bucket_mgr.get(bucket_id))["content"]
    dehydrator = _install_runtime(bucket_mgr)
    monkeypatch.setattr("tools.breath.search.random.random", lambda: 1.0)

    output = await dispatch(
        query="第二场风暴 你只是claude",
        max_tokens=10000,
        max_results=10,
    )
    actual = _returned_body(output, bucket_id, len(stored_before))
    await asyncio.sleep(0)
    stored_after = (await bucket_mgr.get(bucket_id))["content"]

    assert actual == stored_before
    assert _sha256(actual) == _sha256(stored_before)
    assert stored_after == stored_before
    assert dehydrator.calls == 0
    assert "待办" not in output
    assert "\n- " not in output


@pytest.mark.asyncio
async def test_query_equal_to_bucket_id_reads_raw_content_without_indexes(
    bucket_mgr, monkeypatch
):
    original = "- 第一条原始 bullet\n- 第二条保留缩进\n  - 子项不能被摘要\n- 第三条"
    bucket_id = await bucket_mgr.create(
        content=original, domain=["记忆"], importance=10, pinned=True
    )
    dehydrator = _install_runtime(bucket_mgr)
    rt.embedding_engine = ExplodingEmbedding()

    async def unexpected_search(*args, **kwargs):
        raise AssertionError("exact bucket-id lookup must not call BM25/search")

    monkeypatch.setattr(bucket_mgr, "search", unexpected_search)

    output = await dispatch(query=bucket_id, max_tokens=10000)
    actual = _returned_body(output, bucket_id, len(original))
    await asyncio.sleep(0)

    assert actual == original
    assert _sha256(actual) == _sha256(original)
    assert dehydrator.calls == 0


@pytest.mark.asyncio
async def test_query_multiple_buckets_return_each_body_exactly(bucket_mgr, monkeypatch):
    contents = [
        "群星校验词：第一段。\n保留 [[原始双链]] 和标点；A=1。",
        "群星校验词：第二段。\n次数=7，先后顺序不能变化。\n",
    ]
    ids = [
        await bucket_mgr.create(content=content, domain=["测试"], importance=7)
        for content in contents
    ]
    stored = {bucket_id: (await bucket_mgr.get(bucket_id))["content"] for bucket_id in ids}
    # 展示文本只做双链正则清理（strip_wikilinks），不改磁盘原文：
    # 磁盘上 [[原始双链]] 仍保留括号，但 breath 返回的展示文本会去掉它们。
    displayed = {bucket_id: strip_wikilinks(content) for bucket_id, content in stored.items()}
    dehydrator = _install_runtime(bucket_mgr)
    monkeypatch.setattr("tools.breath.search.random.random", lambda: 1.0)

    output = await _search("群星校验词")

    for bucket_id, expected in displayed.items():
        actual = _returned_body(output, bucket_id, len(expected))
        assert actual == expected
        assert _sha256(actual) == _sha256(expected)
    assert dehydrator.calls == 0


@pytest.mark.asyncio
async def test_catalog_still_returns_metadata_without_body(bucket_mgr):
    body = "目录模式绝不能返回的完整私密正文。"
    await bucket_mgr.create(content=body, name="目录校验", domain=["测试"], importance=9)
    dehydrator = _install_runtime(bucket_mgr)

    output = await dispatch(catalog=True)

    assert "目录校验 | 测试 | 9" in output
    assert body not in output
    assert dehydrator.calls == 0


@pytest.mark.asyncio
async def test_breath_never_exposes_source_evidence(
    bucket_mgr, monkeypatch
):
    source_ref = "src_" + "a" * 64
    body = "只返回这条记忆正文，不自动展开背后的聊天原文。"
    bucket_id = await bucket_mgr.create(
        content=body,
        title="京都计划",
        domain=["旅行"],
        importance=8,
        source_refs=[{"ref": source_ref, "ranges": [[1, 3]]}],
    )
    _install_runtime(bucket_mgr)
    monkeypatch.setattr("tools.breath.search.random.random", lambda: 1.0)

    output = await dispatch(query=bucket_id, max_tokens=10000)

    assert body in output
    # 原文回顾能力已删除：不再提示原文存在，也绝不泄漏 ref
    assert "source_available" not in output
    assert source_ref not in output


@pytest.mark.asyncio
async def test_catalog_never_exposes_source_evidence(bucket_mgr):
    source_ref = "src_" + "b" * 64
    body = "目录里绝不能出现的原文或记忆正文。"
    await bucket_mgr.create(
        content=body,
        name="京都旅行",
        title="京都旅行",
        domain=["旅行"],
        importance=9,
        source_refs=[{"ref": source_ref, "ranges": [[1, 1]]}],
    )
    _install_runtime(bucket_mgr)

    output = await dispatch(catalog=True)

    assert "京都旅行 | 旅行 | 9" in output
    # 原文回顾能力已删除：目录不再提示原文存在，也绝不泄漏 ref
    assert "source_available" not in output
    assert body not in output
    assert source_ref not in output


@pytest.mark.asyncio
async def test_token_budget_omits_whole_bucket_instead_of_truncating(monkeypatch):
    first = {
        "id": "first",
        "content": "第一条完整正文。",
        "metadata": {"type": "dynamic", "importance": 8, "domain": []},
    }
    second = {
        "id": "second",
        "content": "第二条正文绝不能只返回前半段。" * 20,
        "metadata": {"type": "dynamic", "importance": 7, "domain": []},
    }
    manager = OrderedBucketManager([first, second])
    dehydrator = _install_runtime(manager)
    monkeypatch.setattr("tools.breath.search.random.random", lambda: 1.0)
    _, first_cost = render_stored_bucket(
        first, "[bucket_id:first]", "👣 Footprint：暂时无法读取"
    )

    output = await _search("预算校验", max_tokens=first_cost)
    await asyncio.sleep(0)

    assert _returned_body(output, "first", len(first["content"])) == first["content"]
    assert "[bucket_id:second]" not in output
    assert second["content"][:20] not in output
    assert "token 预算不足" in output
    assert manager.touched == []
    assert dehydrator.calls == 0


@pytest.mark.asyncio
async def test_default_surface_skips_oversized_core_and_keeps_later_core(monkeypatch):
    oversized = {
        "id": "oversized-core",
        "content": "oversized " * 400,
        "metadata": {
            "type": "permanent",
            "importance": 10,
            "pinned": True,
            "domain": [],
        },
    }
    later = {
        "id": "later-core",
        "content": "Later core rule must still surface in full.",
        "metadata": {
            "type": "permanent",
            "importance": 10,
            "pinned": True,
            "domain": [],
        },
    }
    manager = OrderedBucketManager([oversized, later])
    _install_runtime(manager)
    monkeypatch.setattr("tools.breath.surface.random.random", lambda: 1.0)
    _, later_cost = render_stored_bucket(
        later,
        "📌 [核心准则] [bucket_id:later-core]",
        "👣 Footprint：暂时无法读取",
    )

    output = await surface_default(
        max_results=10,
        max_tokens=later_cost,
        tag_filter=[],
    )

    assert "[bucket_id:later-core]" in output
    assert later["content"] in output
    assert "[bucket_id:oversized-core]" not in output
    assert "token 预算不足" in output


@pytest.mark.asyncio
async def test_oversized_core_rule_does_not_take_ordinary_surfacing_down_with_it(monkeypatch):
    first_core = {
        "id": "first-core",
        "content": "First core rule fits completely.",
        "metadata": {
            "type": "permanent",
            "importance": 10,
            "pinned": True,
            "domain": [],
        },
    }
    oversized_core = {
        "id": "oversized-core",
        "content": "Oversized core rule " * 400,
        "metadata": {
            "type": "permanent",
            "importance": 10,
            "pinned": True,
            "domain": [],
        },
    }
    ordinary = {
        "id": "ordinary",
        "content": "Ordinary memory would fit the remaining budget.",
        "metadata": {
            "type": "dynamic",
            "importance": 10,
            "activation_count": 1,
            "domain": [],
        },
    }
    passive = {
        "id": "passive",
        "content": "Passive memory stays out while the budget is squeezed.",
        "metadata": {
            "type": "dynamic",
            "importance": 9,
            "activation_count": 1,
            "last_active": "2020-01-01T00:00:00",
            "domain": [],
        },
    }
    accidental = {
        "id": "accidental",
        "content": "Accidental memory stays out while the budget is squeezed.",
        "metadata": {
            "type": "dynamic",
            "importance": 5,
            "resolved": True,
            "domain": [],
        },
    }
    manager = OrderedBucketManager(
        [first_core, oversized_core, ordinary, passive, accidental]
    )
    _install_runtime(manager)
    monkeypatch.setattr("tools.breath.surface.random.random", lambda: 0.0)
    _, first_core_cost = render_stored_bucket(
        first_core,
        "📌 [核心准则] [bucket_id:first-core]",
        "👣 Footprint：暂时无法读取",
    )
    _, oversized_core_cost = render_stored_bucket(
        oversized_core,
        "📌 [核心准则] [bucket_id:oversized-core]",
        "👣 Footprint：暂时无法读取",
    )
    _, ordinary_cost = render_stored_bucket(
        ordinary,
        "[权重:10.00] [bucket_id:ordinary]",
        "👣 Footprint：暂时无法读取",
    )

    output = await surface_default(
        max_results=1,
        max_tokens=first_core_cost + ordinary_cost,
        tag_filter=[],
    )

    assert "[bucket_id:first-core]" in output
    assert first_core["content"] in output
    assert "[bucket_id:oversized-core]" not in output
    assert "[bucket_id:ordinary]" in output
    assert ordinary["content"] in output
    assert "[bucket_id:passive]" not in output
    assert "[bucket_id:accidental]" not in output
    assert "token 预算不足" in output
    assert "核心准则" in output
    assert "普通浮现已跳过" not in output
    assert f"required≈{first_core_cost + oversized_core_cost} tokens" in output
    assert f"limit={first_core_cost + ordinary_cost} tokens" in output
    assert "omitted=1" in output
    assert "可由用户明确提高" in output


@pytest.mark.asyncio
async def test_default_surface_reports_hard_cap_when_pins_exceed_40000():
    oversized_core = {
        "id": "hard-cap-core",
        "content": "Oversized core rule " * 10000,
        "metadata": {
            "type": "permanent",
            "importance": 10,
            "pinned": True,
            "domain": [],
        },
    }
    _install_runtime(OrderedBucketManager([oversized_core]))

    output = await surface_default(
        max_results=10,
        max_tokens=40_000,
        tag_filter=[],
    )

    assert "required≈" in output
    assert "limit=40000 tokens" in output
    assert "omitted=1" in output
    assert "已达到当前版本 40000 token 安全上限" in output
    assert "可由用户明确提高" not in output


@pytest.mark.asyncio
async def test_default_surface_keeps_ordinary_results_when_all_core_fits(monkeypatch):
    core = {
        "id": "fitting-core",
        "content": "Fitting core rule remains complete.",
        "metadata": {
            "type": "permanent",
            "importance": 10,
            "pinned": True,
            "domain": [],
        },
    }
    ordinary = {
        "id": "fitting-ordinary",
        "content": "Ordinary memory remains available when pins fit.",
        "metadata": {
            "type": "dynamic",
            "importance": 9,
            "activation_count": 1,
            "domain": [],
        },
    }
    manager = OrderedBucketManager([core, ordinary])
    _install_runtime(manager)
    monkeypatch.setattr("tools.breath.surface.random.random", lambda: 1.0)
    _, core_cost = render_stored_bucket(
        core,
        "📌 [核心准则] [bucket_id:fitting-core]",
        "👣 Footprint：暂时无法读取",
    )
    _, ordinary_cost = render_stored_bucket(
        ordinary,
        "[权重:9.00] [bucket_id:fitting-ordinary]",
        "👣 Footprint：暂时无法读取",
    )

    output = await surface_default(
        max_results=10,
        max_tokens=core_cost + ordinary_cost,
        tag_filter=[],
    )

    assert "[bucket_id:fitting-core]" in output
    assert core["content"] in output
    assert "[bucket_id:fitting-ordinary]" in output
    assert ordinary["content"] in output
    assert "普通浮现已跳过" not in output


@pytest.mark.asyncio
async def test_default_surface_skips_random_oversized_candidate_and_keeps_later_fit(
    monkeypatch,
):
    top = {
        "id": "top",
        "content": "Top weighted memory.",
        "metadata": {
            "type": "dynamic",
            "importance": 10,
            "activation_count": 1,
            "domain": [],
        },
    }
    high = {
        "id": "high",
        "content": "Later high-importance memory must not be blocked.",
        "metadata": {
            "type": "dynamic",
            "importance": 9,
            "activation_count": 1,
            "domain": [],
        },
    }
    blocker = {
        "id": "blocker",
        "content": "blocking " * 400,
        "metadata": {
            "type": "dynamic",
            "importance": 8,
            "activation_count": 1,
            "domain": [],
        },
    }
    manager = OrderedBucketManager([top, high, blocker])
    _install_runtime(manager)

    def blocker_first(items):
        items.sort(key=lambda bucket: bucket["id"] != "blocker")

    monkeypatch.setattr("tools.breath.surface.random.shuffle", blocker_first)
    monkeypatch.setattr("tools.breath.surface.random.random", lambda: 1.0)
    _, top_cost = render_stored_bucket(
        top, "[权重:10.00] [bucket_id:top]", "👣 Footprint：暂时无法读取"
    )
    _, high_cost = render_stored_bucket(
        high, "[权重:9.00] [bucket_id:high]", "👣 Footprint：暂时无法读取"
    )
    rt.config["surfacing"]["breath_max_tokens"] = top_cost + high_cost

    output = await dispatch()

    assert "[bucket_id:top]" in output
    assert "[bucket_id:high]" in output
    assert high["content"] in output
    assert "[bucket_id:blocker]" not in output
    assert "token 预算不足" in output
    assert "有 1 条主要浮现记忆" in output


@pytest.mark.asyncio
async def test_oversized_passive_association_does_not_report_primary_truncation(
    monkeypatch,
):
    top = {
        "id": "top",
        "content": "Primary surfaced memory.",
        "metadata": {
            "type": "dynamic",
            "importance": 10,
            "activation_count": 1,
            "domain": [],
        },
    }
    passive = {
        "id": "passive",
        "content": "optional passive " * 400,
        "metadata": {
            "type": "dynamic",
            "importance": 9,
            "activation_count": 1,
            "last_active": "2020-01-01T00:00:00",
            "domain": [],
        },
    }
    manager = OrderedBucketManager([top, passive])
    _install_runtime(manager)
    monkeypatch.setattr("tools.breath.surface.random.shuffle", lambda items: None)
    monkeypatch.setattr("tools.breath.surface.random.random", lambda: 1.0)
    _, top_cost = render_stored_bucket(
        top, "[权重:10.00] [bucket_id:top]", "👣 Footprint：暂时无法读取"
    )

    output = await surface_default(
        max_results=1,
        max_tokens=top_cost,
        tag_filter=[],
    )

    assert "[bucket_id:top]" in output
    assert "[bucket_id:passive]" not in output
    assert "token 预算不足" not in output


@pytest.mark.asyncio
async def test_filters_and_importance_mode_remain_active(bucket_mgr, monkeypatch):
    keep_id = await bucket_mgr.create(
        content="过滤校验词：应当命中。",
        tags=["保留"],
        domain=["工作"],
        importance=9,
        valence=0.8,
        arousal=0.7,
    )
    wrong_domain_id = await bucket_mgr.create(
        content="过滤校验词：错误 domain。",
        tags=["保留"],
        domain=["私人"],
        importance=4,
    )
    wrong_tag_id = await bucket_mgr.create(
        content="过滤校验词：错误 tag。",
        tags=["忽略"],
        domain=["工作"],
        importance=4,
    )
    dehydrator = _install_runtime(bucket_mgr)
    original_search = bucket_mgr.search
    seen = {}

    async def recording_search(*args, **kwargs):
        seen.update(kwargs)
        return await original_search(*args, **kwargs)

    monkeypatch.setattr(bucket_mgr, "search", recording_search)
    monkeypatch.setattr("tools.breath.search.random.random", lambda: 1.0)

    query_output = await _search(
        "过滤校验词",
        domain="工作",
        valence=0.8,
        arousal=0.7,
        tag_filter=["保留"],
    )
    importance_output = await surface_by_importance(
        importance_min=8,
        max_tokens=10000,
        tag_filter=["保留"],
    )

    assert keep_id in query_output
    assert wrong_domain_id not in query_output
    assert wrong_tag_id not in query_output
    assert seen["domain_filter"] == ["工作"]
    assert seen["query_valence"] == 0.8
    assert seen["query_arousal"] == 0.7
    assert keep_id in importance_output
    assert wrong_domain_id not in importance_output
    assert wrong_tag_id not in importance_output
    assert dehydrator.calls == 0
