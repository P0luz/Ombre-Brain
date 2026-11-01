from __future__ import annotations

import pytest

from tools.breath._verbatim import render_stored_bucket


def _core(bucket_id, content):
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {"type": "permanent", "importance": 10, "pinned": True, "domain": []},
    }


def _ordinary(bucket_id, content, importance=10):
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {
            "type": "dynamic",
            "importance": importance,
            "activation_count": 1,
            "domain": [],
        },
    }


def _cost(bucket, header):
    return render_stored_bucket(bucket, header, "👣 Footprint：暂时无法读取")[1]


@pytest.mark.asyncio
async def test_ordinary_memories_still_surface_when_a_core_rule_is_too_big(monkeypatch):
    from tests.test_breath_verbatim_patch import OrderedBucketManager, _install_runtime
    from tools.breath.surface import surface_default

    fits = _core("fits", "Core rule that fits.")
    huge = _core("huge", "Oversized core rule " * 400)
    ordinary = _ordinary("ordinary", "An ordinary memory that fits the leftovers.")

    _install_runtime(OrderedBucketManager([fits, huge, ordinary]))
    monkeypatch.setattr("tools.breath.surface.random.random", lambda: 1.0)

    budget = _cost(fits, "📌 [核心准则] [bucket_id:fits]") + _cost(
        ordinary, "[权重:10.00] [bucket_id:ordinary]"
    )
    output = await surface_default(max_results=1, max_tokens=budget, tag_filter=[])

    assert "[bucket_id:fits]" in output
    assert "[bucket_id:ordinary]" in output
    assert "[bucket_id:huge]" not in output
    assert "token 预算不足" in output


@pytest.mark.asyncio
async def test_the_omitted_core_rule_is_still_reported(monkeypatch):
    from tests.test_breath_verbatim_patch import OrderedBucketManager, _install_runtime
    from tools.breath.surface import surface_default

    fits = _core("fits", "Core rule that fits.")
    huge = _core("huge", "Oversized core rule " * 400)
    ordinary = _ordinary("ordinary", "An ordinary memory that fits the leftovers.")

    _install_runtime(OrderedBucketManager([fits, huge, ordinary]))
    monkeypatch.setattr("tools.breath.surface.random.random", lambda: 1.0)

    budget = _cost(fits, "📌 [核心准则] [bucket_id:fits]") + _cost(
        ordinary, "[权重:10.00] [bucket_id:ordinary]"
    )
    output = await surface_default(max_results=1, max_tokens=budget, tag_filter=[])

    assert "omitted=1" in output
    assert "surfacing.breath_max_tokens" in output
    assert "普通浮现已跳过" not in output


@pytest.mark.asyncio
async def test_core_rules_still_get_the_budget_first(monkeypatch):
    from tests.test_breath_verbatim_patch import OrderedBucketManager, _install_runtime
    from tools.breath.surface import surface_default

    core = _core("core", "Core rule wins the budget.")
    ordinary = _ordinary("ordinary", "Ordinary memory must not crowd it out.")

    _install_runtime(OrderedBucketManager([core, ordinary]))
    monkeypatch.setattr("tools.breath.surface.random.random", lambda: 1.0)

    only_one_fits = _cost(core, "📌 [核心准则] [bucket_id:core]")
    output = await surface_default(max_results=5, max_tokens=only_one_fits, tag_filter=[])

    assert "[bucket_id:core]" in output
    assert "[bucket_id:ordinary]" not in output


# ---- Dashboard 诊断 ----

class _FakeManager:
    def __init__(self, buckets):
        self._buckets = buckets

    async def list_all(self, include_archive=False):
        return list(self._buckets)


async def _report(monkeypatch, buckets, limit):
    import web._shared as sh
    import web.system as system

    monkeypatch.setattr(sh, "bucket_mgr", _FakeManager(buckets), raising=False)
    monkeypatch.setattr(sh, "config", {"surfacing": {"breath_max_tokens": limit}}, raising=False)
    return await system._pinned_budget_report()


@pytest.mark.asyncio
async def test_report_counts_only_pinned_buckets(monkeypatch):
    report = await _report(
        monkeypatch,
        [_core("a", "rule"), _core("b", "rule"), _ordinary("c", "not pinned")],
        10000,
    )
    assert report["pinned_count"] == 2
    assert report["limit_tokens"] == 10000
    assert report["required_tokens"] > 0


@pytest.mark.asyncio
async def test_report_is_zero_without_pinned_buckets(monkeypatch):
    report = await _report(monkeypatch, [_ordinary("c", "not pinned")], 10000)
    assert report["pinned_count"] == 0
    assert report["required_tokens"] == 0


@pytest.mark.asyncio
async def test_report_notices_when_pins_exceed_the_budget(monkeypatch):
    report = await _report(monkeypatch, [_core("a", "rule " * 5000)], 1000)
    assert report["required_tokens"] > report["limit_tokens"]
    assert report["largest_entry_tokens"] > report["limit_tokens"]


# ---- 默认预算必须装得下钉满的核心准则 ----

_DEFAULT_BREATH_TOKENS = 20000


def _fallbacks():
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    found = {}
    for rel in (
        "src/tools/breath/__init__.py",
        "src/web/config_api.py",
        "src/web/system.py",
        "frontend/dashboard.html",
    ):
        text = (root / rel).read_text(encoding="utf-8")
        found[rel] = {
            int(n)
            for n in re.findall(r"breath_max_tokens[^\n]*?\|\|\s*(\d+)", text)
            + re.findall(r'breath_max_tokens"\)\s*or\s*(\d+)', text)
            + re.findall(r'breath_max_tokens"\)\s*or\s*(\d+)\)', text)
        }
    return found


def test_every_fallback_agrees_on_the_default():
    for rel, values in _fallbacks().items():
        assert values, f"{rel} 里没找到 breath_max_tokens 的 fallback"
        assert values == {_DEFAULT_BREATH_TOKENS}, f"{rel} 的 fallback 是 {values}"


def test_config_example_matches_the_code_default():
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "config.example.yaml").read_text(
        encoding="utf-8"
    )
    match = re.search(r"breath_max_tokens:\s*(\d+)", text)
    assert match is not None
    assert int(match.group(1)) == _DEFAULT_BREATH_TOKENS


def test_the_default_fits_a_full_set_of_core_rules():
    from tools._common import _DEFAULT_MAX_PINNED

    # 一条 250 字的核心准则约 427 token；钉满时必须还剩得下普通浮现
    per_rule = 427
    needed = _DEFAULT_MAX_PINNED * per_rule
    assert needed < _DEFAULT_BREATH_TOKENS, f"钉满要 {needed}，预算只有 {_DEFAULT_BREATH_TOKENS}"
    assert _DEFAULT_BREATH_TOKENS - needed >= per_rule * 5


def test_the_default_stays_under_the_safety_cap():
    from tools.breath.surface import _BREATH_SAFETY_CAP

    assert _DEFAULT_BREATH_TOKENS < _BREATH_SAFETY_CAP
