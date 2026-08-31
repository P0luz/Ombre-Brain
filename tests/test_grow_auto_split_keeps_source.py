"""grow 自动拆分也必须把原话存下来。

以前这条路只把整段 content 喂给 LLM，拆完就丢：桶里留下的全是 LLM
改写过的话，原话一个字都不在，事后无从核对它有没有记岔。
真机验证过——DS 拆一段日记出来 4 个桶，source_id / source_ranges 一个都没有。

items 那条路（模型自己拆好传进来）早就有完整机制：原文存一份，
每个桶用行号区间指回自己那几行。这里补的是同一套，不新造第二种存法。

**LLM 碰不到原文**：它只看到带行号的输入、只能报行号，原话由系统逐字去取。
「禁止压缩原句」因此是结构性的，不靠它自觉。
"""

import pytest

from tools.grow import core as grow_core_mod


class _FakeSourceStore:
    def __init__(self):
        self.saved = []

    def put(self, content):
        self.saved.append(content)
        return f"ref{len(self.saved)}"


class _FakeDehydrator:
    """返回固定的拆分结果，其中一条故意报了越界行号。"""

    api_available = True

    def __init__(self, items):
        self._items = items
        self.seen_input = None

    async def digest(self, content):
        self.seen_input = content
        return self._items

    async def analyze(self, content):
        return {"tags": [], "domain": ["未分类"], "valence": 0.5, "arousal": 0.3}


@pytest.fixture()
def 装配(monkeypatch):
    调用记录 = []

    async def _fake_merge_or_create(**kwargs):
        调用记录.append(kwargs)
        return kwargs.get("name") or "桶", False, ""

    store = _FakeSourceStore()
    import logging
    monkeypatch.setattr(grow_core_mod.rt, "logger", logging.getLogger("test"), raising=False)
    monkeypatch.setattr(grow_core_mod, "merge_or_create", _fake_merge_or_create)
    monkeypatch.setattr(grow_core_mod.rt, "source_store", store, raising=False)
    return 调用记录, store


@pytest.mark.asyncio
async def test_自动拆分把原文存一份并让每个桶指回去(装配, monkeypatch):
    调用记录, store = 装配
    dehy = _FakeDehydrator([
        {"name": "开会", "content": "早上定了方案。" * 6, "source_ranges": [[1, 1]]},
        {"name": "午饭", "content": "中午聊到换组的事。" * 6, "source_ranges": [[2, 2]]},
    ])
    monkeypatch.setattr(grow_core_mod.rt, "dehydrator", dehy, raising=False)

    原文 = "早上开会定了方案\n中午和 Zoey 吃饭聊到她要换组"
    await grow_core_mod.grow_core(原文)

    assert store.saved == [原文], "原文只该存一份，且逐字保存"
    refs = [k.get("source_refs") for k in 调用记录]
    assert all(r for r in refs), "每个桶都要挂上原文引用"
    assert refs[0][0]["ranges"] == [(1, 1)] or refs[0][0]["ranges"] == [[1, 1]]


@pytest.mark.asyncio
async def test_喂给LLM的原文带行号(装配, monkeypatch):
    """prompt 要它报行号，就必须让它看得见行号——否则只能瞎猜。"""
    _, _ = 装配
    dehy = _FakeDehydrator([{"name": "x", "content": "内容" * 30}])
    monkeypatch.setattr(grow_core_mod.rt, "dehydrator", dehy, raising=False)
    await grow_core_mod.grow_core("第一行\n第二行")
    # grow_core 直接把原文交给 dehydrator.digest；加行号发生在 dehydrator 内部，
    # 这里确认 grow 没有擅自改写原文——它必须原样交出去
    assert dehy.seen_input == "第一行\n第二行"


@pytest.mark.asyncio
async def test_越界行号宁可不存也不存错原话(装配, monkeypatch):
    """LLM 报错了行时，与其存一段不相干的原话，不如这条没有佐证。"""
    调用记录, _ = 装配
    dehy = _FakeDehydrator([
        {"name": "越界", "content": "内容" * 30, "source_ranges": [[99, 200]]},
    ])
    monkeypatch.setattr(grow_core_mod.rt, "dehydrator", dehy, raising=False)
    await grow_core_mod.grow_core("只有一行")
    ranges = 调用记录[0]["source_refs"][0]["ranges"]
    assert ranges == [], f"越界行号必须丢弃，实际留下了 {ranges}"


@pytest.mark.asyncio
async def test_原文存不下时记忆照常入库(装配, monkeypatch):
    """佐证存不下不该让整批记忆一起丢——正文优先。"""
    调用记录, store = 装配

    def _boom(_content):
        raise OSError("disk full")

    store.put = _boom
    dehy = _FakeDehydrator([{"name": "x", "content": "内容" * 30}])
    monkeypatch.setattr(grow_core_mod.rt, "dehydrator", dehy, raising=False)
    await grow_core_mod.grow_core("一行")
    assert len(调用记录) == 1, "记忆本身必须照常写入"
    assert 调用记录[0].get("source_refs") is None


def test_digest解析不能把source_ranges滤掉():
    """`_parse_digest` 的 validated 是**显式字段白名单**，不列进去就带不出来。

    真机上就是这么栽的：prompt 要了行号、DeepSeek 也老老实实给了，
    结果全被这个白名单滤掉，桶里 ranges 全是空的——而单元测试当时全绿，
    因为测试喂的是假 dehydrator，根本不走 _parse_digest。
    """
    import json

    from dehydrator import Dehydrator

    原始 = json.dumps([{
        "name": "开会",
        "content": "早上开会定了方案，讨论了退化路径。" * 3,
        "source_ranges": [[1, 2]],
        "importance": 5,
    }], ensure_ascii=False)
    出 = Dehydrator._parse_digest(Dehydrator.__new__(Dehydrator), 原始)
    assert 出, "解析结果不该为空"
    assert 出[0].get("source_ranges") == [[1, 2]], (
        f"source_ranges 被白名单滤掉了，实际拿到 {出[0].get('source_ranges')!r}"
    )
