"""
========================================
tools/grow/__init__.py — grow 工具入口
========================================

grow 是「我把一段长内容整理进记忆」。短内容（<30 字）走 shortpath，
跳过 LLM 拆分省 API；长内容走 core，调 dehydrator.digest 拆成 2~6 条
独立事件桶。

关键行为：
- 入口做 content 校验
- 按 strip 后长度 < 30 字判断走哪个分支

不做什么（边界）：
- 不做 token 级别预算（grow 关心的是「拆几条」而不是「展示多少」）
- 不返回结构化数据，统一中文短句

对外暴露：dispatch(content) → str
========================================
"""

from errors import ToolInputError
import re
from typing import Optional

from ombrebrain.storage.source_store import normalize_source_ranges

from .. import _runtime as rt
from .._common import check_grow_input_size, check_grow_items_payload
from .shortpath import grow_shortpath
from .core import grow_core, grow_items
from .retry_guard import request_fingerprint, run_once


_TITLE_ANCHOR_SPLIT_RE = re.compile(
    r"[\s·•|/\\:：,，。;；!?！？()（）\[\]【】<>《》—–_-]+"
)
_MIN_TITLE_ANCHOR_CHARS = 4


def _compact_evidence_text(value: object) -> str:
    return "".join(str(value or "").casefold().split())


def _title_anchors(value: object) -> list[str]:
    title = str(value or "").strip()
    if not title:
        return []
    anchors: list[str] = []
    for part in _TITLE_ANCHOR_SPLIT_RE.split(title):
        compact = _compact_evidence_text(part)
        if len(compact) >= _MIN_TITLE_ANCHOR_CHARS and compact not in anchors:
            anchors.append(compact)
    return anchors


def _range_contains_line(ranges: list[list[int]], line_no: int) -> bool:
    return any(start <= line_no <= end for start, end in ranges)


def _detect_shifted_source_ranges(items: list, source_content: str) -> list[str]:
    """只用逐字标题锚点识别整批 source_ranges 的明显同向错位。"""

    ranged_items: list[tuple[dict, list[list[int]]]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            ranges = normalize_source_ranges(item.get("source_ranges"))
        except ValueError:
            continue
        if ranges:
            ranged_items.append((item, ranges))
    if len(ranged_items) < 2:
        return []

    compact_lines = [
        _compact_evidence_text(line) for line in source_content.splitlines()
    ]
    suspects: list[tuple[str, int]] = []

    for item, ranges in ranged_items:
        anchors = _title_anchors(item.get("title"))
        if not anchors:
            continue

        hit_lines: set[int] = set()
        supported = False
        for anchor in anchors:
            anchor_hits = {
                index
                for index, line in enumerate(compact_lines, start=1)
                if anchor in line
            }
            if any(_range_contains_line(ranges, line_no) for line_no in anchor_hits):
                supported = True
                break
            hit_lines.update(anchor_hits)

        if supported or not hit_lines:
            continue

        range_start = min(start for start, _end in ranges)
        range_end = max(end for _start, end in ranges)
        if all(line_no > range_end for line_no in hit_lines):
            direction = 1
        elif all(line_no < range_start for line_no in hit_lines):
            direction = -1
        else:
            continue
        suspects.append((str(item.get("title") or "未命名"), direction))

    if len(suspects) < 2:
        return []
    if len({direction for _title, direction in suspects}) != 1:
        return []
    return [title for title, _direction in suspects]


def _shifted_source_ranges_error(items: list, source_content: str) -> str:
    shifted_titles = _detect_shifted_source_ranges(items, source_content)
    if not shifted_titles:
        return ""
    preview = "、".join(shifted_titles[:3])
    if len(shifted_titles) > 3:
        preview += " 等"
    raise ToolInputError("source_ranges 疑似与 items 错位："
        f"{preview} 的显式标题只在各自声明范围之外同向出现。"
        "为避免保存错误原文证据，本批次未创建任何桶；"
        "请重新核对 1-based 闭区间。")


async def dispatch(
    content: str = "", items: Optional[list] = None, test_data: bool = False
) -> str:
    await rt.decay_engine.ensure_started()

    # 预拆分模式：上层 AI 已拆好 N 条最终正文 → 逐字入库，跳过 digest 的二次改写。
    # 传了 items（非空列表）即走此路；不传则行为与旧版完全一致（向后兼容）。
    if isinstance(items, list) and len(items) > 0:
        # 这四处校验都在 grow_items() 之前，失败时一个桶都没建。
        # helper 保持返回错误串（它们在 _common.py 里被多处共用），
        # 由调用点负责抛出——判据是「这次调用有没有写东西」。
        err = check_grow_items_payload(items)
        if err:
            raise ToolInputError(err)
        if content and content.strip():
            err = check_grow_input_size(content)
            if err:
                raise ToolInputError(err)
            err = _shifted_source_ranges_error(items, content)
            if err:
                raise ToolInputError(err)
        fingerprint = request_fingerprint(
            content=content, items=items, test_data=test_data
        )
        return await run_once(
            fingerprint,
            lambda: grow_items(
                items, source_content=content, test_data=test_data
            ),
        )

    if not content or not content.strip():
        raise ToolInputError("内容为空，无法整理。")

    err = check_grow_input_size(content)
    if err:
        raise ToolInputError(err)

    fingerprint = request_fingerprint(
        content=content, items=None, test_data=test_data
    )
    if len(content.strip()) < 30:
        return await run_once(
            fingerprint,
            lambda: grow_shortpath(content, test_data=test_data),
        )
    return await run_once(
        fingerprint,
        lambda: grow_core(content, test_data=test_data),
    )
