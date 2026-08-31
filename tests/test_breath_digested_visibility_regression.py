import asyncio
from datetime import datetime
from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from tools.breath.catalog import surface_catalog
from tools.breath.importance import surface_by_importance
from tools.breath.search import surface_search
from tools.breath.surface import surface_default
from tools.dream.candidates import collect_candidates, collect_core_context


class DisabledEmbedding:
    enabled = False

    async def search_similar(self, query, top_k=20):
        return []


class FlatDecay:
    def calculate_score(self, metadata):
        return float(metadata.get("importance") or 1)


def _install_runtime(bucket_mgr, decay_engine):
    rt.config = {"surfacing": {}}
    rt.bucket_mgr = bucket_mgr
    rt.decay_engine = decay_engine
    rt.embedding_engine = DisabledEmbedding()
    rt.logger = MagicMock()
    rt.fire_webhook = None
    rt.mark_op = None


@pytest.mark.asyncio
async def test_default_breath_hides_all_digested_surface_classes(
    bucket_mgr,
    decay_eng,
    monkeypatch,
):
    visible_id = await bucket_mgr.create(
        content="Visible undigested control memory.",
        importance=9,
    )
    ordinary_id = await bucket_mgr.create(
        content="Digested ordinary memory must stay hidden.",
        importance=10,
    )
    # 3.2.0 起：核心准则不可被消化。这条从"必须隐藏"翻成"必须在场"。
    pinned_id = await bucket_mgr.create(
        content="Digested pinned memory must stay visible.",
        pinned=True,
    )
    resolved_id = await bucket_mgr.create(
        content="Digested resolved memory must not return through encounter.",
        importance=10,
    )
    await bucket_mgr.update(ordinary_id, digested=True)
    await bucket_mgr.update(pinned_id, digested=True)
    await bucket_mgr.update(resolved_id, resolved=True, digested=True)
    _install_runtime(bucket_mgr, decay_eng)

    # Force the 3% resolved-memory encounter branch to prove that its pool is
    # filtered too. A visible control prevents the early empty-pool return.
    monkeypatch.setattr("tools.breath.surface.random.random", lambda: 0.0)

    result = await surface_default(
        max_results=20,
        max_tokens=20_000,
        tag_filter=[],
    )

    assert visible_id in result
    assert ordinary_id not in result
    assert resolved_id not in result
    assert "Digested ordinary memory" not in result
    assert "Digested resolved memory" not in result

    # ⚠️ 3.2.0 有意推翻 2.8.4 的这一条：pinned 桶带着 digested 时仍然浮现。
    #
    # 2.8.4 的立场是 digested 属于用户显式指令（trace(digested=1) 得手动调），
    # 该压过 pinned。3.2.0 的立场是：pinned 是核心准则、始终在场才是它存在的
    # 意义——用 digested 让一条核心准则闭嘴，其实是在用一个标记掩盖另一个标记
    # 的错误。不想让它一直在场，那它本来就不该是核心准则。
    #
    # 代价说清楚：让核心准则安静下来的唯一办法变成 trace(bucket_id, pinned=0)。
    # 触发这次反转的真实场景：12 条核心准则里 2 条带 digested，breath() 只回
    # 10 条，旁边还有普通桶——看起来像被挤掉，实际是压根没进候选。
    assert pinned_id in result
    assert "Digested pinned memory" in result


@pytest.mark.asyncio
async def test_explicit_query_still_returns_digested_memory(
    bucket_mgr,
    decay_eng,
):
    marker = "DIGESTED-EXPLICIT-QUERY-7F91"
    bucket_id = await bucket_mgr.create(
        content=f"{marker} remains explicitly searchable.",
        importance=8,
    )
    await bucket_mgr.update(bucket_id, digested=True)
    _install_runtime(bucket_mgr, decay_eng)

    result = await surface_search(
        query=marker,
        max_results=1,
        max_tokens=10_000,
        domain="",
        valence=-1,
        arousal=-1,
        tag_filter=[],
    )
    await asyncio.sleep(0)

    assert bucket_id in result
    assert marker in result


class DriftBucketManager:
    def __init__(self):
        self.buckets = [
            {
                "id": "visible-drift",
                "content": "Visible spontaneous drift control.",
                "metadata": {"type": "dynamic", "importance": 1, "domain": []},
            },
            {
                "id": "digested-drift",
                "content": "Digested memory must not drift into a query.",
                "metadata": {
                    "type": "dynamic",
                    "importance": 1,
                    "domain": [],
                    "digested": True,
                },
            },
        ]

    async def search(self, query, **_kwargs):
        return []

    async def list_all(self, include_archive=False):
        return list(self.buckets)

    async def touch_many(self, bucket_ids, ripple=False):
        return None


@pytest.mark.asyncio
async def test_query_non_match_drift_uses_spontaneous_visibility_policy(monkeypatch):
    manager = DriftBucketManager()
    _install_runtime(manager, FlatDecay())
    monkeypatch.setattr(
        "tools.breath.search.random.sample",
        lambda population, count: list(population)[:count],
    )
    monkeypatch.setattr("tools.breath.search.random.randint", lambda _a, _b: 5)

    result = await surface_search(
        query="no stored memory matches this query",
        max_results=5,
        max_tokens=10_000,
        domain="",
        valence=-1,
        arousal=-1,
        tag_filter=[],
    )

    assert "=== 忽然想起来（非检索命中） ===" in result
    assert "visible-drift" in result
    assert "Visible spontaneous drift control" in result
    assert "digested-drift" not in result
    assert "Digested memory must not drift" not in result


def test_dream_candidate_and_core_pools_hide_digested_memory():
    now = datetime.now().isoformat(timespec="seconds")
    visible_recent = {
        "id": "visible-recent",
        "content": "visible recent",
        "metadata": {
            "type": "dynamic",
            "importance": 5,
            "created": now,
        },
    }
    digested_recent = {
        "id": "digested-recent",
        "content": "digested recent",
        "metadata": {
            "type": "dynamic",
            "importance": 10,
            "created": now,
            "digested": True,
        },
    }
    visible_core = {
        "id": "visible-core",
        "content": "visible core",
        "metadata": {
            "type": "permanent",
            "importance": 10,
            "created": now,
        },
    }
    digested_core = {
        "id": "digested-core",
        "content": "digested core",
        "metadata": {
            "type": "permanent",
            "importance": 10,
            "created": now,
            "digested": True,
        },
    }
    buckets = [visible_recent, digested_recent, visible_core, digested_core]

    # 普通记忆的 digested 依然生效：dream 的候选池不该被消化过的内容占位。
    assert [row["id"] for row in collect_candidates(buckets, 48)] == [
        "visible-recent"
    ]
    # 但核心准则池不同——3.2.0 起 permanent 不因 digested 消失。
    # dream 是回顾，回顾时看不到自己的核心准则，那这场回顾就没有坐标系。
    assert sorted(row["id"] for row in collect_core_context(buckets)) == [
        "digested-core",
        "visible-core",
    ]


@pytest.mark.asyncio
async def test_explicit_importance_audit_and_catalog_keep_digested_discoverable(
    bucket_mgr,
    decay_eng,
):
    bucket_id = await bucket_mgr.create(
        content="Digested audit body remains explicitly inspectable.",
        name="Digested audit marker",
        importance=9,
    )
    await bucket_mgr.update(bucket_id, digested=True)
    _install_runtime(bucket_mgr, decay_eng)

    importance_result = await surface_by_importance(9, 10_000, [])
    catalog_result = await surface_catalog(max_results=20)

    assert bucket_id in importance_result
    assert "Digested audit body" in importance_result
    assert "Digested audit marker" in catalog_result
