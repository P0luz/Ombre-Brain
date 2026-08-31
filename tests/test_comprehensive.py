"""
全面测试覆盖 — Ombre Brain
涵盖：utils / bucket_manager / decay_engine / errors / embedding_engine 的核心逻辑与边界情况。
不发起任何真实网络请求。
"""

import math
import os
import sys
import pytest
from pathlib import Path
from datetime import datetime, timedelta

# ---------------------------------------------------------
# 路径与环境变量（与 conftest.py 保持一致）
# ---------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

if not os.environ.get("OMBRE_VAULT_DIR") and not os.environ.get("OMBRE_BUCKETS_DIR"):
    _test_dir = _REPO_ROOT / "test_buckets_comprehensive"
    _test_dir.mkdir(exist_ok=True)
    os.environ["OMBRE_VAULT_DIR"] = str(_test_dir)
    os.environ["OMBRE_BUCKETS_DIR"] = str(_test_dir)

if not os.environ.get("OMBRE_EMBED_API_KEY"):
    os.environ["OMBRE_EMBED_API_KEY"] = "__test_dummy__"


# ===========================================================
# 1. utils.py
# ===========================================================

class TestGenerateBucketId:
    def test_returns_12_hex_chars(self):
        from utils import generate_bucket_id
        bid = generate_bucket_id()
        assert isinstance(bid, str)
        assert len(bid) == 12
        assert all(c in "0123456789abcdef" for c in bid)

    def test_unique_on_repeated_calls(self):
        from utils import generate_bucket_id
        ids = {generate_bucket_id() for _ in range(100)}
        assert len(ids) == 100


class TestSanitizeName:
    def test_removes_path_traversal(self):
        from utils import sanitize_name
        result = sanitize_name("../../etc/passwd")
        assert ".." not in result
        assert "/" not in result
        assert "\\" not in result

    def test_keeps_chinese(self):
        from utils import sanitize_name
        result = sanitize_name("我的记忆")
        assert "我的记忆" in result

    def test_keeps_alphanumeric(self):
        from utils import sanitize_name
        result = sanitize_name("Hello World 123")
        assert "Hello" in result
        assert "123" in result

    def test_truncates_to_80_chars(self):
        from utils import sanitize_name
        long_name = "a" * 200
        result = sanitize_name(long_name)
        assert len(result) <= 80

    def test_non_string_returns_unnamed(self):
        from utils import sanitize_name
        assert sanitize_name(None) == "unnamed"
        assert sanitize_name(123) == "unnamed"

    def test_empty_string_returns_unnamed(self):
        from utils import sanitize_name
        assert sanitize_name("") == "unnamed"
        assert sanitize_name("!!!") == "unnamed"


class TestSafePath:
    def test_valid_path_within_base(self, tmp_path):
        from utils import safe_path
        result = safe_path(str(tmp_path), "memory.md")
        assert str(result).startswith(str(tmp_path))

    def test_rejects_path_traversal(self, tmp_path):
        from utils import safe_path
        with pytest.raises(ValueError):
            safe_path(str(tmp_path), "../../etc/passwd")

    def test_returns_path_object(self, tmp_path):
        from utils import safe_path
        result = safe_path(str(tmp_path), "test.md")
        assert isinstance(result, Path)


class TestCountTokensApprox:
    def test_empty_string_returns_zero(self):
        from utils import count_tokens_approx
        assert count_tokens_approx("") == 0
        assert count_tokens_approx(None) == 0

    def test_chinese_heavier_than_same_count_ascii(self):
        from utils import count_tokens_approx
        cn = "我爱Python编程"   # 7 Chinese chars
        en = "i love python"   # 3 English words
        # Chinese chars * 1.5 should dominate
        assert count_tokens_approx(cn) > count_tokens_approx(en)

    def test_english_words_counted(self):
        from utils import count_tokens_approx
        result = count_tokens_approx("hello world foo")
        assert result > 0

    def test_scales_with_length(self):
        from utils import count_tokens_approx
        short = count_tokens_approx("hello")
        long_ = count_tokens_approx("hello " * 100)
        assert long_ > short


class TestExtractWikilinks:
    def test_basic_extraction(self):
        from utils import extract_wikilinks
        result = extract_wikilinks("see [[A]] and [[B]]")
        assert result == ["A", "B"]

    def test_strips_alias(self):
        from utils import extract_wikilinks
        result = extract_wikilinks("see [[Memory|别名]]")
        assert result == ["Memory"]

    def test_strips_section(self):
        from utils import extract_wikilinks
        result = extract_wikilinks("see [[Memory#section]]")
        assert result == ["Memory"]

    def test_deduplicates(self):
        from utils import extract_wikilinks
        result = extract_wikilinks("[[A]] and [[A]]")
        assert result == ["A"]

    def test_empty_or_none_returns_empty_list(self):
        from utils import extract_wikilinks
        assert extract_wikilinks("") == []
        assert extract_wikilinks(None) == []

    def test_no_wikilinks_returns_empty_list(self):
        from utils import extract_wikilinks
        assert extract_wikilinks("plain text without links") == []


class TestNowIso:
    def test_returns_iso_string(self):
        from utils import now_iso
        result = now_iso()
        # Should parse without error
        parsed = datetime.fromisoformat(result)
        assert isinstance(parsed, datetime)

    def test_approximately_now(self):
        from utils import now_iso
        # now_iso() truncates to seconds; compare at second granularity
        before = datetime.now().replace(microsecond=0)
        result = datetime.fromisoformat(now_iso())
        after = datetime.now().replace(microsecond=0)
        assert before <= result <= after


class TestDeepMerge:
    def test_override_takes_precedence(self):
        from utils import _deep_merge
        base = {"a": 1, "b": {"c": 2}}
        override = {"b": {"c": 99, "d": 3}}
        result = _deep_merge(base, override)
        assert result["b"]["c"] == 99
        assert result["b"]["d"] == 3
        assert result["a"] == 1

    def test_non_dict_override(self):
        from utils import _deep_merge
        base = {"a": {"x": 1}}
        override = {"a": "string"}
        result = _deep_merge(base, override)
        assert result["a"] == "string"


# ===========================================================
# 2. BucketManager — CRUD & search
# ===========================================================

@pytest.fixture
def bm_config(tmp_path):
    buckets_dir = str(tmp_path / "buckets")
    for d in ["permanent", "dynamic", "archive", "feel", "plans", "letters"]:
        os.makedirs(os.path.join(buckets_dir, d), exist_ok=True)
    return {
        "buckets_dir": buckets_dir,
        "merge_threshold": 75,
        "matching": {"fuzzy_threshold": 30, "max_results": 10},
        "wikilink": {"enabled": False},
        "scoring_weights": {
            "topic_relevance": 4.0,
            "emotion_resonance": 2.0,
            "time_proximity": 1.5,
            "importance": 1.0,
            "content_weight": 1.0,
        },
        "decay": {
            "lambda": 0.05,
            "threshold": 0.3,
            "check_interval_hours": 24,
            "emotion_weights": {"base": 1.0, "arousal_boost": 0.8},
        },
        "embedding": {"enabled": False, "api_key": ""},
    }


class _FakeEmbeddingEngine:
    """最小化可用替身：这里不验证 embedding 本身，给一个永远成功的假引擎。
    （与 conftest.FakeEmbeddingEngine 同构但不跨文件 import——pytest 在
    tests/ 是 package 的布局下，`from conftest import ...` 容易因为
    sys.path 解析顺序在某些调用方式下找不到模块，保留各文件本地定义更稳妥。）
    """

    enabled = True

    async def generate_and_store(self, bucket_id, content):
        return True

    def delete_embedding(self, bucket_id):
        pass

    async def get_embedding(self, bucket_id):
        return [0.1, 0.2, 0.3]

    async def search_similar(self, query, top_k=10):
        return []


@pytest.fixture
def bucket_mgr(bm_config):
    from bucket_manager import BucketManager
    return BucketManager(bm_config, embedding_engine=_FakeEmbeddingEngine())


class TestBucketManagerCreate:
    @pytest.mark.asyncio
    async def test_create_returns_string_id(self, bucket_mgr):
        bid = await bucket_mgr.create(content="测试内容")
        assert isinstance(bid, str)
        assert len(bid) > 0

    @pytest.mark.asyncio
    async def test_created_bucket_is_retrievable(self, bucket_mgr):
        bid = await bucket_mgr.create(content="这是一段测试记忆", domain=["学习"])
        result = await bucket_mgr.get(bid)
        assert result is not None
        assert result["id"] == bid
        assert "这是一段测试记忆" in result["content"]

    @pytest.mark.asyncio
    async def test_metadata_stored_correctly(self, bucket_mgr):
        bid = await bucket_mgr.create(
            content="记忆内容",
            tags=["python", "测试"],
            importance=8,
            domain=["技术"],
            valence=0.7,
            arousal=0.4,
        )
        result = await bucket_mgr.get(bid)
        meta = result["metadata"]
        assert meta["importance"] == 8
        assert abs(meta["valence"] - 0.7) < 0.01
        assert abs(meta["arousal"] - 0.4) < 0.01
        assert "python" in meta["tags"]

    @pytest.mark.asyncio
    async def test_feel_bucket_goes_to_feel_dir(self, bucket_mgr):
        bid = await bucket_mgr.create(content="深刻的感悟", bucket_type="feel")
        result = await bucket_mgr.get(bid)
        assert result is not None
        assert result["metadata"]["type"] == "feel"

    @pytest.mark.asyncio
    async def test_pinned_bucket_locks_importance_to_10(self, bucket_mgr):
        bid = await bucket_mgr.create(content="重要内容", importance=3, pinned=True)
        result = await bucket_mgr.get(bid)
        assert result["metadata"]["importance"] == 10

    @pytest.mark.asyncio
    async def test_unpin_demotes_permanent_to_dynamic(self, bucket_mgr, decay_eng):
        # 钉选 → update(pinned=True) 自动把桶搬进 permanent/，权重恒 999
        bid = await bucket_mgr.create(content="一条核心准则")
        await bucket_mgr.update(bid, pinned=True)
        pinned = await bucket_mgr.get(bid)
        assert pinned["metadata"]["type"] == "permanent"
        assert decay_eng.calculate_score(pinned["metadata"]) == 999.0

        # 取消钉选 → 必须降级回 dynamic，权重不再卡 999
        ok = await bucket_mgr.update(bid, pinned=False)
        assert ok
        unpinned = await bucket_mgr.get(bid)
        assert unpinned["metadata"].get("pinned") is False
        assert unpinned["metadata"]["type"] == "dynamic"
        assert decay_eng.calculate_score(unpinned["metadata"]) != 999.0

        # 固化配额应实时释放（不再被这条占用）
        from tools._common import count_pinned
        import tools._runtime as rt
        rt.bucket_mgr = bucket_mgr
        assert await count_pinned() == 0

    @pytest.mark.asyncio
    async def test_decay_cycle_preserves_unpinned_permanent(self, bucket_mgr, decay_eng):
        """type=permanent is a first-class bucket type, even without pinned=True."""
        import frontmatter as fm

        bid = await bucket_mgr.create(content="一条曾被钉选的准则")
        await bucket_mgr.update(bid, pinned=True)
        # Simulate stored permanent content that has no pinned flag.
        fpath = bucket_mgr._find_bucket_file(bid)
        post = fm.load(fpath)
        post["pinned"] = False
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(fm.dumps(post))

        orphan = await bucket_mgr.get(bid)
        assert orphan["metadata"]["type"] == "permanent"
        assert decay_eng.calculate_score(orphan["metadata"]) == 999.0

        stats = await decay_eng.run_decay_cycle()

        healed = await bucket_mgr.get(bid)
        assert stats["demoted_orphans"] == 0
        assert healed["metadata"]["type"] == "permanent"
        assert healed["metadata"].get("pinned") is False
        assert decay_eng.calculate_score(healed["metadata"]) == 999.0

    @pytest.mark.asyncio
    async def test_importance_clamped_below_1(self, bucket_mgr):
        bid = await bucket_mgr.create(content="x", importance=-5)
        result = await bucket_mgr.get(bid)
        assert result["metadata"]["importance"] >= 1

    @pytest.mark.asyncio
    async def test_importance_clamped_above_10(self, bucket_mgr):
        bid = await bucket_mgr.create(content="x", importance=999)
        result = await bucket_mgr.get(bid)
        assert result["metadata"]["importance"] <= 10

    @pytest.mark.asyncio
    async def test_valence_clamped_below_0(self, bucket_mgr):
        bid = await bucket_mgr.create(content="x", valence=-1.0)
        result = await bucket_mgr.get(bid)
        assert result["metadata"]["valence"] >= 0.0

    @pytest.mark.asyncio
    async def test_valence_clamped_above_1(self, bucket_mgr):
        bid = await bucket_mgr.create(content="x", valence=2.0)
        result = await bucket_mgr.get(bid)
        assert result["metadata"]["valence"] <= 1.0

    @pytest.mark.asyncio
    async def test_default_domain_when_none_given(self, bucket_mgr):
        bid = await bucket_mgr.create(content="x")
        result = await bucket_mgr.get(bid)
        # Non-feel bucket should have a domain
        assert len(result["metadata"]["domain"]) > 0

    @pytest.mark.asyncio
    async def test_bucket_id_override(self, bucket_mgr):
        custom_id = "my_custom_id_test"
        bid = await bucket_mgr.create(content="override test", bucket_id_override=custom_id)
        # ID should start with the sanitized override or be a fallback
        assert bid is not None

    @pytest.mark.asyncio
    async def test_why_remembered_stored(self, bucket_mgr):
        bid = await bucket_mgr.create(content="x", why_remembered="重要的人生转折")
        result = await bucket_mgr.get(bid)
        assert result["metadata"].get("why_remembered") == "重要的人生转折"

    @pytest.mark.asyncio
    async def test_source_tool_stored(self, bucket_mgr):
        bid = await bucket_mgr.create(content="x", source_tool="hold")
        result = await bucket_mgr.get(bid)
        assert result["metadata"].get("source_tool") == "hold"

    @pytest.mark.asyncio
    async def test_why_remembered_truncated_to_500(self, bucket_mgr):
        long_text = "x" * 600
        bid = await bucket_mgr.create(content="x", why_remembered=long_text)
        result = await bucket_mgr.get(bid)
        stored = result["metadata"].get("why_remembered", "")
        assert len(stored) <= 500


class TestBucketManagerGet:
    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, bucket_mgr):
        result = await bucket_mgr.get("nonexistent_id_xyz")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_returns_expected_keys(self, bucket_mgr):
        bid = await bucket_mgr.create(content="test content")
        result = await bucket_mgr.get(bid)
        assert "id" in result
        assert "metadata" in result
        assert "content" in result
        assert "path" in result


class TestBucketManagerUpdate:
    @pytest.mark.asyncio
    async def test_update_importance(self, bucket_mgr):
        bid = await bucket_mgr.create(content="x", importance=3)
        success = await bucket_mgr.update(bid, importance=7)
        assert success is True
        result = await bucket_mgr.get(bid)
        assert result["metadata"]["importance"] == 7

    @pytest.mark.asyncio
    async def test_update_resolved(self, bucket_mgr):
        bid = await bucket_mgr.create(content="x")
        await bucket_mgr.update(bid, resolved=True)
        result = await bucket_mgr.get(bid)
        assert result["metadata"]["resolved"] is True

    @pytest.mark.asyncio
    async def test_update_nonexistent_returns_false(self, bucket_mgr):
        result = await bucket_mgr.update("nonexistent_bucket_xyz", importance=5)
        assert result is False

    @pytest.mark.asyncio
    async def test_metadata_update_does_not_refresh_last_active(self, bucket_mgr):
        """纯元数据编辑（trace 等）不算「激活」：不刷 last_active、不动 activation_count。"""
        bid = await bucket_mgr.create(content="x")
        before = (await bucket_mgr.get(bid))["metadata"]
        before_active = before["last_active"]
        before_count = float(before.get("activation_count") or 0)
        await bucket_mgr.update(bid, importance=6)
        after = (await bucket_mgr.get(bid))["metadata"]
        assert after["last_active"] == before_active
        assert float(after.get("activation_count") or 0) == before_count

    @pytest.mark.asyncio
    async def test_bump_active_update_refreshes_activation(self, bucket_mgr):
        """真实激活写入（如合并近邻桶）：bump_active=True 刷 last_active 且 activation_count +1。"""
        import time
        bid = await bucket_mgr.create(content="x")
        before = (await bucket_mgr.get(bid))["metadata"]
        before_count = float(before.get("activation_count") or 0)
        time.sleep(1)  # 保证秒级时间戳能前移
        await bucket_mgr.update(bid, importance=6, bump_active=True)
        after = (await bucket_mgr.get(bid))["metadata"]
        assert float(after.get("activation_count") or 0) == before_count + 1
        assert after["last_active"] >= before["last_active"]

    @pytest.mark.asyncio
    async def test_update_valence_clamped(self, bucket_mgr):
        bid = await bucket_mgr.create(content="x")
        await bucket_mgr.update(bid, valence=5.0)
        result = await bucket_mgr.get(bid)
        assert result["metadata"]["valence"] <= 1.0


class TestBucketManagerDelete:
    @pytest.mark.asyncio
    async def test_delete_moves_to_archive(self, bucket_mgr):
        bid = await bucket_mgr.create(content="删除测试", domain=["测试"])
        success = await bucket_mgr.delete(bid)
        assert success is True
        # Should not be findable in active dirs
        result = await bucket_mgr.get(bid)
        # After soft-delete, get() looks in active dirs and may return None or archive
        # Either way the file should have deleted_at stamp if it's still accessible
        if result is not None:
            assert "deleted_at" in result["metadata"]

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_false(self, bucket_mgr):
        result = await bucket_mgr.delete("nonexistent_xyz")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_is_soft_not_physical(self, bucket_mgr):
        bid = await bucket_mgr.create(content="soft delete test")
        await bucket_mgr.delete(bid)
        # Check archive dir has the file
        archive_dir = bucket_mgr.archive_dir
        archive_files = list(Path(archive_dir).rglob("*.md"))
        found = any(bid in str(f.stem) for f in archive_files)
        assert found, "Soft-deleted file should exist in archive/"


class TestBucketManagerSearch:
    @pytest.mark.asyncio
    async def test_empty_query_returns_empty(self, bucket_mgr):
        result = await bucket_mgr.search("")
        assert result == []

    @pytest.mark.asyncio
    async def test_whitespace_query_returns_empty(self, bucket_mgr):
        result = await bucket_mgr.search("   ")
        assert result == []

    @pytest.mark.asyncio
    async def test_keyword_match_returns_results(self, bucket_mgr):
        await bucket_mgr.create(content="Python编程语言学习笔记", domain=["技术"], tags=["python"])
        results = await bucket_mgr.search("Python")
        assert len(results) > 0

    @pytest.mark.asyncio
    async def test_results_have_score_field(self, bucket_mgr):
        await bucket_mgr.create(content="编程学习", tags=["编程"])
        results = await bucket_mgr.search("编程")
        for r in results:
            assert "score" in r
            assert isinstance(r["score"], float)

    @pytest.mark.asyncio
    async def test_results_sorted_by_score_desc(self, bucket_mgr):
        await bucket_mgr.create(content="Python是编程语言", tags=["python"], importance=8)
        await bucket_mgr.create(content="今天天气不错", tags=["天气"], importance=3)
        results = await bucket_mgr.search("Python")
        if len(results) > 1:
            scores = [r["score"] for r in results]
            assert scores == sorted(scores, reverse=True)

    @pytest.mark.asyncio
    async def test_domain_filter_works(self, bucket_mgr):
        await bucket_mgr.create(content="科学知识", domain=["科学"], tags=["科学"])
        await bucket_mgr.create(content="美食食谱", domain=["美食"], tags=["美食"])
        results = await bucket_mgr.search("知识", domain_filter=["科学"])
        for r in results:
            domains = r["metadata"].get("domain", [])
            assert "科学" in domains

    @pytest.mark.asyncio
    async def test_resolved_bucket_penalized(self, bucket_mgr):
        bid_active = await bucket_mgr.create(
            content="Python编程学习", tags=["python"], importance=7
        )
        bid_resolved = await bucket_mgr.create(
            content="Python编程学习已完成", tags=["python"], importance=7
        )
        await bucket_mgr.update(bid_resolved, resolved=True)

        results = await bucket_mgr.search("Python编程")
        result_ids = [r["id"] for r in results]
        if bid_active in result_ids and bid_resolved in result_ids:
            active_score = next(r["score"] for r in results if r["id"] == bid_active)
            resolved_score = next(r["score"] for r in results if r["id"] == bid_resolved)
            assert active_score >= resolved_score

    @pytest.mark.asyncio
    async def test_emotion_resonance_higher_when_matched(self, bucket_mgr):
        # Happy bucket (valence=0.9, arousal=0.7)
        bid_happy = await bucket_mgr.create(
            content="快乐", valence=0.9, arousal=0.7, tags=["情感"]
        )
        # Sad bucket (valence=0.1, arousal=0.2)
        bid_sad = await bucket_mgr.create(
            content="悲伤", valence=0.1, arousal=0.2, tags=["情感"]
        )
        # Query with happy coordinates
        results = await bucket_mgr.search(
            "情感", query_valence=0.9, query_arousal=0.7
        )
        result_map = {r["id"]: r["score"] for r in results}
        if bid_happy in result_map and bid_sad in result_map:
            assert result_map[bid_happy] >= result_map[bid_sad]


class TestBucketManagerListAll:
    @pytest.mark.asyncio
    async def test_list_all_returns_created_buckets(self, bucket_mgr):
        await bucket_mgr.create(content="list test 1", domain=["测试"])
        await bucket_mgr.create(content="list test 2", domain=["测试"])
        results = await bucket_mgr.list_all(include_archive=False)
        assert len(results) >= 2

    @pytest.mark.asyncio
    async def test_list_all_excludes_archive_by_default(self, bucket_mgr):
        bid = await bucket_mgr.create(content="will be deleted")
        await bucket_mgr.delete(bid)
        results = await bucket_mgr.list_all(include_archive=False)
        result_ids = [r["id"] for r in results]
        assert bid not in result_ids


class TestBucketManagerTouch:
    @pytest.mark.asyncio
    async def test_touch_increments_activation_count(self, bucket_mgr):
        bid = await bucket_mgr.create(content="touch test")
        before = (await bucket_mgr.get(bid))["metadata"].get("activation_count", 0)
        await bucket_mgr.touch(bid)
        after = (await bucket_mgr.get(bid))["metadata"].get("activation_count", 0)
        assert after > before

    @pytest.mark.asyncio
    async def test_touch_nonexistent_no_error(self, bucket_mgr):
        # Should not raise
        await bucket_mgr.touch("nonexistent_xyz_123")


class TestBucketManagerAnchor:
    @pytest.mark.asyncio
    async def test_anchor_count_starts_zero(self, bucket_mgr):
        count = await bucket_mgr.count_anchors()
        assert count == 0

    @pytest.mark.asyncio
    async def test_set_anchor_increments_count(self, bucket_mgr):
        bid = await bucket_mgr.create(content="anchor me")
        await bucket_mgr.set_anchor(bid, True)
        count = await bucket_mgr.count_anchors()
        assert count == 1

    @pytest.mark.asyncio
    async def test_anchor_limit_24(self, bucket_mgr):
        """Cannot exceed 24 anchors."""
        ids = []
        for i in range(25):
            bid = await bucket_mgr.create(content=f"anchor test {i}")
            ids.append(bid)
        # Set first 24 as anchors
        for bid in ids[:24]:
            await bucket_mgr.set_anchor(bid, True)
        count_24 = await bucket_mgr.count_anchors()
        assert count_24 == 24
        # 25th should be rejected
        result = await bucket_mgr.set_anchor(ids[24], True)
        count_after = await bucket_mgr.count_anchors()
        assert result["ok"] is False
        assert count_after == 24


# ===========================================================
# 3. DecayEngine — scoring formulas
# ===========================================================

@pytest.fixture
def decay_config(tmp_path):
    buckets_dir = str(tmp_path / "buckets")
    for d in ["permanent", "dynamic", "archive", "feel"]:
        os.makedirs(os.path.join(buckets_dir, d), exist_ok=True)
    return {
        "buckets_dir": buckets_dir,
        "decay": {
            "lambda": 0.05,
            "threshold": 0.3,
            "check_interval_hours": 24,
            "emotion_weights": {"base": 1.0, "arousal_boost": 0.8},
        },
    }


@pytest.fixture
def decay_engine(decay_config):
    from bucket_manager import BucketManager
    from decay_engine import DecayEngine
    bm = BucketManager(decay_config, embedding_engine=_FakeEmbeddingEngine())
    return DecayEngine(decay_config, bm)


class TestDecayEngineTimeWeight:
    def test_t0_returns_2_0(self, decay_engine):
        result = decay_engine._calc_time_weight(0.0)
        assert abs(result - 2.0) < 0.01

    def test_always_at_least_1_0(self, decay_engine):
        for days in [0, 1, 7, 30, 365]:
            assert decay_engine._calc_time_weight(float(days)) >= 1.0

    def test_monotonically_decreasing(self, decay_engine):
        values = [decay_engine._calc_time_weight(float(d)) for d in range(0, 30)]
        for i in range(len(values) - 1):
            assert values[i] >= values[i + 1]

    def test_half_life_approximately_36h(self, decay_engine):
        # At 36h / 24h ≈ 1.5 days, bonus should be ≈ 0.5 (halfway to floor)
        # full bonus = 1.0, at t=36h: 1.0 + e^(-1) ≈ 1.368
        result_36h = decay_engine._calc_time_weight(36.0 / 24.0)
        expected = 1.0 + math.exp(-1.0)
        assert abs(result_36h - expected) < 0.01


class TestDecayEngineScore:
    def _base_meta(self):
        return {
            "type": "dynamic",
            "importance": 5,
            "activation_count": 1,
            "last_active": datetime.now().isoformat(timespec="seconds"),
            "arousal": 0.3,
            "valence": 0.5,
            "resolved": False,
        }

    def test_non_dict_returns_zero(self, decay_engine):
        assert decay_engine.calculate_score(None) == 0.0
        assert decay_engine.calculate_score("string") == 0.0
        assert decay_engine.calculate_score([]) == 0.0

    def test_pinned_returns_999(self, decay_engine):
        meta = self._base_meta()
        meta["pinned"] = True
        assert decay_engine.calculate_score(meta) == 999.0

    def test_protected_returns_999(self, decay_engine):
        meta = self._base_meta()
        meta["protected"] = True
        assert decay_engine.calculate_score(meta) == 999.0

    def test_permanent_returns_999(self, decay_engine):
        meta = self._base_meta()
        meta["type"] = "permanent"
        assert decay_engine.calculate_score(meta) == 999.0

    def test_feel_returns_50(self, decay_engine):
        meta = self._base_meta()
        meta["type"] = "feel"
        assert decay_engine.calculate_score(meta) == 50.0

    def test_plan_returns_50(self, decay_engine):
        meta = self._base_meta()
        meta["type"] = "plan"
        assert decay_engine.calculate_score(meta) == 50.0

    def test_letter_returns_50(self, decay_engine):
        meta = self._base_meta()
        meta["type"] = "letter"
        assert decay_engine.calculate_score(meta) == 50.0

    def test_score_positive_for_fresh_dynamic(self, decay_engine):
        meta = self._base_meta()
        score = decay_engine.calculate_score(meta)
        assert score > 0.0

    def test_recent_beats_old(self, decay_engine):
        recent_meta = self._base_meta()
        old_meta = self._base_meta()
        old_meta["last_active"] = (datetime.now() - timedelta(days=60)).isoformat()
        recent_score = decay_engine.calculate_score(recent_meta)
        old_score = decay_engine.calculate_score(old_meta)
        assert recent_score > old_score

    def test_higher_importance_scores_higher(self, decay_engine):
        low_meta = self._base_meta()
        high_meta = self._base_meta()
        low_meta["importance"] = 2
        high_meta["importance"] = 9
        assert decay_engine.calculate_score(high_meta) > decay_engine.calculate_score(low_meta)

    def test_resolved_only_penalized(self, decay_engine):
        normal = self._base_meta()
        resolved = self._base_meta()
        resolved["resolved"] = True
        assert decay_engine.calculate_score(resolved) < decay_engine.calculate_score(normal)

    def test_resolved_digested_more_penalized_than_resolved_only(self, decay_engine):
        resolved_only = self._base_meta()
        resolved_only["resolved"] = True
        resolved_digested = self._base_meta()
        resolved_digested["resolved"] = True
        resolved_digested["digested"] = True
        assert (
            decay_engine.calculate_score(resolved_digested)
            < decay_engine.calculate_score(resolved_only)
        )

    def test_high_arousal_unresolved_gets_urgency_boost(self, decay_engine):
        calm = self._base_meta()
        calm["arousal"] = 0.3
        urgent = self._base_meta()
        urgent["arousal"] = 0.9
        urgent["resolved"] = False
        assert decay_engine.calculate_score(urgent) > decay_engine.calculate_score(calm)

    def test_urgency_boost_not_applied_when_resolved(self, decay_engine):
        urgent_resolved = self._base_meta()
        urgent_resolved["arousal"] = 0.9
        urgent_resolved["resolved"] = True
        calm_unresolved = self._base_meta()
        calm_unresolved["arousal"] = 0.3
        # Resolved high-arousal should score lower than calm unresolved
        # because resolved_factor=0.05 offsets urgency
        score_urgent_resolved = decay_engine.calculate_score(urgent_resolved)
        score_calm = decay_engine.calculate_score(calm_unresolved)
        assert score_urgent_resolved < score_calm

    def test_bad_importance_falls_back(self, decay_engine):
        meta = self._base_meta()
        meta["importance"] = "not_a_number"
        # Should not raise, should use fallback importance
        score = decay_engine.calculate_score(meta)
        assert isinstance(score, float)
        assert score >= 0.0

    def test_bad_last_active_uses_fallback_days(self, decay_engine):
        meta = self._base_meta()
        meta["last_active"] = "invalid-date-string"
        # Should not raise, uses 30-day fallback
        score = decay_engine.calculate_score(meta)
        assert isinstance(score, float)
        assert score >= 0.0


class TestDecayEngineRunCycle:
    @pytest.mark.asyncio
    async def test_run_cycle_returns_stats_dict(self, decay_engine, bucket_mgr):
        # Give decay_engine a bucket_mgr that has some buckets
        decay_engine.bucket_mgr = bucket_mgr
        await bucket_mgr.create(content="cycle test", domain=["测试"])
        result = await decay_engine.run_decay_cycle()
        assert "checked" in result
        assert "archived" in result
        assert isinstance(result["checked"], int)
        assert isinstance(result["archived"], int)

    @pytest.mark.asyncio
    async def test_run_cycle_archives_low_score_bucket(self, decay_engine, bucket_mgr):
        import frontmatter as fm
        decay_engine.bucket_mgr = bucket_mgr
        decay_engine.threshold = 9999.0  # Set threshold very high to force archiving

        bid = await bucket_mgr.create(
            content="low score bucket", importance=1, domain=["测试"]
        )
        # Patch last_active to 100 days ago
        fpath = bucket_mgr._find_bucket_file(bid)
        post = fm.load(fpath)
        post["last_active"] = (datetime.now() - timedelta(days=100)).isoformat()
        post["activation_count"] = 1
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(fm.dumps(post))

        result = await decay_engine.run_decay_cycle()
        assert result["archived"] >= 1

    @pytest.mark.asyncio
    async def test_run_cycle_preserves_anchor_and_archives_equivalent_dynamic(
        self, decay_engine, bucket_mgr
    ):
        import frontmatter as fm

        decay_engine.bucket_mgr = bucket_mgr
        anchor_id = await bucket_mgr.create(
            content="需要长期保留的关系坐标", importance=5, domain=["测试"]
        )
        dynamic_id = await bucket_mgr.create(
            content="同龄的普通动态记忆", importance=5, domain=["测试"]
        )
        anchor_result = await bucket_mgr.set_anchor(anchor_id, True)
        assert anchor_result["ok"] is True

        old_ts = (datetime.now() - timedelta(days=365)).isoformat()
        for bucket_id in (anchor_id, dynamic_id):
            fpath = bucket_mgr._find_bucket_file(bucket_id)
            post = fm.load(fpath)
            post["created"] = old_ts
            post["last_active"] = old_ts
            post["activation_count"] = 1
            if bucket_id == dynamic_id:
                post["anchor"] = "false"
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(fm.dumps(post))
            assert await bucket_mgr.update(bucket_id, importance=5)

        anchor_before = await bucket_mgr.get(anchor_id)
        dynamic_before = await bucket_mgr.get(dynamic_id)
        assert anchor_before["metadata"].get("anchor") is True
        assert dynamic_before["metadata"].get("anchor") == "false"
        assert decay_engine.calculate_score(anchor_before["metadata"]) < decay_engine.threshold
        assert decay_engine.calculate_score(dynamic_before["metadata"]) < decay_engine.threshold

        stats = await decay_engine.run_decay_cycle()

        active_ids = {
            bucket["id"]
            for bucket in await bucket_mgr.list_all(include_archive=False)
        }
        assert anchor_id in active_ids, "anchor 不应被普通衰减周期自动归档"
        assert dynamic_id not in active_ids, "普通低分动态桶仍应按既有规则归档"
        anchor_after = await bucket_mgr.get(anchor_id)
        dynamic_after = await bucket_mgr.get(dynamic_id)
        assert anchor_after["metadata"]["type"] == "dynamic"
        assert anchor_after["metadata"].get("anchor") is True
        assert dynamic_after["metadata"]["type"] == "archived"
        assert Path(dynamic_after["path"]).is_relative_to(Path(bucket_mgr.archive_dir))
        assert stats["checked"] == 1
        assert stats["archived"] == 1

    @pytest.mark.asyncio
    async def test_run_cycle_skips_pinned(self, decay_engine, bucket_mgr):
        decay_engine.bucket_mgr = bucket_mgr
        decay_engine.threshold = 9999.0  # Force archive anything low

        bid = await bucket_mgr.create(content="pinned bucket", pinned=True)
        await decay_engine.run_decay_cycle()
        # Pinned bucket should never be archived
        still_alive = await bucket_mgr.get(bid)
        assert still_alive is not None

    @pytest.mark.asyncio
    async def test_run_cycle_skips_feel_buckets(self, decay_engine, bucket_mgr):
        decay_engine.bucket_mgr = bucket_mgr
        decay_engine.threshold = 9999.0  # Force archive anything

        bid = await bucket_mgr.create(content="feel bucket", bucket_type="feel")
        await decay_engine.run_decay_cycle()
        # Feel buckets should never be archived
        fpath = bucket_mgr._find_bucket_file(bid)
        if fpath is None:
            # Might be in archive — check
            archive_files = list(Path(bucket_mgr.archive_dir).rglob("*.md"))
            # Feel buckets should NOT be in archive
            assert not any(bid in str(f.stem) for f in archive_files)


# ===========================================================
# 4. errors.py
# ===========================================================

class TestErrorCodes:
    def test_all_error_codes_have_required_fields(self):
        from errors import ERROR_CODES
        for code, spec in ERROR_CODES.items():
            assert spec.code == code
            assert spec.level in ("F", "E", "W", "I")
            assert spec.title_zh
            assert spec.title_en

    def test_fatal_codes_start_with_ob_f(self):
        from errors import ERROR_CODES
        for code, spec in ERROR_CODES.items():
            if spec.level == "F":
                assert code.startswith("OB-F")

    def test_error_codes_start_with_ob_e(self):
        from errors import ERROR_CODES
        for code, spec in ERROR_CODES.items():
            if spec.level == "E":
                assert code.startswith("OB-E")


class TestPushPopWarnings:
    def test_push_warning_then_pop(self):
        from errors import push_warning, pop_warnings, begin_warnings
        begin_warnings()
        push_warning("OB-W001", "test warning detail")
        warnings = pop_warnings()
        assert len(warnings) >= 1

    def test_pop_warnings_clears_queue(self):
        from errors import push_warning, pop_warnings, begin_warnings
        begin_warnings()
        push_warning("OB-W002", "another warning")
        pop_warnings()
        second_pop = pop_warnings()
        assert len(second_pop) == 0


class TestRecordError:
    def test_record_error_returns_dict(self, tmp_path):
        from errors import configure_errors_path, record_error
        configure_errors_path(str(tmp_path))
        result = record_error("OB-E001", "test error detail", log=False)
        assert isinstance(result, dict)
        assert result.get("code") == "OB-E001"

    def test_recent_errors_returns_list(self, tmp_path):
        from errors import configure_errors_path, record_error, recent_errors
        configure_errors_path(str(tmp_path))
        record_error("OB-E001", "detail A", log=False)
        errors = recent_errors(limit=10)
        assert isinstance(errors, list)

    def test_clear_errors_log(self, tmp_path):
        from errors import clear_errors_log, configure_errors_path, record_error
        configure_errors_path(str(tmp_path))
        record_error("OB-E001", "to be cleared", log=False)
        cleared = clear_errors_log()
        assert isinstance(cleared, int)
        assert cleared >= 0


class TestFormatError:
    def test_format_error_contains_code(self):
        from errors import format_error
        result = format_error("OB-E001", "test detail")
        assert "OB-E001" in result

    def test_format_error_unknown_code_graceful(self):
        from errors import format_error
        # Should not raise even for unknown codes
        result = format_error("OB-UNKNOWN", "detail")
        assert isinstance(result, str)


# ===========================================================
# 5. embedding_engine.py — cosine similarity
# ===========================================================

class TestCosineSimilarity:
    def test_identical_vectors_return_1(self):
        from embedding_engine import EmbeddingEngine
        v = [1.0, 0.0, 0.0]
        result = EmbeddingEngine._cosine_similarity(v, v)
        assert abs(result - 1.0) < 1e-9

    def test_orthogonal_vectors_return_0(self):
        from embedding_engine import EmbeddingEngine
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        result = EmbeddingEngine._cosine_similarity(a, b)
        assert abs(result) < 1e-9

    def test_opposite_vectors_return_negative_1(self):
        from embedding_engine import EmbeddingEngine
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        result = EmbeddingEngine._cosine_similarity(a, b)
        assert abs(result + 1.0) < 1e-9

    def test_different_length_vectors_return_zero(self):
        from embedding_engine import EmbeddingEngine
        a = [1.0, 0.0]
        b = [1.0, 0.0, 0.0]
        result = EmbeddingEngine._cosine_similarity(a, b)
        assert result == 0.0

    def test_zero_vector_returns_zero(self):
        from embedding_engine import EmbeddingEngine
        a = [0.0, 0.0]
        b = [1.0, 0.0]
        result = EmbeddingEngine._cosine_similarity(a, b)
        assert result == 0.0 or math.isnan(result) or result == 0.0

    def test_result_range_minus1_to_1(self):
        from embedding_engine import EmbeddingEngine
        import random
        random.seed(42)
        for _ in range(20):
            a = [random.uniform(-1, 1) for _ in range(8)]
            b = [random.uniform(-1, 1) for _ in range(8)]
            result = EmbeddingEngine._cosine_similarity(a, b)
            if not math.isnan(result):
                assert -1.0 - 1e-9 <= result <= 1.0 + 1e-9


# ===========================================================
# 6. BucketManager scoring sub-functions (unit)
# ===========================================================

class TestCalcEmotionScore:
    def test_same_coordinates_returns_1(self, bucket_mgr):
        meta = {"valence": 0.7, "arousal": 0.4}
        result = bucket_mgr._calc_emotion_score(0.7, 0.4, meta)
        assert abs(result - 1.0) < 1e-9

    def test_no_query_emotion_returns_0_5(self, bucket_mgr):
        meta = {"valence": 0.7, "arousal": 0.4}
        result = bucket_mgr._calc_emotion_score(None, None, meta)
        assert result == 0.5

    def test_max_distance_approaches_zero(self, bucket_mgr):
        meta = {"valence": 1.0, "arousal": 1.0}
        result = bucket_mgr._calc_emotion_score(0.0, 0.0, meta)
        assert result >= 0.0
        assert result < 0.1

    def test_result_in_range_0_to_1(self, bucket_mgr):
        for qv, qa in [(0.0, 0.0), (0.5, 0.5), (1.0, 1.0), (0.3, 0.8)]:
            for bv, ba in [(0.0, 0.0), (1.0, 1.0)]:
                result = bucket_mgr._calc_emotion_score(qv, qa, {"valence": bv, "arousal": ba})
                assert 0.0 <= result <= 1.0

    def test_invalid_meta_returns_0_5(self, bucket_mgr):
        result = bucket_mgr._calc_emotion_score(0.5, 0.5, {"valence": "bad", "arousal": "bad"})
        assert result == 0.5


class TestCalcTopicScore:
    @pytest.mark.asyncio
    async def test_exact_name_match_scores_high(self, bucket_mgr):
        bid = await bucket_mgr.create(content="some content", name="Python学习笔记", tags=["python"])
        result = await bucket_mgr.get(bid)
        score = bucket_mgr._calc_topic_score("Python学习笔记", result)
        assert score > 0.5

    @pytest.mark.asyncio
    async def test_no_match_scores_low(self, bucket_mgr):
        bid = await bucket_mgr.create(content="完全无关的内容", tags=["random"])
        result = await bucket_mgr.get(bid)
        score = bucket_mgr._calc_topic_score("ZZZZNOTFOUND", result)
        assert score < 0.5


# ===========================================================
# 7. Clamp helpers
# ===========================================================

class TestClampImportance:
    def test_valid_values_pass_through(self):
        from bucket_manager import _clamp_importance
        assert _clamp_importance(1, "test") == 1
        assert _clamp_importance(5, "test") == 5
        assert _clamp_importance(10, "test") == 10

    def test_below_1_clamped_to_1(self):
        from bucket_manager import _clamp_importance
        assert _clamp_importance(0, "test") == 1
        assert _clamp_importance(-99, "test") == 1

    def test_above_10_clamped_to_10(self):
        from bucket_manager import _clamp_importance
        assert _clamp_importance(11, "test") == 10
        assert _clamp_importance(999, "test") == 10

    def test_non_parseable_returns_5(self):
        from bucket_manager import _clamp_importance
        assert _clamp_importance("abc", "test") == 5
        assert _clamp_importance(None, "test") == 5


class TestClampUnit:
    def test_valid_values_pass_through(self):
        from bucket_manager import _clamp_unit
        assert abs(_clamp_unit(0.0, "valence", "test") - 0.0) < 1e-9
        assert abs(_clamp_unit(0.5, "valence", "test") - 0.5) < 1e-9
        assert abs(_clamp_unit(1.0, "valence", "test") - 1.0) < 1e-9

    def test_below_0_clamped_to_0(self):
        from bucket_manager import _clamp_unit
        assert _clamp_unit(-0.5, "valence", "test") == 0.0

    def test_above_1_clamped_to_1(self):
        from bucket_manager import _clamp_unit
        assert _clamp_unit(1.5, "valence", "test") == 1.0

    def test_non_parseable_returns_0_5(self):
        from bucket_manager import _clamp_unit
        assert _clamp_unit("bad", "valence", "test") == 0.5
        assert _clamp_unit(None, "arousal", "test") == 0.5


# ===========================================================
# 8. BucketManager get_stats / list_by_type
# ===========================================================

class TestBucketManagerStats:
    @pytest.mark.asyncio
    async def test_get_stats_returns_dict(self, bucket_mgr):
        stats = await bucket_mgr.get_stats()
        assert isinstance(stats, dict)

    @pytest.mark.asyncio
    async def test_stats_increments_on_create(self, bucket_mgr):
        before = await bucket_mgr.get_stats()
        await bucket_mgr.create(content="stats test bucket")
        after = await bucket_mgr.get_stats()
        # Total count should increase
        before_total = before.get("total", 0)
        after_total = after.get("total", 0)
        assert after_total >= before_total


# ===========================================================
# 9. BucketManager — archive
# ===========================================================

class TestBucketManagerArchive:
    @pytest.mark.asyncio
    async def test_archive_moves_bucket(self, bucket_mgr):
        bid = await bucket_mgr.create(content="archive me", domain=["测试"])
        success = await bucket_mgr.archive(bid)
        assert success is True
        # Should not appear in active list
        active = await bucket_mgr.list_all(include_archive=False)
        active_ids = [b["id"] for b in active]
        assert bid not in active_ids

    @pytest.mark.asyncio
    async def test_archive_nonexistent_returns_false(self, bucket_mgr):
        result = await bucket_mgr.archive("no_such_bucket_xyz")
        assert result is False


# ===========================================================
# 10. EmbeddingEngine — model-name 归一化（OB-W005 假阳性）
# ===========================================================

class TestEmbeddingModelNorm:
    def test_prefix_equivalent_to_bare(self):
        from embedding_engine import _norm_model
        # Gemini 端点的 models/ 前缀 vs OpenAI 兼容代理的裸名 → 同一模型
        assert _norm_model("models/gemini-embedding-001") == _norm_model("gemini-embedding-001")

    def test_case_and_whitespace_insensitive(self):
        from embedding_engine import _norm_model
        assert _norm_model("  models/Gemini-Embedding-001 ") == _norm_model("gemini-embedding-001")

    def test_different_models_stay_distinct(self):
        from embedding_engine import _norm_model
        assert _norm_model("bge-m3") != _norm_model("gemini-embedding-001")

    def test_latest_tag_equivalent_to_bare(self):
        from embedding_engine import _norm_model
        # Ollama 的 :latest 默认 tag vs 裸名 → 同一模型（假 OB-W005 场景）
        assert _norm_model("bge-m3:latest") == _norm_model("bge-m3")
        assert _norm_model("BGE-M3:latest") == _norm_model("bge-m3")

    def test_quantization_tags_stay_distinct(self):
        from embedding_engine import _norm_model
        # 非 :latest 的 tag 是不同量化版本，必须保持可区分
        assert _norm_model("bge-m3:q4_0") != _norm_model("bge-m3")
        assert _norm_model("bge-m3:q4_0") != _norm_model("bge-m3:latest")
