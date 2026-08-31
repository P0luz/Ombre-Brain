"""走真正的 MCP `Tool.run` 路径，确认「什么都没写」的失败是 isError=True。

与 test_hold_failure_is_error.py / test_trace_failure_is_error.py 的区别：
那两个直接调 dispatch，验的是工具自己抛没抛；这里从 `mcp._tool_manager`
取工具再 `run()`，把 server 的 `_with_notice` 兜底也一并走完——它才是决定
客户端最终看到 isError 是真是假的那一层。

这批用例来自一轮全项目查错：当时 6 处校验失败仍以字符串返回，在 MCP 侧
是一次正常返回，调用方（通常是模型自己）会以为写成功了继续往下走。逐条的
原始表现记在每个用例的 docstring 里。
"""

from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from mcp.server.fastmcp.exceptions import ToolError


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


@pytest_asyncio.fixture
async def 工具(bucket_mgr, monkeypatch, tmp_path):
    """把 runtime 接到临时 vault 上，返回 (取工具的函数, bucket_mgr)。"""
    import server
    import tools._runtime as rt
    from ombrebrain.storage.source_store import SourceStore

    monkeypatch.setattr(rt, "config", {"surfacing": {}, "limits": {}})
    monkeypatch.setattr(rt, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(rt, "decay_engine", _NoopDecay())
    monkeypatch.setattr(rt, "dehydrator", _StubDehydrator())
    monkeypatch.setattr(rt, "source_store", SourceStore(str(tmp_path)))
    monkeypatch.setattr(rt, "embedding_engine", bucket_mgr.embedding_engine)
    monkeypatch.setattr(rt, "logger", MagicMock())
    monkeypatch.setattr(rt, "fire_webhook", None)
    monkeypatch.setattr(rt, "mark_op", None)
    monkeypatch.setattr(rt, "record_v3_tool_event", lambda *a, **k: None)

    return (lambda 名: server.mcp._tool_manager.get_tool(名)), bucket_mgr


async def _桶数(管理器):
    return len(await 管理器.list_all(include_archive=False))


@pytest.mark.asyncio
async def test_plan正文超限是错误且不建桶(工具):
    """原表现：返回「内容过大（4882.8 KB > 上限 50 KB）」，isError=False，桶数不变。"""
    取, 管理器 = 工具
    前 = await _桶数(管理器)
    with pytest.raises(ToolError, match="内容过大"):
        await 取("plan").run({"content": "长" * 5_000_000})
    assert await _桶数(管理器) == 前


@pytest.mark.asyncio
async def test_trace元数据超限是错误且不改桶(工具):
    """原表现：返回「元数据过大（97.7 KB > 上限 16 KB）」，isError=False。"""
    取, 管理器 = 工具
    编号 = await 管理器.create(content="给 trace 用的靶子。", title="靶子")
    with pytest.raises(ToolError, match="元数据过大"):
        await 取("trace").run({"bucket_id": 编号, "meaning_append": "长" * 100_000})
    桶 = await 管理器.get(编号)
    assert 桶["metadata"]["title"] == "靶子", "被拒的调用不该留下任何改动"


@pytest.mark.asyncio
async def test_grow未支持字段是错误且不建桶(工具):
    """原表现：返回「grow items 第 1 项包含未支持字段: unknown」，isError=False。

    校验在 grow_items() 之前，一个桶都没建。helper 仍返回错误串（它在
    _common.py 里被多处共用），由调用点负责抛出。
    """
    取, 管理器 = 工具
    前 = await _桶数(管理器)
    with pytest.raises(ToolError, match="未支持字段"):
        await 取("grow").run({"items": [{"content": "一条内容", "unknown": "字段"}]})
    assert await _桶数(管理器) == 前


@pytest.mark.asyncio
async def test_anchor元数据超限是错误(工具):
    取, _ = 工具
    with pytest.raises(ToolError, match="元数据过大"):
        await 取("anchor").run({"bucket_id": "长" * 100_000})


@pytest.mark.asyncio
async def test_anchor找不到桶是错误(工具):
    """`ok=False` 和 `noop=True` 是两个分支，别混为一谈。

    这条最初被我判成幂等跳过了：「已经是 anchor 了」确实是幂等，但「找不到
    该记忆桶」是桶根本不存在，一个 anchor 都没加上。
    """
    取, _ = 工具
    with pytest.raises(ToolError, match="没能把它锚住"):
        await 取("anchor").run({"bucket_id": "ffffffffffff"})


@pytest.mark.asyncio
async def test_trace归档找不到桶是错误(工具):
    """同一个函数里三种「找不到桶」原本有两种处理：hard_delete 分支抛异常，
    普通 delete 分支返回字符串。调用方拿到 isError=False 会以为归档成功了。"""
    取, _ = 工具
    with pytest.raises(ToolError, match="未找到记忆桶"):
        await 取("trace").run({"bucket_id": "ffffffffffff", "delete": True})


@pytest.mark.asyncio
async def test_hold的原文不会先于媒体失败落盘(工具, tmp_path):
    """半成功：媒体在建桶那步才失败，而原文证据在那之前就写进 _sources 了。

    错误正文说的是「未创建任何桶」——桶确实没建，但原文已经在那了，
    调用方据此重试就会留下上一半副作用。
    """
    取, _ = 工具
    with pytest.raises(ToolError):
        await 取("hold").run({
            "content": "一条带原文和坏媒体的记忆。",
            "source_content": "这段原文不该活下来。",
            "media": [{"data_base64": "@@@不是base64@@@", "filename": "x.png"}],
        })
    残留 = list(tmp_path.glob("**/*.source"))
    assert not 残留, f"原文先落了盘：{[p.name for p in 残留]}"


@pytest.mark.asyncio
async def test_正常调用不受影响(工具):
    """反面：这批改动只该动「失败」这条路，成功的照旧成功。"""
    取, 管理器 = 工具
    前 = await _桶数(管理器)
    结果 = await 取("hold").run({"content": "一条完全正常的记忆内容。"})
    assert await _桶数(管理器) == 前 + 1
    assert 结果 is not None


@pytest.mark.asyncio
async def test_已经是anchor仍算成功(工具):
    """反面：幂等不是失败。你要的状态已经达成了。"""
    取, 管理器 = 工具
    编号 = await 管理器.create(content="要被锚住的记忆。", title="锚")
    await 取("anchor").run({"bucket_id": 编号})
    await 取("anchor").run({"bucket_id": 编号})  # 第二次：不抛
