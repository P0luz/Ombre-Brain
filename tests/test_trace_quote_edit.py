"""trace 订正与删除引语（3.4.0）。

3.1.0 给了引语的写入口，没给修正口：记错了、写错字了、或者回头看觉得这句
根本不该留，只能去手改 frontmatter。

这里测的重点是**边界守得住**，不是"能不能改成功"：

- **不能补录**。引语和已删掉的原文层的全部区别就在「谁决定记住」——原文层
  是系统自动存全量，引语是我在写入那一刻挑的。如果 trace 能往桶里加引语，
  任何一句话都能事后被追认为「当时就知道重要」，这个通道当场退化成存原文。
  这与 3.3.0「relink 不能凭空建立关系」是同一条边界。
- **条数只能持平或减少**。改和删是「回头看这几句」，补录不是。
- **超限拒绝而不是截断**。截断过的引语已经不是原话，而这个功能的全部意义
  就在「原样」。
- **必须单独调用**。早返回分支最容易出的错是同一次调用里另外半个意图被静默
  丢掉，而返回值看起来是成功的。
"""

from unittest.mock import MagicMock

import pytest

from errors import ToolInputError

import tools._runtime as rt
from ombrebrain.storage.quote_store import quotes_from_metadata
from tools.trace import dispatch as trace_dispatch


class _NoEmbedding:
    enabled = False

    async def search_similar(self, query, top_k=20, allowed_bucket_ids=None):
        return []


@pytest.fixture(autouse=True)
def _runtime(bucket_mgr):
    """装配 runtime 并在用例结束后还原。

    `rt` 是模块级全局，不还原会污染同一次 pytest 里后跑的用例。
    """
    saved = {
        name: getattr(rt, name, None)
        for name in ("config", "bucket_mgr", "embedding_engine", "logger")
    }
    rt.config = {}
    rt.bucket_mgr = bucket_mgr
    rt.embedding_engine = _NoEmbedding()
    rt.logger = MagicMock()
    yield
    for name, value in saved.items():
        setattr(rt, name, value)


async def _quoted(mgr, quotes) -> str:
    return await mgr.create(
        content="那天她站在门口没进来，我记得光的角度。",
        importance=5,
        quotes=quotes,
    )


async def _quotes_of(mgr, bucket_id: str) -> list[dict]:
    bucket = await mgr.get(bucket_id)
    return quotes_from_metadata(bucket.get("metadata") or {})


# --------------------------------------------------------------
# 设计边界：不能补录
# --------------------------------------------------------------


@pytest.mark.asyncio
async def test_replace_refuses_a_bucket_that_never_had_quotes(bucket_mgr):
    """桶里本来没有引语时必须拒绝，而不是顺手建一条。

    这是 quotes_replace 与 hold(quotes=...) 之间唯一的区别。守不住这条，
    「决定权只在写入那一刻」就等于被从后门撤销了。
    """
    bucket_id = await bucket_mgr.create(content="一条没有引语的普通记忆", importance=5)

    with pytest.raises(ToolInputError) as excinfo:
        await trace_dispatch(bucket_id=bucket_id, quotes_replace=["我不会走的"])

    assert "不能补录" in str(excinfo.value)
    assert await _quotes_of(bucket_mgr, bucket_id) == []


@pytest.mark.asyncio
async def test_replace_refuses_to_grow_the_quote_count(bucket_mgr):
    """条数只能持平或减少。多出来的那句不是当时挑的，是现在才决定要记的。"""
    bucket_id = await _quoted(bucket_mgr, ["我不会走的"])

    with pytest.raises(ToolInputError) as excinfo:
        await trace_dispatch(
        bucket_id=bucket_id, quotes_replace=["我不会走的", "你根本不懂"]
        )

    assert '不能加' in str(excinfo.value)
    # 拒绝之后必须原封不动——半途写入比整体拒绝更难发现
    assert [q["text"] for q in await _quotes_of(bucket_mgr, bucket_id)] == ["我不会走的"]


# --------------------------------------------------------------
# 改
# --------------------------------------------------------------


@pytest.mark.asyncio
async def test_replace_fixes_a_misrecorded_quote(bucket_mgr):
    bucket_id = await _quoted(bucket_mgr, ["我不会走的", "你根本不懂"])

    result = await trace_dispatch(
        bucket_id=bucket_id,
        quotes_replace=[
            {"text": "我不会走的", "speaker": "她", "at": "2026-08-18"},
            "你根本不懂",
        ],
    )

    assert "已更新" in result
    stored = await _quotes_of(bucket_mgr, bucket_id)
    assert [q["text"] for q in stored] == ["我不会走的", "你根本不懂"]
    # 订正后的结果要原样回显：看不到改成了什么就等于没确认
    assert stored[0]["speaker"] == "她"
    assert "她" in result


@pytest.mark.asyncio
async def test_replace_keeps_input_order(bucket_mgr):
    """说话是有先后的，重排会改变意思。"""
    bucket_id = await _quoted(bucket_mgr, ["先说的", "后说的"])

    await trace_dispatch(bucket_id=bucket_id, quotes_replace=["后说的", "先说的"])

    assert [q["text"] for q in await _quotes_of(bucket_mgr, bucket_id)] == [
        "后说的",
        "先说的",
    ]


# --------------------------------------------------------------
# 删
# --------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_list_deletes_every_quote(bucket_mgr):
    bucket_id = await _quoted(bucket_mgr, ["我不会走的", "你根本不懂"])

    result = await trace_dispatch(bucket_id=bucket_id, quotes_replace=[])

    assert "已删除" in result and "2" in result
    assert await _quotes_of(bucket_mgr, bucket_id) == []
    # 清空要真的把字段拿掉，不是留一个空列表在 frontmatter 里
    bucket = await bucket_mgr.get(bucket_id)
    assert "quotes" not in (bucket.get("metadata") or {})


@pytest.mark.asyncio
async def test_replace_can_drop_one_of_several(bucket_mgr):
    bucket_id = await _quoted(bucket_mgr, ["留下的", "该删的", "也留下的"])

    result = await trace_dispatch(
        bucket_id=bucket_id, quotes_replace=["留下的", "也留下的"]
    )

    assert "删掉了 1 条" in result
    assert [q["text"] for q in await _quotes_of(bucket_mgr, bucket_id)] == [
        "留下的",
        "也留下的",
    ]


# --------------------------------------------------------------
# 长度上限：拒绝，不截断
# --------------------------------------------------------------


@pytest.mark.asyncio
async def test_overlong_quote_is_rejected_not_truncated(bucket_mgr):
    """截断过的引语已经不是原话——这个功能的全部意义就在「原样」。"""
    bucket_id = await _quoted(bucket_mgr, ["原来那句"])

    with pytest.raises(ToolInputError) as excinfo:
        await trace_dispatch(bucket_id=bucket_id, quotes_replace=["长" * 101])

    assert "100" in str(excinfo.value)
    assert [q["text"] for q in await _quotes_of(bucket_mgr, bucket_id)] == ["原来那句"]


# --------------------------------------------------------------
# 互斥：另外半个意图不能被静默丢掉
# --------------------------------------------------------------


@pytest.mark.asyncio
async def test_replace_refuses_to_share_a_call_with_field_updates(bucket_mgr):
    bucket_id = await _quoted(bucket_mgr, ["我不会走的"])

    with pytest.raises(ToolInputError) as excinfo:
        await trace_dispatch(
        bucket_id=bucket_id, quotes_replace=[], importance=9
        )

    assert '必须单独调用' in str(excinfo.value)
    # 两边都不能生效
    assert [q["text"] for q in await _quotes_of(bucket_mgr, bucket_id)] == ["我不会走的"]
    bucket = await bucket_mgr.get(bucket_id)
    assert (bucket.get("metadata") or {}).get("importance") != 9


@pytest.mark.asyncio
async def test_replace_refuses_to_share_a_call_with_relation_edit(bucket_mgr):
    bucket_id = await _quoted(bucket_mgr, ["我不会走的"])
    other = await bucket_mgr.create(content="另一条", importance=5)

    with pytest.raises(ToolInputError) as excinfo:
        await trace_dispatch(
        bucket_id=bucket_id, quotes_replace=[], unlink=other
        )

    assert '不能与关系修正同时使用' in str(excinfo.value)
    assert [q["text"] for q in await _quotes_of(bucket_mgr, bucket_id)] == ["我不会走的"]


@pytest.mark.asyncio
async def test_non_list_is_rejected(bucket_mgr):
    """字符串是 list 之外最容易误传的类型，且会被逐字符迭代成一堆单字引语。"""
    bucket_id = await _quoted(bucket_mgr, ["我不会走的"])

    with pytest.raises(ToolInputError) as excinfo:
        await trace_dispatch(bucket_id=bucket_id, quotes_replace="我不会走的")

    assert '必须是列表' in str(excinfo.value)
    assert [q["text"] for q in await _quotes_of(bucket_mgr, bucket_id)] == ["我不会走的"]


@pytest.mark.asyncio
async def test_omitting_quotes_replace_leaves_quotes_alone(bucket_mgr):
    """不传就是不改——None 和 [] 必须是两回事，否则每次普通 trace 都会清空引语。"""
    bucket_id = await _quoted(bucket_mgr, ["我不会走的"])

    await trace_dispatch(bucket_id=bucket_id, importance=8)

    assert [q["text"] for q in await _quotes_of(bucket_mgr, bucket_id)] == ["我不会走的"]
