from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping
from uuid import uuid4


SCHEMA_VERSION = 1
POLICY_VERSION = "you-policy-v1"
VALID_ASPECTS = frozenset(
    {
        "preferred_address",
        "explicit_boundary",
        "stable_fact",
        "communication_preference",
        "interaction_habit",
    }
)
VALID_LIFECYCLES = frozenset({"candidate", "formal", "superseded", "expired"})
VALID_REVIEW_STATES = frozenset({"pending", "clear", "conflicting"})
VALID_RECALL_POLICIES = frozenset({"core", "contextual"})
VALID_STANCES = frozenset({"supports", "contradicts"})
VALID_BASES = frozenset(
    {"explicit_statement", "observed_pattern", "shared_event", "user_confirmation"}
)
_ID_RE = re.compile(r"^[a-z]+_[0-9a-f]{32}$")


def _choices(values: frozenset[str]) -> str:
    """把枚举直接渲染进报错。手抄一份文案迟早会和代码分家。"""
    return " / ".join(sorted(values))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _require_id(value: object, prefix: str) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized.startswith(f"{prefix}_") or not _ID_RE.fullmatch(normalized):
        raise ValueError(f"invalid {prefix} id")
    return normalized


def _require_text(value: object, field_name: str, *, limit: int = 1000) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > limit or "\x00" in normalized:
        raise ValueError(f"invalid {field_name}")
    return normalized


@dataclass(frozen=True)
class Scope:
    owner_instance_id: str
    observer_role_id: str
    subject_user_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "owner_instance_id", _require_id(self.owner_instance_id, "owner")
        )
        object.__setattr__(
            self, "observer_role_id", _require_id(self.observer_role_id, "role")
        )
        object.__setattr__(
            self, "subject_user_id", _require_id(self.subject_user_id, "user")
        )

    @classmethod
    def new(cls) -> "Scope":
        return cls(_new_id("owner"), _new_id("role"), _new_id("user"))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Scope":
        return cls(
            owner_instance_id=value.get("owner_instance_id", ""),
            observer_role_id=value.get("observer_role_id", ""),
            subject_user_id=value.get("subject_user_id", ""),
        )

    @property
    def key(self) -> str:
        # 三个 id 直接拼。这是身份不是内容，没有「变了要检测」的需求，原来
        # sha256 一遍只是把它变得不可读：出问题时从库里捞出一串 64 位十六进制，
        # 看不出属于哪个 owner/role/user，还得反查。id 本身就在
        # module_state.scope_json 里明文存着，拼接不多暴露任何东西。
        # _ID_RE 限定了 `前缀_32位hex`，字符集里没有分隔符，拼不出歧义。
        return "|".join(
            (self.owner_instance_id, self.observer_role_id, self.subject_user_id)
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "owner_instance_id": self.owner_instance_id,
            "observer_role_id": self.observer_role_id,
            "subject_user_id": self.subject_user_id,
        }


@dataclass(frozen=True)
class ModuleState:
    enabled: bool = False
    scope: Scope | None = None
    state_revision: int = 0
    changed_at: str = ""
    changed_by: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "enabled", bool(self.enabled))
        try:
            revision = int(self.state_revision)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("invalid state_revision") from exc
        if revision < 0:
            raise ValueError("invalid state_revision")
        if self.enabled and self.scope is None:
            raise ValueError("enabled state requires a complete scope")
        object.__setattr__(self, "state_revision", revision)
        object.__setattr__(self, "changed_at", str(self.changed_at or ""))
        object.__setattr__(self, "changed_by", str(self.changed_by or ""))

    @classmethod
    def disabled(cls) -> "ModuleState":
        return cls()

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "scope": self.scope.to_dict() if self.scope else None,
            "state_revision": self.state_revision,
            "changed_at": self.changed_at,
            "changed_by": self.changed_by,
        }


@dataclass(frozen=True)
class EvidenceEdge:
    """一条 you 认识与一个记忆桶的显式关系。

    edge 由模型写 you 的时候自己指定，不是系统从记忆里抽出来的——这是整个
    模块的地基（见 dev 侧设计文档「这是你的记忆，你的想法优先」）。

    这里原本还有一个 evidence_group_id，用来把"同一件事拆成的多个桶"聚成一组，
    免得同一件事被算成多份独立支持。那是自动抽取时代的产物：系统不知道模型
    心里算不算同一件事，只能靠桶间关系去猜。现在模型自己挑要绑哪几个桶，
    "算不算独立"由它自己决定，这个字段没有了意义。
    """

    bucket_id: str
    stance: str
    basis: str
    bucket_revision: str
    source_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "bucket_id", _require_text(self.bucket_id, "bucket_id", limit=200))
        stance = str(self.stance or "").strip().lower()
        basis = str(self.basis or "").strip().lower()
        # 这里是 basis 最早的一道闸——`_build_edges` 先于 service 的观察校验
        # 跑到，所以非法 basis 永远在这里就炸。报错必须自己带上允许值，
        # 否则调用方拿到的是一句无从下手的英文，而后面那道带枚举的提示
        # 根本走不到。
        if stance not in VALID_STANCES:
            raise ValueError(f"stance「{stance}」不是允许值。可选：{_choices(VALID_STANCES)}。")
        if basis not in VALID_BASES:
            got = f"「{basis}」" if basis else "（空）"
            raise ValueError(f"basis {got} 不是允许值。可选：{_choices(VALID_BASES)}。")
        object.__setattr__(self, "stance", stance)
        object.__setattr__(self, "basis", basis)
        object.__setattr__(
            self, "bucket_revision", _require_text(self.bucket_revision, "bucket_revision", limit=100)
        )
        source_id = str(self.source_id or "").strip()
        if len(source_id) > 200 or "\x00" in source_id:
            raise ValueError("invalid source_id")
        object.__setattr__(self, "source_id", source_id)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvidenceEdge":
        return cls(**{key: value.get(key, "") for key in (
            "bucket_id", "stance", "basis", "bucket_revision", "source_id"
        )})

    def to_dict(self) -> dict[str, str]:
        return {
            "bucket_id": self.bucket_id,
            "source_id": self.source_id,
            "stance": self.stance,
            "basis": self.basis,
            "bucket_revision": self.bucket_revision,
        }


@dataclass(frozen=True)
class ReviewReceipt:
    """模型重申一条 you 的收据。一天最多记一条。

    原本这是 LLM 复核的收据（result 是另一个模型判"还站不站得住"）。现在
    复核那层 LLM 已经拿掉，收据记的是模型自己在某一天重新确认了这条认识：
    立一条 you 要三个不同自然日的重申，就是靠这些收据数出来的。

    result 保留三值是为了兼容既有存量与 store 的解析，但语义变了：
    - reaffirmed：模型这天重新确认了它（原 remains_plausible）
    - contradicted / insufficient：留给模型将来主动标记推翻/存疑用，
      当前写入路径不产生这两种。
    """

    reviewed_at: str
    reviewer_role_id: str
    evidence_revision: str
    result: str
    policy_version: str = POLICY_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "reviewed_at", _require_text(self.reviewed_at, "reviewed_at", limit=80))
        object.__setattr__(
            self, "reviewer_role_id", _require_id(self.reviewer_role_id, "role")
        )
        object.__setattr__(
            self,
            "evidence_revision",
            _require_text(self.evidence_revision, "evidence_revision", limit=100),
        )
        result = str(self.result or "").strip().lower()
        # reaffirmed 是新语义；remains_plausible 是它的历史名字，继续收下，
        # 免得已经落库的存量收据在升级后突然读不出来。
        if result not in {"reaffirmed", "remains_plausible", "contradicted", "insufficient"}:
            raise ValueError("invalid review result")
        object.__setattr__(self, "result", result)
        object.__setattr__(
            self, "policy_version", _require_text(self.policy_version, "policy_version", limit=80)
        )

    @property
    def review_date(self) -> str:
        return self.reviewed_at[:10]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ReviewReceipt":
        return cls(
            reviewed_at=value.get("reviewed_at", ""),
            reviewer_role_id=value.get("reviewer_role_id", ""),
            evidence_revision=value.get("evidence_revision", ""),
            result=value.get("result", ""),
            policy_version=value.get("policy_version") or POLICY_VERSION,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "reviewed_at": self.reviewed_at,
            "reviewer_role_id": self.reviewer_role_id,
            "evidence_revision": self.evidence_revision,
            "policy_version": self.policy_version,
            "result": self.result,
        }


# `_new_id` / `_require_id` / `_require_text` 的公开别名。
# `them` 是照 `You` 的形态建的（rule.md 13.3），复用这三件比各自抄一份好：
# 抄一份意味着两边的 id 规则会慢慢漂开，而它们本该一模一样。
new_id = _new_id
require_id = _require_id
require_text = _require_text


@dataclass(frozen=True)
class YouClaim:
    # 子类（`them.models.ThemClaim`）换 id 前缀用。除此之外 them 的条目
    # 与 you 完全同构，这也是 rule.md 13.3 写「形态同 You」的落点。
    ID_PREFIX = "you"

    id: str
    scope: Scope
    concept_key: str
    concept_value: str
    content: str
    aspect: str
    lifecycle: str = "candidate"
    review_state: str = "pending"
    recall_policy: str = "contextual"
    sensitivity: str = "normal"
    evidence: tuple[EvidenceEdge, ...] = field(default_factory=tuple)
    review_receipts: tuple[ReviewReceipt, ...] = field(default_factory=tuple)
    valid_from: str | None = None
    valid_until: str | None = None
    replaces: str | None = None
    conflicts_with: tuple[str, ...] = field(default_factory=tuple)
    evidence_revision: str = ""
    projection_revision: int = 0
    needs_recompute: bool = False
    revision: int = 1
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _require_id(self.id, self.ID_PREFIX))
        object.__setattr__(
            self, "concept_key", _require_text(self.concept_key, "concept_key", limit=120).lower()
        )
        object.__setattr__(
            self, "concept_value", _require_text(self.concept_value, "concept_value", limit=240).lower()
        )
        object.__setattr__(self, "content", _require_text(self.content, "content", limit=500))
        aspect = str(self.aspect or "").strip().lower()
        lifecycle = str(self.lifecycle or "").strip().lower()
        review_state = str(self.review_state or "").strip().lower()
        recall_policy = str(self.recall_policy or "").strip().lower()
        if aspect not in VALID_ASPECTS:
            got = f"「{aspect}」" if aspect else "（空）"
            raise ValueError(f"aspect {got} 不是允许值。可选：{_choices(VALID_ASPECTS)}。")
        if lifecycle not in VALID_LIFECYCLES:
            raise ValueError("invalid claim lifecycle")
        if review_state not in VALID_REVIEW_STATES:
            raise ValueError("invalid claim review_state")
        if recall_policy not in VALID_RECALL_POLICIES:
            raise ValueError("invalid claim recall_policy")
        if str(self.sensitivity or "normal").strip().lower() != "normal":
            raise ValueError("sensitive claims are forbidden")
        object.__setattr__(self, "aspect", aspect)
        object.__setattr__(self, "lifecycle", lifecycle)
        object.__setattr__(self, "review_state", review_state)
        object.__setattr__(self, "recall_policy", recall_policy)
        object.__setattr__(self, "sensitivity", "normal")
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "review_receipts", tuple(self.review_receipts))
        object.__setattr__(self, "conflicts_with", tuple(str(v) for v in self.conflicts_with))
        for field_name in ("projection_revision", "revision"):
            try:
                parsed = int(getattr(self, field_name))
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"invalid {field_name}") from exc
            if parsed < (1 if field_name == "revision" else 0):
                raise ValueError(f"invalid {field_name}")
            object.__setattr__(self, field_name, parsed)

    @classmethod
    def new(
        cls,
        *,
        scope: Scope,
        concept_key: str,
        concept_value: str,
        content: str,
        aspect: str,
        recall_policy: str,
        evidence: tuple[EvidenceEdge, ...],
        review_state: str = "pending",
        conflicts_with: tuple[str, ...] = (),
    ) -> "YouClaim":
        return cls(
            id=_new_id(cls.ID_PREFIX),
            scope=scope,
            concept_key=concept_key,
            concept_value=concept_value,
            content=content,
            aspect=aspect,
            recall_policy=recall_policy,
            evidence=evidence,
            review_state=review_state,
            conflicts_with=conflicts_with,
            evidence_revision=evidence_digest(evidence),
        )

    @property
    def independent_support_count(self) -> int:
        """撑着这条认识的记忆桶有几个（闸二，门槛见 MIN_SUPPORTING_BUCKETS）。

        按 bucket_id 去重，不再按 evidence_group_id——模型自己挑的桶，算不算
        独立由它自己决定，系统不替它把"同一件事"并起来。
        """

        return len({edge.bucket_id for edge in self.evidence if edge.stance == "supports"})

    @property
    def review_date_count(self) -> int:
        """模型在几个不同自然日重申过这条（闸一，门槛 REQUIRED_CONFIRMATIONS）。

        绑定 evidence_revision：证据集合一变，先前的重申就不算数了，得按新的
        证据重新攒三天。改一条 you 因此天然也要三天，不用另写一套逻辑。
        """

        return len(
            {
                receipt.review_date
                for receipt in self.review_receipts
                if receipt.result in {"reaffirmed", "remains_plausible"}
                and receipt.evidence_revision == self.evidence_revision
            }
        )

    def callable_at(self, now: str | None = None) -> bool:
        current = now or utc_now()
        return bool(
            self.lifecycle == "formal"
            and self.review_state == "clear"
            and not self.needs_recompute
            and (not self.valid_from or self.valid_from <= current)
            and (not self.valid_until or current <= self.valid_until)
            and self.evidence
        )

    @classmethod
    def _extra_from_dict(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        """子类比 YouClaim 多出来的字段。基类没有多的。

        存在的理由是 `from_dict` 用 `cls(...)` 构造：子类（`ThemClaim`）继承它
        时，多出来的必填字段得有地方补进去，否则子类只能把这三十行抄一遍，
        然后两份解析逻辑开始各自演化。
        """
        return {}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "YouClaim":
        scope_raw = value.get("scope")
        if not isinstance(scope_raw, Mapping):
            raise ValueError("claim scope is missing")
        evidence_raw = value.get("evidence") or []
        receipts_raw = value.get("review_receipts") or []
        if not isinstance(evidence_raw, list) or not isinstance(receipts_raw, list):
            raise ValueError("invalid claim evidence or receipts")
        return cls(
            id=value.get("id", ""),
            scope=Scope.from_dict(scope_raw),
            concept_key=value.get("concept_key", ""),
            concept_value=value.get("concept_value", ""),
            content=value.get("content", ""),
            aspect=value.get("aspect", ""),
            lifecycle=value.get("lifecycle", "candidate"),
            review_state=value.get("review_state", "pending"),
            recall_policy=value.get("recall_policy", "contextual"),
            sensitivity=value.get("sensitivity", "normal"),
            evidence=tuple(EvidenceEdge.from_dict(item) for item in evidence_raw if isinstance(item, Mapping)),
            review_receipts=tuple(ReviewReceipt.from_dict(item) for item in receipts_raw if isinstance(item, Mapping)),
            valid_from=value.get("valid_from"),
            valid_until=value.get("valid_until"),
            replaces=value.get("replaces"),
            conflicts_with=tuple(value.get("conflicts_with") or ()),
            evidence_revision=value.get("evidence_revision", ""),
            projection_revision=value.get("projection_revision", 0),
            needs_recompute=bool(value.get("needs_recompute", False)),
            revision=value.get("revision", 1),
            created_at=value.get("created_at", ""),
            updated_at=value.get("updated_at", ""),
            **cls._extra_from_dict(value),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "scope": self.scope.to_dict(),
            "concept_key": self.concept_key,
            "concept_value": self.concept_value,
            "content": self.content,
            "aspect": self.aspect,
            "lifecycle": self.lifecycle,
            "review_state": self.review_state,
            "recall_policy": self.recall_policy,
            "sensitivity": self.sensitivity,
            "evidence": [edge.to_dict() for edge in self.evidence],
            "review_receipts": [receipt.to_dict() for receipt in self.review_receipts],
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "replaces": self.replaces,
            "conflicts_with": list(self.conflicts_with),
            "evidence_revision": self.evidence_revision,
            "policy_version": POLICY_VERSION,
            "projection_revision": self.projection_revision,
            "needs_recompute": self.needs_recompute,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def with_confirmation(self, policy_version: str, stamped: str) -> "YouClaim":
        """记一次「模型今天重新确认了它」，同一天重复调用只记一次。

        判重用的「今天」必须和收据时间戳同源：另取一次 now 的话，跨日那一瞬
        两个时间源会给出不同答案，同一天可能记下两条收据，三日门槛就少守一天。

        收据绑当前的 evidence_revision：证据集合一变，先前的重申自动不算数
        （见 review_date_count），所以「改一条也要重新攒三天」不需要另写逻辑。
        正文变更的重置在 service.write() 里单独处理，因为 evidence_revision
        不含正文。

        放在 YouClaim 上而不是各自的 service 里：ThemClaim 是 YouClaim 的子类，
        ReviewReceipt 也是两边共用的，两份实现除了 policy_version 一字不差。
        而这条规则管的是「三个不同自然日」这个门槛怎么数——两份实现漂了，
        其中一边就会重复计数，把门槛悄悄降低。
        `stamped` 由调用方给，不在这里取 utc_now()：时间源归 service 管
        （测试要能把它冻住），这条规则只管「同一自然日算不算重复」。
        """
        today = stamped[:10]
        already = any(
            receipt.review_date == today
            and receipt.evidence_revision == self.evidence_revision
            for receipt in self.review_receipts
        )
        if already:
            return self
        receipt = ReviewReceipt(
            reviewed_at=stamped,
            reviewer_role_id=self.scope.observer_role_id,
            evidence_revision=self.evidence_revision,
            policy_version=policy_version,
            result="reaffirmed",
        )
        return replace(self, review_receipts=(*self.review_receipts, receipt))


def evidence_digest(evidence: tuple[EvidenceEdge, ...] | list[EvidenceEdge]) -> str:
    payload = [edge.to_dict() for edge in evidence]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "evr_" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
