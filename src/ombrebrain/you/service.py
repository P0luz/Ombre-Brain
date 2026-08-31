from __future__ import annotations

from dataclasses import replace
import asyncio
import hashlib
import logging
import re
from typing import Any, Mapping

from ombrebrain.storage.source_store import source_links_from_metadata
from utils import count_tokens_approx, parse_bool

from .models import (
    POLICY_VERSION,
    VALID_ASPECTS,
    VALID_BASES,
    EvidenceEdge,
    ModuleState,
    ReviewReceipt,
    Scope,
    YouClaim,
    evidence_digest,
    utc_now,
)
from .safety import contains_forbidden_subject, leaks_protected_text
from .store import YouStore, YouStoreError


_CONCEPT_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{2,119}$")
_CONCEPT_VALUE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
_CORE_ASPECTS = frozenset({"preferred_address", "explicit_boundary"})


def _aspect_choices() -> str:
    return " / ".join(sorted(VALID_ASPECTS))


def _basis_choices() -> str:
    return " / ".join(sorted(VALID_BASES))


def _core_aspect_choices() -> str:
    return " / ".join(sorted(_CORE_ASPECTS))
_IGNORED_BUCKET_TYPES = frozenset({"archived", "feel", "plan", "letter", "self", "i"})
_MAX_HINT_RESULTS = 6
_MAX_HINT_TOKENS = 160

# 闸一：立一条 you 要模型在几个**不同自然日**重申。改一条同理——证据或正文一变，
# 先前的重申就不算数，得重新攒。
REQUIRED_CONFIRMATIONS = 3
# 闸二：一条 you 至少要几个记忆桶撑着。「一条认识不能只有一个出处」。
MIN_SUPPORTING_BUCKETS = 2


def _bucket_still_counts(bucket: Mapping[str, Any] | None) -> bool:
    """这个桶还能不能撑着一条认识。

    只认**删除**，不认归档。

    这两件事看着像，落地方式也不一样（软删除盖 `deleted_at` 并移进 archive/，
    归档只把 type 改成 archived），但真正的分界不在实现，在语义：

    - 删除是有人决定不要它了 → 证据没了，认识跟着失效
    - 归档多半是**自动衰减**的结果 → 只改变可见性（rule.md 第 9 条），
      原文还在，证据链接与审计能力都还在（SPEC 9.3）

    我一度把归档也算成失效，理由是 `Them` 的工具描述写着「被归档或删除」。
    那是改反了方向：自动衰减归档是常态，让它触发失效等于**一条攒了三天才立住
    的认识，会因为某个依据自然淡出而被时间清空**——和「立得那么难」的设计意图
    直接冲突。工具描述那句话本身才是该改的。

    归档同时带 `deleted_at` 的，按删除处理——`bucket_mgr.get()` 对这种桶直接
    返回 None，这里连判都不用判。
    """
    if not isinstance(bucket, Mapping):
        return False
    metadata = bucket.get("metadata")
    if not isinstance(metadata, Mapping):
        return True
    return not (metadata.get("deleted_at") or metadata.get("tombstone"))


async def partition_by_live_evidence(
    bucket_mgr: Any,
    claims: list,
    *,
    min_buckets: int = MIN_SUPPORTING_BUCKETS,
) -> tuple[list, list]:
    """按依据桶是否还在，把认识分成「还站得住」和「该失效」两堆。

    ## 为什么改成读时校验

    闸二有两半：立的时候要两个出处，依据塌下去之后也不能继续算数——
    立的时候要求两个出处、塌到一个之后还继续生效，等于门槛只在入口处存在。

    后半段原先挂在 `bucket_mgr` 的桶变动观察者上，由 `observe_bucket_change`
    把失效工作入队。3.4.x 拿掉 LLM 那轮把 `observe_bucket_change` 和 outbox
    一起删了，**但没接替代路径**——于是 `_remove_bucket_evidence` 变成了死代码，
    而「依据被归档或删除，这条认识会自动失效」还写在工具描述、rule.md 和
    SPEC 里。功能表里写了没实现的东西，这是要修的那种缺陷。

    没有把观察者接回来，是因为那条路要求 observer 同步、只做持久化入队——
    那就得把刚拆掉的队列重新装回去。改成读时校验不需要队列，语义也更准：
    失效判断发生在**要用它的时候**，而不是桶变动的那一刻。

    代价是每次读回多几次桶查询。控制在可接受范围的办法是只校验**将要返回的**
    那几条：配额限制了每份认识的条数，一次校验通常只查两三个桶，
    而且同一个桶在一次调用里只查一次。

    `bucket_mgr.get` 对软删除的桶返回 None，正好是这里要的语义。
    """
    live: list = []
    dead: list = []
    seen: dict[str, bool] = {}
    for claim in claims:
        supporting: set[str] = set()
        for edge in claim.evidence:
            if edge.stance != "supports":
                continue
            if edge.bucket_id not in seen:
                try:
                    seen[edge.bucket_id] = _bucket_still_counts(
                        await bucket_mgr.get(edge.bucket_id)
                    )
                except Exception:
                    # 读不到桶不等于桶没了（可能是磁盘一时不可用）。
                    # 这种时候按「还在」处理：宁可多返回一条，也不要因为一次
                    # 读取抖动就把一条攒了三天的认识判死。
                    seen[edge.bucket_id] = True
            if seen[edge.bucket_id]:
                supporting.add(edge.bucket_id)
        (live if len(supporting) >= min_buckets else dead).append(claim)
    return live, dead


class YouService:
    """You 的开关、写入把关与安全召回。

    这里**一次 LLM 都不调**。抽取、复核、抽象三层曾经都走 LLM，被整体拿掉了：
    一个替模型总结、替模型判断、替模型决定何时转正的中间层，和「这是你的记忆，
    你的想法优先」是直接冲突的。认识由模型自己写，验证靠两道结构性的闸——
    三个不同自然日的重申，以及与真实记忆桶的显式关系。

    留给后来者：`dehydrator` 这个依赖还在构造签名里，是给 `_protected_sources`
    之外的将来留的口子；**不要**再拿它给 You 加自动抽取或自动复核。
    """

    def __init__(
        self,
        *,
        store: YouStore,
        bucket_mgr: Any,
        dehydrator: Any,
        source_store: Any,
        logger: logging.Logger | None = None,
    ) -> None:
        self.store = store
        self.bucket_mgr = bucket_mgr
        self.dehydrator = dehydrator
        self.source_store = source_store
        self.logger = logger or logging.getLogger("ombre_brain.you")
        # 「写 claim → 读全部 claims → 写 projection」这三步必须连着做完。
        # 中间被另一路插进来，后完成的那一方会拿着自己那份旧快照覆盖新投影：
        # 真源有 2 条认识，投影里只剩 1 条，而 put_projection 是按 scope
        # 无条件 upsert，revision 也拦不住（两边算出来的 projection_revision
        # 可能相同，差别只在 claim 数量）。
        self._projection_lock = asyncio.Lock()

    def status(self) -> ModuleState:
        try:
            return self.store.get_state()
        except YouStoreError:
            return ModuleState.disabled()

    def set_enabled(self, enabled: bool, *, expected_revision: int | None = None) -> ModuleState:
        # 关闭时不再需要清队列——没有队列了。已写下的认识原样留着，
        # 重新打开时它们还在（关闭只是把出口收起来，不是抹掉判断）。
        return self.store.set_enabled(enabled, expected_revision=expected_revision)

    async def _remove_bucket_evidence(self, scope: Scope, bucket_id: str) -> None:
        """闸二的持续那一半：依据没了，这条认识就不再算数。

        门槛是 MIN_SUPPORTING_BUCKETS 而不是"一个都不剩"——立的时候要求两个
        出处，塌到一个之后还继续生效，等于门槛只在入口处存在。
        """

        def mutation(claim: YouClaim) -> YouClaim:
            evidence = tuple(edge for edge in claim.evidence if edge.bucket_id != bucket_id)
            now = utc_now()
            supporting = len({edge.bucket_id for edge in evidence if edge.stance == "supports"})
            if supporting < MIN_SUPPORTING_BUCKETS:
                return replace(
                    claim,
                    evidence=evidence,
                    evidence_revision=evidence_digest(evidence),
                    lifecycle="expired",
                    review_state="pending",
                    valid_until=now,
                    needs_recompute=False,
                )
            return replace(
                claim,
                evidence=evidence,
                evidence_revision=evidence_digest(evidence),
                lifecycle="candidate",
                review_state="pending",
                valid_from=None,
                valid_until=None,
                needs_recompute=False,
            )

        self.store.mutate_claims_for_bucket(scope, bucket_id, mutation)

    def _protected_sources(self, metadata: Mapping[str, Any]) -> tuple[str, list[str]]:
        source_id = ""
        texts: list[str] = []
        for link in source_links_from_metadata(metadata):
            if str(link.get("status") or "active") != "active":
                continue
            ref = str(link.get("ref") or "")
            if not ref:
                continue
            if not source_id:
                source_id = ref
            text = self.source_store.read(ref)
            texts.append(text)
        return source_id, texts

    @staticmethod
    def _validate_observation(
        observation: Mapping[str, Any],
        *,
        protected_texts: list[str],
    ) -> tuple[dict[str, Any] | None, str]:
        """校验一条观察，返回 (归一化结果, 失败原因)；通过时原因是空串。

        为什么要返回原因：这里有九道各自独立的闸，原先全都只返回 `None`，
        调用方只能把九种可能拼成一句话丢回去。收到的人看见「aspect / basis
        必须是允许值」却不知道允许值是什么，也不知道自己到底撞了哪一条——
        而其中两条（核心 aspect 要 explicit、stable_fact 还要 long_term）
        连提都没提，于是枚举填对了照样被拒，且报错一字不变。

        枚举值直接从 VALID_* 生成，不要在文案里手抄一份——手抄的那份会和
        代码分家。
        """
        aspect = str(observation.get("aspect") or "").strip().lower()
        concept_key = str(observation.get("concept_key") or "").strip().lower()
        concept_value = str(observation.get("concept_value") or "").strip().lower()
        content = str(observation.get("content") or "").strip()
        basis = str(observation.get("basis") or "").strip().lower()

        if aspect not in VALID_ASPECTS:
            got = f"「{aspect}」" if aspect else "（空）"
            return None, f"aspect {got} 不是允许值。可选：{_aspect_choices()}。"
        if basis not in VALID_BASES:
            got = f"「{basis}」" if basis else "（空）"
            return None, f"basis {got} 不是允许值。可选：{_basis_choices()}。"
        if not _CONCEPT_KEY_RE.fullmatch(concept_key):
            return None, (
                f"concept_key「{concept_key}」不合法：要 snake_case，"
                "小写字母开头，之后只能是小写字母 / 数字 / 下划线，3~120 字符。"
            )
        if not _CONCEPT_VALUE_RE.fullmatch(concept_value):
            return None, (
                f"concept_value「{concept_value}」不合法：小写字母或数字开头，"
                "之后只能是小写字母 / 数字 / 下划线 / 连字符，1~80 字符。"
            )
        if not content:
            return None, "content 不能为空。"
        if len(content) > 500:
            return None, f"content 有 {len(content)} 字，上限 500 字。"
        if contains_forbidden_subject(content, concept_key, concept_value):
            return None, "这条落在禁止主题里，写不进去。"
        if leaks_protected_text(content, protected_texts):
            return None, "这条照抄了依据桶的原文；请写成你自己的判断，不要复述原文。"

        explicit = bool(observation.get("explicit"))
        long_term = bool(observation.get("long_term"))
        if aspect in _CORE_ASPECTS and not explicit:
            return None, (
                f"aspect「{aspect}」属于核心项（{_core_aspect_choices()}），"
                "只能记人类明确说过的话，必须同时传 explicit=True。"
            )
        if aspect == "stable_fact" and (not explicit or not long_term):
            return None, (
                "aspect「stable_fact」要同时传 explicit=True 与 long_term=True："
                "长期事实必须是人类明确说过、且明确说了长期有效的。"
            )
        return {
            "aspect": aspect,
            "concept_key": concept_key,
            "concept_value": concept_value,
            "content": content,
            "basis": basis,
            "explicit": explicit,
            "long_term": long_term,
        }, ""

    async def write(
        self,
        *,
        content: str,
        bucket_ids: list[str],
        aspect: str,
        concept_key: str,
        concept_value: str,
        basis: str = "observed_pattern",
        explicit: bool = False,
        long_term: bool = False,
    ) -> tuple[YouClaim, str]:
        """模型写下（或重申）一条对人类一方的认识。返回 (条目, 给模型看的话)。

        这是 You 唯一的写入口，**不调用任何 LLM**。同一个
        concept_key + concept_value 再写一次就是"重申"：攒够
        REQUIRED_CONFIRMATIONS 个不同自然日才真正生效。
        """

        state = self.status()
        if not state.enabled or state.scope is None:
            raise YouStoreError("unknown tool")
        scope = state.scope

        edges, protected_texts = await self._build_edges(bucket_ids, basis=basis)

        normalized, reason = self._validate_observation(
            {
                "aspect": aspect,
                "concept_key": concept_key,
                "concept_value": concept_value,
                "content": content,
                "basis": basis,
                "explicit": explicit,
                "long_term": long_term,
            },
            protected_texts=protected_texts,
        )
        if normalized is None:
            raise ValueError(f"这条写不进去：{reason}")

        # 写入与重建必须原子：中间放另一路进来，它读到的 claims 快照会漏掉
        # 这一条，而后完成的那一方会把自己的旧快照当成最新投影发布出去。
        async with self._projection_lock:
            claim = self._upsert_observation(scope, normalized, edges)
            self._rebuild_projection_locked(scope)

        if claim.lifecycle == "formal":
            return claim, f"记下了。这条已经生效：{claim.content}"
        still = max(0, REQUIRED_CONFIRMATIONS - claim.review_date_count)
        return claim, (
            f"先记成候选：{claim.content}\n"
            f"还要在另外 {still} 个不同的日子重新确认它，才会真正落库。"
            "改主意了就别再确认，它不会自己生效。"
        )

    async def _build_edges(
        self,
        bucket_ids: list[str],
        *,
        basis: str,
    ) -> tuple[tuple[EvidenceEdge, ...], list[str]]:
        """闸二：把模型给的 bucket_id 校验成显式关系，顺带收集要防泄漏的原文。

        校验不通过就抛，不降级不兜底——一条没有真实记忆撑着的认识，宁可写不进去。
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
                raise ValueError(
                    f"{bucket_id} 是 {bucket_type} 类型，不能作为 you 的依据。"
                )
            provenance = metadata.get("provenance")
            if isinstance(provenance, dict) and parse_bool(
                provenance.get("erasable"), default=False
            ):
                raise ValueError(f"{bucket_id} 是测试数据，不能作为 you 的依据。")
            body = str(bucket.get("content") or "").strip()
            if not body:
                raise ValueError(f"{bucket_id} 没有正文，不能作为依据。")
            source_id, source_texts = self._protected_sources(metadata)
            protected.append(body)
            protected.extend(source_texts)
            edges.append(
                EvidenceEdge(
                    bucket_id=bucket_id,
                    source_id=source_id,
                    stance="supports",
                    basis=basis,
                    # 必须是**桶内容的指纹**，不能是时间戳：evidence_revision 由
                    # 这些 edge 算出来，而重申收据绑 evidence_revision。用时间戳
                    # 的话每次重申都会让证据"变新"，先前攒的天数全部作废，三天
                    # 门槛永远也到不了。
                    # 反过来，桶正文真的被改了，指纹跟着变、收据作废、重新攒三天
                    # ——依据变了先前的确认就不算数，这个语义是对的。
                    bucket_revision="sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
                )
            )
        return tuple(sorted(edges, key=lambda item: item.bucket_id)), protected

    def _upsert_observation(
        self,
        scope: Scope,
        observation: Mapping[str, Any],
        edges: tuple[EvidenceEdge, ...],
    ) -> YouClaim:
        existing = self.store.list_claims(scope, concept_key=str(observation["concept_key"]))
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
        recall_policy = "core" if observation["aspect"] in _CORE_ASPECTS else "contextual"

        if same is None:
            claim = YouClaim.new(
                scope=scope,
                concept_key=str(observation["concept_key"]),
                concept_value=str(observation["concept_value"]),
                content=str(observation["content"]),
                aspect=str(observation["aspect"]),
                recall_policy=recall_policy,
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
            # 证据集合，管不到正文——但「修改也要三次确认」是 poluz 定死的，
            # 改一句话就悄悄沿用旧收据等于绕开闸一。
            content_changed = str(observation["content"]) != same.content
            receipts = () if content_changed else same.review_receipts
            claim = replace(
                same,
                content=str(observation["content"]),
                aspect=str(observation["aspect"]),
                recall_policy=recall_policy,
                evidence=evidence,
                evidence_revision=evidence_digest(evidence),
                review_receipts=receipts,
                # 正文一改就退回候选：已生效的那句话不能在没重新攒够三天的
                # 情况下被换掉。
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

        # 这里原本有一条 direct_formal 捷径：核心 aspect 或"明确且长期"的
        # stable_fact 可以跳过全部确认直接转正。已删除——poluz 定的是「未经三次
        # 确认的不真正落库」，没有例外。任何"这条一看就成立"的判断，都是在替
        # 模型决定它什么时候算数。
        claim = self._record_confirmation(claim)
        stored = self.store.put_claim(claim, expected_revision=expected_revision)
        return self._promote_if_ready(stored)

    def _record_confirmation(self, claim: YouClaim) -> YouClaim:
        """给这条认识记一笔"模型今天重申过"。同一天重复调用只算一次。

        收据绑当前的 evidence_revision：证据集合一变，先前的重申自动不算数
        （见 models.YouClaim.review_date_count），所以"改一条 you 也要重新攒
        三天"不需要另写逻辑。正文变更的重置在 write() 里单独处理，因为
        evidence_revision 不含正文。
        """

        # "今天"和收据时间戳必须同源：ReviewReceipt.review_date 取的是
        # reviewed_at 的前 10 位，这里若另用 datetime.now() 判重，两个时间源在
        # 跨日的那一瞬间会给出不同答案，可能让同一天记下两条收据。
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
            policy_version=POLICY_VERSION,
            result="reaffirmed",
        )
        return replace(claim, review_receipts=(*claim.review_receipts, receipt))

    def _promote_if_ready(self, claim: YouClaim) -> YouClaim:
        if claim.lifecycle != "candidate":
            return claim
        if (
            claim.independent_support_count < MIN_SUPPORTING_BUCKETS
            or claim.review_date_count < REQUIRED_CONFIRMATIONS
        ):
            return claim
        conflicts = [
            item
            for item in self.store.list_claims(claim.scope)
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

    async def rebuild_projection(self, scope: Scope) -> dict[str, Any]:
        """单独重建投影。写完 claim 紧接着重建的路径不要走这里——
        那种情况要把两步一起放进 _projection_lock，见 write()。"""
        async with self._projection_lock:
            return self._rebuild_projection_locked(scope)

    def _rebuild_projection_locked(self, scope: Scope) -> dict[str, Any]:
        """调用方必须已经持有 _projection_lock。"""
        claims = self.store.list_claims(scope, callable_only=True)
        projection_revision = max((claim.revision for claim in claims), default=0)
        payload = {
            "schema_version": 1,
            "policy_version": POLICY_VERSION,
            "projection_revision": projection_revision,
            "claim_ids": [claim.id for claim in claims],
            "items": [
                {
                    "claim_id": claim.id,
                    "claim_revision": claim.revision,
                    "aspect": claim.aspect,
                    "content": claim.content,
                }
                for claim in claims
            ],
            "generated_at": utc_now(),
        }
        self.store.put_projection(scope, projection_revision, payload)
        return payload

    async def recall(
        self,
        *,
        query: str = "",
        aspect: str = "",
        max_results: int = _MAX_HINT_RESULTS,
        with_ids: bool = False,
    ) -> str:
        state = self.status()
        if not state.enabled or state.scope is None:
            raise YouStoreError("unknown tool")
        normalized_aspect = str(aspect or "").strip().lower()
        if normalized_aspect and normalized_aspect not in VALID_ASPECTS:
            return ""
        try:
            result_limit = max(1, min(_MAX_HINT_RESULTS, int(max_results)))
        except (TypeError, ValueError, OverflowError):
            result_limit = _MAX_HINT_RESULTS
        projection = self.store.get_projection(state.scope)
        if projection is None:
            projection = await self.rebuild_projection(state.scope)
        claims_by_id = {
            claim.id: claim
            for claim in self.store.list_claims(state.scope, callable_only=True)
        }
        candidates = [
            claims_by_id[item["claim_id"]]
            for item in projection.get("items", [])
            if isinstance(item, dict)
            and item.get("claim_id") in claims_by_id
            and (not normalized_aspect or item.get("aspect") == normalized_aspect)
            and (bool(query) or claims_by_id[item["claim_id"]].recall_policy == "core")
        ]
        candidates.sort(key=lambda claim: self._query_score(claim, query), reverse=True)
        if query:
            candidates = [claim for claim in candidates if self._query_score(claim, query) > 0]

        candidates = await self._drop_unsupported(candidates[:result_limit])

        # 这里原本还要再过一层 LLM（abstract_you_hint），把已经成立的认识磨成
        # "概念词组 + 关系词"再交出去。删掉了：模型自己写下的判断，没有理由让
        # 另一个模型改写一遍才还给它。正文直接返回。
        # id 默认不给，要才给。
        #
        # delete_id 此前是够不着的：这条路径从来不交出 claim id，而它是撤回的
        # 唯一入口。但无条件带上也不行——一个 id 约 12 token，而这里总预算只有
        # 160，实测 4 条会掉到 3 条，为一个偶尔才用的能力常年砍掉 1/4 正文。
        #
        # 所以照 breath_search(quotes=True) 那套：默认一字不少，想撤回时明确要。
        lines = ["[你自己写下的、关于对方的长期认识；不是此刻的事实，按需自行判断]"]
        if with_ids:
            lines.append("[带 id 是为了撤回：You(delete_id=\"...\")]")
        for claim in candidates:
            if contains_forbidden_subject(claim.content):
                continue
            next_line = "- " + claim.content
            if with_ids:
                next_line += f"  [id={claim.id}]"
            if count_tokens_approx("\n".join([*lines, next_line])) > _MAX_HINT_TOKENS:
                break
            lines.append(next_line)
        return "\n".join(lines) if len(lines) > 1 else ""

    async def _drop_unsupported(self, claims: list[YouClaim]) -> list[YouClaim]:
        """依据已经塌掉的，当场失效并从这次返回里拿掉。

        闸二的后半段（见 `partition_by_live_evidence`）。失效状态写回库里，
        所以只查这一次——下次这条已经是 expired，`callable_only` 直接把它挡在外面。
        """
        live, dead = await partition_by_live_evidence(self.bucket_mgr, claims)
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
            except YouStoreError:
                # 并发下别处先改了这一条。这次不返回它就够了，状态下次再收。
                self.logger.info("you claim expiry deferred: %s", claim.id)
        return live

    async def delete(self, claim_id: str) -> str:
        """模型撤回自己写的一条认识。不需要三次确认。

        立一条要三个自然日，是因为"还站不站得住"要时间来验；撤一条不需要，
        是因为模型此刻已经知道它不站得住了。收回一个判断不该比立一个更难。
        """

        state = self.status()
        if not state.enabled or state.scope is None:
            raise YouStoreError("unknown tool")
        claim = self.store.get_claim(state.scope, str(claim_id or "").strip())
        if claim is None:
            raise ValueError(f"没有这条 you：{claim_id}")
        # 撤回同样是「改 claim → 重建投影」，与 write() 一个道理。
        async with self._projection_lock:
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
            self._rebuild_projection_locked(state.scope)
        return f"撤回了：{claim.content}"

    @staticmethod
    def _query_score(claim: YouClaim, query: str) -> float:
        normalized = "".join(char.casefold() for char in str(query or "") if char.isalnum())
        if not normalized:
            return 1.0 if claim.recall_policy == "core" else 0.5
        haystack = "".join(
            char.casefold()
            for char in f"{claim.concept_key}{claim.concept_value}{claim.content}"
            if char.isalnum()
        )
        if normalized in haystack:
            return 10.0 + len(normalized)
        if len(normalized) == 1:
            return 1.0 if normalized in haystack else 0.0
        grams = {normalized[index : index + 2] for index in range(len(normalized) - 1)}
        return float(sum(1 for gram in grams if gram in haystack)) / max(1, len(grams))

    def diagnostics(self) -> dict[str, Any]:
        return self.store.integrity_report()
