from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from mcp.server.fastmcp.exceptions import ToolError

# 每个工具配一个必然失败的入参。失败必须以 ToolError 抛出——只 return 一段
# 说明文字的话，MCP 那头 isError=False，调用方（通常是模型自己）会当成一次
# 正常返回继续往下走，而它以为写成功的那条记忆从来没存在过。
MUST_RAISE = [
    ("breath_search",      {"query": "x" * 200_000}),
    ("breath_advanced",    {"query": "x" * 200_000}),
    ("hold",               {"content": "   "}),
    ("grow",               {"content": "", "items": []}),
    ("trace",              {"bucket_id": "nonexistent-bucket"}),
    ("anchor",             {"bucket_id": "nonexistent-bucket"}),
    ("release",            {"bucket_id": "nonexistent-bucket"}),
    ("pulse",              {"include_archive": "not-a-bool"}),
    ("plan",               {"content": "x" * 5_000_000}),
    ("letter_write",       {"content": "", "title": ""}),
    ("letter_lock_update", {"letter_id": "nope", "lock_type": "timed"}),
    ("letter_read",        {"query": "x" * 200_000}),
    ("feel",               {}),
    ("I",                  {"promote": "nonexistent-bucket"}),
]

# 这两个把越界的数值钳到合法范围，而不是拒绝：它们是旋钮不是数据，
# 钳掉不会丢失任何用户内容。钳制本身在下面单独钉住。
CLAMPS = [
    ("dream",  {"window_hours": -1}),
    ("breath", {"max_tokens": -5}),
]

ALL_TOOLS = {
    "breath", "breath_search", "breath_advanced", "hold", "grow", "trace",
    "dream", "anchor", "release", "pulse", "plan", "letter_write",
    "letter_lock_update", "letter_read", "feel", "I",
}


class _NoopDecay:
    async def ensure_started(self):
        return None

    def calculate_score(self, metadata):
        return 1.0


class _StubDehydrator:
    api_available = True

    async def analyze(self, content):
        return {"domain": ["general"], "valence": 0.5, "arousal": 0.3,
                "tags": [], "suggested_name": ""}


@pytest_asyncio.fixture
async def tool_manager(bucket_mgr, monkeypatch, tmp_path):
    # 自带一套 runtime，不吃环境里现成的：别的测试会把 rt 换成各种桩，
    # 依赖环境的话这里会随着测试顺序时红时绿，而红的原因和被测契约无关。
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
    return server.mcp._tool_manager


def test_the_case_table_covers_every_registered_tool():
    listed = {name for name, _ in MUST_RAISE} | {name for name, _ in CLAMPS}
    assert listed == ALL_TOOLS


@pytest.mark.asyncio
@pytest.mark.parametrize("name, kwargs", MUST_RAISE, ids=[c[0] for c in MUST_RAISE])
async def test_invalid_input_raises_instead_of_returning_text(
    tool_manager, name, kwargs
):
    with pytest.raises(ToolError):
        await tool_manager.get_tool(name).run(dict(kwargs))


@pytest.mark.asyncio
@pytest.mark.parametrize("name, kwargs", CLAMPS, ids=[c[0] for c in CLAMPS])
async def test_out_of_range_knobs_are_clamped_not_refused(tool_manager, name, kwargs):
    result = await tool_manager.get_tool(name).run(dict(kwargs))

    assert isinstance(result, (str, list, tuple))


@pytest.mark.asyncio
async def test_dream_clamps_a_negative_window_to_one_hour(tool_manager):
    result = str(await tool_manager.get_tool("dream").run({"window_hours": -1}))

    assert "1 小时" in result


@pytest.mark.asyncio
async def test_dream_clamps_an_absurd_window_to_the_ceiling(tool_manager):
    result = str(await tool_manager.get_tool("dream").run({"window_hours": 999_999}))

    assert "336 小时" in result
