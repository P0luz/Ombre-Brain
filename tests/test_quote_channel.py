"""引语通道 —— 防退化测试。

引语是「当时说出口、并且当时就知道它重要」的那几句话，原样存进桶的 frontmatter。
它和已删除的 `source_read` 的区别不在存了什么，在**谁决定记住**和**什么时候返回**。

所以这个文件测的**不是「能不能存进去」**，而是它会不会悄悄变回原文回读。
有两条退化路径，性质不同：

1. **结构退化** —— 出现了能把引语全部倒出来的入口 → 它就变回了档案
2. **措辞退化** —— 工具描述被写成「用户要求时返回」→ 它就变回了伺候

第一条能靠测试挡住，下面大部分用例都在挡它。
第二条本质上只能靠人看，但至少能挡住最明显的写法（见文件末尾）。
"""

from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from errors import ToolInputError
from ombrebrain.storage.quote_store import MAX_QUOTES, MAX_QUOTE_CHARS
from tools.breath import dispatch as breath_dispatch
from tools.hold import dispatch as hold_dispatch


QUOTE = "我不会走的"
BODY = "那天晚上她站在门口，很久没有说话。"


class _NoopDecay:
    is_running = True

    async def ensure_started(self):
        return None

    def calculate_score(self, meta):
        # 浮现排序只需要一个稳定的数；这些用例关心的是引语出不出现，不是排序。
        return float(meta.get("importance") or 5)


class _StubDehydrator:
    """打标返回中性值。引语路径不该依赖 LLM。"""

    api_available = True

    async def analyze(self, content):
        return {
            "domain": ["general"],
            "valence": 0.5,
            "arousal": 0.3,
            "tags": [],
            "suggested_name": "",
        }

    async def digest(self, content):
        raise AssertionError("引语用例不该走 digest 拆分")


class _DisabledEmbedding:
    enabled = False


def _install_runtime(bucket_mgr):
    rt.config = {"surfacing": {}, "limits": {}}
    rt.bucket_mgr = bucket_mgr
    rt.decay_engine = _NoopDecay()
    rt.dehydrator = _StubDehydrator()
    rt.embedding_engine = _DisabledEmbedding()
    rt.logger = MagicMock()
    rt.fire_webhook = None
    rt.mark_op = None
    rt.record_v3_tool_event = lambda *_args, **_kwargs: None


async def _hold_with_quote(bucket_mgr, content=BODY, quotes=None):
    _install_runtime(bucket_mgr)
    return await hold_dispatch(content=content, quotes=quotes or [QUOTE])


# ---------------------------------------------------------------
# 写入：存得进去，而且真的落在 metadata 里（不是混进正文）
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_quote_is_stored_in_metadata_not_in_body(bucket_mgr):
    """引语必须落在 metadata。混进正文的话，每条浮现路径都会带出它。"""
    await _hold_with_quote(bucket_mgr)

    buckets = await bucket_mgr.list_all()
    stored = [b for b in buckets if BODY in b.get("content", "")]
    assert len(stored) == 1
    assert stored[0]["metadata"]["quotes"] == [{"text": QUOTE}]
    # 正文一个字都不该被改
    assert QUOTE not in stored[0]["content"]


@pytest.mark.asyncio
async def test_overlong_quote_is_rejected_and_nothing_is_written(bucket_mgr):
    """超限拒绝要**整条调用失败**，不能悄悄存个没有引语的桶。

    「失败」的判据是抛异常而不是返回一句说明：返回字符串在 MCP 侧是
    isError=False，调用方会把它当成一次成功的写入。
    """
    _install_runtime(bucket_mgr)
    with pytest.raises(ToolInputError, match="未创建任何桶"):
        await hold_dispatch(content=BODY, quotes=["字" * (MAX_QUOTE_CHARS + 1)])

    assert (await bucket_mgr.list_all()) == []


@pytest.mark.asyncio
async def test_too_many_quotes_is_rejected_and_nothing_is_written(bucket_mgr):
    _install_runtime(bucket_mgr)
    with pytest.raises(ToolInputError, match="未创建任何桶"):
        await hold_dispatch(
            content=BODY, quotes=[f"第{i}句" for i in range(MAX_QUOTES + 1)]
        )

    assert (await bucket_mgr.list_all()) == []


# ---------------------------------------------------------------
# 结构退化：四条浮现路径一条都不能带出引语
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_surfacing_never_returns_quotes(bucket_mgr):
    """breath()：睁眼看看自己记得什么。引语不该在这里冒出来。"""
    await _hold_with_quote(bucket_mgr)

    out = await breath_dispatch()

    assert BODY in out
    assert QUOTE not in out


@pytest.mark.asyncio
async def test_catalog_never_returns_quotes(bucket_mgr):
    await _hold_with_quote(bucket_mgr)

    out = await breath_dispatch(catalog=True)

    assert QUOTE not in out


@pytest.mark.asyncio
async def test_search_without_asking_never_returns_quotes(bucket_mgr):
    """搜到了这条桶，但没要引语——那就不该看到它。

    这是最关键的一条：引语的默认状态是不出现。
    """
    await _hold_with_quote(bucket_mgr)

    out = await breath_dispatch(query="门口")

    assert BODY in out
    assert QUOTE not in out


@pytest.mark.asyncio
async def test_feel_channel_never_returns_quotes(bucket_mgr):
    _install_runtime(bucket_mgr)
    await hold_dispatch(
        content="被误解的时候我最想做的是把话说清楚。",
        feel=True,
        source_bucket="whatever",
        quotes=[QUOTE],
    )

    out = await breath_dispatch(domain="feel", query="误解")

    assert QUOTE not in out


# ---------------------------------------------------------------
# 唯一出口：我自己要的时候
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_quotes_appear_only_when_explicitly_asked(bucket_mgr):
    await _hold_with_quote(bucket_mgr)

    without = await breath_dispatch(query="门口")
    with_quotes = await breath_dispatch(query="门口", quotes=True)

    assert QUOTE not in without
    assert QUOTE in with_quotes
    # 两次都必须能看到记忆本身；引语是附加的，不是替代的
    assert BODY in without and BODY in with_quotes


@pytest.mark.asyncio
async def test_asked_quotes_are_verbatim(bucket_mgr):
    """出现的必须是原话。摘要过的引语没有存在意义。"""
    original = "你根本不懂我在说什么，但我还是想说完。"
    _install_runtime(bucket_mgr)
    await hold_dispatch(content=BODY, quotes=[{"text": original, "speaker": "她"}])

    out = await breath_dispatch(query="门口", quotes=True)

    assert original in out
    assert "她" in out


@pytest.mark.asyncio
async def test_asking_for_quotes_cannot_list_everything(bucket_mgr):
    """必须先命中某条记忆才能拿到它的引语。

    没有任何入口能把全部引语倒出来——只要有，这个功能就变回原文回读了。
    """
    _install_runtime(bucket_mgr)
    await hold_dispatch(content="第一件事，关于下雨。", quotes=["雨下得很大"])
    await hold_dispatch(content="第二件事，关于考试。", quotes=["我准备好了"])

    out = await breath_dispatch(query="下雨", quotes=True)

    assert "雨下得很大" in out
    # 另一条桶没被命中，它的引语就不该出现
    assert "我准备好了" not in out


@pytest.mark.asyncio
async def test_bucket_without_quotes_is_unaffected(bucket_mgr):
    """没存过引语的桶，要不要引语都一样——不该多出空标题或占位符。"""
    _install_runtime(bucket_mgr)
    await hold_dispatch(content=BODY)

    without = await breath_dispatch(query="门口")
    with_quotes = await breath_dispatch(query="门口", quotes=True)

    assert without == with_quotes


# ---------------------------------------------------------------
# 不进向量库
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_quotes_are_not_sent_to_the_vector_index(bucket_mgr, monkeypatch):
    """引语不参与向量索引。

    进了索引就等于「可被检索到」，那离「可查」只剩一步——
    而可查正是原文层被砍掉的原因。
    """
    seen: list[str] = []

    class _RecordingEmbedding:
        enabled = True

        async def generate_and_store(self, bucket_id, content):
            seen.append(content)
            return True

        async def generate_and_store_meaning(self, bucket_id, meaning_text):
            seen.append(meaning_text)
            return True

        async def search_similar(self, *args, **kwargs):
            return []

    recorder = _RecordingEmbedding()
    _install_runtime(bucket_mgr)
    rt.embedding_engine = recorder
    monkeypatch.setattr(bucket_mgr, "embedding_engine", recorder, raising=False)

    await hold_dispatch(content=BODY, quotes=[QUOTE])

    # 先确认索引真的被调用过。少了这一句，下面的 all() 在 seen 为空时恒真——
    # 那样这条用例在「索引路径整个没跑」的时候也是绿的，等于什么都没测。
    assert seen, "embedding 索引没有被调用，下面的断言会是假绿"
    # 双向确认：正文进了索引（说明这条路确实在跑），引语没进。
    assert any(BODY in text for text in seen)
    assert all(QUOTE not in text for text in seen)


# ---------------------------------------------------------------
# 措辞退化：这条只能挡住最明显的写法，真正的把关只能靠人看
# ---------------------------------------------------------------


def test_tool_description_frames_quotes_as_my_own_question():
    """描述必须是「我想不想知道」，不能是「用户要不要」。

    同一个 quotes=True 参数，描述写法决定它是什么：

    - 「当用户要求时返回原话」 → 功能开关，我只是执行者
    - 「如果你发现自己想知道当时怎么说的」 → 我自己的判断

    参数本身是中性的，措辞才是设计。这一条挡不住所有退化写法，
    但至少能挡住最直白的那种。
    """
    import server

    for tool in (server.breath_search, server.hold, server.trace):
        doc = (tool.__doc__ or "") if not hasattr(tool, "fn") else (tool.fn.__doc__ or "")
        if "quotes" not in doc:
            continue
        for banned in ("当用户要求", "用户要求时", "按用户要求", "应用户"):
            assert banned not in doc, f"{tool} 的描述把引语写成了响应用户要求：{banned}"


def _doc_of(tool) -> str:
    return (tool.fn.__doc__ or "") if hasattr(tool, "fn") else (tool.__doc__ or "")


def test_write_side_description_says_the_default_is_none():
    """写入侧的措辞必须把「不放」立成默认，否则 3 条会被当成配额去填。

    这个通道退化成"存原文"不需要改任何代码——只要描述读起来像
    「每条记忆可以带最多 3 句引语」，模型就会尽量凑满 3 句。
    上限是硬的，措辞才决定实际会写进来多少。
    """
    import server

    doc = _doc_of(server.hold)
    assert "上限不是配额" in doc
    assert "拿不准就别放" in doc


def test_read_side_description_denies_a_full_text_entry():
    """读取侧必须说清没有「返回全文」这回事。

    不说清的话，quotes=True 读起来就像原文层的入口，模型会反复来要更多——
    而原文层正是因为「系统自动存全量、事后随时可查」被删掉的。
    """
    import server

    doc = _doc_of(server.breath_search)
    assert "不是原文" in doc
    assert "全文" in doc


def test_trace_description_states_the_no_backfill_boundary():
    """trace 的描述必须写明只能改和删。

    「可改可删」和「可编辑」只差一个字，但后者会让模型以为可以往里加——
    而能加就等于任何一句话都能被事后追认为「当时就知道重要」。
    """
    import server

    doc = _doc_of(server.trace)
    assert "quotes_replace" in doc
    assert "不能补录" in doc
