"""breath catalog 目录模式测试。

目录模式的三条硬承诺（token 经济性的根基）：
1. 0 次 LLM/embedding 调用——dehydrator/embedding 炸了也照常出目录；
2. 每桶一行、只含元数据（名称|域|重要度），不带正文；
3. dispatch(catalog=True) 最先短路，不受 query/importance_min 等其他参数干扰。
"""
from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from tools.breath import dispatch
from tools.breath.catalog import surface_catalog


class ExplodingDehydrator:
    """catalog 承诺 0 LLM 调用——谁调谁炸。"""

    async def dehydrate(self, content, meta=None):
        raise AssertionError("catalog 模式不允许调用 LLM")


class ExplodingEmbedding:
    enabled = True

    async def search_similar(self, query, top_k=20):
        raise AssertionError("catalog 模式不允许调用 embedding")


class NoopDecay:
    is_running = True

    async def ensure_started(self):
        return None

    def calculate_score(self, meta):
        return 1.0


def install_runtime(bucket_mgr):
    rt.config = {"surfacing": {}}
    rt.bucket_mgr = bucket_mgr
    rt.decay_engine = NoopDecay()
    rt.dehydrator = ExplodingDehydrator()
    rt.embedding_engine = ExplodingEmbedding()
    rt.logger = MagicMock()
    rt.fire_webhook = None
    rt.mark_op = None


@pytest.mark.asyncio
async def test_catalog_one_line_per_bucket_no_content(bucket_mgr):
    await bucket_mgr.create(
        content="这段正文绝不能出现在目录里",
        name="项目约定",
        domain=["工作"],
        importance=9,
    )
    await bucket_mgr.create(
        content="另一段私密正文",
        name="旅行计划",
        domain=["生活"],
        importance=5,
    )
    install_runtime(bucket_mgr)

    out = await surface_catalog()

    # 元数据都在
    assert "项目约定 | 工作 | 9" in out
    assert "旅行计划 | 生活 | 5" in out
    # 正文一个字都不带
    assert "绝不能出现" not in out
    assert "私密正文" not in out
    # 总数
    assert "2 桶" in out


@pytest.mark.asyncio
async def test_catalog_sorted_by_importance_desc(bucket_mgr):
    await bucket_mgr.create(content="低", name="低重", importance=3, domain=["a"])
    await bucket_mgr.create(content="高", name="高重", importance=9, domain=["a"])
    install_runtime(bucket_mgr)

    out = await surface_catalog()
    assert out.index("高重") < out.index("低重")


@pytest.mark.asyncio
async def test_catalog_domain_filter(bucket_mgr):
    await bucket_mgr.create(content="x", name="工作项", domain=["工作"], importance=5)
    await bucket_mgr.create(content="y", name="生活项", domain=["生活"], importance=5)
    install_runtime(bucket_mgr)

    out = await surface_catalog(domain_filter=["工作"])
    assert "工作项" in out
    assert "生活项" not in out


@pytest.mark.asyncio
async def test_catalog_marks_pinned(bucket_mgr):
    await bucket_mgr.create(content="核心", name="核心准则", pinned=True)
    install_runtime(bucket_mgr)

    out = await surface_catalog()
    # pinned 桶创建时名字会带时间戳前缀，所以分开断言：📌 标记在行内、名字在行内
    line = next(row for row in out.split("\n") if "核心准则" in row)
    assert line.startswith("📌")


@pytest.mark.asyncio
async def test_catalog_marks_anchor_and_release_removes_marker(bucket_mgr):
    bucket_id = await bucket_mgr.create(
        content="catalog must not expose this anchor body",
        name="坐标系",
        domain=["关系"],
        importance=9,
    )
    result = await bucket_mgr.set_anchor(bucket_id, True)
    assert result["ok"] is True
    install_runtime(bucket_mgr)

    anchored = await dispatch(catalog=True)
    anchor_line = next(row for row in anchored.splitlines() if "坐标系" in row)

    assert "⚓ [anchor]" in anchor_line
    assert "catalog must not expose this anchor body" not in anchored

    result = await bucket_mgr.set_anchor(bucket_id, False)
    assert result["ok"] is True
    released = await surface_catalog()
    released_line = next(row for row in released.splitlines() if "坐标系" in row)

    assert "⚓ [anchor]" not in released_line


@pytest.mark.asyncio
async def test_catalog_parses_anchor_marker_as_explicit_boolean():
    class Snapshot:
        def summary(self, _bucket_id, _meta):
            return "👣 Footprint：无"

    class FakeBucketManager:
        async def list_all(self, include_archive=False):
            return [
                {
                    "id": "anchor-true",
                    "content": "hidden body true",
                    "metadata": {
                        "name": "真锚点",
                        "type": "permanent",
                        "domain": ["测试"],
                        "importance": 9,
                        "pinned": "true",
                        "anchor": "true",
                    },
                },
                {
                    "id": "anchor-false",
                    "content": "hidden body false",
                    "metadata": {
                        "name": "假锚点",
                        "type": "dynamic",
                        "domain": ["测试"],
                        "importance": 5,
                        "anchor": "false",
                    },
                },
            ]

        def footprint_snapshot(self):
            return Snapshot()

    install_runtime(FakeBucketManager())

    out = await surface_catalog()
    true_line = next(row for row in out.splitlines() if "真锚点" in row)
    false_line = next(row for row in out.splitlines() if "假锚点" in row)

    assert true_line.startswith("📌⚓ [anchor]")
    assert "⚓ [anchor]" not in false_line
    assert "hidden body" not in out


@pytest.mark.asyncio
async def test_dispatch_catalog_short_circuits_other_params(bucket_mgr):
    """catalog=True 时 query/importance_min 一概不生效，也绝不触发 LLM/向量。"""
    await bucket_mgr.create(content="正文", name="目录项", domain=["a"], importance=5)
    install_runtime(bucket_mgr)

    out = await dispatch(query="随便搜点什么", importance_min=9, catalog=True)

    # 走的是目录（若走了 search/importance 分支，Exploding* 会 AssertionError）
    assert "记忆目录" in out
    assert "目录项" in out


@pytest.mark.asyncio
async def test_dispatch_catalog_respects_domain_filter(bucket_mgr):
    await bucket_mgr.create(content="x", name="工作项", domain=["工作"], importance=5)
    await bucket_mgr.create(content="y", name="生活项", domain=["生活"], importance=5)
    install_runtime(bucket_mgr)

    out = await dispatch(domain="工作", catalog=True)
    assert "工作项" in out
    assert "生活项" not in out


@pytest.mark.asyncio
async def test_dispatch_catalog_respects_tags_and_max_results(bucket_mgr):
    await bucket_mgr.create(content="x", name="命中高", tags=["拥抱"], importance=9)
    await bucket_mgr.create(content="y", name="命中低", tags=["拥抱"], importance=5)
    await bucket_mgr.create(content="z", name="不命中", tags=["其他"], importance=10)
    install_runtime(bucket_mgr)

    out = await dispatch(tags="拥抱", catalog=True, max_results=1)

    assert "命中高" in out
    assert "命中低" not in out
    assert "不命中" not in out
    assert "1 桶" in out


@pytest.mark.asyncio
async def test_dispatch_catalog_missing_tag_returns_empty(bucket_mgr):
    await bucket_mgr.create(content="x", name="目录项", tags=["已存在"])
    install_runtime(bucket_mgr)

    out = await dispatch(tags="definitely_missing", catalog=True, max_results=5)

    assert "没有匹配过滤条件" in out
    assert "目录项" not in out


@pytest.mark.asyncio
async def test_catalog_empty_library(bucket_mgr):
    install_runtime(bucket_mgr)
    assert "记忆库为空" in await surface_catalog()
