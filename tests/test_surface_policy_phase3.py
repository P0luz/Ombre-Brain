import json

import pytest

from ombrebrain.policy.surfacing import SurfacePolicyVM
import web.search as web_search


def _bucket(**metadata):
    base = {"type": "dynamic", "importance": 5}
    base.update(metadata)
    return {"id": metadata.get("id", "bucket-1"), "content": "hello", "metadata": base}


@pytest.mark.parametrize(
    ("metadata", "reason"),
    [
        ({"dont_surface": True}, "dont_surface"),
        ({"anchor": True}, "anchor"),
        ({"protected": True}, "protected"),
        ({"type": "feel"}, "private_type"),
        ({"type": "plan"}, "private_type"),
        ({"type": "letter"}, "private_type"),
        ({"type": "self"}, "private_type"),
        ({"type": "i"}, "private_type"),
        ({"type": "archived"}, "archived"),
        ({"deleted_at": "2026-07-02T00:00:00+00:00"}, "deleted"),
        ({"type": "tombstone"}, "tombstone"),
        ({"tombstone": True}, "tombstone"),
    ],
)
def test_spontaneous_policy_denies_non_surfaceable_memory(metadata, reason):
    vm = SurfacePolicyVM.default()

    decision = vm.evaluate_bucket(_bucket(**metadata), mode="spontaneous")

    assert not decision.allowed
    assert reason in decision.reasons


def test_search_policy_keeps_dont_surface_reachable_by_explicit_query():
    vm = SurfacePolicyVM.default()

    decision = vm.evaluate_bucket(_bucket(dont_surface=True), mode="search")

    assert decision.allowed
    assert decision.reasons == ()


@pytest.mark.parametrize("digested", [True, "true"])
@pytest.mark.parametrize(
    ("mode", "allowed"),
    [
        ("spontaneous", False),
        ("dream", False),
        ("search", True),
        ("importance", True),
    ],
)
def test_digested_visibility_depends_on_explicit_or_passive_mode(
    digested,
    mode,
    allowed,
):
    decision = SurfacePolicyVM.default().evaluate_bucket(
        _bucket(digested=digested),
        mode=mode,
    )

    assert decision.allowed is allowed
    if allowed:
        assert "digested" not in decision.reasons
    else:
        assert "digested" in decision.reasons


@pytest.mark.parametrize("protected", [True, "true"])
@pytest.mark.parametrize(
    ("mode", "allowed"),
    [
        ("spontaneous", False),
        ("dream", False),
        ("search", True),
        ("importance", True),
    ],
)
def test_protected_visibility_depends_on_explicit_or_passive_mode(
    protected,
    mode,
    allowed,
):
    decision = SurfacePolicyVM.default().evaluate_bucket(
        _bucket(protected=protected),
        mode=mode,
    )

    assert decision.allowed is allowed
    if allowed:
        assert "protected" not in decision.reasons
    else:
        assert decision.reasons == ("protected",)


@pytest.mark.parametrize("mode", ["spontaneous", "search", "importance"])
@pytest.mark.parametrize(
    ("metadata", "reason"),
    [
        ({"type": "archived"}, "archived"),
        ({"deleted_at": "2026-07-02T00:00:00+00:00"}, "deleted"),
        ({"type": "tombstone"}, "tombstone"),
    ],
)
def test_terminal_memory_states_are_denied_in_every_read_mode(mode, metadata, reason):
    vm = SurfacePolicyVM.default()

    decision = vm.evaluate_bucket(_bucket(**metadata), mode=mode)

    assert not decision.allowed
    assert reason in decision.reasons


def test_filter_buckets_returns_only_allowed_items():
    vm = SurfacePolicyVM.default()
    visible = _bucket(id="visible")
    hidden = _bucket(id="hidden", dont_surface=True)
    archived = _bucket(id="archived", type="archived")

    filtered = vm.filter_buckets([visible, hidden, archived], mode="spontaneous")

    assert [bucket["id"] for bucket in filtered] == ["visible"]


class FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(handler):
            for method in methods:
                self.routes[(method, path)] = handler
            return handler

        return decorator


class FakeRequest:
    headers = {}
    path_params = {}
    query_params = {"n": "10"}


class FakeDecayEngine:
    def calculate_score(self, metadata):
        return float(metadata.get("importance") or 0)


class FakeBucketManager:
    async def list_all(self, include_archive=False):
        return [
            _bucket(id="visible", importance=8),
            _bucket(id="hidden", importance=10, dont_surface=True),
            _bucket(id="digested", importance=10, digested=True),
            _bucket(id="protected", importance=10, protected="true"),
            _bucket(id="archived", importance=10, type="archived"),
        ]


class FakeSearchRequest:
    headers = {}
    path_params = {}
    query_params = {"q": "memory"}


class FakeSearchBucketManager:
    async def search(self, query, limit=10, vector_scores=None):
        assert query == "memory"
        return [
            _bucket(id="visible", name="Visible", importance=8),
            _bucket(id="hidden", name="Hidden", importance=10, dont_surface=True),
            _bucket(id="digested", name="Digested", importance=10, digested=True),
            _bucket(id="deleted", name="Deleted", deleted_at="2026-07-03T00:00:00+00:00"),
            _bucket(id="tombstone", name="Tombstone", tombstone=True),
            _bucket(id="archived", name="Archived", type="archived"),
        ]


@pytest.mark.asyncio
async def test_dashboard_breath_filters_with_spontaneous_policy(monkeypatch):
    monkeypatch.setattr(web_search.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(web_search.sh, "bucket_mgr", FakeBucketManager(), raising=False)
    monkeypatch.setattr(web_search.sh, "decay_engine", FakeDecayEngine(), raising=False)

    mcp = FakeMCP()
    web_search.register(mcp)

    response = await mcp.routes[("GET", "/api/breath")](FakeRequest())
    payload = json.loads(response.body.decode("utf-8"))

    assert [bucket["id"] for bucket in payload["buckets"]] == ["visible"]


@pytest.mark.asyncio
async def test_dashboard_search_filters_terminal_states_but_keeps_dont_surface(monkeypatch):
    monkeypatch.setattr(web_search.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(web_search.sh, "bucket_mgr", FakeSearchBucketManager(), raising=False)

    mcp = FakeMCP()
    web_search.register(mcp)

    response = await mcp.routes[("GET", "/api/search")](FakeSearchRequest())
    payload = json.loads(response.body.decode("utf-8"))

    assert [bucket["id"] for bucket in payload] == ["visible", "hidden", "digested"]
    # 响应体形状不变（前端依赖 Array.isArray），语义检索状态走响应头。
    assert isinstance(payload, list)


@pytest.mark.asyncio
async def test_dashboard_search_reports_semantic_degraded_via_header_when_provider_off(monkeypatch):
    """回归锁死找茬会话发现的 bug：语义服务不可用时 /api/search 原来完全

    静默降级——bucket_mgr.search() 内部自己吞掉 embedding 异常，调用方拿到
    的响应体和「语义检索正常」时长得一模一样，没有任何信号。"""
    monkeypatch.setattr(web_search.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(web_search.sh, "bucket_mgr", FakeSearchBucketManager(), raising=False)
    monkeypatch.setattr(web_search.sh, "embedding_engine", None, raising=False)

    mcp = FakeMCP()
    web_search.register(mcp)

    response = await mcp.routes[("GET", "/api/search")](FakeSearchRequest())

    assert response.headers.get("x-semantic-search") == "degraded"


@pytest.mark.asyncio
async def test_dashboard_search_reports_semantic_ok_when_provider_available(monkeypatch):
    class FakeEmbeddingEngine:
        enabled = True

        async def search_similar_strict(self, query, top_k=20):
            return [("visible", 0.9)]

    monkeypatch.setattr(web_search.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(web_search.sh, "bucket_mgr", FakeSearchBucketManager(), raising=False)
    monkeypatch.setattr(web_search.sh, "embedding_engine", FakeEmbeddingEngine(), raising=False)

    mcp = FakeMCP()
    web_search.register(mcp)

    response = await mcp.routes[("GET", "/api/search")](FakeSearchRequest())

    assert response.headers.get("x-semantic-search") == "ok"
