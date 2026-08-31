"""Stored-content rendering for breath compatibility.

This module is intentionally small so the compatibility patch can be removed
without touching retrieval, ranking, or bucket storage.
"""

from ombrebrain.storage.attribution import (
    known_person_names,
    names_from_config,
    render_third_party_block,
    split_third_party_speech,
)
from ombrebrain.storage.relation_store import relation_hint
from utils import count_tokens_approx, strip_wikilinks

from .. import _runtime as rt


def stored_bucket_content(bucket: dict) -> str:
    """Return the bucket body without stripping or normalizing any character."""
    content = bucket.get("content", "")
    if not isinstance(content, str):
        raise TypeError("bucket content must be a string")
    return content


def _miss_block(bucket: dict) -> str:
    """Miss: meaning/media 元数据，和 tags/importance 一样是桶的基本信息之一。

    meaning 是 list[str]（可能被反复触动过多次），逐条展示，不合并/不改写。
    media 只给 path/title 元数据，不读取或内联文件内容。
    """
    meta = bucket.get("metadata", {}) or {}
    lines = []
    for item in meta.get("meaning") or []:
        if item:
            lines.append(f"💭 meaning: {item}")
    for m in meta.get("media") or []:
        if not isinstance(m, dict) or not m.get("path"):
            continue
        title = m.get("title")
        label = f" ({title})" if title and title != m.get("path") else ""
        lines.append(f"🖼️ media: {m['path']}{label}")
    return ("\n" + "\n".join(lines)) if lines else ""


def render_stored_bucket(
    bucket: dict,
    metadata_header: str,
    footprint: str = "",
) -> tuple[str, int]:
    """Render metadata around, but never inside, the stored bucket body.

    展示文本只做双链正则清理（strip_wikilinks）与第三方发言分块，不改动磁盘原文；
    正文本身不加任何边界/哈希标记，返回的就是记忆正文本身。

    第三方发言（`名字：内容`）从展示正文里整行移出，改成正文之后的一条 JSON。
    动机见 `ombrebrain.storage.attribution`：混在正文里返回时，容易幻觉的模型
    会把别人说的话读成用户说的。移出而不是留一份，是因为留一份就等于返回两次，
    其中没有归属标记的那一次正是要防的那一次。
    """
    content = strip_wikilinks(stored_bucket_content(bucket))
    content, third_party = split_third_party_speech(
        content,
        known_names=known_person_names(bucket),
        **names_from_config(getattr(rt, "config", None)),
    )
    miss_block = _miss_block(bucket)
    rendered = f"{metadata_header}{miss_block}\n{content}"
    speech_block = render_third_party_block(third_party)
    if speech_block:
        rendered += f"\n{speech_block}"
    hint = relation_hint(bucket)
    if hint:
        rendered += f"\n{hint}"
    if footprint:
        rendered += f"\n{footprint}"
    return rendered, count_tokens_approx(rendered)
