"""them 的开关与配额端点：显隐、乐观并发、配额落盘。

结构照 `test_you_toggle.py`。多测的是配额那一半——第一版我只把它写进内存里的
config，人类在前端看到「保存成功」，进程一重启就被磁盘上的旧值盖回去。
这里的用例锁的就是那件事：**配额必须真的落到 config.yaml**，
落盘失败必须如实报错，而且不能顺手把已经生效的开关一起回滚。
"""

import json

import pytest
from mcp.server.fastmcp import FastMCP

from ombrebrain.them import Person, ThemService, ThemStore, ThemStoreError, ThemToolGate
from tools import _runtime as tools_runtime
from tools.them import dispatch
from web import them as them_web


class FakeBucketManager:
    async def get(self, _bucket_id):
        return None


class FakeDecay:
    @staticmethod
    def calculate_score(_metadata):
        return 1.0


class FakeSourceStore:
    def read(self, _source_id):
        return ""


class RouteMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(handler):
            for method in methods:
                self.routes[(method, path)] = handler
            return handler

        return decorator


class JsonRequest:
    def __init__(self, body=None):
        self.body = body

    async def json(self):
        return self.body


def make_service(tmp_path, config=None):
    return ThemService(
        store=ThemStore(tmp_path),
        bucket_mgr=FakeBucketManager(),
        decay_engine=FakeDecay(),
        source_store=FakeSourceStore(),
        config=config if config is not None else {},
    )


def _wire(tmp_path, monkeypatch, config=None):
    service = make_service(tmp_path, config)
    tool_mcp = FastMCP("tools")
    gate = ThemToolGate(tool_mcp, dispatch)
    route_mcp = RouteMCP()
    monkeypatch.setattr(them_web.sh, "them_service", service)
    monkeypatch.setattr(them_web.sh, "them_tool_gate", gate)
    monkeypatch.setattr(them_web.sh, "_require_auth", lambda _request: None)
    them_web.register(route_mcp)
    return service, tool_mcp, route_mcp


@pytest.mark.asyncio
async def test_默认关闭时不建库也不挂工具(tmp_path, monkeypatch):
    _, tool_mcp, route_mcp = _wire(tmp_path, monkeypatch)
    resp = await route_mcp.routes[("GET", "/api/settings/them")](JsonRequest())
    payload = json.loads(resp.body)
    assert payload["enabled"] is False
    assert payload["max_tokens_per_person"] == 1500
    assert not (tmp_path / ".them").exists()
    assert await tool_mcp.list_tools() == []


@pytest.mark.asyncio
async def test_开关驱动工具显隐并守乐观并发(tmp_path, monkeypatch):
    _, tool_mcp, route_mcp = _wire(tmp_path, monkeypatch)
    post = route_mcp.routes[("POST", "/api/settings/them")]

    on = await post(JsonRequest({"enabled": True, "state_revision": 0}))
    assert on.status_code == 200
    assert [tool.name for tool in await tool_mcp.list_tools()] == ["Them"]

    stale = await post(JsonRequest({"enabled": False, "state_revision": 0}))
    assert stale.status_code == 409
    assert [tool.name for tool in await tool_mcp.list_tools()] == ["Them"]

    off = await post(JsonRequest({"enabled": False, "state_revision": 1}))
    assert off.status_code == 200
    assert await tool_mcp.list_tools() == []


@pytest.mark.asyncio
async def test_关闭之后工具句柄立刻失效(tmp_path, monkeypatch):
    service, _, route_mcp = _wire(tmp_path, monkeypatch)
    monkeypatch.setattr(tools_runtime, "them_service", service)
    post = route_mcp.routes[("POST", "/api/settings/them")]
    await post(JsonRequest({"enabled": True, "state_revision": 0}))
    缓存的句柄 = dispatch
    await post(JsonRequest({"enabled": False, "state_revision": 1}))
    with pytest.raises(ThemStoreError, match="unknown tool"):
        await 缓存的句柄()


class TestQuota:
    @pytest.mark.asyncio
    async def test_配额写进config并同步到内存(self, tmp_path, monkeypatch):
        写入的 = {}

        def 假落盘(mutate):
            save = {}
            mutate(save)
            写入的.update(save)
            return save

        monkeypatch.setattr(them_web, "atomic_update_config_yaml", 假落盘)
        service, _, route_mcp = _wire(tmp_path, monkeypatch, config={})
        post = route_mcp.routes[("POST", "/api/settings/them")]

        r = await post(JsonRequest({
            "enabled": True, "state_revision": 0, "max_tokens_per_person": 900
        }))
        assert r.status_code == 200
        # 磁盘那一份
        assert 写入的["them"]["max_tokens_per_person"] == 900
        # 内存那一份
        assert service.max_tokens_per_person == 900
        assert json.loads(r.body)["max_tokens_per_person"] == 900

    @pytest.mark.asyncio
    async def test_落盘失败要如实报错且不回滚开关(self, tmp_path, monkeypatch):
        """开关已经生效了，报错不能报在没坏的那一半上。"""
        def 落盘就炸(_mutate):
            raise OSError("config.yaml is read-only")

        monkeypatch.setattr(them_web, "atomic_update_config_yaml", 落盘就炸)
        service, tool_mcp, route_mcp = _wire(tmp_path, monkeypatch, config={})
        post = route_mcp.routes[("POST", "/api/settings/them")]

        r = await post(JsonRequest({
            "enabled": True, "state_revision": 0, "max_tokens_per_person": 900
        }))
        assert r.status_code == 503
        assert "配额没能写进" in json.loads(r.body)["error"]
        # 开关确实生效了，没被顺手撤掉
        assert service.status().enabled is True
        assert [tool.name for tool in await tool_mcp.list_tools()] == ["Them"]
        # 配额仍是原值，不是那个没写成功的新值
        assert service.max_tokens_per_person == 1500

    @pytest.mark.asyncio
    @pytest.mark.parametrize("值", [100, 5000, -1, 0])
    async def test_超出范围的配额被拒(self, tmp_path, monkeypatch, 值):
        _, _, route_mcp = _wire(tmp_path, monkeypatch, config={})
        post = route_mcp.routes[("POST", "/api/settings/them")]
        r = await post(JsonRequest({
            "enabled": True, "state_revision": 0, "max_tokens_per_person": 值
        }))
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_配额必须是整数(self, tmp_path, monkeypatch):
        _, _, route_mcp = _wire(tmp_path, monkeypatch, config={})
        post = route_mcp.routes[("POST", "/api/settings/them")]
        for 坏值 in (True, "1500", 1500.5, None):
            r = await post(JsonRequest({
                "enabled": True, "state_revision": 0, "max_tokens_per_person": 坏值
            }))
            assert r.status_code == 400, 坏值

    @pytest.mark.asyncio
    async def test_不接受多余字段(self, tmp_path, monkeypatch):
        _, _, route_mcp = _wire(tmp_path, monkeypatch, config={})
        post = route_mcp.routes[("POST", "/api/settings/them")]
        r = await post(JsonRequest({
            "enabled": True, "state_revision": 0, "scope": "谁的"
        }))
        assert r.status_code == 400


class TestPeopleRoutes:
    """人类唯一看得见、改得动的东西：称呼。"""

    async def _person(self, service, tmp_path):
        service.set_enabled(True)
        scope = service.status().scope
        return service.store.put_person(scope, Person.new(["Zoey", "小 Z"]))

    @pytest.mark.asyncio
    async def test_名册只给称呼(self, tmp_path, monkeypatch):
        service, _, route_mcp = _wire(tmp_path, monkeypatch)
        person = await self._person(service, tmp_path)
        resp = await route_mcp.routes[("GET", "/api/them/people")](JsonRequest())
        people = json.loads(resp.body)["people"]
        assert len(people) == 1
        assert people[0]["person_id"] == person.id
        assert people[0]["names"] == ["Zoey", "小 Z"]
        assert people[0]["origin"] == "model"
        # 模型自己认识的人：名册里不带任何一条认识
        assert "claims" not in people[0]

    @pytest.mark.asyncio
    async def test_改名走乐观并发(self, tmp_path, monkeypatch):
        service, _, route_mcp = _wire(tmp_path, monkeypatch)
        person = await self._person(service, tmp_path)
        post = route_mcp.routes[("POST", "/api/them/people")]

        ok = await post(JsonRequest({
            "person_id": person.id, "names": ["Zoey Chen"], "revision": person.revision
        }))
        assert ok.status_code == 200
        assert json.loads(ok.body)["names"] == ["Zoey Chen"]

        stale = await post(JsonRequest({
            "person_id": person.id, "names": ["别的"], "revision": person.revision
        }))
        assert stale.status_code == 409

    @pytest.mark.asyncio
    async def test_关闭时改不动(self, tmp_path, monkeypatch):
        service, _, route_mcp = _wire(tmp_path, monkeypatch)
        person = await self._person(service, tmp_path)
        state = service.status()
        service.set_enabled(False, expected_revision=state.state_revision)
        resp = await route_mcp.routes[("POST", "/api/them/people")](JsonRequest({
            "person_id": person.id, "names": ["Zoey Chen"], "revision": person.revision
        }))
        assert resp.status_code == 503
        名册 = await route_mcp.routes[("GET", "/api/them/people")](JsonRequest())
        assert json.loads(名册.body)["people"] == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("坏body", [
        {"person_id": "person_" + "0" * 32, "names": ["x"]},
        {"person_id": 1, "names": ["x"], "revision": 1},
        {"person_id": "p", "names": "x", "revision": 1},
        {"person_id": "p", "names": [1], "revision": 1},
        {"person_id": "p", "names": ["x"], "revision": True},
        {"person_id": "p", "names": ["x"], "revision": 1, "多的": 1},
    ])
    async def test_参数格式被守住(self, tmp_path, monkeypatch, 坏body):
        service, _, route_mcp = _wire(tmp_path, monkeypatch)
        await self._person(service, tmp_path)
        resp = await route_mcp.routes[("POST", "/api/them/people")](JsonRequest(坏body))
        assert resp.status_code == 400
