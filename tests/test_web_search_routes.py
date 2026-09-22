import json

import pytest
from starlette.responses import JSONResponse

import web.search as web_search


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

    def __init__(self, **query_params):
        self.query_params = query_params


def _bucket(bucket_id, *, content="", **metadata):
    return {"id": bucket_id, "content": content, "metadata": metadata}


def _payload(response):
    return json.loads(response.body.decode("utf-8"))


def _routes(monkeypatch, bucket_mgr, *, embedding_engine=None, decay_engine=None):
    monkeypatch.setattr(web_search.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(web_search.sh, "bucket_mgr", bucket_mgr, raising=False)
    monkeypatch.setattr(
        web_search.sh,
        "embedding_engine",
        embedding_engine,
        raising=False,
    )
    if decay_engine is not None:
        monkeypatch.setattr(web_search.sh, "decay_engine", decay_engine, raising=False)
    mcp = FakeMCP()
    web_search.register(mcp)
    return mcp.routes


def test_unit_query_float_rejects_non_numeric_object():
    with pytest.raises(ValueError, match="valence must be a finite number"):
        web_search._unit_query_float(object(), "valence")


@pytest.mark.asyncio
async def test_duplicates_deduplicates_pairs_sorts_scores_and_hides_locked_letters(
    monkeypatch,
):
    locked = {
        "lock_type": "permanent",
        "unlock_date": "9999-12-31T23:59:59Z",
        "locked_by_principal": "ai",
    }

    class Manager:
        async def list_all(self, include_archive=False):
            assert include_archive is False
            return [
                _bucket("a", name="A", dup_candidate="b", dup_score=0.96),
                _bucket("b", name="B", dup_candidate="a", dup_score=0.97),
                _bucket("c", name="C", dup_candidate="a", dup_score=0.99),
                _bucket("secret", name="Secret", dup_candidate="a", **locked),
            ]

    routes = _routes(monkeypatch, Manager())
    response = await routes[("GET", "/api/duplicates")](FakeRequest())

    assert response.status_code == 200
    payload = _payload(response)
    assert payload["total"] == 2
    assert [(pair["a"]["id"], pair["b"]["id"]) for pair in payload["pairs"]] == [
        ("c", "a"),
        ("a", "b"),
    ]
    assert payload["pairs"][1]["score"] == 0.96


@pytest.mark.asyncio
async def test_concept_network_normalizes_tokens_counts_cooccurrence_and_marks_anchors(
    monkeypatch,
):
    class Manager:
        async def list_all(self, include_archive=False):
            return [
                _bucket(
                    "a",
                    content="[[Memory]] [[memory]] [[Focus]]",
                    tags=["memory", "#Work"],
                    anchor=True,
                ),
                _bucket("b", content="[[Focus]]", tags="work, #Deep"),
            ]

    routes = _routes(monkeypatch, Manager())
    response = await routes[("GET", "/api/network")](FakeRequest(mode="wikilinks"))

    payload = _payload(response)
    assert payload["mode"] == "concept"
    nodes = {node["id"]: node for node in payload["nodes"]}
    assert nodes["memory"] == {
        "id": "memory",
        "label": "Memory",
        "kind": "mixed",
        "freq": 1,
        "buckets": ["a"],
        "anchor": True,
    }
    assert nodes["focus"]["freq"] == 2
    assert nodes["focus"]["anchor"] is True
    assert nodes["deep"]["anchor"] is False
    edges = {(edge["source"], edge["target"]): edge["weight"] for edge in payload["edges"]}
    assert edges[("focus", "work")] == 2


@pytest.mark.asyncio
async def test_embedding_network_only_emits_edges_above_similarity_threshold(monkeypatch):
    class Manager:
        async def list_all(self, include_archive=False):
            return [_bucket("a", importance=8), _bucket("b", importance=5)]

    class EmbeddingEngine:
        enabled = True

        async def get_embedding(self, bucket_id):
            return {"a": [1.0, 0.0], "b": [0.8, 0.6]}[bucket_id]

        def _cosine_similarity(self, left, right):
            assert left == [1.0, 0.0]
            assert right == [0.8, 0.6]
            return 0.8

    class DecayEngine:
        def calculate_score(self, metadata):
            return metadata["importance"]

    routes = _routes(
        monkeypatch,
        Manager(),
        embedding_engine=EmbeddingEngine(),
        decay_engine=DecayEngine(),
    )
    response = await routes[("GET", "/api/network")](FakeRequest(mode="embedding"))

    payload = _payload(response)
    assert payload["mode"] == "embedding"
    assert [node["id"] for node in payload["nodes"]] == ["a", "b"]
    assert payload["edges"] == [
        {"source": "a", "target": "b", "weight": 0.8, "kind": "similarity"}
    ]


@pytest.mark.asyncio
async def test_breath_rejects_non_integer_count(monkeypatch):
    routes = _routes(monkeypatch, object())

    response = await routes[("GET", "/api/breath")](FakeRequest(n="many"))

    assert response.status_code == 400
    assert _payload(response) == {"error": "n must be an integer in [1,50]"}


@pytest.mark.asyncio
async def test_breath_ranks_visible_buckets_and_applies_resolved_penalty(monkeypatch):
    class Manager:
        async def list_all(self, include_archive=False):
            return [
                _bucket("active", name="Active", importance=5),
                _bucket("resolved", name="Resolved", importance=10, resolved=True),
                _bucket("hidden", importance=20, dont_surface=True),
            ]

    class DecayEngine:
        def calculate_score(self, metadata):
            return metadata["importance"]

    routes = _routes(monkeypatch, Manager(), decay_engine=DecayEngine())
    response = await routes[("GET", "/api/breath")](FakeRequest(n="99"))

    assert _payload(response) == {
        "buckets": [
            {
                "id": "active",
                "name": "Active",
                "score": 5.0,
                "domain": [],
                "type": "dynamic",
            },
            {
                "id": "resolved",
                "name": "Resolved",
                "score": 3.0,
                "domain": [],
                "type": "dynamic",
            },
        ]
    }


@pytest.mark.asyncio
async def test_breath_debug_reports_breakdown_and_skips_one_bad_bucket(monkeypatch):
    class Manager:
        w_topic = 0.4
        w_emotion = 0.2
        w_time = 0.2
        w_importance = 0.2
        fuzzy_threshold = 50

        async def list_all(self, include_archive=False):
            return [
                _bucket("active", name="Active", importance=8),
                _bucket("resolved", importance=8, resolved=True),
                _bucket("bad", importance=8),
            ]

        def _calc_topic_score(self, query, bucket):
            if bucket["id"] == "bad":
                raise RuntimeError("unscorable")
            assert query == "topic"
            return 1.0

        def _calc_emotion_score(self, valence, arousal, metadata):
            assert (valence, arousal) == (0.25, 0.75)
            return 0.5

        def _calc_time_score(self, metadata):
            return 0.25

    routes = _routes(monkeypatch, Manager())
    response = await routes[("GET", "/api/breath-debug")](
        FakeRequest(q="topic", valence="0.25", arousal="0.75")
    )

    payload = _payload(response)
    assert payload["total_candidates"] == 2
    assert payload["passed_count"] == 1
    assert payload["results"][0]["id"] == "active"
    assert payload["results"][0]["normalized"] == 71.0
    assert payload["results"][0]["passed_threshold"] is True
    assert payload["results"][1]["id"] == "resolved"
    assert payload["results"][1]["normalized"] == 21.3
    assert payload["results"][1]["passed_threshold"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({}, "missing q parameter"),
        ({"q": "topic", "valence": "nan"}, "valence must be a finite number in [0,1]"),
    ],
)
async def test_search_routes_reject_invalid_query_parameters(monkeypatch, params, message):
    routes = _routes(monkeypatch, object())
    path = "/api/search" if not params else "/api/breath-debug"

    response = await routes[("GET", path)](FakeRequest(**params))

    assert response.status_code == 400
    assert _payload(response) == {"error": message}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/search", "/api/breath-debug"])
async def test_query_size_errors_are_returned_before_storage_access(monkeypatch, path):
    routes = _routes(monkeypatch, object())
    monkeypatch.setattr(web_search, "check_query_size", lambda _query: "query too large")

    response = await routes[("GET", path)](FakeRequest(q="oversized"))

    assert response.status_code == 400
    assert _payload(response) == {"error": "query too large"}


@pytest.mark.asyncio
async def test_search_passes_unlocked_allowlist_and_caps_results_at_ten(monkeypatch):
    visible = [
        _bucket(
            f"b{i}",
            content=f"[[topic]] result {i}",
            name=f"Bucket {i}",
            importance=5,
        )
        for i in range(12)
    ]
    locked = _bucket(
        "secret",
        content="must stay hidden",
        lock_type="permanent",
        unlock_date="9999-12-31T23:59:59Z",
        locked_by_principal="ai",
    )

    class Manager:
        async def list_all(self, include_archive=False):
            return [*visible, locked]

        async def search(self, query, *, limit, vector_scores, allowed_bucket_ids):
            assert query == "topic"
            assert limit == 50
            assert vector_scores == {"b0": 0.9}
            assert allowed_bucket_ids == {bucket["id"] for bucket in visible}
            return [*visible, locked]

    class EmbeddingEngine:
        enabled = True

        async def search_similar_strict(self, query, *, top_k, allowed_bucket_ids):
            assert query == "topic"
            assert top_k == 50
            assert allowed_bucket_ids == {bucket["id"] for bucket in visible}
            return [("b0", 0.9)]

    routes = _routes(monkeypatch, Manager(), embedding_engine=EmbeddingEngine())
    response = await routes[("GET", "/api/search")](FakeRequest(q="topic"))

    payload = _payload(response)
    assert response.headers["x-semantic-search"] == "ok"
    assert len(payload) == 10
    assert [item["id"] for item in payload] == [f"b{i}" for i in range(10)]
    assert payload[0]["content_preview"] == "topic result 0"
    assert "secret" not in {item["id"] for item in payload}


@pytest.mark.asyncio
async def test_routes_filter_locked_buckets_even_when_manager_returns_them(monkeypatch):
    locked = _bucket(
        "secret",
        content="must stay hidden",
        lock_type="permanent",
        unlock_date="9999-12-31T23:59:59Z",
        locked_by_principal="ai",
        dup_candidate="missing",
    )
    visible = _bucket("visible", content="visible", importance=5)

    class Manager:
        w_topic = 0.4
        w_emotion = 0.2
        w_time = 0.2
        w_importance = 0.2
        fuzzy_threshold = 50

        async def list_all(self, include_archive=False):
            return [locked, visible]

        async def search(self, query, *, limit, vector_scores, allowed_bucket_ids):
            return [locked, visible]

        def _calc_topic_score(self, query, bucket):
            return 1.0

        def _calc_emotion_score(self, valence, arousal, metadata):
            return 0.5

        def _calc_time_score(self, metadata):
            return 0.5

    class DecayEngine:
        def calculate_score(self, metadata):
            return metadata.get("importance", 0)

    routes = _routes(monkeypatch, Manager(), decay_engine=DecayEngine())

    search = _payload(await routes[("GET", "/api/search")](FakeRequest(q="visible")))
    duplicates = _payload(await routes[("GET", "/api/duplicates")](FakeRequest()))
    breath = _payload(await routes[("GET", "/api/breath")](FakeRequest()))
    debug = _payload(await routes[("GET", "/api/breath-debug")](FakeRequest()))

    assert [item["id"] for item in search] == ["visible"]
    assert duplicates == {"pairs": [], "total": 0}
    assert [item["id"] for item in breath["buckets"]] == ["visible"]
    assert [item["id"] for item in debug["results"]] == ["visible"]


@pytest.mark.asyncio
async def test_concept_network_ignores_empty_tokens_and_upgrades_tag_to_mixed(monkeypatch):
    class Manager:
        async def list_all(self, include_archive=False):
            return [
                _bucket("tag-first", tags=["shared", "#"]),
                _bucket("wiki-second", content="[[shared]] [[]]"),
            ]

    routes = _routes(monkeypatch, Manager())
    response = await routes[("GET", "/api/network")](FakeRequest())

    assert _payload(response)["nodes"] == [
        {
            "id": "shared",
            "label": "shared",
            "kind": "mixed",
            "freq": 2,
            "buckets": ["tag-first", "wiki-second"],
            "anchor": False,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed_bucket_ids", [None, {"visible"}])
async def test_semantic_dashboard_falls_back_to_legacy_engine_api(
    monkeypatch,
    allowed_bucket_ids,
):
    class LegacyEngine:
        enabled = True

        def __init__(self):
            self.kwargs = None

        async def search_similar(self, query, **kwargs):
            assert query == "memory"
            self.kwargs = kwargs
            return [("visible", "0.875")]

    engine = LegacyEngine()
    monkeypatch.setattr(web_search.sh, "embedding_engine", engine, raising=False)

    scores, notice = await web_search._semantic_scores_for_dashboard(
        "memory",
        12,
        allowed_bucket_ids=allowed_bucket_ids,
    )

    expected = {"top_k": 12}
    if allowed_bucket_ids is not None:
        expected["allowed_bucket_ids"] = allowed_bucket_ids
    assert engine.kwargs == expected
    assert scores == {"visible": 0.875}
    assert notice == ""


@pytest.mark.asyncio
async def test_semantic_dashboard_reports_provider_failure(monkeypatch):
    class BrokenEngine:
        enabled = True

        async def search_similar(self, query, **kwargs):
            raise ConnectionError("provider offline")

    monkeypatch.setattr(web_search.sh, "embedding_engine", BrokenEngine(), raising=False)

    scores, notice = await web_search._semantic_scores_for_dashboard(
        "memory",
        12,
        allowed_bucket_ids=None,
    )

    assert scores == {}
    assert notice == web_search._SEMANTIC_DISABLED_NOTE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/api/search", {"q": "memory"}),
        ("/api/duplicates", {}),
        ("/api/network", {}),
        ("/api/breath", {}),
        ("/api/breath-debug", {}),
    ],
)
async def test_search_routes_short_circuit_on_auth_failure(monkeypatch, path, params):
    denied = JSONResponse({"error": "denied"}, status_code=401)
    monkeypatch.setattr(web_search.sh, "_require_auth", lambda _request: denied)
    mcp = FakeMCP()
    web_search.register(mcp)

    response = await mcp.routes[("GET", path)](FakeRequest(**params))

    assert response is denied


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/api/search", {"q": "memory"}),
        ("/api/duplicates", {}),
        ("/api/network", {}),
        ("/api/breath", {}),
        ("/api/breath-debug", {}),
    ],
)
async def test_search_routes_return_json_500_when_bucket_listing_fails(
    monkeypatch,
    path,
    params,
):
    class BrokenManager:
        w_topic = 0.4
        w_emotion = 0.2
        w_time = 0.2
        w_importance = 0.2
        fuzzy_threshold = 50

        async def list_all(self, include_archive=False):
            raise OSError("storage offline")

    routes = _routes(monkeypatch, BrokenManager())

    response = await routes[("GET", path)](FakeRequest(**params))

    assert response.status_code == 500
    assert _payload(response) == {"error": "storage offline"}
