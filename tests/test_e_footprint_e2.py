from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from ombrebrain.eventsourcing.footprint import (
    cli_origin,
    import_origin,
    mcp_origin,
    render_origin_line,
)
from tools import _identity, _runtime as rt
from tools.breath.footprint_projection import (
    append_projection_if_fits,
    projection_line,
)
from tools.breath.handoff import build_handoff
from tools.breath.search import surface_search
from tools.breath.surface import surface_default
from utils import count_tokens_approx


class _Manager:
    def __init__(self, buckets, matches=None):
        self.buckets = list(buckets)
        self.matches = list(self.buckets if matches is None else matches)
        self.touched = []

    async def list_all(self, include_archive=False, fresh=False):
        return list(self.buckets)

    async def search(self, *args, **kwargs):
        return list(self.matches)

    async def get(self, bucket_id):
        return next((b for b in self.buckets if b["id"] == bucket_id), None)

    async def get_stats(self):
        return {"permanent_count": 0, "dynamic_count": len(self.buckets)}

    async def touch_many(self, bucket_ids, ripple=False):
        self.touched.extend(bucket_ids)


class _Embedding:
    enabled = True

    async def search_similar(self, query, top_k=20):
        return []


class _Dehydrator:
    async def dehydrate(self, content, metadata):
        return content


def _bucket(
    bucket_id="mine",
    *,
    owner="cheng",
    origin=None,
    content="memory body",
    **metadata,
):
    tags = [] if owner is None else [f"owner:{owner}"]
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {
            "id": bucket_id,
            "name": bucket_id,
            "tags": tags,
            "type": "dynamic",
            "importance": 5,
            "activation_count": 1,
            "created": "2026-08-27T00:00:00+00:00",
            "footprint_origin": origin or mcp_origin("hold", "cheng"),
            **metadata,
        },
    }


def _install(buckets, *, matches=None, enabled=True):
    manager = _Manager(buckets, matches=matches)
    rt.init(
        config={
            "identity_filter": {
                "enabled": enabled,
                "shared_owner_values": ["shared", "shared_core"],
            },
            "surfacing": {"sampling": {"enabled": False}},
        },
        bucket_mgr=manager,
        dehydrator=_Dehydrator(),
        decay_engine=SimpleNamespace(
            calculate_score=lambda meta: float(meta.get("_score", 5.0))
        ),
        embedding_engine=_Embedding(),
        logger=MagicMock(),
        mark_op=None,
        fire_webhook=None,
    )
    return manager


@pytest.fixture(autouse=True)
def _reset_caller():
    _identity.set_caller("")
    yield
    _identity.set_caller("")


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        (mcp_origin("hold", "cheng"), "👣 来源：经 hold 留下"),
        (mcp_origin("grow", "cheng"), "👣 来源：经 grow 整理留下"),
        (mcp_origin("plan", "cheng"), "👣 来源：经 plan 登记"),
        (import_origin("system", "system"), "👣 来源：经 import 导入"),
        (cli_origin(), "👣 来源：由本地直接写入"),
    ],
)
def test_pure_renderer_exposes_channel_but_never_actor_principal(origin, expected):
    rendered = render_origin_line(origin)
    assert rendered == expected
    assert origin["actor_principal"] not in rendered
    assert "actor_principal" not in rendered


@pytest.mark.parametrize("owner", ["cheng", "shared", "shared_core"])
def test_projection_admits_exact_owner_and_core_shared(owner):
    _install([])
    _identity.set_caller("cheng")
    assert projection_line(_bucket(owner=owner)) == "👣 来源：经 hold 留下"


@pytest.mark.parametrize(
    "bucket",
    [
        _bucket(owner="huaiyin"),
        _bucket(owner=None),
        _bucket(owner="human"),
        _bucket(owner="unknown"),
        _bucket(owner=None, tags=["breath:all"]),
        _bucket(owner="cheng", tags=["owner:cheng", "owner:huaiyin"]),
        _bucket(owner="cheng", tags="owner:cheng"),
    ],
)
def test_projection_denies_foreign_untagged_human_allowlist_and_malformed(bucket):
    _install([])
    _identity.set_caller("cheng")
    assert projection_line(bucket) == ""


def test_projection_requires_caller_and_enabled_identity_filter():
    bucket = _bucket()
    _install([])
    assert projection_line(bucket) == ""
    _identity.set_caller("cheng")
    _install([], enabled=False)
    assert projection_line(bucket) == ""


def test_missing_origin_is_silent_and_invalid_origin_logs_bounded_diagnostic():
    _install([])
    _identity.set_caller("cheng")
    missing = _bucket()
    missing["metadata"].pop("footprint_origin")
    assert projection_line(missing) == ""
    rt.logger.warning.assert_not_called()

    secret = "principal-must-not-enter-log"
    invalid = _bucket()
    invalid["metadata"]["footprint_origin"] = {
        "schema": 99,
        "via": "hold",
        "actor_kind": "mcp_tool",
        "actor_principal": secret,
        "surface": "mcp",
    }
    assert projection_line(invalid) == ""
    rendered_log = repr(rt.logger.warning.call_args)
    assert "invalid_origin" in rendered_log
    assert secret not in rendered_log


def test_token_shortage_omits_whole_line_without_truncation():
    _install([])
    _identity.set_caller("cheng")
    bucket = _bucket()
    full_addition = "\n👣 来源：经 hold 留下"
    cost = count_tokens_approx(full_addition)
    text, used = append_projection_if_fits("base", bucket, cost - 1)
    assert text == "base"
    assert used == 0
    text, used = append_projection_if_fits("base", bucket, cost)
    assert text == "base" + full_addition
    assert used == cost


@pytest.mark.asyncio
async def test_default_pinned_dynamic_passive_and_random_branches_project(monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    main = _bucket("main", _score=10)
    pinned = _bucket("pinned", pinned=True, _score=20)
    passive = _bucket(
        "passive",
        importance=9,
        activation_count=1,
        last_active=old,
        _score=1,
    )
    resolved = _bucket("resolved", resolved=True, _score=0)
    _install([main, pinned, passive, resolved])
    _identity.set_caller("cheng")
    monkeypatch.setattr("tools.breath.surface.random.random", lambda: 0.0)
    monkeypatch.setattr("tools.breath.surface.random.shuffle", lambda values: None)

    output = await surface_default(max_results=1, max_tokens=10000, tag_filter=[])
    assert all(bucket_id in output for bucket_id in ("pinned", "main", "passive", "resolved"))
    assert output.count("👣 来源：经 hold 留下") == 4
    assert "actor_principal" not in output


@pytest.mark.asyncio
async def test_query_preserves_foreign_attribution_and_never_projects_foreign_or_letter(
    monkeypatch,
):
    mine = _bucket("mine", content="mine body")
    foreign = _bucket(
        "foreign",
        owner="huaiyin",
        origin=mcp_origin("grow", "huaiyin"),
        content="foreign body",
    )
    letter = _bucket(
        "letter",
        content="LOCKED SECRET",
        type="letter",
        tags=["owner:cheng", "__letter__"],
    )
    _install([mine, foreign, letter], matches=[mine, foreign, letter])
    _identity.set_caller("cheng")
    monkeypatch.setattr("tools.breath.search.random.random", lambda: 0.99)

    output = await surface_search("body", 10, 10000, "", -1, -1, [])
    assert "mine body" in output
    assert "foreign body" in output
    assert "[owner:huaiyin] [非本线记忆]" in output
    assert output.count("👣 来源：") == 1
    assert "经 grow" not in output
    assert "LOCKED SECRET" not in output
    assert "actor_principal" not in output


@pytest.mark.asyncio
async def test_query_random_drift_uses_same_strict_projection(monkeypatch):
    drift = _bucket("drift", content="drift body", _score=0.1)
    _install([drift], matches=[])
    _identity.set_caller("cheng")
    monkeypatch.setattr("tools.breath.search.random.random", lambda: 0.0)
    monkeypatch.setattr("tools.breath.search.random.randint", lambda a, b: 1)
    monkeypatch.setattr("tools.breath.search.random.sample", lambda values, count: values[:count])

    output = await surface_search("none", 10, 10000, "", -1, -1, [])
    assert "[surface_type: random]" in output
    assert "drift body" in output
    assert "👣 来源：经 hold 留下" in output


def test_handoff_output_is_byte_for_byte_unchanged_by_origin_metadata():
    bucket = _bucket("handoff", content="continuity")
    without = deepcopy(bucket)
    without["metadata"].pop("footprint_origin")
    with_rendered = build_handoff([bucket], "cheng")["rendered"]
    without_rendered = build_handoff([without], "cheng")["rendered"]
    assert with_rendered == without_rendered
    assert "👣 来源" not in with_rendered


def test_projection_is_wired_only_to_ordinary_surface_and_search():
    breath_dir = Path(__file__).resolve().parents[1] / "src" / "tools" / "breath"
    consumers = {
        path.name
        for path in breath_dir.glob("*.py")
        if "from .footprint_projection import" in path.read_text(encoding="utf-8")
    }
    assert consumers == {"surface.py", "search.py"}
