"""media 存不下时，调用方必须知道「没存上」和「为什么」。

真机复现（改之前）：

    hold(content="带图", media=["screenshot.png"])
      isError = False                                    ← 说成功
      落库   = 0 个                                       ← 实际什么都没有
      正文   = MediaPersistenceError：异常正文已隐藏         ← 还不说原因

三样凑齐，是所有失败形态里最坏的一种。而 media_store 里的原话是
「媒体临时路径在 OB 服务器上不可读：xxx。**请改传 data_base64**」——
一句能让调用方自己改对的话，一个字都没送出去。

正文可以回显，因为 MediaPersistenceError 的消息里只含调用方自己传进来的
路径，没有服务器内部路径（media_store.py 全文核对过）。把它自己给的东西
还给它，不泄露任何新信息。

为什么不让 media_store 直接抛 ToolInputError：ombrebrain/ 这个包全文
零处 import 顶层的 errors 模块，那条分层边界比少写一层翻译更值钱。
翻译放在工具层。
"""

from unittest.mock import MagicMock

import pytest

from errors import ToolInputError


class _NoopDecay:
    is_running = True

    async def ensure_started(self):
        return None

    def calculate_score(self, meta):
        return float(meta.get("importance") or 5)


class _StubDehydrator:
    """打标返回中性值。media 这条路不该依赖 LLM。"""

    api_available = True

    async def analyze(self, content):
        return {"domain": ["general"], "valence": 0.5, "arousal": 0.3,
                "tags": [], "suggested_name": ""}

    async def digest(self, content):
        raise AssertionError("media 用例不该走 digest 拆分")


class _DisabledEmbedding:
    enabled = False


@pytest.fixture
def hold_env(bucket_mgr, monkeypatch):
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
    return bucket_mgr


坏media = [
    ("路径不存在", ["不存在的图.png"], "data_base64"),
    ("绝对路径不存在", ["/tmp/根本没有这个文件.png"], "data_base64"),
    ("dict 缺 path", [{"type": "image/png"}], "path"),
    ("base64 不合法", [{"data_base64": "这不是base64!!!", "filename": "x.png"}], "Base64"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "说明, media, 关键词", 坏media, ids=[c[0] for c in 坏media]
)
async def test_hold的media失败必须报错并说清原因(hold_env, 说明, media, 关键词):
    from tools.hold import dispatch as hold

    with pytest.raises(ToolInputError) as excinfo:
        await hold(content=f"带图的记忆：{说明}", media=media)

    assert 关键词 in str(excinfo.value), f"{说明}：原因没送到调用方手里"
    assert await hold_env.list_all() == [], "说失败就不能留下半个桶"


@pytest.mark.asyncio
async def test_trace的media失败同样报错(hold_env):
    """trace 有 media_append / media_replace 两条口子，走的是同一个存储。"""
    from tools.hold import dispatch as hold
    from tools.trace import dispatch as trace

    await hold(content="先建一条正常的记忆。")
    桶 = await hold_env.list_all()
    编号 = 桶[0]["id"]

    with pytest.raises(ToolInputError, match="data_base64"):
        await trace(bucket_id=编号, media_append=["不存在的图.png"])


@pytest.mark.asyncio
async def test_合法的media照常存进去(hold_env):
    """反面：没有这一条，上面几条可以靠「media 一律报错」作弊通过。"""
    from tools.hold import dispatch as hold

    # 一个最小的合法 PNG 头，够证明这条路是通的
    await hold(content="带一张真图的记忆。", media=[
        {"data_base64": "iVBORw0KGgo=", "filename": "x.png", "type": "image/png"},
    ])

    桶 = await hold_env.list_all()
    assert len(桶) == 1
    assert 桶[0]["metadata"].get("media"), "合法 media 应该落到 metadata 上"


@pytest.mark.asyncio
async def test_空media不算失败(hold_env):
    """`media=[]` 是「没有媒体」，不是「媒体有问题」。"""
    from tools.hold import dispatch as hold

    await hold(content="不带图的记忆。", media=[])
    assert len(await hold_env.list_all()) == 1
