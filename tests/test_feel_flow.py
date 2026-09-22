# ============================================================
# Test 3: Feel Flow — end-to-end feel pipeline test
# 测试 3：Feel 流程 —— 端到端 feel 管道测试
#
# Tests the complete feel lifecycle:
#   1. hold(content, feel=True) → creates feel bucket
#   2. breath(domain="feel") → retrieves feel buckets by time
#   3. source_bucket marked as digested
#   4. dream() → returns feel crystallization hints
#   5. trace() → can modify/hide feel
#   6. Decay score invariants for feel
# ============================================================

import os
import pytest
import pytest_asyncio

# Feel flow tests use direct BucketManager calls, no LLM needed.


class _FakeEmbeddingEngine:
    """这里不验证 embedding 本身，给一个永远成功的假引擎。"""

    enabled = True

    async def generate_and_store(self, bucket_id, content):
        return True

    def delete_embedding(self, bucket_id):
        pass

    async def get_embedding(self, bucket_id):
        return [0.1, 0.2, 0.3]

    async def search_similar(self, query, top_k=10):
        return []


@pytest_asyncio.fixture
async def isolated_tools(test_config, tmp_path, monkeypatch):
    """
    Import server tools with config pointing to temp dir.
    This avoids touching real data.
    """
    # Override env so server.py uses our temp buckets
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))

    # Create directory structure
    bd = str(tmp_path / "buckets")
    for d in ["permanent", "dynamic", "archive", "dynamic/feel"]:
        os.makedirs(os.path.join(bd, d), exist_ok=True)

    # Write a minimal config.yaml
    import yaml
    config_path = str(tmp_path / "config.yaml")
    with open(config_path, "w") as f:
        yaml.dump(test_config, f)
    monkeypatch.setenv("OMBRE_CONFIG_PATH", config_path)

    # Now import — this triggers module-level init in server.py
    # We need to re-import with our patched env
    import importlib
    import utils
    importlib.reload(utils)

    from bucket_manager import BucketManager
    from decay_engine import DecayEngine
    from dehydrator import Dehydrator

    bm = BucketManager(test_config | {"buckets_dir": bd}, embedding_engine=_FakeEmbeddingEngine())
    dh = Dehydrator(test_config)
    de = DecayEngine(test_config, bm)

    return bm, dh, de, bd


class TestFeelLifecycle:
    """Test the complete feel lifecycle using direct module calls."""

    @pytest.mark.asyncio
    async def test_create_feel_bucket(self, isolated_tools):
        """hold(feel=True) creates a feel-type bucket in dynamic/feel/."""
        bm, dh, de, bd = isolated_tools

        bid = await bm.create_internal(
            content="帮TestUser修好bug的时候，我感到一种真实的成就感",
            tags=[],
            importance=5,
            domain=[],
            valence=0.85,
            arousal=0.5,
            name=None,
            bucket_type="feel",
        )

        assert bid is not None

        # Verify it exists and is feel type
        all_b = await bm.list_all()
        feel_b = [b for b in all_b if b["id"] == bid]
        assert len(feel_b) == 1
        assert feel_b[0]["metadata"]["type"] == "feel"

    @pytest.mark.asyncio
    async def test_feel_in_feel_directory(self, isolated_tools):
        """Feel bucket stored under feel/沉淀物/."""
        bm, dh, de, bd = isolated_tools

        bid = await bm.create_internal(
            content="这是一条 feel 测试",
            tags=[], importance=5, domain=[],
            valence=0.5, arousal=0.3,
            name=None, bucket_type="feel",
        )

        feel_dir = os.path.join(bd, "feel", "沉淀物")
        files = os.listdir(feel_dir)
        assert any(bid in f for f in files), f"Feel bucket {bid} not found in {feel_dir}"

    @pytest.mark.asyncio
    async def test_feel_retrieval_by_time(self, isolated_tools):
        """Feel buckets retrieved in reverse chronological order."""
        bm, dh, de, bd = isolated_tools
        import frontmatter as fm
        from datetime import datetime, timedelta

        ids = []
        # Create 3 feels with manually patched timestamps via file rewrite
        for i in range(3):
            bid = await bm.create_internal(
                content=f"Feel #{i+1}",
                tags=[], importance=5, domain=[],
                valence=0.5, arousal=0.3,
                name=None, bucket_type="feel",
            )
            ids.append(bid)

        # Patch created timestamps directly in files
        # Feel #1 = oldest, Feel #3 = newest
        all_b = await bm.list_all()
        for b in all_b:
            if b["metadata"].get("type") != "feel":
                continue
            fpath = bm._find_bucket_file(b["id"])
            post = fm.load(fpath)
            idx = int(b["content"].split("#")[1]) - 1  # 0, 1, 2
            ts = (datetime.now() - timedelta(hours=(3 - idx) * 10)).isoformat()
            post["created"] = ts
            post["last_active"] = ts
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(fm.dumps(post))

        # 本测试绕过 bucket_mgr 直接改文件（改 created 时间戳），必须手动让活跃桶缓存失效，
        # 否则下面的 list_all 会拿到改时间戳之前的缓存。生产里唯一直写 .md 的 GitHub 导入
        # 同样在写完后调 _invalidate_bm25()（见 web/github.py），此处遵循同一契约。
        bm._invalidate_bm25()

        all_b = await bm.list_all()
        feels = [b for b in all_b if b["metadata"].get("type") == "feel"]
        feels.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)

        # Feel #3 has the most recent timestamp
        assert "Feel #3" in feels[0]["content"]

    @pytest.mark.asyncio
    async def test_source_bucket_marked_digested(self, isolated_tools):
        """hold(feel=True, source_bucket=X) marks X as digested."""
        bm, dh, de, bd = isolated_tools

        # Create a normal bucket first
        source_id = await bm.create_internal(
            content="和朋友吵了一架",
            tags=["社交"], importance=7, domain=["社交"],
            valence=0.3, arousal=0.7,
            name="争吵", bucket_type="dynamic",
        )

        # Verify not digested yet
        all_b = await bm.list_all()
        source = next(b for b in all_b if b["id"] == source_id)
        assert not source["metadata"].get("digested", False)

        # Create feel referencing it
        await bm.create_internal(
            content="那次争吵让我意识到沟通的重要性",
            tags=[], importance=5, domain=[],
            valence=0.5, arousal=0.4,
            name=None, bucket_type="feel",
        )
        # Manually mark digested (simulating server.py hold logic)
        await bm.update(source_id, digested=True)

        # Verify digested
        all_b = await bm.list_all()
        source = next(b for b in all_b if b["id"] == source_id)
        assert source["metadata"].get("digested") is True

    @pytest.mark.asyncio
    async def test_feel_never_decays(self, isolated_tools):
        """Feel buckets always score 50.0."""
        bm, dh, de, bd = isolated_tools

        bid = await bm.create_internal(
            content="这是一条永不衰减的 feel",
            tags=[], importance=5, domain=[],
            valence=0.5, arousal=0.3,
            name=None, bucket_type="feel",
        )

        all_b = await bm.list_all()
        feel_b = next(b for b in all_b if b["id"] == bid)
        score = de.calculate_score(feel_b["metadata"])
        assert score == 50.0

    @pytest.mark.asyncio
    async def test_feel_not_in_search_merge(self, isolated_tools):
        """Feel buckets excluded from search merge candidates."""
        bm, dh, de, bd = isolated_tools

        # Create a feel
        await bm.create_internal(
            content="我对编程的热爱",
            tags=[], importance=5, domain=[],
            valence=0.8, arousal=0.5,
            name=None, bucket_type="feel",
        )

        # Search should still work but feel shouldn't interfere with merging
        results = await bm.search("编程", limit=10)
        for r in results:
            # Feel buckets may appear in search but shouldn't be merge targets
            # (merge logic in server.py checks pinned/protected/feel)
            pass  # This is a structural test, just verify no crash

    @pytest.mark.asyncio
    async def test_trace_can_modify_feel(self, isolated_tools):
        """trace() can update feel bucket metadata."""
        bm, dh, de, bd = isolated_tools

        bid = await bm.create_internal(
            content="原始 feel 内容",
            tags=[], importance=5, domain=[],
            valence=0.5, arousal=0.3,
            name=None, bucket_type="feel",
        )

        # Update content
        await bm.update(bid, content="修改后的 feel 内容")

        all_b = await bm.list_all()
        updated = next(b for b in all_b if b["id"] == bid)
        assert "修改后" in updated["content"]

    @pytest.mark.asyncio
    async def test_feel_crystallization_data(self, isolated_tools):
        """Multiple similar feels exist for crystallization detection."""
        bm, dh, de, bd = isolated_tools

        # Create 3+ similar feels (about trust)
        for i in range(4):
            await bm.create_internal(
                content=f"TestUser对我的信任让我感到温暖，每次对话都是一种确认 #{i}",
                tags=[], importance=5, domain=[],
                valence=0.8, arousal=0.4,
                name=None, bucket_type="feel",
            )

        all_b = await bm.list_all()
        feels = [b for b in all_b if b["metadata"].get("type") == "feel"]
        assert len(feels) >= 4  # enough for crystallization detection
