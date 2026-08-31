"""trace 的每一种「本次未修改」都必须以 MCP 错误呈现。

真机复现（改之前）：

    trace(bucket_id="ffffffffffff", name="改个名")
      isError = False              ← 说成功
      正文   = "未找到记忆桶: ffffffffffff"

调用方（模型自己）只看 isError，就会以为改成功了。trace 尤其危险：
它是「修正记忆」的入口，一次静默失败意味着模型认定自己纠正了一条错误
记忆，而那条错的还原样躺在库里。

trace 里这类分支有四十多处，共同点是正文自己就写着「本次未修改」
「未找到」「拒绝」——函数明说什么都没做，却用一次正常返回表达。

不在这里拦的两类：
- 幂等：`A 与 B 之间本来就没有关系` —— 你要的状态已经达成，不是失败
- 降级：主体改成功了、只是附带信息没读到
"""

from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from errors import ToolInputError


class _NoopDecay:
    is_running = True

    async def ensure_started(self):
        return None

    def calculate_score(self, meta):
        return float(meta.get("importance") or 5)


class _StubDehydrator:
    api_available = True

    async def analyze(self, content):
        return {"domain": ["general"], "valence": 0.5, "arousal": 0.3,
                "tags": [], "suggested_name": ""}

    async def digest(self, content):
        raise AssertionError("trace 用例不该走 digest")


class _DisabledEmbedding:
    enabled = False


@pytest_asyncio.fixture
async def 环境(bucket_mgr, monkeypatch):
    """装好 runtime，并预先种一条真实记忆供改。"""
    import tools._runtime as rt

    monkeypatch.setattr(rt, "config", {"surfacing": {}, "limits": {}})
    monkeypatch.setattr(rt, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(rt, "decay_engine", _NoopDecay())
    monkeypatch.setattr(rt, "dehydrator", _StubDehydrator())
    monkeypatch.setattr(rt, "embedding_engine", _DisabledEmbedding())
    monkeypatch.setattr(rt, "logger", MagicMock())
    monkeypatch.setattr(rt, "fire_webhook", None)
    monkeypatch.setattr(rt, "mark_op", None)
    monkeypatch.setattr(rt, "record_v3_tool_event", lambda *a, **k: None)

    编号 = await bucket_mgr.create(
        content="那天下午我们把四个接口的退化路径逐条过了一遍。",
        title="接口评审",
        importance=6,
    )
    return bucket_mgr, 编号


# (说明, 参数是否需要真实 bucket_id, 参数, 错误正文里必须出现的片段)
失败用例 = [
    ("bucket_id 为空", False, {"bucket_id": "", "name": "x"}, "bucket_id"),
    ("bucket_id 全空白", False, {"bucket_id": "   ", "name": "x"}, "bucket_id"),
    ("bucket_id 不存在", False, {"bucket_id": "ffffffffffff", "name": "x"}, "未找到"),
    ("关系修正指向不存在的桶", False,
     {"bucket_id": "ffffffffffff", "unlink": "eeeeeeeeeeee"}, "找不到"),
    ("protected 传了非法值", True, {"protected": 7}, "protected"),
    ("unlink 与 relink 同时给", True,
     {"unlink": "aaaaaaaaaaaa", "relink": "bbbbbbbbbbbb"}, "不能同时"),
    ("relation_type 不配 relink", True, {"relation_type": "causes"}, "relink"),
    ("relink 不给 relation_type", True, {"relink": "bbbbbbbbbbbb"}, "relation_type"),
    ("relation_type 是未知值", True,
     {"relink": "bbbbbbbbbbbb", "relation_type": "根本没这种关系"}, "relation_type"),
    ("quotes_replace 不是列表", True, {"quotes_replace": "不是列表"}, "列表"),
    ("quotes_replace 混别的字段", True,
     {"quotes_replace": [], "importance": 8}, "单独调用"),
    ("quotes_replace 混关系修正", True,
     {"quotes_replace": [], "unlink": "bbbbbbbbbbbb"}, "分开调用"),
    ("old_str 找不到", True, {"old_str": "根本不存在的原文", "new_str": "x"}, "old_str"),
    ("只给 new_str 不给 old_str", True, {"new_str": "x"}, "old_str"),
    ("old_str 与 new_str 相同", True, {"old_str": "四个接口", "new_str": "四个接口"}, "没有"),
    ("content 与 old_str 同时给", True,
     {"content": "整段替换", "old_str": "四个接口", "new_str": "五个接口"}, "参数冲突"),
    ("delete 与 old_str 同时给", True,
     {"delete": True, "old_str": "四个接口", "new_str": "x"}, "参数冲突"),
    ("delete 与 hard_delete 同时给", True, {"delete": True, "hard_delete": True}, "不能同时"),
    ("hard_delete 普通桶", True,
     {"hard_delete": True, "delete_reason": "清理"}, "拒绝永久删除"),
    ("hard_delete 不给理由", True, {"hard_delete": True}, "拒绝永久删除"),
    ("restore 与删除混用", True, {"restore": True, "delete": True}, "参数冲突"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "说明, 要真id, 参数, 关键词", 失败用例, ids=[c[0] for c in 失败用例]
)
async def test_每一种未修改都必须抛错(环境, 说明, 要真id, 参数, 关键词):
    from tools.trace import dispatch as trace

    管理器, 编号 = 环境
    调用 = dict(参数)
    if 要真id:
        调用["bucket_id"] = 编号

    原文 = (await 管理器.get(编号))["content"]

    with pytest.raises(ToolInputError) as excinfo:
        await trace(**调用)

    assert 关键词 in str(excinfo.value), f"{说明}：错误正文没说清原因"
    assert (await 管理器.get(编号))["content"] == 原文, "说未修改就不能真的改了"


@pytest.mark.asyncio
async def test_正常修改照旧成功(环境):
    """反面。没有这一条，上面全部可以靠「trace 一律抛错」作弊通过。"""
    from tools.trace import dispatch as trace

    管理器, 编号 = 环境
    出 = await trace(bucket_id=编号, name="改过的名字")
    assert "改过的名字" in 出
    # 注意 name ≠ title：trace 的 name 改的是桶名，title 是另一个字段，
    # 见 test_trace_can_edit_title.py。
    assert (await 管理器.get(编号))["metadata"]["name"] == "改过的名字"


@pytest.mark.asyncio
async def test_正常局部替换照旧成功(环境):
    from tools.trace import dispatch as trace

    管理器, 编号 = 环境
    await trace(bucket_id=编号, old_str="四个接口", new_str="五个接口")
    assert "五个接口" in (await 管理器.get(编号))["content"]


@pytest.mark.asyncio
async def test_解一段本来就不存在的关系不算失败(环境):
    """幂等：你要的状态已经达成，不该报错。

    这条是上面那批的边界——分类标准是「有没有做事」时，很容易把
    「不需要做事」也一并算成失败。
    """
    from tools.trace import dispatch as trace

    管理器, 编号 = 环境
    另一条 = await 管理器.create(content="另一条无关的记忆。", importance=5)

    出 = await trace(bucket_id=编号, unlink=另一条)
    assert "本来就没有关系" in 出
