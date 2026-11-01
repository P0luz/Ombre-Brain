"""上游两条反馈：报错不说是哪个字段，以及 You 读回看不见自己在攒什么。

## 报错要指名道姓

写入这条路一次查 content / concept_key / concept_value 三个字段，但报错只说
「这条写不进去」。真机上正文完全合规（「他做事犹豫，体制内工作」查下来是
False），踩线的是 `concept_key="personality"`——模型看着那句指向正文的报错，
**连续五次重写正文**，那个键一次都没动。

## You 读回要看得见候选

候选不进召回是对的，但它同时意味着写完就失联：重申要求「同一个 concept_key
+ concept_value 再写一次」，而那两个字符串只存在于写它的那次对话里。them 早
就列了欠账清单，You 一直没有——上游反馈里 You 的抱怨更尖锐，原因就在这。

带 query 的读回**不**附欠账：`recall(query="Lin")` 问的是「我对 Lin 了解
什么」，拿还没算数的候选去回答它是答非所问，这条由 test_you_pipeline 守着。
"""

import tempfile

import pytest

from ombrebrain.them import ThemService, ThemStore
from ombrebrain.them.safety import forbidden_subject_fields as them_fields
from ombrebrain.you import YouService, YouStore
from ombrebrain.you.safety import forbidden_subject_fields as you_fields


class _Buckets:
    def __init__(self):
        self.buckets = {
            f"memory-{i}": {
                "id": f"memory-{i}",
                "content": f"第 {i} 次，老张做事总要想很久，他在体制内。",
                "metadata": {"type": "dynamic"},
            }
            for i in (1, 2)
        }

    async def get(self, bucket_id):
        return self.buckets.get(bucket_id)


class _NoSources:
    def read(self, source_id):
        raise KeyError(source_id)


class _NoLLM:
    def __getattr__(self, name):
        async def _boom(*_a, **_k):
            raise AssertionError("you/them 不允许调用 LLM")

        return _boom


class _Decay:
    @staticmethod
    def calculate_score(_metadata):
        return 1.0


def _them():
    service = ThemService(
        store=ThemStore(tempfile.mkdtemp()),
        bucket_mgr=_Buckets(),
        decay_engine=_Decay(),
        source_store=_NoSources(),
        config={},
    )
    service.dehydrator = _NoLLM()
    service.set_enabled(True)
    return service


def _you():
    service = YouService(
        store=YouStore(tempfile.mkdtemp()),
        bucket_mgr=_Buckets(),
        dehydrator=_NoLLM(),
        source_store=_NoSources(),
    )
    service.set_enabled(True)
    return service


def test_only_the_offending_field_is_named():
    """正文合规、键踩线时，点名的必须是键。"""
    assert them_fields(
        content="他做事犹豫，体制内工作",
        concept_key="personality",
        concept_value="hesitant",
    ) == ["concept_key"]
    assert you_fields(
        content="他要我直接讲结论",
        concept_key="reply_style",
        concept_value="direct",
    ) == []


@pytest.mark.asyncio
async def test_them_rejection_says_which_field_tripped():
    service = _them()

    with pytest.raises(ValueError) as excinfo:
        await service.write(
            content="他做事犹豫，体制内工作",
            bucket_ids=["memory-1", "memory-2"],
            aspect="stable_fact",
            concept_key="personality",
            concept_value="hesitant",
            names=["老张"],
        )

    message = str(excinfo.value)
    assert "concept_key" in message, "没点名踩线的字段，模型只会去重写正文"
    assert "content" not in message, "正文本身合规，不该被牵连点名"


@pytest.mark.asyncio
async def test_you_bare_read_lists_what_it_is_still_accumulating():
    service = _you()
    await service.write(
        content="他要我直接讲结论，别铺垫",
        bucket_ids=["memory-1", "memory-2"],
        aspect="communication_preference",
        concept_key="reply_style",
        concept_value="direct",
        basis="observed_pattern",
    )

    read = await service.recall()

    # 接力需要的三样：还差几天、重申要用的两个键、撤回要用的 id。
    assert "还差" in read
    assert "reply_style=direct" in read, "没有这两个键，换个窗口就接不上了"
    assert "id=" in read
    assert "还没算数" in read, "候选必须明写还没生效，不能读成已成立的认识"


@pytest.mark.asyncio
async def test_you_passive_surfacing_never_carries_the_backlog():
    """欠账是待办，浮现是「想起了什么」，两回事。"""
    service = _you()
    await service.write(
        content="他要我直接讲结论，别铺垫",
        bucket_ids=["memory-1", "memory-2"],
        aspect="communication_preference",
        concept_key="reply_style",
        concept_value="direct",
        basis="observed_pattern",
    )

    assert await service.recall(with_pending=False) == ""
