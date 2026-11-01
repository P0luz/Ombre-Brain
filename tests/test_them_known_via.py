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
async def test_heard_from_user_is_visible_even_when_the_model_registered_it(tmp_path):
    """rule.md 13.3：分界是「怎么认识的」，不是「谁登记的」。

    人类亲口介绍、模型顺手登记下来的人——按 origin 分会被划进不可见，而撞名
    又挡住人类自己登记，那个人就永远看不到；他说过的话被记成什么样，他自己
    没有任何路径知道。
    """
    service = _enabled(tmp_path)
    await _write(service, known_via=KNOWN_VIA_HEARD_FROM_USER)

    person = _person(service)
    assert person.known_via == KNOWN_VIA_HEARD_FROM_USER
    assert person.origin == ORIGIN_MODEL
    assert person.human_visible is True
    # 出处仍然记着，只是不再决定看得见多少——撞名闸还靠它。
    assert person.human_registered is False


@pytest.mark.asyncio
async def test_a_person_you_introduced_is_readable_and_correctable(tmp_path):
    """人类介绍的人，人类要看得见正文、也留得下纠错。

    这是真机上撞见的那条路：模型顺手登记（origin=model）并标了
    heard_from_user，于是内容不可见；而撞名闸又挡住人类自己去名册登记同名的
    人，两头堵死——人类连自己说过的话被记成什么样都无从知道，更谈不上纠错。
    """
    service = _enabled(tmp_path)
    from ombrebrain.them.service import REQUIRED_CONFIRMATIONS

    for _ in range(REQUIRED_CONFIRMATIONS):
        await _write(service, known_via=KNOWN_VIA_HEARD_FROM_USER)

    条目 = service.list_people()[0]
    assert 条目["origin"] == ORIGIN_MODEL
    assert 条目["known_via"] == KNOWN_VIA_HEARD_FROM_USER
    assert "claims" in 条目, "他说过的话被记成什么样，他自己得看得见"

    # 反馈入口：留言纠错必须开着，否则「看得见」只是只读的展示。
    service.leave_note(条目["person_id"], "她其实会先铺垫一句再讲结论")
    assert service.list_people()[0]["pending_notes"], "留言没落到人身上"


@pytest.mark.asyncio
async def test_met_myself_refuses_notes(tmp_path):
    """看不见的那一档不开留言：对着看不见的东西提意见不是纠错。"""
    service = _enabled(tmp_path)
    await _write(service, known_via=KNOWN_VIA_MET_MYSELF)

    person = _person(service)
    with pytest.raises(ValueError) as excinfo:
        service.leave_note(person.id, "你记错了")
    assert "无从纠起" in str(excinfo.value)


@pytest.mark.asyncio
async def test_met_myself_stays_private(tmp_path):
    """第一手的印象不出这层边界，人类只看得到称呼。"""
    service = _enabled(tmp_path)
    await _write(service, known_via=KNOWN_VIA_MET_MYSELF)

    person = _person(service)
    assert person.human_visible is False


@pytest.mark.asyncio
async def test_human_registered_person_is_heard_from_user_and_visible(tmp_path):
    service = _enabled(tmp_path)
    service.add_person(["Iris"])

    person = [p for p in service.store.list_persons(service.status().scope)][0]
    assert person.origin == ORIGIN_HUMAN
    assert person.known_via == KNOWN_VIA_HEARD_FROM_USER
    assert person.human_visible is True


@pytest.mark.asyncio
async def test_roster_carries_known_via_so_the_dashboard_can_group_by_it(tmp_path):
    """名册要按「怎么认识的」分栏，那 `list_people` 就得把这个字段发出去。

    前端一度只能看到 `origin`（谁登记的），于是模型登记、明写了
    `heard_from_user` 的人照样被划进「自己遇到的」那一栏。
    """
    service = _enabled(tmp_path)
    await _write(service, known_via=KNOWN_VIA_HEARD_FROM_USER)
    service.add_person(["Iris"])

    roster = {row["names"][0]: row for row in service.list_people()}

    模型登记的 = next(row for row in roster.values() if row["origin"] == ORIGIN_MODEL)
    assert 模型登记的["known_via"] == KNOWN_VIA_HEARD_FROM_USER
    # 标了「听你说的」，正文就该出这个接口——那些话本来就是人类说的。
    assert "claims" in 模型登记的

    assert roster["Iris"]["known_via"] == KNOWN_VIA_HEARD_FROM_USER
    assert roster["Iris"]["origin"] == ORIGIN_HUMAN
    assert "claims" in roster["Iris"]


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
