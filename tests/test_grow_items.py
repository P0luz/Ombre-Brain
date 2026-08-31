"""grow 预拆分（items）模式测试。

issue 诉求：上层 AI 已拆好的正文应逐字入库，消除「廉价 LLM 二次拆分改写」这次失真。
硬承诺：
1. 传 items 时**绝不调用 digest / merge**（谁调谁炸），只调 analyze 打元数据；
2. 存进桶的正文与传入**一字不差**；
3. 同批共享 grow_batch_id；不传 items 时行为不变（走原 digest 路径）。
"""
import asyncio
from unittest.mock import MagicMock
from pathlib import Path

import pytest

from errors import ToolInputError

import tools._runtime as rt
from tools.grow import dispatch
from tools.grow.core import grow_core, grow_items
from tools.grow.shortpath import grow_shortpath
from tools.trace.core import trace_core
from errors import PublicToolError
from ombrebrain.storage.source_store import SourceStore


class StubDehydrator:
    """items 模式只允许 analyze；digest/merge 一旦被调用即判定失真回归。"""

    def __init__(self):
        self.analyze_calls = 0

    async def analyze(self, content, *, include_why=False):
        self.analyze_calls += 1
        result = {"domain": ["工作"], "valence": 0.6, "arousal": 0.4,
                  "tags": ["标签"], "suggested_name": "事件"}
        if include_why:
            result["why_remembered"] = "这条短记忆会影响我后续的判断。"
        return result

    async def digest(self, content):
        raise AssertionError("items 模式不允许调用 digest（会造成二次改写失真）")

    async def merge(self, old_content, new_content):
        raise AssertionError("items 模式不允许调用 merge（会压缩老+新）")


class NoopDecay:
    is_running = True

    async def ensure_started(self):
        return None

    def calculate_score(self, meta):
        return 1.0


@pytest.fixture
def grow_rt(bucket_mgr, monkeypatch):
    stub = StubDehydrator()
    monkeypatch.setattr(rt, "config", {"limits": {}, "merge_threshold": 75}, raising=False)
    monkeypatch.setattr(rt, "bucket_mgr", bucket_mgr, raising=False)
    monkeypatch.setattr(rt, "dehydrator", stub, raising=False)
    monkeypatch.setattr(rt, "decay_engine", NoopDecay(), raising=False)
    monkeypatch.setattr(rt, "logger", MagicMock(), raising=False)
    monkeypatch.setattr(rt, "fire_webhook", None, raising=False)
    monkeypatch.setattr(rt, "source_store", SourceStore(bucket_mgr.base_dir), raising=False)
    return bucket_mgr, stub


@pytest.mark.asyncio
async def test_items_stored_verbatim_no_digest(grow_rt):
    bucket_mgr, stub = grow_rt
    original = [
        "昨天和产品经理开会，分歧在多租户 SaaS 的隐私边界。",
        "晚上把 grow 的 items 模式草稿写完了，明天验证。",
    ]
    out = await grow_items(list(original))

    assert "2条(预拆分·逐字)" in out
    assert stub.analyze_calls == 2  # 每条只打标一次

    # 正文一字不差地存进了桶
    buckets = await bucket_mgr.list_all(include_archive=False)
    stored = sorted(b["content"] for b in buckets)
    assert stored == sorted(original)


@pytest.mark.asyncio
async def test_items_share_batch_and_metadata(grow_rt):
    bucket_mgr, stub = grow_rt
    await grow_items(["第一条内容啊啊啊啊啊", "第二条内容哦哦哦哦哦"])
    buckets = await bucket_mgr.list_all(include_archive=False)
    batch_ids = {b["metadata"].get("grow_batch_id") for b in buckets}
    assert len(batch_ids) == 1 and next(iter(batch_ids))  # 同批共享一个非空 batch_id
    for b in buckets:
        assert b["metadata"].get("source_tool") == "grow"
        assert "工作" in (b["metadata"].get("domain") or [])


@pytest.mark.asyncio
async def test_empty_items_creates_nothing(grow_rt):
    bucket_mgr, stub = grow_rt
    out = await grow_items(["", "   ", None])
    assert "未创建任何桶" in out
    assert stub.analyze_calls == 0
    assert await bucket_mgr.list_all(include_archive=False) == []


@pytest.mark.asyncio
async def test_dict_items_take_content_field(grow_rt):
    bucket_mgr, stub = grow_rt
    await grow_items([{"content": "对象形式的一条正文内容"}, "字符串形式的一条正文内容"])
    buckets = await bucket_mgr.list_all(include_archive=False)
    stored = sorted(b["content"] for b in buckets)
    assert stored == sorted(["对象形式的一条正文内容", "字符串形式的一条正文内容"])


@pytest.mark.asyncio
async def test_dispatch_routes_to_items_when_provided(grow_rt):
    bucket_mgr, stub = grow_rt
    # 传 items 时 content 是共享原文证据，不进入 digest；items 仍是最终正文。
    out = await dispatch(content="x" * 100, items=["逐字入库的唯一正文条目内容"])
    assert "预拆分·逐字" in out
    buckets = await bucket_mgr.list_all(include_archive=False)
    assert [b["content"] for b in buckets] == ["逐字入库的唯一正文条目内容"]
    assert buckets[0]["metadata"]["source_refs"][0]["ref"].startswith("src_")


@pytest.mark.asyncio
async def test_dict_items_preserve_explicit_metadata_and_title(grow_rt):
    bucket_mgr, stub = grow_rt
    await grow_items([{
        "title": "wife",
        "content": "她说 wife 喔，不是 girlfriend 喔。",
        "tags": ["老婆", "称呼"],
        "importance": 8,
        "domain": ["恋爱"],
        "valence": 0.9,
        "arousal": 0.6,
    }])
    bucket = (await bucket_mgr.list_all(include_archive=False))[0]
    metadata = bucket["metadata"]
    assert metadata["title"] == "wife"
    assert metadata["tags"] == ["老婆", "称呼"]
    assert metadata["importance"] == 8
    assert metadata["domain"] == ["恋爱"]
    assert metadata["valence"] == 0.9
    assert metadata["arousal"] == 0.6
    assert stub.analyze_calls == 0


@pytest.mark.asyncio
async def test_dict_item_stores_explicit_why_remembered_on_first_create(grow_rt):
    bucket_mgr, stub = grow_rt
    await grow_items([{
        "title": "保留原因",
        "content": "调用方已经知道这条记忆为什么值得留下。",
        "tags": [],
        "importance": 6,
        "domain": ["自省"],
        "valence": 0.5,
        "arousal": 0.3,
        "why_remembered": "  它解释了我为什么会在未来回看这件事。  ",
    }])

    bucket = (await bucket_mgr.list_all(include_archive=False))[0]
    assert bucket["metadata"]["why_remembered"] == (
        "它解释了我为什么会在未来回看这件事。"
    )
    assert stub.analyze_calls == 0


@pytest.mark.asyncio
async def test_digest_reason_is_stored_on_first_create_and_preserved_on_merge(grow_rt):
    bucket_mgr, _stub = grow_rt

    class DigestWhyDehydrator(StubDehydrator):
        async def digest(self, _content):
            return [{
                "name": "重复事件",
                "content": "这是两次 grow 都命中的同一个具体事件。",
                "domain": ["自省"],
                "valence": 0.5,
                "arousal": 0.3,
                "tags": ["同一事件"],
                "importance": 6,
                "why_remembered": "这个理由应该在首次新建时写入。",
            }]

    rt.dehydrator = DigestWhyDehydrator()
    long_source = "这是一段长度超过三十个字符的原始内容，用于稳定走日记拆分路径而不是短内容快速路径。"

    first = await grow_core(long_source)
    first_bucket = (await bucket_mgr.list_all(include_archive=False))[0]
    assert "新1合0" in first
    assert first_bucket["metadata"]["why_remembered"] == (
        "这个理由应该在首次新建时写入。"
    )

    second = await grow_core(long_source)
    buckets = await bucket_mgr.list_all(include_archive=False)
    assert "新0合1" in second
    assert len(buckets) == 1
    assert buckets[0]["metadata"]["why_remembered"] == (
        "这个理由应该在首次新建时写入。"
    )


@pytest.mark.asyncio
async def test_shortpath_reason_is_stored_on_first_create_and_preserved_on_merge(grow_rt):
    bucket_mgr, _stub = grow_rt
    content = "短内容二次命中同一事件"

    class ChangingWhyDehydrator(StubDehydrator):
        async def analyze(self, content, *, include_why=False):
            result = await super().analyze(content, include_why=include_why)
            result["why_remembered"] = (
                "首次新建时的自动理由。"
                if self.analyze_calls == 1
                else "后来的候选理由不应覆盖旧值。"
            )
            return result

    dehydrator = ChangingWhyDehydrator()
    rt.dehydrator = dehydrator

    # This is a merge-policy unit test, so call the short path directly.
    # Public dispatch deliberately treats an immediate identical call as a
    # transport retry and reuses the first result.
    first = await grow_shortpath(content)
    first_bucket = (await bucket_mgr.list_all(include_archive=False))[0]
    assert "新建" in first
    assert first_bucket["metadata"]["why_remembered"] == (
        "首次新建时的自动理由。"
    )

    second = await grow_shortpath(content)
    buckets = await bucket_mgr.list_all(include_archive=False)
    assert "合并" in second
    assert len(buckets) == 1
    assert buckets[0]["metadata"]["why_remembered"] == (
        "首次新建时的自动理由。"
    )
    assert dehydrator.analyze_calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "candidate, expected",
    [
        (None, None),
        (["不是字符串"], None),
        ("  " + "值" * 501 + "  ", "值" * 500),
    ],
)
async def test_shortpath_reason_keeps_invalid_and_length_boundaries(
    grow_rt, candidate, expected
):
    bucket_mgr, _stub = grow_rt

    class BoundaryWhyDehydrator(StubDehydrator):
        async def analyze(self, content, *, include_why=False):
            assert include_why is True
            result = await super().analyze(content, include_why=False)
            result["why_remembered"] = candidate
            return result

    rt.dehydrator = BoundaryWhyDehydrator()
    await dispatch(content="短理由边界")

    bucket = (await bucket_mgr.list_all(include_archive=False))[0]
    if expected is None:
        assert "why_remembered" not in bucket["metadata"]
    else:
        assert bucket["metadata"]["why_remembered"] == expected


@pytest.mark.asyncio
async def test_digest_merge_never_overwrites_existing_why_remembered(grow_rt):
    bucket_mgr, _stub = grow_rt
    event_content = "旧桶已经有人工写下的保留理由。"
    bucket_id = await bucket_mgr.create(
        content=event_content,
        title="旧理由优先",
        why_remembered="这是旧桶中已经确认的理由。",
    )

    class DigestWhyDehydrator(StubDehydrator):
        async def digest(self, _content):
            return [{
                "name": "旧理由优先",
                "content": event_content,
                "domain": ["自省"],
                "valence": 0.5,
                "arousal": 0.3,
                "tags": ["优先级"],
                "importance": 6,
                "why_remembered": "这是 digest 后来生成的候选理由。",
            }]

    rt.dehydrator = DigestWhyDehydrator()
    out = await grow_core(
        "这是一段超过三十个字符的日记原文，用来确认自动理由不会覆盖旧桶中的人工理由。"
    )

    assert "新0合1" in out
    stored = await bucket_mgr.get(bucket_id)
    assert stored["metadata"]["why_remembered"] == (
        "这是旧桶中已经确认的理由。"
    )


@pytest.mark.asyncio
async def test_items_merge_fills_empty_why_once_and_preserves_it(grow_rt):
    bucket_mgr, _stub = grow_rt
    event_content = "items 二次写入命中的同一个具体事件。"
    bucket_id = await bucket_mgr.create(
        content=event_content,
        title="items 合并原因",
    )

    first = await grow_items([{
        "title": "items 合并原因",
        "content": event_content,
        "why_remembered": "第一个非空理由。",
    }])
    after_first = await bucket_mgr.get(bucket_id)
    assert "新0合1" in first
    assert after_first["metadata"]["why_remembered"] == "第一个非空理由。"

    second = await grow_items([{
        "title": "items 合并原因",
        "content": event_content,
        "why_remembered": "后来的理由不应覆盖旧值。",
    }])
    after_second = await bucket_mgr.get(bucket_id)
    assert "新0合1" in second
    assert after_second["metadata"]["why_remembered"] == "第一个非空理由。"


@pytest.mark.asyncio
async def test_concurrent_trace_why_wins_over_stale_grow_merge(
    grow_rt, monkeypatch
):
    bucket_mgr, _stub = grow_rt
    event_content = "grow 合并与 trace 同时为同一桶补写原因。"
    bucket_id = await bucket_mgr.create(
        content=event_content,
        title="并发原因",
    )
    original_get = bucket_mgr.get
    snapshot_ready = asyncio.Event()
    trace_finished = asyncio.Event()
    grow_task = None
    paused_once = False

    async def gated_get(target_id):
        # grow 内部改用 asyncio.gather 并发处理各 item 后，单条 item 的
        # merge_or_create 跑在 gather 派生的子任务里，不再是外层 grow_task
        # 本身；用目标桶 id 匹配同样能只在 grow 触碰这个桶时暂停一次。
        nonlocal paused_once
        bucket = await original_get(target_id)
        if target_id == bucket_id and not paused_once:
            paused_once = True
            snapshot_ready.set()
            await trace_finished.wait()
        return bucket

    monkeypatch.setattr(bucket_mgr, "get", gated_get)
    grow_task = asyncio.create_task(grow_items([{
        "title": "并发原因",
        "content": event_content,
        "tags": [],
        "importance": 5,
        "domain": ["自省"],
        "valence": 0.5,
        "arousal": 0.3,
        "why_remembered": "grow 基于旧快照准备补入的理由。",
    }]))

    await asyncio.wait_for(snapshot_ready.wait(), timeout=2)
    try:
        traced = await trace_core(
            bucket_id,
            why_remembered="trace 显式写入的理由。",
        )
        assert "已修改" in traced
    finally:
        trace_finished.set()
    await asyncio.wait_for(grow_task, timeout=2)

    stored = await original_get(bucket_id)
    assert stored["metadata"]["why_remembered"] == (
        "trace 显式写入的理由。"
    )


@pytest.mark.asyncio
async def test_missing_title_and_importance_are_filled_by_analysis(grow_rt):
    bucket_mgr, _stub = grow_rt

    class MetadataDehydrator(StubDehydrator):
        async def analyze(self, content):
            self.analyze_calls += 1
            return {
                "domain": ["工作"],
                "valence": 0.6,
                "arousal": 0.4,
                "tags": ["模型补全"],
                "suggested_name": "模型补齐标题",
                "importance": 7,
                "why_remembered": "这是打标模型候选理由，items 不得偷偷采用。",
            }

    dehydrator = MetadataDehydrator()
    rt.dehydrator = dehydrator
    await grow_items([{"content": "只给正文，其他元数据由整理者补齐。"}])

    bucket = (await bucket_mgr.list_all(include_archive=False))[0]
    assert bucket["metadata"]["title"] == "模型补齐标题"
    assert bucket["metadata"]["importance"] == 7
    assert bucket["metadata"]["tags"] == ["模型补全"]
    assert "why_remembered" not in bucket["metadata"]
    assert dehydrator.analyze_calls == 1


@pytest.mark.asyncio
async def test_explicit_empty_tags_are_not_refilled_by_model(grow_rt):
    bucket_mgr, _stub = grow_rt
    await grow_items([{
        "content": "人工明确决定不设标签。",
        "title": "空标签决定",
        "tags": [],
    }])

    bucket = (await bucket_mgr.list_all(include_archive=False))[0]
    assert bucket["metadata"]["tags"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "item, expected",
    [
        ({"content": "字段拼错", "titel": "x"}, "未支持字段"),
        ({"content": 123}, "content 必须是字符串"),
        ({"content": "越界重要度", "importance": 11}, "importance"),
        ({"content": "过长标题", "title": "长" * 121}, "120"),
        ({"content": "原因类型错误", "why_remembered": ["不是字符串"]}, "why_remembered 必须是字符串"),
        ({"content": "原因过长", "why_remembered": "长" * 501}, "500"),
    ],
)
async def test_invalid_structured_item_is_rejected_before_any_write(
    grow_rt, item, expected
):
    bucket_mgr, stub = grow_rt
    out = await grow_items([item], source_content="这份原文也不应先落盘")

    assert expected in out
    assert stub.analyze_calls == 0
    assert await bucket_mgr.list_all(include_archive=False) == []
    assert not list((Path(bucket_mgr.base_dir) / "_sources").glob("*.source"))


@pytest.mark.asyncio
async def test_items_why_remembered_counts_toward_metadata_budget(grow_rt):
    bucket_mgr, stub = grow_rt
    rt.config["limits"]["max_metadata_bytes"] = 64

    out = await grow_items([{
        "content": "正文不计入元数据预算。",
        "why_remembered": "原因" * 40,
    }])

    assert "元数据过大" in out
    assert stub.analyze_calls == 0
    assert await bucket_mgr.list_all(include_archive=False) == []


@pytest.mark.asyncio
async def test_source_ranges_without_source_content_are_rejected(grow_rt):
    bucket_mgr, stub = grow_rt
    with pytest.raises(ToolInputError) as excinfo:
        await grow_items([
        {"content": "事件", "title": "标题", "source_ranges": [[1, 1]]}
        ])

    assert 'source_ranges 需要同时提供 content' in str(excinfo.value)
    assert stub.analyze_calls == 0
    assert await bucket_mgr.list_all(include_archive=False) == []


@pytest.mark.asyncio
async def test_invalid_source_range_rejects_batch_before_any_write(grow_rt):
    bucket_mgr, _stub = grow_rt
    with pytest.raises(ToolInputError) as excinfo:
        await grow_items(
            [{"title": "越界", "content": "最终正文", "source_ranges": [[3, 4]]}],
            source_content="只有一行",
        )
    assert "超出原文总行数" in str(excinfo.value)
    assert await bucket_mgr.list_all(include_archive=False) == []
    assert not list((Path(bucket_mgr.base_dir) / "_sources").glob("*.source"))


@pytest.mark.asyncio
async def test_obviously_shifted_source_ranges_are_rejected_before_write(grow_rt):
    bucket_mgr, stub = grow_rt
    source = (
        "开场：这两行只是寒暄。\n"
        "今天先聊点别的。\n"
        "[蓝鲸计划]\n"
        "蓝鲸计划讨论海洋声呐与深海航线。\n"
        "大家决定继续整理蓝鲸观测记录。\n"
        "[樱桃烘焙]\n"
        "樱桃烘焙测试黄油面团与烤箱温度。\n"
        "最后记录樱桃派的配方调整。\n"
        "[火星旅行]\n"
        "火星旅行讨论着陆舱与红色沙丘路线。\n"
        "最后确认火星基地的补给窗口。\n"
    )
    items = [
        {
            "title": "蓝鲸计划",
            "content": "蓝鲸计划围绕海洋声呐、深海航线和观测记录展开。",
            "source_ranges": [[1, 2]],
        },
        {
            "title": "樱桃烘焙",
            "content": "樱桃烘焙记录黄油面团、烤箱温度和樱桃派配方。",
            "source_ranges": [[3, 5]],
        },
        {
            "title": "火星旅行",
            "content": "火星旅行记录着陆舱、红色沙丘路线与基地补给窗口。",
            "source_ranges": [[6, 8]],
        },
    ]

    with pytest.raises(ToolInputError) as excinfo:
        await dispatch(content=source, items=items)

    assert 'source_ranges 疑似与 items 错位' in str(excinfo.value)
    assert stub.analyze_calls == 0
    assert await bucket_mgr.list_all(include_archive=False) == []
    assert not list((Path(bucket_mgr.base_dir) / "_sources").glob("*.source"))


@pytest.mark.asyncio
async def test_single_title_anchor_mismatch_does_not_guess(grow_rt):
    bucket_mgr, _stub = grow_rt
    source = (
        "开场寒暄。\n"
        "这里是调用方明确指定的证据行。\n"
        "[蓝鲸计划]\n"
        "蓝鲸计划讨论海洋声呐与深海航线。\n"
    )
    out = await dispatch(
        content=source,
        items=[{
            "title": "蓝鲸计划",
            "content": "蓝鲸计划围绕海洋声呐与深海航线展开。",
            "source_ranges": [[1, 2]],
        }],
    )

    assert "1条(预拆分·逐字)" in out
    buckets = await bucket_mgr.list_all(include_archive=False)
    assert len(buckets) == 1
    assert buckets[0]["metadata"]["source_refs"][0]["ranges"] == [[1, 2]]


@pytest.mark.asyncio
async def test_aligned_multi_event_source_ranges_round_trip(grow_rt):
    bucket_mgr, _stub = grow_rt
    source = (
        "开场：这两行只是寒暄。\n"
        "今天先聊点别的。\n"
        "[蓝鲸计划]\n"
        "蓝鲸计划讨论海洋声呐与深海航线。\n"
        "大家决定继续整理蓝鲸观测记录。\n"
        "[樱桃烘焙]\n"
        "樱桃烘焙测试黄油面团与烤箱温度。\n"
        "最后记录樱桃派的配方调整。\n"
        "[火星旅行]\n"
        "火星旅行讨论着陆舱与红色沙丘路线。\n"
        "最后确认火星基地的补给窗口。\n"
    )
    items = [
        {
            "title": "蓝鲸计划",
            "content": "蓝鲸计划围绕海洋声呐、深海航线和观测记录展开。",
            "source_ranges": [[3, 5]],
        },
        {
            "title": "樱桃烘焙",
            "content": "樱桃烘焙记录黄油面团、烤箱温度和樱桃派配方。",
            "source_ranges": [[6, 8]],
        },
        {
            "title": "火星旅行",
            "content": "火星旅行记录着陆舱、红色沙丘路线与基地补给窗口。",
            "source_ranges": [[9, 11]],
        },
    ]

    out = await dispatch(content=source, items=items)
    assert "3条(预拆分·逐字)" in out

    buckets = await bucket_mgr.list_all(include_archive=False)
    by_title = {bucket["metadata"]["title"]: bucket for bucket in buckets}
    # 工具层已删除，直接从存储层验证每个事件的 ranges 只选中自己那几行
    store = rt.source_store

    def event_text(title: str) -> str:
        ref = by_title[title]["metadata"]["source_refs"][0]
        return store.select_ranges(store.read(ref["ref"]), ref["ranges"])

    first = event_text("蓝鲸计划")
    middle = event_text("樱桃烘焙")
    last = event_text("火星旅行")
    full = store.read(by_title["蓝鲸计划"]["metadata"]["source_refs"][0]["ref"])

    assert "深海航线" in first and "樱桃派" not in first
    assert "樱桃派" in middle and "火星基地" not in middle
    assert "火星基地" in last and "蓝鲸观测" not in last
    assert "开场：这两行只是寒暄。" in full and "火星基地" in full


@pytest.mark.asyncio
async def test_dispatch_without_items_unchanged(grow_rt):
    # 不传 items：长文仍走 digest 路径（stub.digest 会炸，grow_core 包成 RuntimeError）
    # → 证明默认路径没变、digest 仍在原路径被调用
    with pytest.raises(RuntimeError):
        await dispatch(content="这是一段超过三十个字的长文本内容需要走 digest 拆分路径来验证向后兼容性没有被破坏")


@pytest.mark.asyncio
async def test_grow_digest_failure_hides_provider_detail(grow_rt):
    _bucket_mgr, _stub = grow_rt
    provider_secret = "sk-provider-secret https://provider.invalid/private"

    class FailingDehydrator:
        api_available = True  # key 是好的，炸的是调用本身

        async def digest(self, _content):
            raise RuntimeError(provider_secret)

    rt.dehydrator = FailingDehydrator()

    with pytest.raises(PublicToolError) as caught:
        await grow_core("这是一段需要调用摘要服务的长内容，长度足够进入 grow 的日记拆分主路径。")

    # key 可用时不许甩锅给 key：这条路径上绝大多数失败（供应商 5xx、超时、模型
    # 返回空）都与 key 无关，指向 OMBRE_COMPRESS_API_KEY 会把排查方向带偏。
    assert "OMBRE_COMPRESS_API_KEY" not in caught.value.public_message
    assert "日记拆分" in caught.value.public_message
    assert "桶未创建" in caught.value.public_message
    # 也不许反过来打包票说 key 没问题：api_available 只知道「配没配」，
    # key 填错/过期/欠费时它仍是 True，调用照样 401。
    assert "key 配置正常" not in caught.value.public_message
    # 供应商正文一律不得进入公开文案或日志
    assert provider_secret not in caught.value.public_message
    assert provider_secret not in str(caught.value)
    rendered_logs = "\n".join(
        str(call) for call in rt.logger.error.call_args_list
    )
    assert "RuntimeError" in rendered_logs
    assert provider_secret not in rendered_logs


@pytest.mark.asyncio
async def test_grow_digest_blames_key_only_when_api_unavailable(grow_rt):
    """只有 API 真的没配好时，才允许把人指向 OMBRE_COMPRESS_API_KEY。"""
    _bucket_mgr, _stub = grow_rt

    class UnconfiguredDehydrator:
        api_available = False

        async def digest(self, _content):
            raise RuntimeError("脱水 API 不可用，请检查 config.yaml 中的 dehydration 配置")

    rt.dehydrator = UnconfiguredDehydrator()

    with pytest.raises(PublicToolError) as caught:
        await grow_core("这是一段需要调用摘要服务的长内容，长度足够进入 grow 的日记拆分主路径。")

    assert "OMBRE_COMPRESS_API_KEY" in caught.value.public_message
    assert "桶未创建" in caught.value.public_message
