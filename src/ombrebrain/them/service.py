"""`them` 的开关、写入把关、姓名命中与浮现。

这里**一次 LLM 都不调**，理由同 `you`：我对一个人的认识不该经别人之口总结。
验证靠两道结构性的闸——与真实记忆桶的显式关系，以及三个不同自然日的重申。

## 配额与 compact

每人 1500 token（前端可改）。**只算已生效的条目**：候选还没真正落库，
占位就等于让"还没算数的东西"挤掉算数的东西。

超限时系统只挡，不代压：拒绝这次写入，把这个人当前的全部条目**按 aspect
分层**摆出来，让模型自己比对、自己决定合并哪几条。系统自动压缩就又变成了
替模型决定什么该留下——而 compact 恰恰是最需要判断力的那一步。

压缩不需要新接口：撤掉几条旧的（`delete`，模型自己决定，不需要确认）
再写一条合并后的，就是 compact。**不给它一条能一次性改写多条的捷径**，
因为那条捷径同时也是"绕开三日门槛换掉一句已生效的话"的捷径。

## 浮现

无 query：按衰减权重排序，只追加最高的三人。有 query：姓名命中谁就返回谁，
不受名额限制——认人不该因为这个人最近没被提起就失败。

两条路径都走独立通道：追加在浮现结果之后，不进融合打分。任何一条普通记忆
的分数与名次都不因为 them 的存在而改变（rule.md 13.3）。
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import logging
import re
from typing import Any, Mapping

from ombrebrain.storage.source_store import source_links_from_metadata
from utils import count_tokens_approx, parse_bool

from ..you.models import (
    VALID_ASPECTS,
    VALID_BASES,
    EvidenceEdge,
    ModuleState,
    ReviewReceipt,
    Scope,
    evidence_digest,
    utc_now,
)
from ..you.service import partition_by_live_evidence

from .models import (
    KNOWN_VIA_HEARD_FROM_USER,
    MAX_PENDING_NOTES,
    ORIGIN_HUMAN,
    THEM_POLICY_VERSION,
    VALID_KNOWN_VIA,
    Person,
    ThemClaim,
)
from .safety import (
    contains_forbidden_subject,
    is_relation_label,
    leaks_protected_text,
)
from .store import ThemStore, ThemStoreError

_CONCEPT_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{2,119}$")
_CONCEPT_VALUE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
_IGNORED_BUCKET_TYPES = frozenset({"archived", "feel", "plan", "letter", "self", "i"})

# 闸一 / 闸二，与 you 同一档：them 放松任何一道，都等于给"关于第三方的判断"
# 定了一条比"关于用户的判断"更低的门槛，而第三方连纠正的机会都没有。
REQUIRED_CONFIRMATIONS = 3
MIN_SUPPORTING_BUCKETS = 2

# 每人的 token 配额默认值。前端可改（config: them.max_tokens_per_person）。
DEFAULT_MAX_TOKENS_PER_PERSON = 1500
# 每人的候选条数上限。候选不占 token 配额，所以需要另一道结构性的闸挡住
# "无限写候选"；这不是效果参数，是防失控的硬上限。
MAX_CANDIDATES_PER_PERSON = 12
# 无 query 浮现时最多追加几个人。them 是浮现的补注，不是花名册。
MAX_SURFACED_PERSONS = 3

_SURFACE_HEADER = (
    "[以下是我自己写下的、关于**别人**的长期认识——不是用户本人的信息，"
    "也不是这些人此刻的状态。把其中任何一条当成用户的属性或意见都是错的。]"
)


class ThemService:
    """them 的全部行为。不调用任何 LLM。"""

    def __init__(
        self,
        *,
        store: ThemStore,
        bucket_mgr: Any,
        decay_engine: Any,
        source_store: Any,
        config: Mapping[str, Any] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.store = store
        self.bucket_mgr = bucket_mgr
        self.decay_engine = decay_engine
        self.source_store = source_store
        self.config = config or {}
        self.logger = logger or logging.getLogger("ombre_brain.them")

    # --- 开关 ---

    def status(self) -> ModuleState:
        try:
            return self.store.get_state()
        except ThemStoreError:
            return ModuleState.disabled()

    def set_enabled(self, enabled: bool, *, expected_revision: int | None = None) -> ModuleState:
        return self.store.set_enabled(enabled, expected_revision=expected_revision)

    def _require_scope(self) -> Scope:
        state = self.status()
        if not state.enabled or state.scope is None:
            raise ThemStoreError("unknown tool")
        return state.scope

    @property
    def max_tokens_per_person(self) -> int:
        section = self.config.get("them") if isinstance(self.config, Mapping) else None
        if isinstance(section, Mapping):
            try:
                configured = int(section.get("max_tokens_per_person") or 0)
            except (TypeError, ValueError):
                configured = 0
            if configured > 0:
                return configured
        return DEFAULT_MAX_TOKENS_PER_PERSON

    # --- 人 ---

    def _apply_known_via(self, scope: Scope, person: Person, known_via: str) -> Person:
        """写入时顺带订正「我见没见过这个人」。空串 = 不改。

        订正走写入路径而不是单开一个接口：改这一项的时机，恰好就是「我正要写
        关于他的一条，忽然意识到我其实没见过他」。分成两次调用只会让人忘掉第二次。
        """
        wanted = str(known_via or "").strip().lower()
        if not wanted or wanted == person.known_via:
            return person
        if wanted not in VALID_KNOWN_VIA:
            raise ValueError(
                f"known_via「{wanted}」不是允许值。"
                f"可选：{' / '.join(sorted(VALID_KNOWN_VIA))}。"
            )
        return self.store.put_person(
            scope,
            person.with_known_via(wanted),
            expected_revision=person.revision,
        )

    def _resolve_person(
        self,
        scope: Scope,
        names: list[str],
        person_id: str,
        known_via: str = "",
    ) -> Person:
        """按 person_id 或名字找人；找不到就按给的名字新建一个。

        名字由模型自己列（正名 + 昵称），系统只做规范化和长度校验，不去和记忆里
        的 `[[双链]]` 自动对齐——让系统判断"这两个称呼是不是同一个人"，
        就是又插了一层替模型做判断的中间层。
        """
        if person_id:
            person = self.store.get_person(scope, str(person_id).strip())
            if person is None:
                raise ValueError(f"没有这个人：{person_id}")
            # 带了 id 也要看 names——**身份闸不能只守没带 id 的那条路。**
            #
            # DS 真机撞闸时找到的绕法：直接写两个同名的人会被拦，但错误信息里
            # 给出了 person_id，它读完照着带上那个 id 再写一次就过了。
            # 本意是「你确认是同一个人就带 id 再来」，结果成了绕过它的说明书。
            #
            # 而且 names 此前被**静默忽略**：模型以为自己把两个称呼并到了一起，
            # 实际什么都没发生、也没有任何提示，它就照着这个错觉往下写，
            # 正文里断言「这两个是同一个人」。
            额外 = [
                str(name or "").strip()
                for name in names or []
                if str(name or "").strip() and str(name or "").strip() not in person.names
            ]
            if not 额外:
                return self._apply_known_via(scope, person, known_via)
            别人 = []
            for name in 额外:
                另一个 = self.store.find_person_by_name(scope, name)
                if 另一个 is not None and 另一个.id != person.id:
                    别人.append((name, 另一个))
            if 别人:
                清单 = "、".join(
                    f"「{name}」已经是{'、'.join(p.names)}"
                    f"（person_id={p.id}）的称呼" for name, p in 别人
                )
                raise ValueError(
                    f"{清单}。\n"
                    "带 person_id 是说「就写这个人」，不是把别人的称呼并过来——"
                    "两个已经存在的人，系统不会因为一次写入就当成同一个。\n"
                    "真是同一个人：这一条只写其中一个，另一个人的条目撤掉再重写。\n"
                    "不是同一个人：names 里只留属于他自己的称呼。"
                )
            # 全是新称呼，不构成合并：并进去。原先这里直接 return person，
            # 新名字被无声丢掉，下次换个叫法就认不出来了。
            merged = list(dict.fromkeys([*person.names, *额外]))
            return self.store.put_person(
                scope,
                replace(person, names=tuple(merged)),
                expected_revision=person.revision,
            )
        cleaned = [str(name or "").strip() for name in names or []]
        cleaned = [name for name in cleaned if name]
        if not cleaned:
            raise ValueError("要写关于谁的认识，至少给一个名字（names）。")
        # 先把 cleaned 里每个名字命中的人收齐，再判断。
        #
        # 上一版是边遍历边决定，**第一个命中就 return**：names 里先出现一个
        # 模型自己遇到的人，后面那个人类登记的同名者根本走不到下面那道闸，
        # 会被静默并进前者。真机复现出来是
        #     model  ['Zoey', '张三']    ← 人类登记的「张三」被并了进来
        #     human  ['张三']
        # 两个人共用一个称呼——正是这道闸要挡的东西，却从旁边绕过去了。
        命中: list[Person] = []
        for name in cleaned:
            existing = self.store.find_person_by_name(scope, name)
            if existing is None:
                continue
            if not any(other.id == existing.id for other in 命中):
                命中.append(existing)

        # 撞上人类登记的人：优先报这一条。两类的区别是「怎么认识的」，
        # 比「碰巧同名」重要得多，错误信息也更能指导下一步怎么写。
        for existing in 命中:
            if existing.human_visible:
                # **跨类同名不自动合并。**
                #
                # 「听别人描述一个人」和「自己认识一个人」是两码事。人类登记的
                # 那个「张三」，模型一次都没见过，关于他的一切都是转述；
                # 模型在别处遇到的「张三」是第一手的。系统按名字字符串把两者
                # 并成一个人，就是在制造张冠李戴——而且并完之后来源标记还是
                # 「听你说的」，人类看到会以为那是自己说过的话。
                #
                # 是不是同一个人，是判断，不是字符串比对。所以这里只挡下来，
                # 把那个人摆出来，让模型自己决定：认，就带 person_id 再写一次；
                # 不认，就换一个能区分的称呼。
                raise ValueError(
                    f"「{name}」这个称呼，人类登记过一个同名的人"
                    f"（person_id={existing.id}，登记的称呼：{'、'.join(existing.names)}）。"
                    "他和你要写的是同一个人吗？\n"
                    f"是 → 带上 person_id=\"{existing.id}\" 再写一次。\n"
                    "不是 → 换一个能区分的称呼，别让两个人共用一个名字。"
                )
        # 一次写入命中两个**已经存在**的人：同样不合并。
        # 「是不是同一个人」是判断，不是字符串比对——这条对同类一样成立。
        # 真要合并，带 person_id 明说，别让一次顺手的写入把两份认识搅在一起。
        if len(命中) > 1:
            清单 = "、".join(
                f"{'、'.join(p.names)}（person_id={p.id}）" for p in 命中
            )
            raise ValueError(
                f"这些称呼分别指向两个已经存在的人：{清单}。\n"
                "他们是同一个人吗？\n"
                "是 → 带上其中一个 person_id 再写一次。\n"
                "不是 → 这一条只写其中一个人，别把两份认识并到一起。"
            )

        if 命中:
            existing = 命中[0]
            # 同为它自己遇到的人：把这次带来的新称呼并进去，下次换个叫法也认得出。
            merged = list(dict.fromkeys([*existing.names, *cleaned]))
            if merged != list(existing.names):
                existing = self.store.put_person(
                    scope,
                    replace(existing, names=tuple(merged)),
                    expected_revision=existing.revision,
                )
            return self._apply_known_via(scope, existing, known_via)
        return self.store.put_person(
            scope, Person.new(cleaned, known_via=known_via)
        )

    def _touch_person(self, scope: Scope, person: Person) -> Person:
        """被提起了一次。them 的衰减只由这里驱动。"""
        try:
            return self.store.put_person(
                scope, person.mentioned(), expected_revision=person.revision
            )
        except ThemStoreError:
            # 并发下有人先改了这个人：提及计数少记一次而已，不该让读路径失败。
            return person

    # --- 人类唯一能碰的那一处：称呼 ---

    def list_people(self) -> list[dict[str, Any]]:
        """给前端看的名册。看得见多少，取决于这个人是谁登记的。

        - 模型自己认出来的人：**只有称呼**。认识、依据、历史一概不出现——
          那是模型的，不是给人读的。
        - 人类自己登记的人：连模型写下的认识一起给。人类本来就认识他，
          而且要能纠错——看不见就只能瞎猜。
        """
        try:
            scope = self._require_scope()
        except ThemStoreError:
            return []
        名册: list[dict[str, Any]] = []
        for person in self.store.list_persons(scope):
            条目: dict[str, Any] = {
                "person_id": person.id,
                "names": list(person.names),
                "revision": person.revision,
                "origin": person.origin,
                "pending_notes": [dict(note) for note in person.pending_notes],
            }
            if person.human_visible:
                条目["claims"] = [
                    {
                        "claim_id": claim.id,
                        "aspect": claim.aspect,
                        "content": claim.content,
                        "lifecycle": claim.lifecycle,
                    }
                    for claim in self.store.list_claims(scope, person_id=person.id)
                    if claim.lifecycle in {"formal", "candidate"}
                ]
            名册.append(条目)
        return 名册

    @staticmethod
    def _reject_relation_labels(names: list[str]) -> None:
        """人类不能拿关系当称呼。

        「老公」这个名字会跟着每一次浮现进模型的上下文，比在留言里写一句
        更持久、也更难被发现。关系只能是模型自己觉得的（poluz 2026-08-21），
        人类这一侧连称呼这个口子也不留。

        只拦整个称呼就是关系词的情况——「张老师」「李阿姨」照常，
        那是真的在叫人。
        """
        坏的 = [name for name in names if is_relation_label(name)]
        if 坏的:
            raise ValueError(
                f"「{'、'.join(坏的)}」是在说他和你是什么关系，不是在叫他。\n"
                "这里只写称呼——大名、小名、你平时怎么叫他都行（老张、陈工、Zoey）。\n"
                "关系不用你写：它自己会去认，认出来了你在名册上看得到。"
            )

    def add_person(self, names: list[str]) -> Person:
        """人类登记一个自己认识的人。

        这一份的认识对人类可见（`origin="human"`）。模型照样能往上写，
        门槛也一样——可见性是人类那一侧的事，不该反过来改变模型能记什么。
        """
        scope = self._require_scope()
        cleaned = list(
            dict.fromkeys(
                str(name or "").strip() for name in names or [] if str(name or "").strip()
            )
        )
        if not cleaned:
            raise ValueError("至少要给一个称呼。")
        self._reject_relation_labels(cleaned)
        for name in cleaned:
            existing = self.store.find_person_by_name(scope, name)
            if existing is not None:
                raise ValueError(
                    f"「{existing.display_name}」已经用了「{name}」这个称呼。"
                    "两个人共用一个名字的话，姓名命中会同时把两份认识都拉出来。"
                )
        # 人类登记的人，模型按定义没见过——是听人类说起才知道有这么个人。
        # 显式写死而不是让它从 origin 推：拆分之后 origin 只管可见性了。
        return self.store.put_person(
            scope,
            Person.new(
                cleaned,
                origin=ORIGIN_HUMAN,
                known_via=KNOWN_VIA_HEARD_FROM_USER,
            ),
        )

    def leave_note(self, person_id: str, text: str) -> Person:
        """人类给模型留一条纠错。

        只对人类自己登记的人开放：模型自己认识的人，人类连它记了什么都看不见，
        那种情况下的「纠错」是在对着看不见的东西提意见。

        留言攒着，下次浮现时一起交给模型，念一次就清。**不占 token 配额**——
        配额管的是模型自己沉淀了多少，人类说的话不该挤掉模型的记忆。
        """
        scope = self._require_scope()
        person = self.store.get_person(scope, str(person_id or "").strip())
        if person is None:
            raise ValueError(f"没有这个人：{person_id}")
        if not person.human_visible:
            raise ValueError(
                "这个人是它自己认识的，你看不到它记了什么，也就无从纠起。"
                "留言只对你自己登记的人开放。"
            )
        内容 = str(text or "").strip()
        if not 内容:
            raise ValueError("留言不能是空的。")
        # **人类不能在这里定义关系。**
        #
        # 留言是人类唯一能往模型上下文里塞自由文本的地方，而且不占配额、
        # 下次浮现直接念给模型听。放任它写「他是我老公」「他跟我关系很好」，
        # 等于人类替模型认定了一段关系——them 拦住模型自己写关系，却从这里
        # 让人写进来，那道闸就是形同虚设。
        #
        # 关系只能是模型自己觉得的（poluz 2026-08-21）。人类要纠正的是
        # **事实**：认错人了、哪句记岔了、称呼变了。
        if contains_forbidden_subject(内容):
            raise ValueError(
                "留言里不能定义你和他的关系，也不能写人格、健康、财务这些话题。\n"
                "这里只用来纠正事实——认错人了、哪句记岔了、称呼变了。\n"
                "比如「你说的这个 Zoey 是设计部的，不是市场部那个」可以；\n"
                "「他是我老公」「他跟我关系很好」不行——关系只能由它自己去认。"
            )
        if len(person.pending_notes) >= MAX_PENDING_NOTES:
            raise ValueError(
                f"这个人身上已经有 {MAX_PENDING_NOTES} 条还没被读走的留言了。"
                "先让它回想一次，读过之后再留新的。"
            )
        return self.store.put_person(
            scope, person.with_note(内容), expected_revision=person.revision
        )

    def rename_person(
        self, person_id: str, names: list[str], *, expected_revision: int | None = None
    ) -> Person:
        """人类改这个人的正名与昵称。

        改完留一张待读回执，下次浮现时告诉模型一次。**不动任何一条认识的正文**：
        那些句子是模型自己写的，人类改的是名册上的称呼，不是模型的判断。
        正文里留着的旧称呼，由模型看到提醒之后自己决定要不要改。
        """
        scope = self._require_scope()
        person = self.store.get_person(scope, str(person_id or "").strip())
        if person is None:
            raise ValueError(f"没有这个人：{person_id}")
        cleaned = list(
            dict.fromkeys(str(name or "").strip() for name in names or [] if str(name or "").strip())
        )
        if not cleaned:
            raise ValueError("至少要留一个称呼。")
        self._reject_relation_labels(cleaned)
        if cleaned == list(person.names):
            return person
        conflict = next(
            (
                other
                for name in cleaned
                for other in [self.store.find_person_by_name(scope, name)]
                if other is not None and other.id != person.id
            ),
            None,
        )
        if conflict is not None:
            raise ValueError(
                f"「{conflict.display_name}」已经用了其中一个称呼。"
                "两个人共用一个名字的话，姓名命中会同时把两份认识都拉出来。"
            )
        return self.store.put_person(
            scope,
            person.renamed_to(cleaned),
            expected_revision=(
                person.revision if expected_revision is None else expected_revision
            ),
        )

    def _take_human_notes(self, scope: Scope, persons: list[Person]) -> list[str]:
        """把人类留下的纠错取出来，并当场清掉。

        与改名提醒同构：只念一次。清掉之前先写回库，写失败就不返回这一条——
        宁可漏一次，也不要每次浮现都念同一句。反复念不是纠错，是施压。
        """
        notes: list[str] = []
        for person in persons:
            if not person.pending_notes:
                continue
            条目 = [note["text"] for note in person.pending_notes]
            try:
                self.store.put_person(
                    scope, person.notes_read(), expected_revision=person.revision
                )
            except ThemStoreError:
                continue
            for 文 in 条目:
                notes.append(f"关于{person.display_name}：{文}")
        return notes

    def _take_rename_notices(self, scope: Scope, persons: list[Person]) -> list[str]:
        """把这些人身上的待读回执取出来，并当场清掉。

        只读一次：清掉之前先写回库，写失败就不返回这一条——宁可漏一次提醒，
        也不要每次浮现都念同一句。
        """
        notices: list[str] = []
        for person in persons:
            if not person.pending_rename:
                continue
            旧 = "、".join(person.pending_rename)
            新 = "、".join(person.names)
            try:
                self.store.put_person(
                    scope, person.rename_notice_read(), expected_revision=person.revision
                )
            except ThemStoreError:
                continue
            notices.append(
                f"你当时记的是「{旧}」，现在登记的称呼是「{新}」。"
                "这只是称呼变了，你对他的认识没有被动过；"
                "认识正文里如果还写着旧称呼，要不要改由你自己定。"
            )
        return notices

    def _person_score(self, person: Person) -> float:
        try:
            return float(self.decay_engine.calculate_score(person.decay_metadata()))
        except Exception:
            # 算不出分就按最久没提起处理，排在最后，而不是让整条浮现路径断掉。
            return 0.0

    # --- 写入 ---

    async def write(
        self,
        *,
        content: str,
        bucket_ids: list[str],
        aspect: str,
        concept_key: str,
        concept_value: str,
        names: list[str] | None = None,
        person_id: str = "",
        basis: str = "observed_pattern",
        known_via: str = "",
    ) -> tuple[ThemClaim, str]:
        """模型写下（或重申）一条关于某个人的认识。返回 (条目, 给模型看的话)。

        `known_via` 空串表示不动：新人默认 `met_myself`，已有的人保持原样。
        给了就顺带订正——这一项本来就是「我见没见过他」，只有模型说了算。
        """

        scope = self._require_scope()
        person = self._resolve_person(scope, names or [], person_id, known_via)

        edges, protected_texts = await self._build_edges(
            bucket_ids, basis=basis, person=person
        )
        normalized = self._validate(
            aspect=aspect,
            concept_key=concept_key,
            concept_value=concept_value,
            content=content,
            basis=basis,
            protected_texts=protected_texts,
        )

        over, report = self._quota_report(
            scope,
            person,
            incoming=normalized["content"],
            concept_key=normalized["concept_key"],
            concept_value=normalized["concept_value"],
        )
        if over:
            raise ValueError(report)

        claim = self._upsert(scope, person, normalized, edges)
        person = self._touch_person(scope, person)

        if claim.lifecycle == "formal":
            return claim, f"记下了。关于{person.display_name}的这条已经生效：{claim.content}"
        still = max(0, REQUIRED_CONFIRMATIONS - claim.review_date_count)
        return claim, (
            f"先记成候选（{person.display_name}）：{claim.content}\n"
            f"还要在另外 {still} 个不同的日子重新确认它，才会真正落库。"
            "改主意了就别再确认，它不会自己生效。"
        )

    def _validate(
        self,
        *,
        aspect: str,
        concept_key: str,
        concept_value: str,
        content: str,
        basis: str,
        protected_texts: list[str],
    ) -> dict[str, str]:
        aspect = str(aspect or "").strip().lower()
        concept_key = str(concept_key or "").strip().lower()
        concept_value = str(concept_value or "").strip().lower()
        content = str(content or "").strip()
        basis = str(basis or "").strip().lower()
        # 逐项报，不要把五个条件堆成一句。
        # 真机试用时我自己被这句通用提示误导过一次：concept_key 只写了两个字符
        # 触发的也是它，我以为是 aspect 填错了，换了半天 aspect。
        # 一条说不清哪里错的提示，等于让调用方拿盲试当调试。
        if aspect not in VALID_ASPECTS:
            raise ValueError(
                f"aspect 只能是这五个之一：{'、'.join(sorted(VALID_ASPECTS))}。"
                f"你给的是「{aspect or '空'}」。"
            )
        if basis not in VALID_BASES:
            raise ValueError(
                f"basis 只能是这几个之一：{'、'.join(sorted(VALID_BASES))}。"
                f"你给的是「{basis or '空'}」。"
            )
        if not _CONCEPT_KEY_RE.fullmatch(concept_key):
            raise ValueError(
                f"concept_key 要用 snake_case，3–120 个字符，字母开头。"
                f"你给的是「{concept_key or '空'}」。"
            )
        if not _CONCEPT_VALUE_RE.fullmatch(concept_value):
            raise ValueError(
                f"concept_value 要用规范化短值（小写字母数字，可带 - 和 _，"
                f"不超过 80 字符）。你给的是「{concept_value or '空'}」。"
            )
        if not content:
            raise ValueError("正文不能是空的。")
        if len(content) > 500:
            raise ValueError(f"正文不超过 500 字，你给了 {len(content)} 字。")
        if contains_forbidden_subject(content, concept_key, concept_value):
            raise ValueError(
                "这条写不进去：them 只记这个人本身，不记人格判断、健康财务性与"
                "亲密这些话题，**也不描述任何关系**——"
                "「和谁关系怎么样」「对谁意味着什么」都不属于这里。"
                "改成只讲这个人本身的说法再试。"
            )
        if leaks_protected_text(content, protected_texts):
            raise ValueError("这条写不进去：不能照抄记忆原文，用你自己的话写。")
        return {
            "aspect": aspect,
            "concept_key": concept_key,
            "concept_value": concept_value,
            "content": content,
            "basis": basis,
        }

    async def _build_edges(
        self, bucket_ids: list[str], *, basis: str, person: Person
    ) -> tuple[tuple[EvidenceEdge, ...], list[str]]:
        """闸二：把模型给的 bucket_id 校验成显式关系，顺带收集要防泄漏的原文。

        比 `you` 多守一条：**每个依据桶的正文里都必须出现这个人的名字**
        （正名或任一昵称，命中一个就算）。要论证关于 Zoey 的事，每一条依据都
        该提到 Zoey——否则「关于这个人的认识」和「碰巧同时发生的别的事」
        就没有区别，凑出两个出处也太容易了。

        poluz 2026-08-20 定的是**每个桶都要有**，不是「至少一个」。代价是真实
        记忆里常见的「第一条写名字、第二条用代词承接」会被拒；那种情况得去找
        写了名字的那条桶。严在这里是有意的：一条依据自己都指不明白是谁，
        就不该拿来撑一条关于谁的判断。

        校验不过就抛，不降级不兜底——一条没有真实记忆撑着的认识，宁可写不进去。
        """
        unique = list(dict.fromkeys(str(item or "").strip() for item in bucket_ids or []))
        unique = [item for item in unique if item]
        if len(unique) < MIN_SUPPORTING_BUCKETS:
            raise ValueError(
                f"至少要给出 {MIN_SUPPORTING_BUCKETS} 个不同的 bucket_id："
                "一条认识不能只有一个出处。"
            )

        edges: list[EvidenceEdge] = []
        protected: list[str] = []
        for bucket_id in unique:
            bucket = await self.bucket_mgr.get(bucket_id)
            if not bucket:
                raise ValueError(f"找不到记忆桶 {bucket_id}，无法作为依据。")
            metadata = dict(bucket.get("metadata") or {})
            bucket_type = str(metadata.get("type") or "dynamic").strip().lower()
            if bucket_type in _IGNORED_BUCKET_TYPES:
                raise ValueError(f"{bucket_id} 是 {bucket_type} 类型，不能作为 them 的依据。")
            provenance = metadata.get("provenance")
            if isinstance(provenance, dict) and parse_bool(
                provenance.get("erasable"), default=False
            ):
                raise ValueError(f"{bucket_id} 是测试数据，不能作为 them 的依据。")
            body = str(bucket.get("content") or "").strip()
            if not body:
                raise ValueError(f"{bucket_id} 没有正文，不能作为依据。")
            folded = body.casefold()
            if not any(name in folded for name in person.name_keys):
                raise ValueError(
                    f"{bucket_id} 的正文里没有出现{person.display_name}"
                    f"（登记的称呼：{'、'.join(person.names)}）。"
                    "关于一个人的认识，每一条依据都得指明是谁——"
                    "换一条写了名字的记忆，或者先把这个称呼补进 names。"
                )
            source_id = ""
            for link in source_links_from_metadata(metadata):
                if str(link.get("status") or "active") != "active":
                    continue
                ref = str(link.get("ref") or "")
                if not ref:
                    continue
                if not source_id:
                    source_id = ref
                protected.append(self.source_store.read(ref))
            protected.append(body)
            edges.append(
                EvidenceEdge(
                    bucket_id=bucket_id,
                    source_id=source_id,
                    stance="supports",
                    basis=basis,
                    # 必须是桶内容的指纹，不能是时间戳。用时间戳的话每次重申都让
                    # 证据"变新"，先前攒的天数全部作废，三日门槛永远到不了。
                    bucket_revision="sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
                )
            )
        return tuple(sorted(edges, key=lambda item: item.bucket_id)), protected

    def _upsert(
        self,
        scope: Scope,
        person: Person,
        observation: Mapping[str, str],
        edges: tuple[EvidenceEdge, ...],
    ) -> ThemClaim:
        existing = self.store.list_claims(
            scope, person_id=person.id, concept_key=observation["concept_key"]
        )
        same = next(
            (
                claim
                for claim in existing
                if claim.concept_value == observation["concept_value"]
                and claim.lifecycle != "superseded"
            ),
            None,
        )
        formal_conflicts = tuple(
            claim.id
            for claim in existing
            if claim.lifecycle == "formal"
            and claim.concept_value != observation["concept_value"]
        )

        if same is None:
            candidates = [
                claim
                for claim in self.store.list_claims(scope, person_id=person.id)
                if claim.lifecycle == "candidate"
            ]
            if len(candidates) >= MAX_CANDIDATES_PER_PERSON:
                raise ValueError(
                    f"关于{person.display_name}的候选已经有 {len(candidates)} 条了。"
                    "候选不占 token 配额，但也不该无限堆——先把其中站不住的用 "
                    "delete_id 撤掉，或者去把还站得住的确认满三天。"
                )
            claim = ThemClaim.new_for(
                scope=scope,
                person_id=person.id,
                concept_key=observation["concept_key"],
                concept_value=observation["concept_value"],
                content=observation["content"],
                aspect=observation["aspect"],
                evidence=edges,
                review_state="conflicting" if formal_conflicts else "pending",
                conflicts_with=formal_conflicts,
            )
            expected_revision = 0
        else:
            by_bucket = {item.bucket_id: item for item in same.evidence}
            for edge in edges:
                by_bucket[edge.bucket_id] = edge
            evidence = tuple(sorted(by_bucket.values(), key=lambda item: item.bucket_id))
            # 正文改了就把先前的重申作废，重新攒三天。evidence_revision 只覆盖
            # 证据集合，管不到正文——但「修改也要三次确认」不能因为只改了一句话
            # 就绕过去。
            content_changed = observation["content"] != same.content
            claim = replace(
                same,
                content=observation["content"],
                aspect=observation["aspect"],
                evidence=evidence,
                evidence_revision=evidence_digest(evidence),
                review_receipts=() if content_changed else same.review_receipts,
                lifecycle="candidate"
                if (same.lifecycle == "expired" or content_changed)
                else same.lifecycle,
                review_state="conflicting" if formal_conflicts else same.review_state,
                conflicts_with=tuple(sorted(set((*same.conflicts_with, *formal_conflicts)))),
                valid_from=None if content_changed else same.valid_from,
                valid_until=None,
                needs_recompute=False,
            )
            expected_revision = same.revision

        claim = self._record_confirmation(claim)
        stored = self.store.put_claim(claim, expected_revision=expected_revision)
        return self._promote_if_ready(stored)

    def _record_confirmation(self, claim: ThemClaim) -> ThemClaim:
        """记一笔"模型今天重申过"。同一天重复调用只算一次。

        判重用的"今天"必须和收据时间戳同源：另取一次 now 的话，跨日那一瞬两个
        时间源会给出不同答案，同一天可能记下两条收据，三日门槛就少守了一天。
        """
        stamped = utc_now()
        today = stamped[:10]
        already = any(
            receipt.review_date == today
            and receipt.evidence_revision == claim.evidence_revision
            for receipt in claim.review_receipts
        )
        if already:
            return claim
        receipt = ReviewReceipt(
            reviewed_at=stamped,
            reviewer_role_id=claim.scope.observer_role_id,
            evidence_revision=claim.evidence_revision,
            policy_version=THEM_POLICY_VERSION,
            result="reaffirmed",
        )
        return replace(claim, review_receipts=(*claim.review_receipts, receipt))

    def _promote_if_ready(self, claim: ThemClaim) -> ThemClaim:
        if claim.lifecycle != "candidate":
            return claim
        if (
            claim.independent_support_count < MIN_SUPPORTING_BUCKETS
            or claim.review_date_count < REQUIRED_CONFIRMATIONS
        ):
            return claim
        conflicts = [
            item
            for item in self.store.list_claims(claim.scope, person_id=claim.person_id)
            if item.id in claim.conflicts_with and item.lifecycle == "formal"
        ]
        now = utc_now()
        for old in conflicts:
            self.store.put_claim(
                replace(old, lifecycle="superseded", valid_until=now),
                expected_revision=old.revision,
            )
        promoted = replace(
            claim,
            lifecycle="formal",
            review_state="clear",
            valid_from=now,
            replaces=conflicts[0].id if conflicts else claim.replaces,
        )
        return self.store.put_claim(promoted, expected_revision=claim.revision)

    # --- 配额 ---

    def _quota_report(
        self,
        scope: Scope,
        person: Person,
        *,
        incoming: str,
        concept_key: str = "",
        concept_value: str = "",
    ) -> tuple[bool, str]:
        """超了没有？超了就把该压的材料摆出来，但不替它压。

        只算已生效的条目：候选还没真正落库，让它占位就等于让还不算数的东西
        挤掉算数的东西。
        """
        limit = self.max_tokens_per_person
        formal = [
            claim
            for claim in self.store.list_claims(scope, person_id=person.id)
            if claim.lifecycle == "formal"
        ]
        # 配额要算的是「写完之后有多少」，不是「现在有多少 + 又来一份」。
        #
        # 重申一条已经生效的认识时，`_upsert` 会更新原条目而不是新增一条，
        # 净增长是零。可这里原本把 incoming 无条件加到 used 上，而 used 里
        # 已经含着那条自己——同一份内容被算了两遍。后果是一条认识只要超过
        # 配额的一半，就再也重申不了（真机：68/120 的占用，重申一个字没改的
        # 同一条被拒），而重申恰恰是维持它有效的必需动作。
        被替换的 = next(
            (
                claim
                for claim in formal
                if claim.concept_key == concept_key
                and claim.concept_value == concept_value
            ),
            None,
        )
        留存 = [claim for claim in formal if claim is not 被替换的]
        used = count_tokens_approx("\n".join(claim.content for claim in 留存))
        if used + count_tokens_approx(incoming) <= limit:
            return False, ""

        # 分层照 `I` 的模式：按 aspect 归组摆出来，一层一层看，不跨层揉。
        grouped: dict[str, list[ThemClaim]] = {}
        for claim in formal:
            grouped.setdefault(claim.aspect, []).append(claim)
        lines = [
            f"关于{person.display_name}的认识已经满了（{used}/{limit} token），这条先写不进去。",
            "先比对相关记忆，把下面能合并的几条用 delete_id 撤掉，再写压缩后的那一条。",
            "撤回不需要确认，那是你自己的判断；但压缩后的新条目要重新攒三天。",
            "",
        ]
        for aspect in sorted(grouped):
            lines.append(f"【{aspect}】")
            for claim in grouped[aspect]:
                lines.append(f"  - [{claim.id}] {claim.content}")
        return True, "\n".join(lines)

    # --- 读回与浮现 ---

    async def recall(
        self, *, query: str = "", max_results: int = 12, with_pending: bool = True
    ) -> str:
        """模型显式读回。命中的人算被提起一次。

        `with_pending` 控制要不要附上「还在攒的候选」清单。显式调工具时给，
        breath / dream 的追加块不给——欠账是待办，而浮现是「想起了什么」，
        往里面塞待办会让每一次呼吸都多一段催办。
        """
        scope = self._require_scope()
        persons = self.store.list_persons(scope)
        if not persons:
            return ""
        # 称呼变迁的提醒**不看命中**：人类改了名字，下次浮现就该说一次，
        # 哪怕这次的 query 跟那个人毫无关系。挂在命中上的话，一个久不被提起的
        # 人改了名，这条提醒可能几个月都出不来——那时再说已经没有意义了。
        notices = self._take_rename_notices(scope, persons)
        # 人类的纠错留言同样不看命中，而且**不占每人的 token 配额**：
        # 配额管的是模型自己沉淀了多少，人类说的话不该挤掉模型的记忆。
        human_notes = self._take_human_notes(scope, persons)

        matched = self._match_persons(query, persons) if query else self._top_persons(persons)
        for person in matched:
            self._touch_person(scope, person)
        块 = await self._render(scope, matched, max_results=max_results) if matched else ""

        段: list[str] = []
        if 块:
            段.append(块)
        if with_pending:
            欠账 = self._pending_digest(scope, persons if not query else matched)
            if 欠账:
                段.append(欠账)
        if notices:
            段.append(
                "[信息变迁：有人在前端改了称呼。]\n"
                + "\n".join(f"- {n}" for n in notices)
            )
        if human_notes:
            段.append(
                "[有人给你留了话，是对你记下的东西提出的更正。信不信、改不改由你自己定。]\n"
                + "\n".join(f"- {n}" for n in human_notes)
            )
        return "\n\n".join(段)

    def _pending_digest(self, scope: Scope, persons: list[Person]) -> str:
        """把还在攒的候选列出来，附上重申需要的那两个键。

        ## 没有这一段，三日门槛根本走不完

        候选不进召回（那是对的：还没算数的东西不该被当认识用）。但它同时意味着
        **写完就失联**——重申要求「同一个 concept_key + concept_value 再写一次」，
        而那两个字符串只存在于写它的那次对话里。跨会话之后我记不得自己填过什么，
        于是那条候选永远停在原地，门槛不是「难通过」，是「无法通过」。

        真机试用时就是这么发现的：写完一条候选，读回是空的，再没有任何入口
        能问出「我有哪些在攒的」。

        所以这里列的不是认识，是**欠账清单**：明写还没算数、还差几天，
        并给出重申要用的键。放在正常读回内容之后，与已生效的部分结构分离——
        看得见自己写过什么，不等于可以把它当成已经成立的判断。
        """
        条目: list[str] = []
        for person in persons:
            for claim in self.store.list_claims(scope, person_id=person.id):
                if claim.lifecycle != "candidate":
                    continue
                还差 = max(0, REQUIRED_CONFIRMATIONS - claim.review_date_count)
                条目.append(
                    f"- {person.display_name}｜{claim.concept_key}="
                    f"{claim.concept_value}｜{claim.aspect}\n"
                    f"  「{claim.content}」还差 {还差} 个不同的日子\n"
                    f"  id={claim.id}"
                )
        if not 条目:
            return ""
        return (
            "[下面这些还没算数，是你自己在攒的。想让哪条立住，就用同一个 "
            "concept_key + concept_value 再写一次；改主意了就别再确认，"
            "它不会自己生效——也可以用 delete_id=<id> 现在就撤掉。]\n"
            + "\n".join(条目)
        )

    async def surface(self, *, query: str = "") -> str:
        """给 breath / dream 用的追加块。

        独立通道：只在浮现结果**之后**追加，不参与融合打分。关掉 them，
        breath / dream 的输出必须与没有这个模块时逐字一致（rule.md 13.3）。
        """
        try:
            return await self.recall(query=query, with_pending=False)
        except ThemStoreError:
            # them 没开或库不可用：浮现照常，不该因为一个可选模块而失败。
            return ""

    def _top_persons(self, persons: list[Person]) -> list[Person]:
        """无 query 时按衰减权重取前三。

        名额不需要另设规则去争，也不需要一条"多久算冷"的阈值：常被提起的人
        自然排在前面，久不提起的自己沉下去。这就是"按提及时间次数自然衰减"
        的全部实现。
        """
        ranked = sorted(persons, key=self._person_score, reverse=True)
        return ranked[:MAX_SURFACED_PERSONS]

    @staticmethod
    def _match_persons(query: str, persons: list[Person]) -> list[Person]:
        """姓名命中：命中任一个登记的名字就返回整份。

        分词用 BM25 那套（jieba），保证和记忆检索对同一段 query 的切法一致；
        再补一次整串包含，挡住分词把名字切碎的情况。

        有 query 时不受前三名额限制——认人不该因为这个人最近没被提起就失败。
        """
        text = str(query or "").strip().casefold()
        if not text:
            return []
        try:
            from bm25_index import _tokenize

            tokens = {token.casefold() for token in _tokenize(text)}
        except Exception:
            tokens = set(text.split())
        matched: list[Person] = []
        for person in persons:
            keys = person.name_keys
            if keys & tokens or any(key in text for key in keys):
                matched.append(person)
        return matched

    async def _render(
        self, scope: Scope, persons: list[Person], *, max_results: int
    ) -> str:
        """渲染成一条 JSON。

        用 JSON 而不是散文，是因为这些话说的全是**别人**：混在自然语言里返回，
        容易幻觉的模型会把「Zoey 说话很直接」重述成用户的属性。
        一条 JSON 带 speaker/person 字段，归属是结构性的，不靠措辞。
        """
        payload: list[dict[str, Any]] = []
        for person in persons:
            claims = await self._drop_unsupported(
                self.store.list_claims(
                    scope, person_id=person.id, callable_only=True
                )[:max_results]
            )
            # claim_id 必须带上：工具签名里有 delete_id，描述也写着「带 delete_id
            # 是撤回一条」，但在这之前**没有任何路径把 id 交出来过**——那个参数
            # 等于够不着。一个宣称得到、却拿不到前提的能力，比没有更糟。
            notes = [
                {
                    "claim_id": claim.id,
                    "aspect": claim.aspect,
                    "content": claim.content,
                }
                for claim in claims
                if not contains_forbidden_subject(claim.content)
            ]
            if notes:
                payload.append(
                    {
                        "person": person.display_name,
                        # 「听人类描述过的人」和「我自己遇到过的人」是两种认识，
                        # 混起来就是张冠李戴。人类那一侧靠名册的分组看得见，
                        # 模型这一侧得靠这个字段——否则它读回时无从分辨自己
                        # 到底见没见过这个人，而这恰恰是最该谨慎的那一档。
                        # 3.6.3 起这是一个独立字段，不再由 origin 推导。
                        # 推导版本的问题：想把某人标成「只听说过」，就必然连带
                        # 把自己关于他的私有认识对人类公开——那是同一个开关。
                        "known_via": person.known_via,
                        "notes": notes,
                    }
                )
        if not payload:
            return ""
        encoded = json.dumps(
            {
                "them": payload,
                "attribution_note": "about other people; not the user, not me",
                "known_via_note": (
                    "heard_from_user = I have never met this person; everything here "
                    "came from the user describing them, so it may be second-hand or "
                    "mistaken. met_myself = my own first-hand impression."
                ),
            },
            ensure_ascii=False,
        )
        return f"{_SURFACE_HEADER}\n```json\n{encoded}\n```"

    # --- 撤回 ---

    async def delete(self, claim_id: str) -> str:
        """模型撤回自己写的一条。不需要三次确认。

        立一条要三个自然日，是因为"还站不站得住"要时间来验；撤一条不需要，
        是因为模型此刻已经知道它不站得住了。收回一个判断不该比立一个更难。
        """
        scope = self._require_scope()
        claim = self.store.get_claim(scope, str(claim_id or "").strip())
        if claim is None:
            raise ValueError(f"没有这条 them：{claim_id}")
        self.store.put_claim(
            replace(
                claim,
                lifecycle="expired",
                review_state="pending",
                valid_until=utc_now(),
                needs_recompute=False,
            ),
            expected_revision=claim.revision,
        )
        return f"撤回了：{claim.content}"

    async def _drop_unsupported(self, claims: list[ThemClaim]) -> list[ThemClaim]:
        """依据已经塌掉的，当场失效并从这次返回里拿掉。

        闸二的后半段。实现与 you 共用（`partition_by_live_evidence`），
        原因见那边的 docstring：桶变动观察者那条路在 3.4.x 被拆掉之后
        一直没有替代，`remove_bucket_evidence` 也就成了只有测试在调的死代码。
        """
        live, dead = await partition_by_live_evidence(
            self.bucket_mgr, claims, min_buckets=MIN_SUPPORTING_BUCKETS
        )
        now = utc_now()
        for claim in dead:
            try:
                self.store.put_claim(
                    replace(
                        claim,
                        lifecycle="expired",
                        review_state="pending",
                        valid_until=now,
                        needs_recompute=False,
                    ),
                    expected_revision=claim.revision,
                )
            except ThemStoreError:
                self.logger.info("them claim expiry deferred: %s", claim.id)
        return live

    async def remove_bucket_evidence(self, bucket_id: str) -> None:
        """闸二的持续那一半：依据没了，这条认识就不再算数。

        门槛是 MIN_SUPPORTING_BUCKETS 而不是"一个都不剩"——立的时候要两个出处，
        塌到一个之后还继续生效，等于门槛只在入口处存在。
        """
        try:
            scope = self._require_scope()
        except ThemStoreError:
            return
        for claim in self.store.list_claims(scope):
            if not any(edge.bucket_id == bucket_id for edge in claim.evidence):
                continue
            evidence = tuple(edge for edge in claim.evidence if edge.bucket_id != bucket_id)
            supporting = len({edge.bucket_id for edge in evidence if edge.stance == "supports"})
            now = utc_now()
            updated = replace(
                claim,
                evidence=evidence,
                evidence_revision=evidence_digest(evidence),
                lifecycle="expired" if supporting < MIN_SUPPORTING_BUCKETS else "candidate",
                review_state="pending",
                valid_from=None,
                valid_until=now if supporting < MIN_SUPPORTING_BUCKETS else None,
                needs_recompute=False,
            )
            self.store.put_claim(updated, expected_revision=claim.revision)

    def diagnostics(self) -> dict[str, Any]:
        return self.store.integrity_report()
