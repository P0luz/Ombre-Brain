import pytest

from tools import _runtime as rt
import tools.breath as breath_mod
import tools.hold as hold_mod
from errors import ToolInputError
import tools.trace.core as trace_mod


class _Decay:
    async def ensure_started(self):
        return None


@pytest.mark.asyncio
async def test_breath_dispatch_records_v3_tool_event(monkeypatch) -> None:
    calls = []

    async def fake_surface_default(**_kwargs):
        return "breath result"

    rt.init(config={"surfacing": {}}, decay_engine=_Decay(), mark_op=None)
    monkeypatch.setattr(rt, "record_v3_tool_event", lambda name, payload: calls.append((name, payload)))
    monkeypatch.setattr(breath_mod, "surface_default", fake_surface_default)

    result = await breath_mod.dispatch(query="", max_results=2)

    assert result == "breath result"
    assert calls[0][0] == "breath"
    assert calls[0][1]["query"] == ""
    assert calls[0][1]["max_results"] == 2


@pytest.mark.asyncio
async def test_hold_dispatch_records_v3_tool_event_without_content_body(monkeypatch) -> None:
    calls = []

    async def fake_store_core(**_kwargs):
        return "hold result"

    rt.init(config={}, decay_engine=_Decay(), mark_op=None)
    monkeypatch.setattr(rt, "record_v3_tool_event", lambda name, payload: calls.append((name, payload)))
    monkeypatch.setattr(hold_mod, "check_content_size", lambda _content: None)
    monkeypatch.setattr(hold_mod, "store_core", fake_store_core)

    result = await hold_mod.dispatch(content="private memory body", tags="x,y", importance=7)

    assert result == "hold result"
    assert calls[0][0] == "hold"
    assert calls[0][1]["content_length"] == len("private memory body")
    assert "content" not in calls[0][1]


@pytest.mark.asyncio
async def test_hold_dispatch_normalizes_and_forwards_domain(monkeypatch) -> None:
    captured = {}

    async def fake_store_core(**kwargs):
        captured.update(kwargs)
        return "hold result"

    rt.init(config={}, decay_engine=_Decay(), mark_op=None)
    monkeypatch.setattr(hold_mod, "check_content_size", lambda _content: None)
    monkeypatch.setattr(hold_mod, "store_core", fake_store_core)

    result = await hold_mod.dispatch(
        content="domain memory",
        domain=" 工作, 生活,工作 ",
    )

    assert result == "hold result"
    assert captured["explicit_domain"] == ["工作", "生活"]


@pytest.mark.asyncio
async def test_hold_rejects_explicit_domain_for_feel(monkeypatch) -> None:
    called = False

    async def fake_store_feel(**_kwargs):
        nonlocal called
        called = True
        return "unexpected"

    rt.init(config={}, decay_engine=_Decay(), mark_op=None)
    monkeypatch.setattr(hold_mod, "store_feel", fake_store_feel)

    # 拒绝必须是抛出，不是返回一句说明：返回字符串在 MCP 侧是 isError=False，
    # 调用方会以为这条 feel 写成功了。
    with pytest.raises(ToolInputError, match="feel 的 domain 固定为 feel"):
        await hold_mod.dispatch(
            content="feel memory",
            feel=True,
            source_bucket="source-1",
            domain="生活",
        )
    assert called is False


@pytest.mark.asyncio
async def test_trace_core_records_v3_tool_event_without_content_body(monkeypatch) -> None:
    calls = []

    rt.init(config={}, mark_op=None)
    monkeypatch.setattr(rt, "record_v3_tool_event", lambda name, payload: calls.append((name, payload)))

    with pytest.raises(ToolInputError) as excinfo:
        await trace_mod.trace_core(bucket_id="", content="private replacement", delete=True)

    assert 'bucket_id' in str(excinfo.value)
    assert calls[0][0] == "trace"
    assert calls[0][1]["delete"] is True
    assert calls[0][1]["content_length"] == len("private replacement")
    assert "content" not in calls[0][1]


@pytest.mark.asyncio
async def test_trace_core_records_v3_tool_event_without_patch_bodies(monkeypatch) -> None:
    calls = []

    rt.init(config={}, mark_op=None)
    monkeypatch.setattr(rt, "record_v3_tool_event", lambda name, payload: calls.append((name, payload)))

    with pytest.raises(ToolInputError) as excinfo:
        await trace_mod.trace_core(
        bucket_id="",
        old_str="private old fragment",
        new_str="private new fragment",
        )

    assert 'bucket_id' in str(excinfo.value)
    assert calls[0][0] == "trace"
    assert calls[0][1]["content_length"] == 0
    assert calls[0][1]["old_str_length"] == len("private old fragment")
    assert calls[0][1]["new_str_length"] == len("private new fragment")
    assert "content" not in calls[0][1]
    assert "old_str" not in calls[0][1]
    assert "new_str" not in calls[0][1]
