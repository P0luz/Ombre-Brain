"""Open WebUI Filter：inlet / outlet 对上 OB 的 session-start / prompt / stop。"""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_autorecall_gateway import FakeOB  # noqa: E402

_PATH = Path(__file__).resolve().parents[1] / "deploy" / "openwebui" / "ob_filter.py"


def _load():
    spec = importlib.util.spec_from_file_location("ob_filter", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def setup():
    ob = FakeOB()
    f = _load().Filter()
    f.valves.ob_url = ob.url
    f.valves.hook_token = "tok"
    yield ob, f
    ob.server.shutdown()


def test_inlet_injects_and_outlet_stores(setup):
    ob, f = setup
    user = {"id": "u1"}
    meta = {"chat_id": "c1"}
    body = {"messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": "你还记得吗"}]}
    out = asyncio.run(f.inlet(body, __user__=user, __metadata__=meta))
    system = out["messages"][0]["content"]
    assert system.startswith("sys") and "[接续]" in system and "[记忆] 猫叫阿树" in system
    assert ob.calls[0][1]["cwd"] == "open-webui:u1" and ob.calls[0][1]["session_id"] == "owui-c1"

    async def outlet():
        await f.outlet({"messages": [{"role": "user", "content": "你还记得吗"},
                                     {"id": "m9", "role": "assistant", "content": "记得"}]},
                       __user__=user, __metadata__=meta)
        await asyncio.gather(*f._pending)
    asyncio.run(outlet())
    stop = [c[1] for c in ob.calls if c[0] == "stop"][0]
    assert stop["messages"][0]["text"] == "记得" and stop["messages"][0]["key"].startswith("a:m9:")


def test_followup_reuses_start_and_skips_tasks(setup):
    ob, f = setup
    meta = {"chat_id": "c2"}
    first = [{"role": "user", "content": "嗨"}]
    asyncio.run(f.inlet({"messages": first}, __metadata__=meta))
    out = asyncio.run(f.inlet({"messages": first + [{"role": "assistant", "content": "嗨"},
                                                    {"role": "user", "content": "再来"}]}, __metadata__=meta))
    assert [c[0] for c in ob.calls].count("session-start") == 1
    assert out["messages"][0] == {"role": "system", "content": "[接续] 上次聊到猫"}
    n = len(ob.calls)
    asyncio.run(f.inlet({"messages": [{"role": "user", "content": "生成标题"}]},
                        __metadata__={"chat_id": "c2", "task": "title_generation"}))
    assert len(ob.calls) == n


def test_no_token_or_ob_down_passes_through():
    f = _load().Filter()
    body = {"messages": [{"role": "user", "content": "x"}]}
    assert asyncio.run(f.inlet(dict(body), __metadata__={"chat_id": "c"})) == body
    ob = FakeOB(down=True)
    try:
        f.valves.ob_url, f.valves.hook_token = ob.url, "tok"
        assert asyncio.run(f.inlet(dict(body), __metadata__={"chat_id": "c3"})) == body
    finally:
        ob.server.shutdown()


def test_without_chat_id_inlet_and_outlet_agree_on_session(setup):
    """直连 API 没有 chat_id：outlet 看到的 messages 带着注入的 system，会话 id 仍须和 inlet 一致。"""
    ob, f = setup
    asyncio.run(f.inlet({"messages": [{"role": "user", "content": "你还记得吗"}]}, __metadata__={}))

    async def outlet():
        await f.outlet({"messages": [{"role": "system", "content": "[接续] 上次聊到猫"},
                                     {"role": "user", "content": "你还记得吗"},
                                     {"role": "assistant", "content": "记得"}]}, __metadata__={})
        await asyncio.gather(*f._pending)
    asyncio.run(outlet())
    assert len({c[1]["session_id"] for c in ob.calls}) == 1
