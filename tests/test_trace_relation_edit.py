"""trace 修正连错的桶间关系（3.3.0）。

3.2.0 把关系建立交给后端自动推断，却没留修正入口——连错了只能手改
frontmatter，而且关系是双向的、得记住改两个文件。

这里测的重点是**边界守得住**，不是"能不能改成功"：

- `relink` 不能凭空建立关系。这是整个设计的支点：3.0.0 有意删掉了
  `relation_attach`，因为关联是发现不是决定。如果 relink 顺手能建，
  等于把那个动作从后门放回来了。
- 双向必须真的双向。只改一侧的话，hint 会从一边看有关系、从另一边看没有，
  而且不会报任何错——这种不一致比彻底改不了更难发现。
"""

from unittest.mock import MagicMock

import pytest

from errors import ToolInputError

import tools._runtime as rt
from ombrebrain.storage.relation_store import (
    normalize_relation_links,
    retype_relation,
    unlink_relation,
)
from tools.trace import dispatch as trace_dispatch


class _NoEmbedding:
    enabled = False

    async def search_similar(self, query, top_k=20, allowed_bucket_ids=None):
        return []


@pytest.fixture(autouse=True)
def _runtime(bucket_mgr):
    """装配 runtime 并在用例结束后还原。

    `rt` 是模块级全局，不还原会污染同一次 pytest 里后跑的用例——
    单独跑全绿、全量跑却红，最难查的那种。
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


def _link(target: str, rel_type: str = "related_to", score: float | None = 0.8) -> dict:
    link = {
        "target_bucket_id": target,
        "type": rel_type,
        "label": "",
        "status": "active",
    }
    if score is not None:
        link["auto"] = True
        link["score"] = score
    return link


async def _attach(mgr, left: str, right: str, left_type="related_to", right_type=None,
                  *, one_way: bool = False):
    """直接写进文件，模拟后端已经建好的一对关系。"""
    right_type = right_type or left_type

    def _mutation(left_post, right_post):
        left_post["relation_links"] = [_link(right, left_type)]
        if not one_way:
            right_post["relation_links"] = [_link(left, right_type)]
        return True, not one_way, True

    await mgr.mutate_relation_pair(left, right, _mutation)


async def _links_of(mgr, bucket_id: str) -> list[dict]:
    bucket = await mgr.get(bucket_id)
    return normalize_relation_links((bucket.get("metadata") or {}).get("relation_links"))


async def _pair(mgr, **kwargs) -> tuple[str, str]:
    a = await mgr.create(content="上线成功那一刻的踏实感，记一笔。", importance=5)
    b = await mgr.create(content="今天把镜像推上去了，回滚脚本也备好了。", importance=5)
    await _attach(mgr, a, b, **kwargs)
    return a, b


# --------------------------------------------------------------
# 设计边界：不能凭空建立
# --------------------------------------------------------------


@pytest.mark.asyncio
async def test_relink_refuses_to_create_a_relation_that_does_not_exist(bucket_mgr):
    """两条记忆之间没有关系时，relink 必须拒绝，而不是顺手建一条。

    这是 relink 与被删掉的 relation_attach 之间唯一的区别。守不住这条，
    3.0.0「关联是发现不是决定」就等于被从后门撤销了。
    """
    a = await bucket_mgr.create(content="毫无关系的甲", importance=5)
    b = await bucket_mgr.create(content="毫无关系的乙", importance=5)

    with pytest.raises(ToolInputError) as excinfo:
        await trace_dispatch(bucket_id=a, relink=b, relation_type="same_event")

    assert '不能凭空建立' in str(excinfo.value)
    assert await _links_of(bucket_mgr, a) == []
    assert await _links_of(bucket_mgr, b) == []


# --------------------------------------------------------------
# 解绑
# --------------------------------------------------------------


@pytest.mark.asyncio
async def test_unlink_removes_both_directions(bucket_mgr):
    a, b = await _pair(bucket_mgr)

    result = await trace_dispatch(bucket_id=a, unlink=b)

    assert "已断开" in result
    # 两侧都要干净——只清一侧的话 hint 从另一边看仍然存在，且不会报错
    assert await _links_of(bucket_mgr, a) == []
    assert await _links_of(bucket_mgr, b) == []


@pytest.mark.asyncio
async def test_unlink_reports_when_there_was_nothing_to_remove(bucket_mgr):
    """本来就没有关系时要如实说，不能报成"已断开"。

    否则「删掉了」和「压根没连上，我看错了」在返回里长得一样。
    """
    a = await bucket_mgr.create(content="甲", importance=5)
    b = await bucket_mgr.create(content="乙", importance=5)

    result = await trace_dispatch(bucket_id=a, unlink=b)

    assert "本来就没有关系" in result


@pytest.mark.asyncio
async def test_unlink_cleans_up_one_way_leftovers(bucket_mgr):
    """单向残留也要能删干净——存量数据里真的有这种。"""
    a, b = await _pair(bucket_mgr, one_way=True)
    assert len(await _links_of(bucket_mgr, a)) == 1
    assert await _links_of(bucket_mgr, b) == []

    result = await trace_dispatch(bucket_id=a, unlink=b)

    assert "已断开" in result
    assert await _links_of(bucket_mgr, a) == []


@pytest.mark.asyncio
async def test_unlink_leaves_other_relations_alone(bucket_mgr):
    a, b = await _pair(bucket_mgr)
    c = await bucket_mgr.create(content="第三条", importance=5)
    await _attach(bucket_mgr, a, c, "same_event")

    await trace_dispatch(bucket_id=a, unlink=b)

    remaining = await _links_of(bucket_mgr, a)
    assert [link["target_bucket_id"] for link in remaining] == [c]


# --------------------------------------------------------------
# 改类型
# --------------------------------------------------------------


@pytest.mark.asyncio
async def test_relink_changes_type_on_both_sides_with_reverse(bucket_mgr):
    """对侧取反向类型：A 是 B 的后续，那 B 就是 A 的前段。

    两侧写成同一个类型的话，从 B 看就成了"B 是 A 的后续"，方向反了。
    """
    a, b = await _pair(bucket_mgr)

    result = await trace_dispatch(
        bucket_id=a, relink=b, relation_type="continuation_of"
    )

    assert "已把" in result
    assert (await _links_of(bucket_mgr, a))[0]["type"] == "continuation_of"
    assert (await _links_of(bucket_mgr, b))[0]["type"] == "continues"


@pytest.mark.asyncio
async def test_relink_demotes_the_relation_to_manual(bucket_mgr):
    """改过的关系不再是自动关系。

    留着 auto 标记的话，它会继续按 score 参与每桶上限的裁剪——一条人明确
    判定过的关系，可能因为分数低被后来的自动推断挤掉。
    """
    a, b = await _pair(bucket_mgr)
    assert (await _links_of(bucket_mgr, a))[0]["auto"] is True

    await trace_dispatch(bucket_id=a, relink=b, relation_type="same_event")

    for bucket_id in (a, b):
        link = (await _links_of(bucket_mgr, bucket_id))[0]
        assert "auto" not in link
        assert "score" not in link


@pytest.mark.asyncio
async def test_relink_to_the_same_type_reports_no_change(bucket_mgr):
    a, b = await _pair(bucket_mgr, left_type="same_event", right_type="same_event")
    # 先降级成手动，再改成同一个类型才是真正的"无需改动"
    await trace_dispatch(bucket_id=a, relink=b, relation_type="same_event")

    result = await trace_dispatch(bucket_id=a, relink=b, relation_type="same_event")

    assert "已经是 same_event" in result


# --------------------------------------------------------------
# 参数组合
# --------------------------------------------------------------


@pytest.mark.asyncio
async def test_unlink_and_relink_are_mutually_exclusive(bucket_mgr):
    a, b = await _pair(bucket_mgr)

    with pytest.raises(ToolInputError) as excinfo:
        await trace_dispatch(
        bucket_id=a, unlink=b, relink=b, relation_type="same_event"
        )

    assert '不能同时使用' in str(excinfo.value)
    # 拒绝之后关系必须原封不动
    assert (await _links_of(bucket_mgr, a))[0]["type"] == "related_to"


@pytest.mark.asyncio
async def test_relink_without_relation_type_is_rejected(bucket_mgr):
    a, b = await _pair(bucket_mgr)

    with pytest.raises(ToolInputError) as excinfo:
        await trace_dispatch(bucket_id=a, relink=b)

    assert '必须同时指定 relation_type' in str(excinfo.value)
    assert (await _links_of(bucket_mgr, a))[0]["type"] == "related_to"


@pytest.mark.asyncio
async def test_relation_type_alone_is_rejected(bucket_mgr):
    """光传类型不说改哪一条，不能静默变成"什么都没做"。"""
    a, _ = await _pair(bucket_mgr)

    with pytest.raises(ToolInputError) as excinfo:
        await trace_dispatch(bucket_id=a, relation_type="same_event")

    assert '只能配合 relink 使用' in str(excinfo.value)


@pytest.mark.asyncio
async def test_custom_type_is_rejected_because_it_needs_a_label(bucket_mgr):
    a, b = await _pair(bucket_mgr)

    with pytest.raises(ToolInputError) as excinfo:
        await trace_dispatch(bucket_id=a, relink=b, relation_type="custom")

    assert 'custom' in str(excinfo.value)
    assert (await _links_of(bucket_mgr, a))[0]["type"] == "related_to"


@pytest.mark.asyncio
async def test_unknown_type_is_rejected(bucket_mgr):
    a, b = await _pair(bucket_mgr)

    with pytest.raises(ToolInputError) as excinfo:
        await trace_dispatch(bucket_id=a, relink=b, relation_type="是同一件事")

    assert '未知的 relation_type' in str(excinfo.value)
    assert (await _links_of(bucket_mgr, a))[0]["type"] == "related_to"


@pytest.mark.asyncio
async def test_self_reference_is_rejected(bucket_mgr):
    a = await bucket_mgr.create(content="只有自己", importance=5)

    with pytest.raises(ToolInputError) as excinfo:
        await trace_dispatch(bucket_id=a, unlink=a)
    assert '和它自己' in str(excinfo.value)


@pytest.mark.asyncio
async def test_missing_target_bucket_is_rejected(bucket_mgr):
    a = await bucket_mgr.create(content="存在的那条", importance=5)

    with pytest.raises(ToolInputError) as excinfo:
        await trace_dispatch(bucket_id=a, unlink="20990101-not-a-bucket")

    assert '找不到目标记忆' in str(excinfo.value)


@pytest.mark.asyncio
async def test_missing_source_bucket_is_rejected(bucket_mgr):
    b = await bucket_mgr.create(content="存在的那条", importance=5)

    with pytest.raises(ToolInputError) as excinfo:
        await trace_dispatch(bucket_id="20990101-not-a-bucket", unlink=b)

    assert '找不到记忆' in str(excinfo.value)


@pytest.mark.asyncio
async def test_ordinary_trace_still_works_when_relation_args_are_empty(bucket_mgr):
    """空的关系参数不能把普通 trace 改动吞掉。

    早返回分支写错的话，所有 trace 调用都会静默变成 no-op。
    """
    a, b = await _pair(bucket_mgr)

    result = await trace_dispatch(bucket_id=a, importance=9)

    bucket = await bucket_mgr.get(a)
    assert int(bucket["metadata"]["importance"]) == 9
    assert "已断开" not in result
    # 普通字段更新不应碰到关系
    assert len(await _links_of(bucket_mgr, b)) == 1


# --------------------------------------------------------------
# 纯函数
# --------------------------------------------------------------


def test_unlink_relation_reports_no_change_when_target_absent():
    links = [_link("b1")]
    kept, changed = unlink_relation(links, "b2")

    assert changed is False
    assert kept == links


def test_retype_relation_never_adds_a_missing_target():
    links = [_link("b1")]
    updated, changed = retype_relation(links, "b2", "same_event")

    assert changed is False
    assert [link["target_bucket_id"] for link in updated] == ["b1"]


def test_retype_relation_keeps_existing_label():
    """存量 custom 关系的 label 是当初写下的原话，改类型不该顺手抹掉。"""
    links = [{**_link("b1", "custom", score=None), "label": "一起淋的那场雨"}]
    updated, changed = retype_relation(links, "b1", "related_to")

    assert changed is True
    assert updated[0]["type"] == "related_to"
    assert updated[0]["label"] == "一起淋的那场雨"


def test_retype_relation_is_idempotent_on_manual_links():
    links = [_link("b1", "same_event", score=None)]
    _, changed = retype_relation(links, "b1", "same_event")

    assert changed is False
