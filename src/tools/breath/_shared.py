"""breath 五条分支共用的小工具。

`footprint_reader` 原本以「取 snapshot + 定义闭包 + 兜底文案」的形态在
surface / feel / catalog / importance / search 里各抄了一份（surface 抄了两份），
`bucket_has_tags` 抄了三份。抄的那几份迟早会不一样。
"""

from __future__ import annotations

from .. import _runtime as rt

_UNAVAILABLE = "👣 Footprint：暂时无法读取"


class FootprintReader:
    """一次快照，两种读法：渲染用的 summary，和归档判型用的 original_kind。

    可调用，所以旧的 `_footprint(bucket)` 写法原样能用。
    """

    __slots__ = ("snapshot",)

    def __init__(self, snapshot) -> None:
        self.snapshot = snapshot

    def __call__(self, bucket: dict, meta: dict | None = None) -> str:
        if self.snapshot is None:
            return _UNAVAILABLE
        if meta is None:
            meta = bucket.get("metadata", {})
        return self.snapshot.summary(str(bucket.get("id") or ""), meta)

    def original_kind(self, bucket_id: str, meta: dict, default: str = "dynamic") -> str:
        if self.snapshot is None:
            return default
        return self.snapshot.original_kind(str(bucket_id or ""), meta)


def footprint_reader() -> FootprintReader:
    """取一次快照并包成读取器；拿不到时降级为固定兜底文案。

    快照只取一次：每条桶各取一次会把「省 token 的目录模式」变成 N 次全库读。
    """
    try:
        snapshot = rt.bucket_mgr.footprint_snapshot()
    except Exception as exc:
        rt.logger.warning(f"Footprint snapshot unavailable / 足迹读取失败: {exc}")
        snapshot = None
    return FootprintReader(snapshot)


def bucket_has_tags(meta: dict, tag_filter: list) -> bool:
    """tag 过滤是 AND：给了几个就必须全都有。"""
    if not tag_filter:
        return True
    bucket_tags = set(meta.get("tags", []) or [])
    return all(t in bucket_tags for t in tag_filter)


def render_within_budget(
    buckets: list[dict],
    max_tokens: int,
    footprint,
) -> tuple[list[str], int]:
    """按 `[created] [bucket_id:x]` 头逐条渲染，返回 (已渲染, 未返回条数)。

    放不下就**整条停下**，不截断也不摘要——feel 与 plan 两条通道都靠这个语义：
    截断过的正文比没返回更糟，因为看不出少了什么。

    feel.py 与 surface_plans 原本各写了一份逐字相同的循环（surface_plans 的
    docstring 甚至写着「与 feel 通道同构」）。
    """
    from ._verbatim import render_stored_bucket

    lines: list[str] = []
    used = 0
    for index, bucket in enumerate(buckets):
        created = bucket["metadata"].get("created", "")
        entry, cost = render_stored_bucket(
            bucket,
            f"[{created}] [bucket_id:{bucket['id']}]",
            footprint(bucket),
        )
        if used + cost > max_tokens:
            return lines, len(buckets) - index
        lines.append(entry)
        used += cost
    return lines, 0
