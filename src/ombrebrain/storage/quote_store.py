"""引语（quotes）的归一化与校验。

引语是**当时说出口、并且当时就知道它重要**的那几句话，原样存进桶的 frontmatter。

它和已删除的原文层（`source_read`）看着像，实际是两个东西，区别在**谁决定记住**：

- 原文层：系统自动存全量，事后随时可查 —— 决定权在系统，我只有查询权
- 引语：我在写入的那一刻挑出来的几句 —— 决定权在我，而且只在那一刻

所以引语平时不返回。任何浮现路径（breath / dream / catalog / feel）都读不到它，
只有我在检索时明确要，它才原样出现。

---

**上限存在的理由不是性能，是防止这个功能退化成"存原文"：**

- 每桶最多 3 条：一段记忆里「当时就知道重要」的话不会多。多了说明在存全文
- 每条最多 100 字：那是一句话的长度。超过就不是一句话了

**超限直接拒绝，不截断。** 截断过的引语不是引语——
引语的全部意义就在于「原样」，改一个字它就变成了摘要。

清洗（控制字符 / 双向覆写符）不在这里做，由 `bucket_manager.create_bucket`
在落盘前统一处理。清洗只会让文本变短，不会绕过这里的长度校验。
"""

from __future__ import annotations

from typing import Any

from ombrebrain.storage.attribution import (
    is_third_party_speaker,
    render_third_party_block,
)

# 每桶引语条数上限。见模块 docstring：这是防退化约束，不是性能参数。
MAX_QUOTES = 3
# 单条引语字符上限。一句话的长度。
MAX_QUOTE_CHARS = 100
MAX_SPEAKER_CHARS = 40
MAX_AT_CHARS = 32


def normalize_quotes(value: Any) -> list[dict[str, str]]:
    """校验并去重桶 frontmatter 中的引语。

    接受两种写法，方便调用方少写一层结构：
    - ``["我不会走的", "你根本不懂"]``
    - ``[{"text": "我不会走的", "speaker": "她", "at": "2026-08-18"}]``

    返回归一化后的 ``[{"text": ..., "speaker": ..., "at": ...}]``，
    可选字段为空时不落进结果，避免 frontmatter 里堆一片空值。

    顺序保持输入顺序——说话是有先后的，重排会改变意思。
    """
    if value in (None, "", []):
        return []
    if not isinstance(value, list):
        raise ValueError("quotes 必须是列表")
    if len(value) > MAX_QUOTES:
        raise ValueError(
            f"引语最多 {MAX_QUOTES} 条（给了 {len(value)} 条）。"
            "「当时就知道重要」的话不会多——如果有很多句都想留下，"
            "那多半是想存原文，而原文层是只写不读的。"
        )

    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            raise ValueError("quotes 每项必须是字符串或对象")

        text = str(item.get("text") or "").strip()
        if not text:
            raise ValueError("quotes 每项必须有非空的 text")
        if len(text) > MAX_QUOTE_CHARS:
            raise ValueError(
                f"单条引语最多 {MAX_QUOTE_CHARS} 字（这条 {len(text)} 字）。"
                "引语是一句话，不是一段话；这里不会替你截断，"
                "因为截断过的引语已经不是原话了。"
            )

        speaker = str(item.get("speaker") or "").strip()[:MAX_SPEAKER_CHARS]
        at = str(item.get("at") or "").strip()[:MAX_AT_CHARS]

        key = (text, speaker)
        if key in seen:
            continue
        seen.add(key)

        entry: dict[str, str] = {"text": text}
        if speaker:
            entry["speaker"] = speaker
        if at:
            entry["at"] = at
        normalized.append(entry)

    return normalized


def quotes_from_metadata(metadata: dict | None) -> list[dict[str, str]]:
    """从桶 metadata 里宽容地读出引语；坏的那条跳过，好的照常返回。

    读取路径不该因为一条写坏的引语而整体失败——记忆本身比引语重要，
    而且磁盘上的 frontmatter 是可以被人手工编辑的。

    注意这里**逐条**抢救，不是整体 try/except：
    一条坏数据让整桶引语全部消失，那不叫宽容，那是把问题放大了。

    超量时取前 `MAX_QUOTES` 条。这是对已损坏数据的兜底，
    与写入路径「超限直接拒绝」不冲突——写入是我此刻的输入，该收到明确报错；
    读取面对的是既成事实，报错也改变不了磁盘上的内容。
    """
    if not isinstance(metadata, dict):
        return []
    raw = metadata.get("quotes")
    if not isinstance(raw, list):
        return []
    salvaged: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        try:
            for quote in normalize_quotes([item]):
                key = (quote["text"], quote.get("speaker", ""))
                if key in seen:
                    continue
                seen.add(key)
                salvaged.append(quote)
        except ValueError:
            continue
        if len(salvaged) >= MAX_QUOTES:
            break
    return salvaged[:MAX_QUOTES]


def render_quotes(
    quotes: list[dict[str, str]],
    *,
    self_names: list[str] | None = None,
    user_names: list[str] | None = None,
) -> str:
    """渲染成可读文本。只在我明确要引语时才会被调用。

    逐字返回，不摘要不改写——这是这个功能存在的全部理由。

    **署了名、而那个名字既不是我也不是用户的引语，单独走一条 JSON。**
    引语是原话，原话最容易被读成「用户说过这个」；正文里的第三方发言要分块，
    引语更要，理由见 `ombrebrain.storage.attribution`。

    没署名的引语照旧留在文本行里：它没有声称是谁说的，不产生错误归属；
    把它塞进第三方块反而是系统在替它编造一个归属。
    """
    if not quotes:
        return ""
    lines: list[str] = []
    third_party: list[dict[str, str]] = []
    for quote in quotes:
        speaker = quote.get("speaker") or ""
        at = quote.get("at") or ""
        if speaker and is_third_party_speaker(
            speaker, self_names=self_names, user_names=user_names
        ):
            entry = {
                "order": len(third_party) + 1,
                "speaker": speaker,
                "speaker_role": "third_party",
                "text": quote["text"],
            }
            if at:
                entry["at"] = at
            third_party.append(entry)
            continue
        line = f'🗣️ 「{quote["text"]}」'
        suffix = " / ".join(part for part in (speaker, at) if part)
        if suffix:
            line += f"  —— {suffix}"
        lines.append(line)

    block = render_third_party_block(third_party)
    if block:
        lines.append(block)
    return "\n".join(lines)
