from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping, Any


class SurfaceMode(str, Enum):
    SPONTANEOUS = "spontaneous"
    SEARCH = "search"
    IMPORTANCE = "importance"
    DREAM = "dream"


@dataclass(frozen=True)
class SurfaceDecision:
    allowed: bool
    mode: str
    bucket_id: str
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "mode": self.mode,
            "bucket_id": self.bucket_id,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class SurfacePolicyVM:
    """Deterministic read-side policy for memory surfacing.

    This is deliberately small for Phase 3: Markdown buckets remain canonical,
    and the VM only decides whether a bucket may enter a specific read pool.
    """

    private_types: tuple[str, ...] = ("feel", "plan", "letter", "self", "i")

    @classmethod
    def default(cls) -> "SurfacePolicyVM":
        return cls()

    def evaluate_bucket(self, bucket: Mapping[str, Any] | None, mode: str | SurfaceMode) -> SurfaceDecision:
        normalized_mode = _coerce_mode(mode)
        if not bucket:
            return SurfaceDecision(
                allowed=False,
                mode=normalized_mode.value,
                bucket_id="",
                reasons=("missing_bucket",),
            )

        metadata = bucket.get("metadata") or {}
        if not isinstance(metadata, Mapping):
            metadata = {}
        bucket_id = str(bucket.get("id") or metadata.get("id") or "")
        bucket_type = _metadata_type(metadata)
        reasons: list[str] = []

        if bucket_type == "tombstone" or _truthy(metadata.get("tombstone")):
            reasons.append("tombstone")
        if bucket_type == "archived":
            reasons.append("archived")
        if metadata.get("deleted_at"):
            reasons.append("deleted")

        # 核心准则与坐标系不可被消化。
        #
        # `digested` 的语义是"这条我消化过了，别再自动浮现"，`pinned` /
        # `permanent` 的语义是"这是核心准则，必须始终在场"，`anchor` 的语义是
        # "这是我的坐标系"。两个标记打在同一个桶上时，后者赢——人生中最重要的
        # 锚点不可被消化。
        #
        # 实际发生过：12 条核心准则里有 2 条带着 digested，于是 breath() 只返回
        # 10 条。看起来像被普通桶挤掉了（旁边确实有普通桶），实际是压根没进候选。
        # 这类静默缺失最难发现——没有报错，只是少了两条。
        #
        # 注意这里只豁免 digested。`anchor` 自身仍然不主动浮现（下面那条规则
        # 保留），豁免的意思是"它不会因为被消化过而从它该出现的地方消失"。
        _never_digested = (
            _truthy(metadata.get("pinned"))
            or bucket_type == "permanent"
            or _truthy(metadata.get("anchor"))
        )

        if normalized_mode in (SurfaceMode.SPONTANEOUS, SurfaceMode.DREAM):
            if _truthy(metadata.get("dont_surface")):
                reasons.append("dont_surface")
            if _truthy(metadata.get("digested")) and not _never_digested:
                reasons.append("digested")
            if _truthy(metadata.get("anchor")):
                reasons.append("anchor")
            if _truthy(metadata.get("protected")):
                reasons.append("protected")
            if bucket_type in self.private_types:
                reasons.append("private_type")
        elif normalized_mode == SurfaceMode.IMPORTANCE:
            if _truthy(metadata.get("dont_surface")):
                reasons.append("dont_surface")
            if bucket_type in self.private_types:
                reasons.append("private_type")

        return SurfaceDecision(
            allowed=not reasons,
            mode=normalized_mode.value,
            bucket_id=bucket_id,
            reasons=tuple(reasons),
        )

    def filter_buckets(
        self,
        buckets: Iterable[Mapping[str, Any]],
        mode: str | SurfaceMode,
    ) -> list[Mapping[str, Any]]:
        return [bucket for bucket in buckets if self.evaluate_bucket(bucket, mode).allowed]


def _coerce_mode(mode: str | SurfaceMode) -> SurfaceMode:
    if isinstance(mode, SurfaceMode):
        return mode
    try:
        return SurfaceMode(str(mode or SurfaceMode.SPONTANEOUS.value).lower())
    except ValueError:
        return SurfaceMode.SPONTANEOUS


def _metadata_type(metadata: Mapping[str, Any]) -> str:
    return str(metadata.get("type") or "dynamic").strip().lower()


def _truthy(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
