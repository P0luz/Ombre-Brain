"""`them` 的数据模型：一个人，和关于这个人的若干条认识。

形态同 `You`（rule.md 13.3），所以 `ThemClaim` 直接继承 `YouClaim`，只多一个
`person_id`。不另抄一份是因为两者**本该**一模一样：抄一份的结果是两边的
aspect、生命周期、门槛逻辑慢慢漂开，而任何一次漂开都是 them 悄悄放松了
you 那边守住的东西。

与 `You` 真正不同的只有这里的 `Person`：

- 一个人有好几个名字（正名 + 昵称），**命中任一个就是命中这个人**。
  同一个人被怎么称呼是随场合变的，认人不该因为换了个叫法就失败。
- 被提起就刷新 `last_active` 与 `activation_count`，喂给 `decay_engine`
  已有的那套公式算权重。不为 them 另立一套衰减曲线——衰减是效果性参数，
  另立一套就得重新扫、重新定基线，而这套已经在跑、已经调过。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from ..you.models import (
    VALID_ASPECTS,
    EvidenceEdge,
    ReviewReceipt,
    Scope,
    YouClaim,
    evidence_digest,
    new_id,
    require_id,
    require_text,
    utc_now,
)

__all__ = [
    "ORIGIN_HUMAN",
    "ORIGIN_MODEL",
    "VALID_ASPECTS",
    "EvidenceEdge",
    "ReviewReceipt",
    "Scope",
    "ThemClaim",
    "Person",
    "THEM_POLICY_VERSION",
    "evidence_digest",
    "utc_now",
]

SCHEMA_VERSION = 1
THEM_POLICY_VERSION = "them-policy-v1"

# 一个人最多登记几个名字。正名 + 几个昵称够用了；再多基本是在把「这个人
# 相关的词」都塞进来，那会让姓名命中变成一次模糊检索。
MAX_NAMES = 8
# 单个名字的长度上限，与发言归属那边保持一致。
MAX_NAME_CHARS = 25
# 喂给 decay_engine 的固定 importance。them 没有「这个人有多重要」这种字段，
# 也不该有——那正是 rule.md 13.3 要挡的关系评价。给中性值，让分数完全由
# 「多久没提起」和「提起过几次」决定，也就是 poluz 说的「按提及时间次数自然衰减」。
NEUTRAL_IMPORTANCE = 5


def normalize_name(value: object) -> str:
    text = str(value or "").strip()
    if not text or len(text) > MAX_NAME_CHARS or "\x00" in text:
        raise ValueError("invalid person name")
    return text


# 这个人是谁登记的。只是出处，**不决定人类看得见多少**——那是 known_via 的事。
ORIGIN_MODEL = "model"
ORIGIN_HUMAN = "human"
VALID_ORIGINS = frozenset({ORIGIN_MODEL, ORIGIN_HUMAN})

# 我到底见没见过这个人。**这一个字段决定人类看得见多少**（rule.md 13.3：
# 「them 分两种，分界是模型怎么认识这个人的，不是谁登记的」）。
#
# 这两件事原先是同一个 bit：`known_via` 由 `origin` 推导出来。于是模型写一个
# 「只听人类说起过」的人时，没有任何办法把它标对。拆成两个字段解决了标注，
# 但可见性被留在了 `origin` 上——那一版里模型登记、标了 heard_from_user 的人
# 依然对人类不可见，而撞名又挡住人类自己登记，于是那个人永远看不到。
#
# 现在：`known_via` 管可见性（也就是 13.3 的那条分界），`origin` 只是出处。
KNOWN_VIA_MET_MYSELF = "met_myself"
KNOWN_VIA_HEARD_FROM_USER = "heard_from_user"
VALID_KNOWN_VIA = frozenset({KNOWN_VIA_MET_MYSELF, KNOWN_VIA_HEARD_FROM_USER})

# 一个人身上最多挂几条待读的人类留言。留言是给模型看一次的提醒，不是收件箱；
# 堆到这个数还没被读走，说明浮现根本没发生，再堆下去也没用。
MAX_PENDING_NOTES = 10
# 单条留言的长度上限。纠错是「你把这件事记错了」，不是一封信。
MAX_NOTE_CHARS = 500


@dataclass(frozen=True)
class Person:
    """一个被记住的人。名字是它的内部字段，而且是多值的。

    ## 两种认识途径，区别只在人类看得见多少

    - `known_via="met_myself"`：模型自己在相处里遇到的人。第一手的印象，
      人类只看得见、也只改得动称呼。
    - `known_via="heard_from_user"`：模型一次都没见过，关于他的一切都是人类
      转述的。所以这一份对人类可见，并且可以留言纠错——**纠错要有对象，
      看不见就只能瞎猜**。

    分界是「怎么认识的」，不是「谁登记的」：人类亲口介绍、模型顺手登记下来的
    人，内容照样该给人类看——那些话本来就是人类说的。`origin` 只记出处。

    模型对两种人一视同仁：同样的两桶三日门槛，同样写得进去。可见性是人类那一侧
    的事，不该反过来改变模型能记什么。

    ## 两张待读回执

    `pending_rename` 是人类改过称呼之后留下的；`pending_notes` 是人类留下的
    纠错留言。都只念一次：反复念同一句就不是纠错，是施压。

    留言**不占每人的 token 配额**——配额管的是模型自己沉淀了多少，
    人类说的话不该挤掉模型的记忆。
    """

    id: str
    names: tuple[str, ...]
    activation_count: int = 1
    last_active: str = field(default_factory=utc_now)
    created_at: str = field(default_factory=utc_now)
    revision: int = 1
    pending_rename: tuple[str, ...] = ()
    renamed_at: str = ""
    origin: str = ORIGIN_MODEL
    known_via: str = ""
    pending_notes: tuple[dict[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", require_id(self.id, "person"))
        names: list[str] = []
        seen: set[str] = set()
        for item in self.names or ():
            name = normalize_name(item)
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            names.append(name)
        if not names or len(names) > MAX_NAMES:
            raise ValueError("invalid person names")
        object.__setattr__(self, "names", tuple(names))
        for field_name in ("activation_count", "revision"):
            try:
                parsed = int(getattr(self, field_name))
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"invalid {field_name}") from exc
            if parsed < 1:
                raise ValueError(f"invalid {field_name}")
            object.__setattr__(self, field_name, parsed)
        object.__setattr__(self, "last_active", require_text(self.last_active, "last_active", limit=80))
        object.__setattr__(self, "created_at", require_text(self.created_at, "created_at", limit=80))
        previous: list[str] = []
        for item in self.pending_rename or ():
            name = str(item or "").strip()
            if name and len(name) <= MAX_NAME_CHARS and name not in previous:
                previous.append(name)
        object.__setattr__(self, "pending_rename", tuple(previous[:MAX_NAMES]))
        object.__setattr__(self, "renamed_at", str(self.renamed_at or ""))
        origin = str(self.origin or ORIGIN_MODEL).strip().lower()
        if origin not in VALID_ORIGINS:
            raise ValueError(
                f"origin「{origin}」不是允许值。可选：{' / '.join(sorted(VALID_ORIGINS))}。"
            )
        object.__setattr__(self, "origin", origin)
        # 空串 = 这条数据早于 known_via 独立成字段。按老规则从 origin 推一次，
        # 存量记录的表现因此与拆分前逐字一致；此后它就是一个独立字段了。
        known_via = str(self.known_via or "").strip().lower()
        if not known_via:
            known_via = (
                KNOWN_VIA_HEARD_FROM_USER
                if origin == ORIGIN_HUMAN
                else KNOWN_VIA_MET_MYSELF
            )
        if known_via not in VALID_KNOWN_VIA:
            raise ValueError(
                f"known_via「{known_via}」不是允许值。"
                f"可选：{' / '.join(sorted(VALID_KNOWN_VIA))}。"
            )
        object.__setattr__(self, "known_via", known_via)
        notes: list[dict[str, str]] = []
        for item in self.pending_notes or ():
            if not isinstance(item, Mapping):
                continue
            text = str(item.get("text") or "").strip()
            if not text or "\x00" in text:
                continue
            notes.append(
                {
                    "text": text[:MAX_NOTE_CHARS],
                    "at": str(item.get("at") or "").strip()[:80],
                }
            )
        object.__setattr__(self, "pending_notes", tuple(notes[:MAX_PENDING_NOTES]))

    @classmethod
    def new(
        cls,
        names: list[str] | tuple[str, ...],
        *,
        origin: str = ORIGIN_MODEL,
        known_via: str = "",
    ) -> "Person":
        return cls(
            id=new_id("person"),
            names=tuple(names),
            origin=origin,
            known_via=known_via,
        )

    @property
    def human_registered(self) -> bool:
        """这个人是人类自己在名册上登记的吗。

        **和 `human_visible` 是两件事，别再共用一个属性。** 这一条只在撞名时
        用：人类登记的那份不能被模型顺手按名字并走（rule.md 13.3「撞名时挡
        下来，让模型自己判断是不是同一个人」）。看得见多少由 `known_via` 决定，
        跟这里无关——两者曾经是同一个属性，改了可见性的语义就把这道闸一起
        改错了：模型第二次写同一个人会被自己第一次写的记录挡住。
        """
        return self.origin == ORIGIN_HUMAN

    @property
    def human_visible(self) -> bool:
        """人类看得见这个人身上的认识吗。

        **分界是 `known_via`——模型怎么认识这个人的，不是谁登记的。**
        rule.md 13.3 把这条写死了：

          - `met_myself`：第一手的印象。人类只看得见、也只改得动称呼。
            认识本身、依据、历史一概不可见，那是模型的。
          - `heard_from_user`：模型一次都没见过，关于他的一切都是人类转述的。
            这一份对人类可见，并且可以留言纠错——**纠错要有对象，看不见就
            只能瞎猜**；对一份自己看不见的认识提意见，那不是纠错。

        曾经这里返回的是 `self.origin == ORIGIN_HUMAN`。那让「人类亲口介绍、
        模型抢先登记」的人卡死：内容看不见，而撞名又挡住人类自己登记（那道
        挡是对的，13.3 要求跨类同名不合并），于是没有任何路径拿到可见性——
        人类连自己说过的话被记成什么样都不知道。

        按 `origin` 分是在防「模型标一个 heard_from_user 就把私有认识交出去」。
        但标 `heard_from_user` 本身就是在声明「我没见过这个人，全是转述」，
        那一份里没有第一手印象可保护。`origin` 仍然记着谁登记的，只是不再
        决定看得见多少。
        """
        return self.known_via == KNOWN_VIA_HEARD_FROM_USER

    def with_known_via(self, known_via: str) -> "Person":
        """订正「我到底见没见过这个人」。不动可见性，也不算一次被提起。"""
        return replace(self, known_via=known_via)

    def with_note(self, text: str, *, at: str | None = None) -> "Person":
        """人类留一条纠错。攒着，等下次浮现一起交给模型。

        不动 `activation_count` 与 `last_active`：人类留言不是「这个人被提起
        了一次」，算进去会凭空抬高衰减权重。同 `renamed_to`。
        """
        note = {"text": str(text or "").strip()[:MAX_NOTE_CHARS], "at": at or utc_now()}
        return replace(self, pending_notes=(*self.pending_notes, note))

    def notes_read(self) -> "Person":
        """留言念过了就清。反复念同一句不是纠错，是施压。"""
        return replace(self, pending_notes=())

    @property
    def display_name(self) -> str:
        """对外显示用的名字：登记的第一个，也就是正名。"""
        return self.names[0]

    @property
    def name_keys(self) -> frozenset[str]:
        """用于匹配的名字集合（大小写无关）。命中任一个即命中这个人。"""
        return frozenset(name.casefold() for name in self.names)

    def mentioned(self, at: str | None = None) -> "Person":
        """被提起了一次。这是 them 衰减的唯一输入。

        「被提起」= 姓名在 query 里命中，或模型显式写/读了关于这个人的认识。
        不刷新则自然沉下去——不需要另设一条「多久算冷」的阈值，
        排序本身就是淘汰。
        """
        return replace(
            self,
            activation_count=self.activation_count + 1,
            last_active=at or utc_now(),
        )

    def decay_metadata(self) -> dict[str, Any]:
        """摊成 `decay_engine.calculate_score` 认得的形状。

        只给它 activation_count 与 last_active 两个真实输入，其余取中性值：
        them 的权重必须完全由「被提起」驱动，掺进 importance 或 arousal
        就变成了系统在替模型判断这个人有多要紧。
        """
        return {
            "importance": NEUTRAL_IMPORTANCE,
            "activation_count": self.activation_count,
            "last_active": self.last_active,
        }

    def renamed_to(self, names: list[str] | tuple[str, ...]) -> "Person":
        """人类改了称呼。记下改动前的名字，留给下次浮现提醒模型一次。

        只改称呼，不动 `activation_count` 与 `last_active`：人类整理名册不是
        「这个人被提起了」，把它算成一次提及会凭空抬高衰减权重，让一个久不
        出现的人因为被改了个名字就挤进前三。
        """
        return replace(
            self,
            names=tuple(names),
            pending_rename=self.names,
            renamed_at=utc_now(),
        )

    def rename_notice_read(self) -> "Person":
        """提醒过了就清掉。这张回执只读一次，不是每次浮现都念一遍。"""
        return replace(self, pending_rename=(), renamed_at="")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Person":
        return cls(
            id=value.get("id", ""),
            names=tuple(value.get("names") or ()),
            activation_count=value.get("activation_count", 1),
            last_active=value.get("last_active", "") or utc_now(),
            created_at=value.get("created_at", "") or utc_now(),
            revision=value.get("revision", 1),
            pending_rename=tuple(value.get("pending_rename") or ()),
            renamed_at=value.get("renamed_at", "") or "",
            origin=value.get("origin") or ORIGIN_MODEL,
            # 老记录没有这个键。空串交给 __post_init__ 按 origin 推一次，
            # 存量数据的表现与拆分前逐字一致。
            known_via=value.get("known_via") or "",
            pending_notes=tuple(value.get("pending_notes") or ()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "names": list(self.names),
            "activation_count": self.activation_count,
            "last_active": self.last_active,
            "created_at": self.created_at,
            "revision": self.revision,
            "pending_rename": list(self.pending_rename),
            "renamed_at": self.renamed_at,
            "origin": self.origin,
            "known_via": self.known_via,
            "pending_notes": [dict(note) for note in self.pending_notes],
        }


@dataclass(frozen=True)
class ThemClaim(YouClaim):
    """关于某个人的一条认识。除 `person_id` 外与 `YouClaim` 同构。"""

    ID_PREFIX = "them"

    person_id: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "person_id", require_id(self.person_id, "person"))

    @classmethod
    def new_for(
        cls,
        *,
        scope: Scope,
        person_id: str,
        concept_key: str,
        concept_value: str,
        content: str,
        aspect: str,
        evidence: tuple[EvidenceEdge, ...],
        review_state: str = "pending",
        conflicts_with: tuple[str, ...] = (),
    ) -> "ThemClaim":
        return cls(
            id=new_id(cls.ID_PREFIX),
            scope=scope,
            person_id=person_id,
            concept_key=concept_key,
            concept_value=concept_value,
            content=content,
            aspect=aspect,
            # them 不进「核心准则」那条路：core 的意思是无 query 也要浮现，
            # 而 them 无 query 时走的是自己那条按衰减排序取前三的通道。
            recall_policy="contextual",
            evidence=evidence,
            review_state=review_state,
            conflicts_with=conflicts_with,
            evidence_revision=evidence_digest(evidence),
        )

    @classmethod
    def _extra_from_dict(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        return {"person_id": value.get("person_id", "")}

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload["person_id"] = self.person_id
        payload["policy_version"] = THEM_POLICY_VERSION
        return payload
