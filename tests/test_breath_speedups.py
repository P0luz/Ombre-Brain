"""breath_search 提速：ollama 常驻（原生 /api/embed + keep_alive）、含归档列举的按文件缓存。

两项都不许改变结果：向量来自同一模型同一输入；缓存命中返回的桶与逐个解析逐字相同。
"""

import os
from pathlib import Path
from unittest.mock import MagicMock

import frontmatter
import pytest

from embedding_engine import APIEmbeddingEngine


# ---------------- ollama 常驻 ----------------

class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return self._payload


def _engine(api_format="ollama", base_url="http://ombre-ollama:11434/v1"):
    return APIEmbeddingEngine(api_key="ollama", base_url=base_url, model="bge-m3", dim=3,
                              api_format=api_format, keep_alive=-1)


@pytest.mark.asyncio
async def test_ollama_uses_native_endpoint_with_keep_alive(monkeypatch):
    eng = _engine()
    calls = []

    async def post(url, json):
        calls.append((url, json))
        return _Resp(200, {"embeddings": [[0.1, 0.2, 0.3]]})

    monkeypatch.setattr(eng._http, "post", post)
    eng._client.embeddings.create = MagicMock(side_effect=AssertionError("should not hit /v1"))
    assert await eng.generate_async("还记得火锅吗") == [0.1, 0.2, 0.3]
    assert calls == [("http://ombre-ollama:11434/api/embed",
                      {"model": "bge-m3", "input": "还记得火锅吗", "keep_alive": -1})]


@pytest.mark.asyncio
async def test_ollama_native_unavailable_falls_back_once_and_remembers(monkeypatch):
    eng = _engine()
    native_calls = []

    async def post(url, json):
        native_calls.append(url)
        return _Resp(404, {})

    monkeypatch.setattr(eng._http, "post", post)
    item = MagicMock()
    item.embedding = [1.0, 0.0, 0.0]
    response = MagicMock()
    response.data = [item]

    async def create(**kwargs):
        return response

    eng._client.embeddings.create = create
    assert await eng.generate_async("a") == [1.0, 0.0, 0.0]
    assert await eng.generate_async("b") == [1.0, 0.0, 0.0]
    assert len(native_calls) == 1  # 第二次不再试原生接口


@pytest.mark.asyncio
async def test_non_ollama_never_touches_native(monkeypatch):
    eng = _engine(api_format="openai_compat", base_url="https://api.example.com/v1")

    async def post(url, json):
        raise AssertionError("cloud providers must not get /api/embed")

    monkeypatch.setattr(eng._http, "post", post)
    item = MagicMock()
    item.embedding = [0.5, 0.5, 0.0]
    response = MagicMock()
    response.data = [item]

    async def create(**kwargs):
        return response

    eng._client.embeddings.create = create
    assert await eng.generate_async("x") == [0.5, 0.5, 0.0]


# ---------------- 含归档列举的按文件缓存 ----------------

def _write(root, rel, bid, body, **meta):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.dumps(frontmatter.Post(body, id=bid, name=bid, type="dynamic",
                                                       domain=["日常"], **meta)), encoding="utf-8")
    return path


def _snapshot(buckets):
    return sorted((b["id"], b["content"], repr(sorted(b["metadata"].items()))) for b in buckets)


async def _uncached(bm):
    bm._list_all_with_archive_cached = MagicMock(side_effect=RuntimeError("off"))
    try:
        return await bm.list_all(include_archive=True)
    finally:
        del bm._list_all_with_archive_cached


@pytest.mark.asyncio
async def test_archive_cache_tracks_edits_adds_and_deletes(bucket_mgr):
    root = Path(bucket_mgr.base_dir)
    a = _write(root, "dynamic/日常/a_a.md", "a", "第一版")
    _write(root, "archive/日常/b_b.md", "b", "归档的")
    first = await bucket_mgr.list_all(include_archive=True)
    assert _snapshot(first) == _snapshot(await _uncached(bucket_mgr))

    a.write_text(frontmatter.dumps(frontmatter.Post("第二版，长一点", id="a", name="a", type="dynamic",
                                                    domain=["日常"])), encoding="utf-8")
    _write(root, "archive/日常/c_c.md", "c", "新归档")
    os.remove(root / "archive/日常/b_b.md")
    second = await bucket_mgr.list_all(include_archive=True)
    assert _snapshot(second) == _snapshot(await _uncached(bucket_mgr))
    assert {b["id"]: b["content"] for b in second} == {"a": "第二版，长一点", "c": "新归档"}


@pytest.mark.asyncio
async def test_archive_cache_hands_out_copies(bucket_mgr):
    root = Path(bucket_mgr.base_dir)
    _write(root, "archive/日常/b_b.md", "b", "归档的", tags=["x"])
    got = await bucket_mgr.list_all(include_archive=True)
    got[0]["metadata"]["importance"] = 99
    got[0]["score"] = 1
    again = await bucket_mgr.list_all(include_archive=True)
    assert again[0]["metadata"]["importance"] != 99 and "score" not in again[0]


@pytest.mark.asyncio
async def test_archive_cache_failure_falls_back(bucket_mgr, monkeypatch):
    root = Path(bucket_mgr.base_dir)
    _write(root, "archive/日常/b_b.md", "b", "归档的")
    monkeypatch.setattr(bucket_mgr, "_list_all_with_archive_cached", MagicMock(side_effect=OSError("boom")))
    assert [b["id"] for b in await bucket_mgr.list_all(include_archive=True)] == ["b"]


@pytest.mark.asyncio
async def test_breath_search_output_identical_with_and_without_cache(bucket_mgr):
    import tools._runtime as rt
    from tools.breath.search import surface_search

    root = Path(bucket_mgr.base_dir)
    for i, (body, where) in enumerate([
        ("周五和阿树吃了重庆火锅，辣到流泪", "dynamic"), ("周末去爬山看日出", "dynamic"),
        ("火锅底料买了牛油的", "archive"), ("以前学过一阵吉他", "archive"), ("工作日每天开站会", "dynamic"),
    ]):
        _write(root, f"{where}/日常/m{i}_m{i}.md", f"m{i}", body, created=f"2026-10-0{i + 1}T10:00:00")
    rt.config = {"surfacing": {}}
    rt.bucket_mgr = bucket_mgr
    rt.logger = MagicMock()
    rt.fire_webhook = None
    rt.mark_op = None
    queries = ["火锅", "还记得爬山吗", "吉他", "站会"]

    async def run_all():
        return [await surface_search(q, 10, 20000, "", -1, -1, []) for q in queries]

    cached = await run_all()
    cached_again = await run_all()
    bucket_mgr._list_all_with_archive_cached = MagicMock(side_effect=RuntimeError("off"))
    uncached = await run_all()
    assert cached == cached_again == uncached
    assert any("火锅" in out for out in cached)
