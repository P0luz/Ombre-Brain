"""桶元数据的规范化与净化 —— 纯函数，不碰任何实例状态。

从 `bucket_manager.BucketManager` 里搬出来的六个 classmethod。它们只做
「把任意输入收敛成可安全落盘的形状」这一件事：剥控制字符、限深度、限条数、
限长度。搬出来的理由是那个类已经 3845 行 / 88 个方法，而这一组既不读也不写
实例状态，留在里面只是让类更难读。

**边界一句话**：这里只做形状与安全的收敛，不判断业务含义。哪些字段该存、
存多久、要不要合并，都是调用方的事。
"""

from __future__ import annotations

from datetime import date, datetime
import math
import re
from typing import Any

# 元数据规范化的上限。放在这里而不是 bucket_manager，是因为改这些数字只影响
# 下面这几个函数的行为。
_MAX_METADATA_DEPTH = 16
_MAX_METADATA_NODES = 10_000
_MEANING_ITEM_MAX = 2000        # 单条 meaning 的长度上限
_MEANING_LIST_MAX_ITEMS = 50    # 一个桶最多累积多少条 meaning
_MEDIA_MAX_ITEMS = 20           # 单条记忆最多关联多少个 media 引用
_MEDIA_PATH_MAX = 500
_MEDIA_TITLE_MAX = 200
_MEDIA_TYPE_MAX = 32
_MEDIA_NOTE_MAX = 500


def _sanitize_text(text: str) -> str:
    """F-04 fix: 清除 NUL、危险控制字符和双向覆写符（Unicode bidi override / isolate）。

    保留 \\n（LF）、\\r（CR）、\\t（Tab）。
    清除范围：
      U+0000~U+0008, U+000B, U+000C, U+000E~U+001F, U+007F（C0/C1 控制字符）
      U+202A~U+202E 双向控制符（LRE / RLE / PDF / LRO / RLO）
      U+2066~U+2069 双向隔离符（LRI / RLI / FSI / PDI）
    Emoji 与 CJK 不受影响。
    """
    _ctrl_table = {
        c: None
        for c in list(range(0x00, 0x09))    # 0x00..0x08
        + [0x0B, 0x0C]                       # VT, FF
        + list(range(0x0E, 0x20))            # 0x0E..0x1F
        + [0x7F]                             # DEL
        + list(range(0x202A, 0x202F))        # bidi controls 0x202A..0x202E
        + list(range(0x2066, 0x206A))        # bidi isolates 0x2066..0x2069
    }
    return str(text).translate(_ctrl_table)


def _normalize_metadata_value(
    value,
    *,
    _depth: int = 0,
    _seen: set[int] | None = None,
    _budget: list[int] | None = None,
):
    """Return bounded, alias-free JSON-safe YAML metadata.

    SafeLoader blocks object construction but still permits recursive and
    exponentially shared aliases.  Reject repeated containers and cap the
    expansion before rebuilding untrusted frontmatter into ordinary lists.
    """
    if _depth > _MAX_METADATA_DEPTH:
        raise ValueError("bucket metadata exceeds nesting-depth limit")
    if _seen is None:
        _seen = set()
    if _budget is None:
        _budget = [_MAX_METADATA_NODES]
    _budget[0] -= 1
    if _budget[0] < 0:
        raise ValueError("bucket metadata exceeds node limit")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        # RFC 8259/JSON has no NaN or infinity.  Normalize YAML's .nan and
        # .inf scalars to null; known numeric fields below then apply their
        # documented defaults instead of poisoning dashboard responses.
        return value if math.isfinite(value) else None
    if isinstance(value, (bytes, bytearray, memoryview, set, frozenset)):
        raise ValueError(
            f"bucket metadata contains non-JSON-safe value: {type(value).__name__}"
        )
    if isinstance(value, dict):
        identity = id(value)
        if identity in _seen:
            raise ValueError("bucket metadata contains recursive/shared aliases")
        _seen.add(identity)
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, datetime):
                normalized_key = key.isoformat()
            elif isinstance(key, date):
                normalized_key = key.isoformat()
            elif key is None or isinstance(key, (str, bool, int)):
                normalized_key = str(key)
            elif isinstance(key, float) and math.isfinite(key):
                normalized_key = str(key)
            else:
                raise ValueError(
                    "bucket metadata contains a non-JSON mapping key"
                )
            if normalized_key in normalized:
                raise ValueError(
                    "bucket metadata contains colliding normalized keys"
                )
            normalized[normalized_key] = _normalize_metadata_value(
                item,
                _depth=_depth + 1,
                _seen=_seen,
                _budget=_budget,
            )
        return normalized
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in _seen:
            raise ValueError("bucket metadata contains recursive/shared aliases")
        _seen.add(identity)
        return [
            _normalize_metadata_value(
                v,
                _depth=_depth + 1,
                _seen=_seen,
                _budget=_budget,
            )
            for v in value
        ]
    raise ValueError(
        f"bucket metadata contains unsupported scalar: {type(value).__name__}"
    )


def _normalize_metadata_list(
    values,
    *,
    max_items: int,
    max_chars: int,
) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    elif not isinstance(values, (list, tuple, set)):
        values = [values]
    normalized: list[str] = []
    for value in values:
        text = _sanitize_text(str(value)).strip()[:max_chars]
        if text and text not in normalized:
            normalized.append(text)
        if len(normalized) >= max_items:
            break
    return normalized


def _normalize_meaning_item(text) -> str:
    """裁剪单条 meaning 文本；不是摘要，只做长度上限保护。"""
    if not text:
        return ""
    return _sanitize_text(str(text)).strip()[:_MEANING_ITEM_MAX]


def _normalize_meaning_list(values) -> list[str]:
    """整体替换用：逐条裁剪 + 丢空条目 + 裁总数上限。

    不去重：同一句话在不同时刻写下也是信息，去重会抹掉这个时间差。
    """
    if not values:
        return []
    if isinstance(values, str):
        values = [values]
    normalized: list[str] = []
    for v in values:
        item = _normalize_meaning_item(v)
        if item:
            normalized.append(item)
        if len(normalized) >= _MEANING_LIST_MAX_ITEMS:
            break
    return normalized


def _normalize_media(media) -> list[dict]:
    """校验持久媒体元数据；path 必须已经由 MediaStore 稳定化。"""
    if not media:
        return []
    if not isinstance(media, list):
        media = [media]
    normalized: list[dict] = []
    for item in media:
        if not isinstance(item, dict):
            continue
        path = _sanitize_text(str(item.get("path") or "")).strip()[:_MEDIA_PATH_MAX]
        if not path:
            continue
        entry: dict = {"path": path}
        title = item.get("title")
        if title:
            entry["title"] = _sanitize_text(str(title)).strip()[:_MEDIA_TITLE_MAX]
        media_type = item.get("type")
        if media_type:
            entry["type"] = _sanitize_text(str(media_type)).strip()[:_MEDIA_TYPE_MAX]
        note = item.get("note")
        if note:
            entry["note"] = _sanitize_text(str(note)).strip()[:_MEDIA_NOTE_MAX]
        digest = str(item.get("sha256") or "").lower()
        if re.fullmatch(r"[0-9a-f]{64}", digest):
            entry["sha256"] = digest
        try:
            size = int(item.get("size"))
        except (TypeError, ValueError, OverflowError):
            size = -1
        if size >= 0:
            entry["size"] = size
        if item.get("stored") is True:
            entry["stored"] = True
        normalized.append(entry)
        if len(normalized) >= _MEDIA_MAX_ITEMS:
            break
    return normalized
