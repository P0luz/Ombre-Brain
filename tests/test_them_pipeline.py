"""them 的写入、姓名命中、衰减排序与配额：模型自己写，全程不碰 LLM。

这些用例锁的是 rule.md 13.3 那几条边界：形态同 You（两桶 + 三日）、
只记这个人本身不描述关系、姓名命中任一别名即返回、按提及自然衰减、
每人配额满了系统只挡不代压。
"""

import json

import pytest

from ombrebrain.them import Person, ThemService, ThemStore, ThemStoreError
from ombrebrain.them.service import (
    MAX_CANDIDATES_PER_PERSON,
    MAX_SURFACED_PERSONS,
    MIN_SUPPORTING_BUCKETS,
    REQUIRED_CONFIRMATIONS,
)


class FakeBucketManager:
    def __init__(self):
        self.buckets = {}

    async def get(self, bucket_id):
        return self.buckets.get(bucket_id)


class FakeSourceStore:
    def __init__(self):
        self.sources = {}

    def read(self, source_id):
        return self.sources[source_id]


class RealisticDecay:
    """按 last_active 与 activation_count 排序，够用来验「常被提起的排前面」。

    them 复用的是 decay_engine.calculate_score，这里只需要一个单调一致的替身。
    """

    @staticmethod
    def calculate_score(metadata):
        return float(metadata.get("activation_count") or 1)


class ExplodingLLM:
    """任何一次 LLM 调用都会炸。

    them 与 you 同一条规矩：不许有自动抽取 / 自动复核 / 自动摘要。做成地雷
    而不是空壳，是为了让「哪天有人把 LLM 接回来」当场变红灯。
    """

    def __getattr__(self, name):
        async def _boom(*_args, **_kwargs):
            raise AssertionError(f"them 不允许调用 LLM，却调了 {name}")

        return _boom


def _service(tmp_path, **config):
    manager = FakeBucketManager()
    service = ThemService(
        store=ThemStore(tmp_path),
        bucket_mgr=manager,
        decay_engine=RealisticDecay(),
        source_store=FakeSourceStore(),
        config=config,
    )
    service.dehydrator = ExplodingLLM()
    return service, manager


def _bucket(bucket_id, content, **metadata):
    return {"id": bucket_id, "content": content, "metadata": {"type": "dynamic", **metadata}}


def _enabled(tmp_path, *, buckets=2, **config):
    service, manager = _service(tmp_path, **config)
    service.set_enabled(True)
    for index in range(1, buckets + 1):
        bucket_id = f"memory-{index}"
        manager.buckets[bucket_id] = _bucket(
            bucket_id, f"第 {index} 次，Zoey 讲话都是直奔结论。"
        )
    return service, manager


def _write(service, **overrides):
    payload = {
        "content": "她讲话直奔结论，不铺垫",
        "bucket_ids": ["memory-1", "memory-2"],
        "aspect": "communication_preference",
        "concept_key": "talk_style",
        "concept_value": "blunt",
        "names": ["Zoey"],
    }
    payload.update(overrides)
    return service.write(**payload)


def _age_receipts(service, claim):
    """把已有收据的日期改早，模拟「那是前几天记的」。

    改的是测试数据的时间戳，不是绕过判定——三日门槛本身照常由代码算。
    """
    from dataclasses import replace

    receipts = tuple(
        replace(receipt, reviewed_at=f"2026-08-{10 + index:02d}T10:00:00+00:00")
        for index, receipt in enumerate(claim.review_receipts)
    )
    return service.store.put_claim(
        replace(claim, review_receipts=receipts), expected_revision=claim.revision
    )


class TestGates:
    @pytest.mark.asyncio
    async def test_单个出处被拒(self, tmp_path):
        service, _ = _enabled(tmp_path)
        with pytest.raises(ValueError, match="不能只有一个出处"):
            await _write(service, bucket_ids=["memory-1"])

    @pytest.mark.asyncio
    async def test_不存在的桶被拒(self, tmp_path):
        service, _ = _enabled(tmp_path)
        with pytest.raises(ValueError, match="找不到记忆桶"):
            await _write(service, bucket_ids=["memory-1", "nope"])

    @pytest.mark.asyncio
    async def test_首写只落候选(self, tmp_path):
        service, _ = _enabled(tmp_path)
        claim, message = await _write(service)
        assert claim.lifecycle == "candidate"
        assert "候选" in message
        assert f"另外 {REQUIRED_CONFIRMATIONS - 1} 个" in message

    @pytest.mark.asyncio
    async def test_同日重申不推进(self, tmp_path):
        service, _ = _enabled(tmp_path)
        await _write(service)
        claim, _ = await _write(service)
        assert claim.review_date_count == 1
        assert claim.lifecycle == "candidate"

    @pytest.mark.asyncio
    async def test_三个不同自然日才转正(self, tmp_path):
        service, _ = _enabled(tmp_path)
        claim, _ = await _write(service)
        for _ in range(REQUIRED_CONFIRMATIONS - 1):
            claim = _age_receipts(service, claim)
            claim, _ = await _write(service)
        assert claim.review_date_count == REQUIRED_CONFIRMATIONS
        assert claim.lifecycle == "formal"
        assert claim.independent_support_count >= MIN_SUPPORTING_BUCKETS

    @pytest.mark.asyncio
    async def test_候选不作为认识被召回(self, tmp_path):
        """候选不进 them 块——还没算数的东西不该被当认识用。

        但它会出现在**欠账清单**里（见 TestPendingDigest）：那两件事必须分清，
        不然「看得见自己写过什么」和「可以拿它当已成立的判断」就混成一件事了。
        """
        service, _ = _enabled(tmp_path)
        await _write(service)
        输出 = await service.recall(query="Zoey")
        assert "关于**别人**的长期认识" not in 输出  # them 块的块头没出现
        assert '"them"' not in 输出

    @pytest.mark.asyncio
    async def test_候选不进浮现(self, tmp_path):
        """breath / dream 那条路一个字都不该有：浮现是想起了什么，不是待办。"""
        service, _ = _enabled(tmp_path)
        await _write(service)
        assert await service.surface(query="Zoey") == ""


class TestPendingDigest:
    """还在攒的候选要看得见，否则三日门槛根本走不完。

    候选不进召回是对的，但它同时意味着写完就失联——重申要求「同一个
    concept_key + concept_value 再写一次」，而那两个字符串只存在于写它的
    那次对话里。跨会话之后模型记不得自己填过什么，那条候选就永远停在原地。

    真机试用时就是这么发现的：写完一条候选，读回是空的，再没有任何入口
    能问出「我有哪些在攒的」。
    """

    @pytest.mark.asyncio
    async def test_显式读回时给出欠账与重申用的键(self, tmp_path):
        service, _ = _enabled(tmp_path)
        输出 = await service.recall(query="Zoey")
        assert "还没算数" not in 输出  # 还没写，自然没有欠账

        await _write(service)
        输出 = await service.recall(query="Zoey")
        assert "还没算数" in 输出
        assert "talk_style=blunt" in 输出  # 重申要用的两个键
        assert "还差 2 个不同的日子" in 输出

    @pytest.mark.asyncio
    async def test_无query时列出全部候选而不只是前三人(self, tmp_path):
        """欠账清单不受浮现的前三名额限制——那是待办，不是浮现。"""
        service, manager = _enabled(tmp_path, buckets=2)
        for index in range(MAX_SURFACED_PERSONS + 2):
            名 = f"人{index}"
            for 桶 in ("a", "b"):
                bid = f"{名}-{桶}"
                manager.buckets[bid] = _bucket(bid, f"{名}又一次这样做。")
            await _write(
                service, names=[名], bucket_ids=[f"{名}-a", f"{名}-b"],
                concept_key=f"trait_{index}", content=f"关于{名}的一个判断",
            )
        输出 = await service.recall()
        for index in range(MAX_SURFACED_PERSONS + 2):
            assert f"人{index}" in 输出

    @pytest.mark.asyncio
    async def test_转正之后就不在欠账里了(self, tmp_path):
        service, _ = _enabled(tmp_path)
        claim, _ = await _write(service)
        for _ in range(REQUIRED_CONFIRMATIONS - 1):
            claim = _age_receipts(service, claim)
            claim, _ = await _write(service)
        输出 = await service.recall(query="Zoey")
        assert "还没算数" not in 输出
        assert "直奔结论" in 输出  # 已经作为认识出现了

    @pytest.mark.asyncio
    async def test_候选条数有上限(self, tmp_path):
        service, _ = _enabled(tmp_path)
        for index in range(MAX_CANDIDATES_PER_PERSON):
            await _write(service, concept_key=f"trait_{index:02d}", content=f"特点 {index}")
        with pytest.raises(ValueError, match="候选已经有"):
            await _write(service, concept_key="one_too_many", content="再多一条")


class TestNameInBucket:
    """poluz 2026-08-20：每个依据桶的正文里都必须出现这个人的名字。

    要论证关于 Zoey 的事，每一条依据都该提到 Zoey——否则「关于这个人的认识」
    和「碰巧同时发生的别的事」就没有区别，凑出两个出处也太容易了。
    """

    @pytest.mark.asyncio
    async def test_桶里没这个人名就写不进去(self, tmp_path):
        service, manager = _enabled(tmp_path)
        manager.buckets["memory-2"] = _bucket("memory-2", "她又一次直接给了结论。")
        with pytest.raises(ValueError, match="都没有出现"):
            await _write(service)

    @pytest.mark.asyncio
    async def test_定的是每个桶都要有不是至少一个(self, tmp_path):
        """严在这里是有意的：一条依据自己都指不明白是谁，就不该拿来撑判断。"""
        service, manager = _enabled(tmp_path, buckets=3)
        manager.buckets["memory-3"] = _bucket("memory-3", "他后来又提了一次。")
        with pytest.raises(ValueError, match="memory-3"):
            await _write(service, bucket_ids=["memory-1", "memory-2", "memory-3"])

    @pytest.mark.asyncio
    async def test_昵称命中也算(self, tmp_path):
        service, manager = _enabled(tmp_path)
        manager.buckets["memory-2"] = _bucket("memory-2", "小 Z 又一次直接给了结论。")
        claim, _ = await _write(service, names=["Zoey", "小 Z"])
        assert claim.independent_support_count == 2

    @pytest.mark.asyncio
    async def test_大小写不影响命中(self, tmp_path):
        service, manager = _enabled(tmp_path)
        manager.buckets["memory-2"] = _bucket("memory-2", "ZOEY said it again.")
        claim, _ = await _write(service)
        assert claim.independent_support_count == 2

    @pytest.mark.asyncio
    async def test_双链里的名字也算(self, tmp_path):
        service, manager = _enabled(tmp_path)
        manager.buckets["memory-2"] = _bucket("memory-2", "和 [[Zoey]] 又聊了一次。")
        claim, _ = await _write(service)
        assert claim.independent_support_count == 2


class TestRenameByHuman:
    """人类唯一碰得到的东西：称呼。改完提醒模型一次。"""

    @pytest.mark.asyncio
    async def test_模型认识的人只给称呼不给认识(self, tmp_path):
        """rule.md 13.3：模型自己认出来的人，人类只看得见称呼。"""
        service, _ = _enabled(tmp_path)
        await _write(service)
        名册 = service.list_people()
        assert len(名册) == 1
        assert 名册[0]["origin"] == "model"
        assert "claims" not in 名册[0]
        assert "直奔结论" not in json.dumps(名册, ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_改名之后下次浮现提醒一次(self, tmp_path):
        service, _ = _enabled(tmp_path)
        await TestRecall()._formalized(service)
        scope = service.status().scope
        person = service.store.find_person_by_name(scope, "Zoey")
        service.rename_person(person.id, ["Zoey Chen", "小 Z"])

        第一次 = await service.recall(query="Zoey Chen")
        assert "信息变迁" in 第一次
        assert "Zoey" in 第一次 and "Zoey Chen" in 第一次  # 新旧对照都给

        第二次 = await service.recall(query="Zoey Chen")
        assert "信息变迁" not in 第二次  # 只念一次

    @pytest.mark.asyncio
    async def test_提醒不看命中(self, tmp_path):
        """久不被提起的人改了名，提醒也得出来；挂在命中上可能几个月都不出现。"""
        service, _ = _enabled(tmp_path)
        await TestRecall()._formalized(service)
        scope = service.status().scope
        person = service.store.find_person_by_name(scope, "Zoey")
        service.rename_person(person.id, ["Zoey Chen"])

        输出 = await service.recall(query="今天天气不错")
        assert "信息变迁" in 输出

    @pytest.mark.asyncio
    async def test_改名不算一次提及(self, tmp_path):
        """人类整理名册不是「这个人被提起了」，算成提及会凭空抬高衰减权重。"""
        service, _ = _enabled(tmp_path)
        await _write(service)
        scope = service.status().scope
        person = service.store.find_person_by_name(scope, "Zoey")
        改后 = service.rename_person(person.id, ["Zoey Chen"])
        assert 改后.activation_count == person.activation_count
        assert 改后.last_active == person.last_active

    @pytest.mark.asyncio
    async def test_认识正文一个字都不动(self, tmp_path):
        service, _ = _enabled(tmp_path)
        claim, _ = await _write(service)
        scope = service.status().scope
        person = service.store.find_person_by_name(scope, "Zoey")
        service.rename_person(person.id, ["Zoey Chen"])
        after = service.store.get_claim(scope, claim.id)
        assert after.content == claim.content

    @pytest.mark.asyncio
    async def test_称呼撞车被拒(self, tmp_path):
        """两个人共用一个名字，姓名命中会同时把两份认识都拉出来。"""
        service, manager = _enabled(tmp_path, buckets=4)
        manager.buckets["memory-3"] = _bucket("memory-3", "阿哲说了同样的话。")
        manager.buckets["memory-4"] = _bucket("memory-4", "阿哲又说了一次。")
        await _write(service)
        await _write(
            service, names=["阿哲"], bucket_ids=["memory-3", "memory-4"],
            concept_key="other_style", content="他讲话也很直接",
        )
        scope = service.status().scope
        阿哲 = service.store.find_person_by_name(scope, "阿哲")
        with pytest.raises(ValueError, match="已经用了其中一个称呼"):
            service.rename_person(阿哲.id, ["Zoey"])

    @pytest.mark.asyncio
    async def test_不能把称呼清空(self, tmp_path):
        service, _ = _enabled(tmp_path)
        await _write(service)
        scope = service.status().scope
        person = service.store.find_person_by_name(scope, "Zoey")
        with pytest.raises(ValueError, match="至少要留一个称呼"):
            service.rename_person(person.id, [])


class TestNoRelationships:
    """rule.md 13.3：只记这个人本身，不描述任何关系。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "content",
        [
            "她和我关系很好",
            "他们两人之间有点僵",
            "她对我来说是很重要的人",
            "她比阿哲更亲近我",
            "她更信任阿哲",
        ],
    )
    async def test_关系描述写不进去(self, tmp_path, content):
        service, _ = _enabled(tmp_path)
        with pytest.raises(ValueError, match="不描述任何关系"):
            await _write(service, content=content)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "content",
        [
            "他跟我配合得比别人顺",
            "他比别人更懂我",
            "他站在我这边",
            "我们合作起来很顺",
            "他记得我说过的每一件事",
        ],
    )
    async def test_带人称的句子一律挡下(self, tmp_path, content):
        """真机试用时这几句全漏了，第一版只有一张关系句式表。

        中文表达关系的方式太多，补词表永远补不完。换成结构性判据：
        them 记的是这个人本身，一句只讲他的话不需要提到「我」；
        一旦提到我，主语就不再只是他了。
        """
        service, _ = _enabled(tmp_path)
        with pytest.raises(ValueError, match="不描述任何关系"):
            await _write(service, content=content)

    @pytest.mark.asyncio
    async def test_只讲这个人的句子照常写入(self, tmp_path):
        service, _ = _enabled(tmp_path)
        claim, _ = await _write(service, content="她讲话直奔结论，不铺垫")
        assert claim.content == "她讲话直奔结论，不铺垫"

    @pytest.mark.asyncio
    async def test_you那张禁止表照样生效(self, tmp_path):
        service, _ = _enabled(tmp_path)
        with pytest.raises(ValueError):
            await _write(service, content="她是典型的内向人格")


class TestPersons:
    @pytest.mark.asyncio
    async def test_昵称命中同一个人(self, tmp_path):
        service, _ = _enabled(tmp_path)
        claim, _ = await _write(service, names=["Zoey", "小 Z"])
        again, _ = await _write(service, names=["小 Z"])
        assert again.person_id == claim.person_id

    @pytest.mark.asyncio
    async def test_新称呼会并进已有的人(self, tmp_path):
        service, _ = _enabled(tmp_path)
        await _write(service, names=["Zoey"])
        await _write(service, names=["Zoey", "阿 Z"])
        scope = service.status().scope
        person = service.store.find_person_by_name(scope, "阿 Z")
        assert person is not None
        assert set(person.names) == {"Zoey", "阿 Z"}

    @pytest.mark.asyncio
    async def test_跨类同名不自动合并(self, tmp_path):
        """「听别人描述一个人」和「自己认识一个人」是两码事。

        人类登记的那个「Zoey」模型一次都没见过，关于她的一切都是转述；
        模型在别处遇到的「Zoey」是第一手的。按名字字符串把两者并成一个人，
        就是在制造张冠李戴——而且并完之后来源标记还是「听你说的」，
        人类看到会以为那是自己说过的话。

        真机试出来的：登记一个同事「张三」，模型在技术分享会遇到另一个张三，
        两个人合成了一个。
        """
        service, _ = _enabled(tmp_path)
        service.add_person(["Zoey"])          # 人类说起过的那个
        with pytest.raises(ValueError, match="同一个人吗"):
            await _write(service)             # 模型自己遇到的那个

    @pytest.mark.asyncio
    async def test_名字列表里混进人类登记的人同样挡得住(self, tmp_path):
        """闸不能只看 names 里的第一个名字。

        上一版的循环在**第一个命中**就 return 了：names 里先出现一个模型
        自己遇到的人，后面那个人类登记的同名者根本走不到那道闸，会被静默
        并进前者。真机复现出来的名册是：

            model  ['Zoey', '张三']    ← 人类登记的「张三」被并了进来
            human  ['张三']

        两个人共用一个称呼，之后按名字解析命中谁取决于返回顺序——
        这正是那道闸本来要挡的张冠李戴。
        """
        service, _ = _enabled(tmp_path)
        人 = service.add_person(["张三"])
        await _write(service, names=["Zoey"])
        with pytest.raises(ValueError, match="同一个人吗"):
            await _write(service, names=["Zoey", "张三"])
        名册 = {p["origin"]: p["names"] for p in service.list_people()}
        assert 名册["model"] == ["Zoey"], "模型那个人不该被塞进别人的称呼"
        assert 名册["human"] == ["张三"]
        assert 人.id == [
            p["person_id"] for p in service.list_people() if p["origin"] == "human"
        ][0]

    def test_带person_id时names里混进别人也拦得住(self, tmp_path):
        """身份闸不能只守没带 person_id 的那条路。

        DS 真机撞闸时找到的：直接写两个同名的人会被拦，但**错误信息里
        给出了 person_id**——它读完错误信息，带上那个 id 再写一次就过了。
        本意是「你确认是同一个人就带 id 再来」，结果成了绕过这道闸的说明书。

        更糟的是 names 被**静默忽略**：模型以为自己把两个称呼并到了一起，
        实际什么都没发生，也没有任何提示。它照着这个错觉往下写，
        写出来的正文里断言「这两个是同一个人」。
        """
        service, _ = _enabled(tmp_path)
        scope = service.status().scope
        甲 = service.add_person(["张三"])
        service.store.put_person(scope, Person.new(["张三老师"]))
        with pytest.raises(ValueError, match="张三老师"):
            service._resolve_person(scope, ["张三", "张三老师"], 甲.id)

    def test_带person_id时只给他自己的名字照常通过(self, tmp_path):
        """别把正常路径也堵了：带 id 又重复他已有的称呼，是常见写法。"""
        service, _ = _enabled(tmp_path)
        scope = service.status().scope
        甲 = service.add_person(["张三", "老张"])
        人 = service._resolve_person(scope, ["张三"], 甲.id)
        assert 人.id == 甲.id

    def test_带person_id时可以补一个全新的称呼(self, tmp_path):
        """新名字不属于任何人，不构成合并，应该允许并且真的存下来。"""
        service, _ = _enabled(tmp_path)
        scope = service.status().scope
        甲 = service.add_person(["张三"])
        人 = service._resolve_person(scope, ["张三", "小张"], 甲.id)
        assert "小张" in 人.names, "新称呼被静默丢掉了"

    @pytest.mark.asyncio
    async def test_一次写入不把两个已有的人并成一个(self, tmp_path):
        """同为模型自己遇到的人，也不能因为写在同一个 names 里就合并。

        「是不是同一个人」是判断，不是字符串比对——这条对同类一样成立。
        真要合并，带 person_id 明说。
        """
        service, _ = _enabled(tmp_path)
        第一个, _ = await _write(service, names=["Zoey"])
        service.store.put_person(
            service.status().scope, Person.new(["老陈"])
        )
        with pytest.raises(ValueError, match="两个已经存在的人|同一个人吗"):
            await _write(service, names=["Zoey", "老陈"])

    @pytest.mark.asyncio
    async def test_认下来就带person_id写(self, tmp_path):
        """挡下来不是不让写，是让模型自己判断。认，就带 id 再来一次。"""
        service, _ = _enabled(tmp_path)
        人 = service.add_person(["Zoey"])
        claim, _ = await _write(service, names=[], person_id=人.id)
        assert claim.person_id == 人.id

    @pytest.mark.asyncio
    async def test_同为它自己遇到的人照常并称呼(self, tmp_path):
        """这一档没有混淆风险：两次都是第一手的印象。"""
        service, _ = _enabled(tmp_path)
        first, _ = await _write(service, names=["Zoey"])
        again, _ = await _write(service, names=["Zoey", "小 Z"])
        assert again.person_id == first.person_id

    @pytest.mark.asyncio
    async def test_没给名字写不进去(self, tmp_path):
        service, _ = _enabled(tmp_path)
        with pytest.raises(ValueError, match="至少给一个名字"):
            await _write(service, names=[])


class TestRecall:
    async def _formalized(self, service):
        claim, _ = await _write(service)
        for _ in range(REQUIRED_CONFIRMATIONS - 1):
            claim = _age_receipts(service, claim)
            claim, _ = await _write(service)
        return claim

    @pytest.mark.asyncio
    async def test_生效之后按姓名读得回(self, tmp_path):
        service, _ = _enabled(tmp_path)
        await self._formalized(service)
        output = await service.recall(query="今天和 Zoey 聊了聊")
        assert "直奔结论" in output
        assert "Zoey" in output

    @pytest.mark.asyncio
    async def test_读回是一条JSON并写明不是用户(self, tmp_path):
        service, _ = _enabled(tmp_path)
        await self._formalized(service)
        output = await service.recall(query="Zoey")
        assert output.count("```json") == 1
        assert "不是用户本人的信息" in output
        payload = json.loads(output.split("```json")[1].split("```")[0])
        assert payload["them"][0]["person"] == "Zoey"

    @pytest.mark.asyncio
    async def test_自己遇到的人标成第一手(self, tmp_path):
        service, _ = _enabled(tmp_path)
        await self._formalized(service)
        payload = json.loads(
            (await service.recall(query="Zoey")).split("```json")[1].split("```")[0]
        )
        assert payload["them"][0]["known_via"] == "met_myself"

    @pytest.mark.asyncio
    async def test_听人类说起的人标成转述(self, tmp_path):
        """模型读回时得分得清自己见没见过这个人。

        人类那一侧靠名册的分组看得见，模型这一侧只有这个字段。缺了它，
        「我认识的张三」和「用户跟我讲过的张三」在读回时长得一模一样——
        而后者本该带着一层「可能记岔、也可能是另一个同名的人」的不确定。
        """
        service, _ = _enabled(tmp_path)
        人 = service.add_person(["Zoey"])
        claim, _ = await _write(service, names=[], person_id=人.id)
        for _ in range(REQUIRED_CONFIRMATIONS - 1):
            _age_receipts(service, claim)
            claim, _ = await _write(service, names=[], person_id=人.id)
        payload = json.loads(
            (await service.recall(query="Zoey")).split("```json")[1].split("```")[0]
        )
        assert payload["them"][0]["known_via"] == "heard_from_user"
        assert "never met" in payload["known_via_note"]

    @pytest.mark.asyncio
    async def test_没提到名字就不返回(self, tmp_path):
        service, _ = _enabled(tmp_path)
        await self._formalized(service)
        assert await service.recall(query="今天的天气不错") == ""

    @pytest.mark.asyncio
    async def test_命中算被提起一次(self, tmp_path):
        service, _ = _enabled(tmp_path)
        await self._formalized(service)
        scope = service.status().scope
        before = service.store.find_person_by_name(scope, "Zoey").activation_count
        await service.recall(query="Zoey")
        after = service.store.find_person_by_name(scope, "Zoey").activation_count
        assert after == before + 1


class TestSurface:
    @pytest.mark.asyncio
    async def test_无query只追加前三人(self, tmp_path):
        service, _ = _enabled(tmp_path)
        scope = service.status().scope
        for index in range(MAX_SURFACED_PERSONS + 2):
            person = service.store.put_person(scope, Person.new([f"人{index}"]))
            for _ in range(index):
                person = service.store.put_person(
                    scope, person.mentioned(), expected_revision=person.revision
                )
        persons = service._top_persons(service.store.list_persons(scope))
        assert len(persons) == MAX_SURFACED_PERSONS
        # 提及次数最多的排最前：这就是「按提及时间次数自然衰减」的全部实现
        assert persons[0].display_name == f"人{MAX_SURFACED_PERSONS + 1}"

    @pytest.mark.asyncio
    async def test_有query时不受前三名额限制(self, tmp_path):
        service, _ = _enabled(tmp_path)
        scope = service.status().scope
        names = [f"人{index}" for index in range(MAX_SURFACED_PERSONS + 2)]
        for name in names:
            service.store.put_person(scope, Person.new([name]))
        matched = service._match_persons(
            " ".join(names), service.store.list_persons(scope)
        )
        assert len(matched) == len(names)

    @pytest.mark.asyncio
    async def test_关掉them时追加块是空的(self, tmp_path):
        """rule.md 13.3 那条边界唯一可被检验的形式。"""
        service, _ = _enabled(tmp_path)
        await TestRecall()._formalized(service)
        state = service.status()
        service.set_enabled(False, expected_revision=state.state_revision)
        assert await service.surface(query="Zoey") == ""


class TestQuota:
    @pytest.mark.asyncio
    async def test_超限时拒绝并按aspect分层摆出来(self, tmp_path):
        service, _ = _enabled(tmp_path, them={"max_tokens_per_person": 200})
        # 先把配额撑满：直接落 formal，绕开三日只是为了造场景，不是产品路径
        from dataclasses import replace

        claim, _ = await _write(service)
        service.store.put_claim(
            replace(
                claim,
                content="她" + "讲话直奔结论。" * 40,
                lifecycle="formal",
                review_state="clear",
                valid_from="2026-01-01T00:00:00+00:00",
            ),
            expected_revision=claim.revision,
        )
        with pytest.raises(ValueError) as excinfo:
            await _write(service, concept_key="another_trait", content="她还很守时")
        message = str(excinfo.value)
        assert "已经满了" in message
        assert "communication_preference" in message  # 分层照 I 的模式
        assert claim.id in message  # 摆出 id，模型才能 delete 掉

    @pytest.mark.asyncio
    async def test_重申已生效的那条不算重复占额(self, tmp_path):
        """配额算的是「写完之后有多少」，不是「现在有多少 + 又来一份」。

        重申时 `_upsert` 更新原条目而不是新增，净增长为零。原先却把 incoming
        无条件加到 used 上，而 used 里已经含着那条自己——同一份内容算两遍。
        后果是一条认识只要超过配额的一半就再也重申不了（真机：68/120 的占用，
        重申一个字没改的同一条被拒），而重申恰恰是维持它有效的必需动作。
        """
        service, _ = _enabled(tmp_path)
        service.config.setdefault("them", {})["max_tokens_per_person"] = 120
        长句 = "她在评审里会把每个假设单独列出来再逐条问依据，" * 2
        claim, _ = await _write(
            service, content=长句, concept_key="review_style", concept_value="v"
        )
        for _ in range(REQUIRED_CONFIRMATIONS - 1):
            _age_receipts(service, claim)
            claim, _ = await _write(
                service, content=长句, concept_key="review_style", concept_value="v"
            )
        assert claim.lifecycle == "formal"

        _age_receipts(service, claim)
        again, _ = await _write(
            service, content=长句, concept_key="review_style", concept_value="v"
        )
        assert again.id == claim.id, "重申应该更新原条目，不是新增一条"

    @pytest.mark.asyncio
    async def test_别的概念仍然照常受配额约束(self, tmp_path):
        """放宽的只有「重申自己」那一种情况，不是把闸拆了。"""
        service, _ = _enabled(tmp_path)
        service.config.setdefault("them", {})["max_tokens_per_person"] = 120
        长句 = "她在评审里会把每个假设单独列出来再逐条问依据，" * 2
        claim, _ = await _write(
            service, content=长句, concept_key="review_style", concept_value="v"
        )
        for _ in range(REQUIRED_CONFIRMATIONS - 1):
            _age_receipts(service, claim)
            claim, _ = await _write(
                service, content=长句, concept_key="review_style", concept_value="v"
            )
        with pytest.raises(ValueError, match="已经满了"):
            await _write(
                service, content=长句 + "另一件事。",
                concept_key="another_trait", concept_value="v",
            )

    @pytest.mark.asyncio
    async def test_候选不占配额(self, tmp_path):
        """候选还没真正落库，占位就等于让不算数的东西挤掉算数的。"""
        service, _ = _enabled(tmp_path, them={"max_tokens_per_person": 200})
        for index in range(4):
            await _write(
                service, concept_key=f"trait_{index}", content="她" + "讲话直奔结论。" * 10
            )

    @pytest.mark.asyncio
    async def test_配置能改上限(self, tmp_path):
        service, _ = _enabled(tmp_path, them={"max_tokens_per_person": 700})
        assert service.max_tokens_per_person == 700


class TestDelete:
    @pytest.mark.asyncio
    async def test_撤回不需要三次确认(self, tmp_path):
        service, _ = _enabled(tmp_path)
        claim, _ = await _write(service)
        message = await service.delete(claim.id)
        assert "撤回了" in message
        after = service.store.get_claim(service.status().scope, claim.id)
        assert after.lifecycle == "expired"

    @pytest.mark.asyncio
    async def test_依据塌到一个就失效(self, tmp_path):
        service, _ = _enabled(tmp_path)
        claim = await TestRecall()._formalized(service)
        await service.remove_bucket_evidence("memory-1")
        after = service.store.get_claim(service.status().scope, claim.id)
        assert after.lifecycle == "expired"
        assert not after.callable_at()

    @pytest.mark.asyncio
    async def test_桶被删掉之后读回时当场失效(self, tmp_path):
        """闸二的后半段，走的是读时校验而不是桶变动通知。

        这条以前是坏的：`remove_bucket_evidence` 写了，但没有任何人调用它
        （`bucket_change_observers` 从来没被注册过），而「依据被删除后这条
        认识会自动失效」写在工具描述和 rule.md 里。功能表里写了没实现的东西。
        """
        service, manager = _enabled(tmp_path)
        claim = await TestRecall()._formalized(service)
        assert "直奔结论" in await service.recall(query="Zoey")

        del manager.buckets["memory-1"]  # 依据塌到只剩一个

        assert await service.recall(query="Zoey") == ""
        after = service.store.get_claim(service.status().scope, claim.id)
        assert after.lifecycle == "expired"

    @pytest.mark.asyncio
    async def test_只塌了一个但还够就继续生效(self, tmp_path):
        service, manager = _enabled(tmp_path, buckets=3)
        claim, _ = await _write(
            service, bucket_ids=["memory-1", "memory-2", "memory-3"]
        )
        for _ in range(REQUIRED_CONFIRMATIONS - 1):
            claim = _age_receipts(service, claim)
            claim, _ = await _write(
                service, bucket_ids=["memory-1", "memory-2", "memory-3"]
            )
        del manager.buckets["memory-1"]  # 还剩两个，仍然过门槛
        assert "直奔结论" in await service.recall(query="Zoey")

    @pytest.mark.asyncio
    async def test_软删除的桶不再撑得住(self, tmp_path):
        """软删除盖 deleted_at，bucket_mgr.get 对它返回 None。"""
        service, manager = _enabled(tmp_path)
        await TestRecall()._formalized(service)
        manager.buckets["memory-1"] = None  # get() 对软删除桶就是返回 None
        assert await service.recall(query="Zoey") == ""

    @pytest.mark.asyncio
    async def test_归档的桶仍然撑得住(self, tmp_path):
        """归档只改变可见性，不使证据失效（rule.md 第 9 条、SPEC 9.3）。

        我一度把归档也算成失效，理由是工具描述写着「被归档或删除」。
        那是改反了方向：自动衰减归档是常态，让它触发失效等于一条攒了三天
        才立住的认识会因为某个依据自然淡出而被时间清空。
        """
        service, manager = _enabled(tmp_path)
        await TestRecall()._formalized(service)
        归档了 = dict(manager.buckets["memory-1"])
        归档了["metadata"] = {**归档了["metadata"], "type": "archived"}
        manager.buckets["memory-1"] = 归档了
        assert "结论" in await service.recall(query="Zoey")

    @pytest.mark.asyncio
    async def test_归档但带删除墓碑的按删除处理(self, tmp_path):
        """SPEC 9.3：若归档同时带有 deleted_at，则按删除源记忆处理。"""
        service, manager = _enabled(tmp_path)
        await TestRecall()._formalized(service)
        manager.buckets["memory-1"] = None  # get() 对带墓碑的桶就是返回 None
        assert await service.recall(query="Zoey") == ""

    @pytest.mark.asyncio
    async def test_读桶抖动不会误杀(self, tmp_path):
        """读不到桶不等于桶没了。一次磁盘抖动不该判死一条攒了三天的认识。"""
        service, manager = _enabled(tmp_path)
        await TestRecall()._formalized(service)

        async def 炸(_bucket_id):
            raise OSError("disk hiccup")

        manager.get = 炸
        assert "直奔结论" in await service.recall(query="Zoey")


class TestDisabled:
    @pytest.mark.asyncio
    async def test_关闭时读写都不通(self, tmp_path):
        service, _ = _service(tmp_path)
        with pytest.raises(ThemStoreError):
            await service.recall(query="Zoey")
        with pytest.raises(ThemStoreError):
            await _write(service)

    @pytest.mark.asyncio
    async def test_默认关闭时不建库(self, tmp_path):
        service, _ = _service(tmp_path)
        assert service.status().enabled is False
        assert not service.store.exists
