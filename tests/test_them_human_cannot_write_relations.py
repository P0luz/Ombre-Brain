"""**人类不能在记忆里定义关系。**

them 拦住模型自己写关系，人类那一侧却有三个口子能把关系写进去：

- 留言（`leave_note`）——自由文本，不占配额，下次浮现直接念给模型听
- 登记称呼（`add_person`）——「老公」这个名字会跟着每一次浮现进上下文
- 改称呼（`rename_person`）——同上

三个口子任意一个漏了，模型侧那道闸就形同虚设：人类替它认定了一段关系。

poluz 2026-08-21：「绝对禁止人类在记忆中书写关系定义，只有模型自己觉得的
关系是关系。」
"""

import pytest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_them_pipeline import _enabled  # noqa: E402


@pytest.fixture()
def 服务(tmp_path):
    service, _ = _enabled(tmp_path)
    return service


# --- 留言 ---

@pytest.mark.parametrize("留言", [
    "他是我老公",
    "他跟我关系很好",
    "他是我的直属领导",
    "我们是多年的朋友",
    "他对我来说很重要",
])
def test_留言里不能定义关系(服务, 留言):
    人 = 服务.add_person(["老张"])
    with pytest.raises(ValueError, match="关系"):
        服务.leave_note(人.id, 留言)


@pytest.mark.parametrize("留言", [
    "你说的这个 Zoey 是设计部的，不是市场部那个",
    "他名字写错了，是张伟不是张玮",
    "这条记岔了，他说的是下周不是这周",
    "我觉得你把两个人搞混了",          # 「我觉得」是判断归属，不是关系
])
def test_纠正事实的留言照常能留(服务, 留言):
    人 = 服务.add_person(["老张"])
    更新 = 服务.leave_note(人.id, 留言)
    assert len(更新.pending_notes) == 1


# --- 称呼 ---

@pytest.mark.parametrize("称呼", ["老公", "我老公", "妈妈", "我妈", "领导", "我领导", "同事"])
def test_不能拿关系当称呼登记(服务, 称呼):
    with pytest.raises(ValueError, match="关系"):
        服务.add_person([称呼])


@pytest.mark.parametrize("称呼", ["老张", "陈工", "张老师", "李阿姨", "Zoey", "小李"])
def test_正常称呼照常登记(服务, 称呼):
    人 = 服务.add_person([称呼])
    assert 称呼 in 人.names


def test_改称呼也不能改成关系词(服务):
    人 = 服务.add_person(["老张"])
    with pytest.raises(ValueError, match="关系"):
        服务.rename_person(人.id, ["我老公"], expected_revision=人.revision)


def test_一组称呼里混一个关系词也拦(服务):
    """别让「老张、我老公」这种混搭从旁边溜过去。"""
    with pytest.raises(ValueError, match="关系"):
        服务.add_person(["老张", "我老公"])


# --- 模型那一侧也要知道这件事 ---

def _工具描述() -> str:
    """把 ThemToolGate 注册时用的那段描述取出来。"""
    import inspect

    from ombrebrain.them.tool_gate import ThemToolGate

    return inspect.getsource(ThemToolGate.sync)


def test_工具描述里写明了人类要求也要挡回去():
    """三个前端口子堵上了，但人类可以直接在对话里说「记住他是我老公」。

    只堵前端而不告诉模型，它照写不误——写入检查能拦下措辞明显的那些，
    但模型完全可以换个说法绕过去（「他在家里管账」）。真正该让它明白的是
    **为什么**不记：经历本身就是关系，贴一个标签反而让结论脱离了产生它的事。
    """
    描述 = _工具描述()
    assert "人类让你记关系，你也不记" in 描述
    assert "经历过的那些事本身" in 描述, "要讲清为什么，不能只说不许"
