"""trace 必须能改 title，不只是 name。

真机复现（改之前）：

    trace(bucket_id=信件id, name="【改过的标题】")
      → "已修改记忆桶 853474f22fe8: name=【改过的标题】"
      → 信件的 title 一个字没变

`name` 和 `title` 是两个字段：name 是桶名（进文件名、做显示回退），
title 是这条记忆自己的标题，信件的标题就存在这里。trace 只开放了 name，
所以模型看着「已修改」的回执，改的却是另一样东西——它没有任何办法
知道自己改错了地方。

BucketManager.update() 一直支持 title，缺的只是 trace 这一层的入口。

带锁的信仍然改不了：锁检查在收集字段之前，title 走同一条路。
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


class _DisabledEmbedding:
    enabled = False


@pytest_asyncio.fixture
async def 环境(bucket_mgr, monkeypatch):
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
        content="一条有标题的普通记忆。", title="原来的标题", importance=6,
    )
    return bucket_mgr, 编号


@pytest.mark.asyncio
async def test_trace能改title(环境):
    from tools.trace import dispatch as trace

    管理器, 编号 = 环境
    出 = await trace(bucket_id=编号, title="改过的标题")

    assert (await 管理器.get(编号))["metadata"]["title"] == "改过的标题"
    assert "title" in 出, "回执要说清改的是 title，不能让调用方以为改的是别的字段"


@pytest.mark.asyncio
async def test_title与name是两个字段互不影响(环境):
    """这正是 bug 的核心：改 name 时 title 纹丝不动，反过来也一样。"""
    from tools.trace import dispatch as trace

    管理器, 编号 = 环境
    await trace(bucket_id=编号, name="桶名")
    桶 = await 管理器.get(编号)
    assert 桶["metadata"]["name"] == "桶名"
    assert 桶["metadata"]["title"] == "原来的标题", "改 name 不该动 title"

    await trace(bucket_id=编号, title="新标题")
    桶 = await 管理器.get(编号)
    assert 桶["metadata"]["title"] == "新标题"
    assert 桶["metadata"]["name"] == "桶名", "改 title 不该动 name"


@pytest.mark.asyncio
async def test_可以同时改name和title(环境):
    from tools.trace import dispatch as trace

    管理器, 编号 = 环境
    await trace(bucket_id=编号, name="新桶名", title="新标题")
    桶 = await 管理器.get(编号)
    assert 桶["metadata"]["name"] == "新桶名"
    assert 桶["metadata"]["title"] == "新标题"


@pytest.mark.asyncio
async def test_title超长被拒且不改(环境):
    from tools.trace import dispatch as trace

    管理器, 编号 = 环境
    with pytest.raises(ToolInputError, match="120"):
        await trace(bucket_id=编号, title="长" * 121)
    assert (await 管理器.get(编号))["metadata"]["title"] == "原来的标题"


@pytest.mark.asyncio
async def test_信件的标题改得动(环境):
    """用户报的就是这条：AI 无法修改信件标题。"""
    from tools.plan.core import letter_write
    from tools.trace import dispatch as trace

    管理器, _ = 环境
    await letter_write(author="ai", content="一封普通的信。", title="原来的信题")
    信 = next(
        b for b in await 管理器.list_all()
        if (b["metadata"].get("type") == "letter")
    )

    await trace(bucket_id=信["id"], title="改过的信题")
    assert (await 管理器.get(信["id"]))["metadata"]["title"] == "改过的信题"


@pytest.mark.asyncio
async def test_对方锁着的信改不动标题(环境):
    """title 不能成为绕过锁的新口子。

    锁是冲着另一方的：`locked = lock_type != none and not owner`。AI 自己
    锁的信 AI 当然能改，改不动的是**对方锁的那封**。而从 MCP 入口造不出
    「user 锁的信」（代存不能带锁），所以这里直接落一条那样的桶。
    """
    from tools.trace import dispatch as trace

    管理器, _ = 环境
    信id = await 管理器.create(
        content="user 锁住的信。",
        title="锁着的信题",
        bucket_type="letter",
        lock_type="permanent",
        locked_by="user",
        writer_name="poluz",
    )

    with pytest.raises(ToolInputError, match="尚未向你开放"):
        await trace(bucket_id=信id, title="想偷偷改掉")
    assert (await 管理器.get(信id))["metadata"]["title"] == "锁着的信题"


@pytest.mark.asyncio
async def test_自己锁的信自己能改标题(环境):
    """反面：锁不该把写信的人自己也挡在外面。"""
    from tools.plan.core import letter_write
    from tools.trace import dispatch as trace

    管理器, _ = 环境
    await letter_write(
        author="ai", content="我自己锁的信。", title="我的信题",
        lock_type="permanent", ai_name="小影", user_name="poluz",
    )
    信 = next(
        b for b in await 管理器.list_all()
        if b["metadata"].get("lock_type") == "permanent"
    )

    await trace(bucket_id=信["id"], title="我改我的")
    assert (await 管理器.get(信["id"]))["metadata"]["title"] == "我改我的"
