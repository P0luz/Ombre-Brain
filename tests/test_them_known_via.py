import json

import pytest

from ombrebrain.them import Person, ThemService, ThemStore
from ombrebrain.them.models import (
    KNOWN_VIA_HEARD_FROM_USER,
    KNOWN_VIA_MET_MYSELF,
    ORIGIN_HUMAN,
    ORIGIN_MODEL,
)


class FakeBucketManager:
    def __init__(self):
        self.buckets = {}

    async def get(self, bucket_id):
        return self.buckets.get(bucket_id)


class FakeSourceStore:
    def read(self, source_id):
        raise KeyError(source_id)


class RealisticDecay:
    @staticmethod
    def calculate_score(metadata):
        return float(metadata.get("activation_count") or 1)


class ExplodingLLM:
    def __getattr__(self, name):
        async def _boom(*_args, **_kwargs):
            raise AssertionError(f"them 不允许调用 LLM，却调了 {name}")

        return _boom


def _enabled(tmp_path):
    manager = FakeBucketManager()
    service = ThemService(
        store=ThemStore(tmp_path),
        bucket_mgr=manager,
        decay_engine=RealisticDecay(),
        source_store=FakeSourceStore(),
        config={},
    )
    service.dehydrator = ExplodingLLM()
    service.set_enabled(True)
    for index in (1, 2):
        bucket_id = f"memory-{index}"
        manager.buckets[bucket_id] = {
            "id": bucket_id,
            "content": f"第 {index} 次，Zoey 讲话都是直奔结论。",
            "metadata": {"type": "dynamic"},
        }
    return service


async def _write(service, **overrides):
    payload = {
        "content": "她讲话直奔结论，不铺垫",
        "bucket_ids": ["memory-1", "memory-2"],
        "aspect": "communication_preference",
        "concept_key": "talk_style",
        "concept_value": "blunt",
        "names": ["Zoey"],
    }
    payload.update(overrides)
    return await service.write(**payload)


def _person(service):
    scope = service.status().scope
    return service.store.list_persons(scope)[0]


@pytest.mark.asyncio
async def test_model_written_person_defaults_to_met_myself(tmp_path):
    service = _enabled(tmp_path)
    await _write(service)

    assert _person(service).known_via == KNOWN_VIA_MET_MYSELF


@pytest.mark.asyncio
async def test_write_can_declare_heard_from_user(tmp_path):
    service = _enabled(tmp_path)
    await _write(service, known_via=KNOWN_VIA_HEARD_FROM_USER)

    assert _person(service).known_via == KNOWN_VIA_HEARD_FROM_USER


@pytest.mark.asyncio
async def test_a_later_write_corrects_a_wrong_known_via(tmp_path):
    service = _enabled(tmp_path)
    await _write(service)
    assert _person(service).known_via == KNOWN_VIA_MET_MYSELF

    await _write(service, known_via=KNOWN_VIA_HEARD_FROM_USER)

    assert _person(service).known_via == KNOWN_VIA_HEARD_FROM_USER


@pytest.mark.asyncio
async def test_omitting_known_via_leaves_it_alone(tmp_path):
    service = _enabled(tmp_path)
    await _write(service, known_via=KNOWN_VIA_HEARD_FROM_USER)

    await _write(service)

    assert _person(service).known_via == KNOWN_VIA_HEARD_FROM_USER


@pytest.mark.asyncio
async def test_bad_known_via_names_the_allowed_values(tmp_path):
    service = _enabled(tmp_path)

    with pytest.raises(ValueError) as excinfo:
        await _write(service, known_via="nonsense")

    message = str(excinfo.value)
    assert KNOWN_VIA_MET_MYSELF in message
    assert KNOWN_VIA_HEARD_FROM_USER in message


@pytest.mark.asyncio
async def test_known_via_does_not_change_human_visibility(tmp_path):
    """拆分的全部意义：标成「只听说过」不能顺带把私有认识交给人类。"""
    service = _enabled(tmp_path)
    await _write(service, known_via=KNOWN_VIA_HEARD_FROM_USER)

    person = _person(service)
    assert person.known_via == KNOWN_VIA_HEARD_FROM_USER
    assert person.origin == ORIGIN_MODEL
    assert person.human_visible is False


@pytest.mark.asyncio
async def test_human_registered_person_is_heard_from_user_and_visible(tmp_path):
    service = _enabled(tmp_path)
    service.add_person(["Iris"])

    person = [p for p in service.store.list_persons(service.status().scope)][0]
    assert person.origin == ORIGIN_HUMAN
    assert person.known_via == KNOWN_VIA_HEARD_FROM_USER
    assert person.human_visible is True


def _age_receipts(service, claim):
    from dataclasses import replace

    receipts = tuple(
        replace(receipt, reviewed_at=f"2026-08-{10 + index:02d}T10:00:00+00:00")
        for index, receipt in enumerate(claim.review_receipts)
    )
    return service.store.put_claim(
        replace(claim, review_receipts=receipts), expected_revision=claim.revision
    )


@pytest.mark.asyncio
async def test_recall_reports_the_field_not_the_derivation(tmp_path):
    """已生效条目的 known_via 来自字段本身，而不是从 origin 推。

    这个人是模型自己写下的（origin=model），推导版本必然报 met_myself；
    只有真读字段才会是 heard_from_user。
    """
    service = _enabled(tmp_path)
    from ombrebrain.them.service import REQUIRED_CONFIRMATIONS

    claim = None
    for _ in range(REQUIRED_CONFIRMATIONS):
        claim, _ = await _write(service, known_via=KNOWN_VIA_HEARD_FROM_USER)
        claim = _age_receipts(
            service, service.store.get_claim(service.status().scope, claim.id)
        )
    await _write(service, known_via=KNOWN_VIA_HEARD_FROM_USER)

    payload = json.loads(
        (await service.recall()).split("```json", 1)[1].split("```", 1)[0]
    )

    assert payload["them"][0]["known_via"] == KNOWN_VIA_HEARD_FROM_USER


def test_legacy_person_without_the_field_keeps_old_behaviour(tmp_path):
    """存量数据没有这个字段，按老规则从 origin 推一次，表现逐字不变。"""
    model_side = Person(id="person_" + "0" * 32, names=("A",), origin=ORIGIN_MODEL)
    human_side = Person(id="person_" + "1" * 32, names=("B",), origin=ORIGIN_HUMAN)

    assert model_side.known_via == KNOWN_VIA_MET_MYSELF
    assert human_side.known_via == KNOWN_VIA_HEARD_FROM_USER


def test_legacy_payload_roundtrips(tmp_path):
    """老 payload_json 里没有 known_via 键，反序列化不能炸。"""
    store = ThemStore(tmp_path)
    payload = json.loads(
        json.dumps(
            {
                "id": "person_" + "2" * 32,
                "names": ["C"],
                "origin": ORIGIN_MODEL,
            }
        )
    )
    person = Person(**payload)

    assert person.known_via == KNOWN_VIA_MET_MYSELF
    assert store is not None
