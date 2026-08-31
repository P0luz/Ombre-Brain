"""把桶正文里「不是用户、也不是我自己说的话」单独拆出来。

## 为什么要有这一层

记忆正文是自由文本。里面写「Zoey：她觉得这个方案不行」的时候，我自己读得懂
那是 Zoey 说的；但把这段原样喂给一个容易幻觉的模型，它很容易读成
「用户觉得这个方案不行」——第三方的判断被贴到了用户身上。

这不是排版问题，是归属问题。所以第三方说的话不跟正文混在一段里返回，
单独成一条 JSON，每条自带 speaker，块头写明这些话不代表用户。

## 认的是一个书写协议，不是猜

系统不理解正文语义，只认行首 `名字：内容`。但光有这个形状远远不够——
正文里「结论：」「她的原话：」「精准命中正文：」全是这个形状。第一版就是靠
一张结构词黑名单挡这些，跑全量的时候被一条真实用例当场抓出来：
`精准命中正文：两个都是你` 被拆成了「精准命中正文」说的话。

黑名单补不完，因为「短语 + 冒号」是中文正文最常见的写法之一。所以判据换成
**名字必须先被独立认定为人**，形状只是第二道：

1. `@名字：内容` —— 显式标记，零歧义，任何名字都认
2. `名字：内容`，且这个名字在**该桶已知的人名**里：
   - 引语 `quotes` 的 `speaker`（写入时就署过名）
   - 正文里的 `[[双链]]` 目标（`[[Zoey]]` 与 `Zoey：` 同时出现，两个独立信号叠加）

两条都不满足就原样留在正文里。

宁可漏拆，不可错拆——漏拆只是维持现状（那句话仍在正文里，只是没被标出来），
错拆是把用户自己写的一段话标成别人说的，那是系统凭空造出一个错误归属。

认定为人之后仍要过三道：不在「我自己 / 用户」称呼表、不在结构词表、
不是 `https://` 这类被冒号切开的假前缀。

配置项 `attribution.self_names` / `attribution.user_names` 用来补充称呼表：
用户叫什么、我这一侧叫什么，都是部署时才知道的，写死在代码里必然认不全。
补不全的后果是保守方向的——一个没登记的用户别名会被当成第三方拆出来，
而拆出来的块里明写了归属，不会变成「用户说过」。
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping

# 名字整体长度上限，用于 speaker 字段的独立校验（引语那条路径不过行首正则）。
MAX_SPEAKER_CHARS = 25
# 非拉丁名字（中文昵称、称谓）的长度上限。这一条是挡误判的主力：
# 10 个中文字放得下任何真实人名或昵称，放不下一句完整的话——
# 而「一句话被当成说话人」正是行首冒号协议最容易出的错。
MAX_CJK_NAME = 10
# 拉丁名字的长度上限，单段。英文名 + 姓比中文名长，另给一档。
MAX_LATIN_NAME = 20
# 一条记忆里最多拆出的发言条数。桶正文若有几十行对白，那是在存原文，
# 不是在记一段记忆；这里只挡住失控的量，不改变已拆出的内容。
MAX_STATEMENTS = 24

# 名字里的空格**只对纯拉丁名字放行**（`Zoey Chen：`）。
# 中文那一侧一律不许带空格：放行空格会让「我记得她说过 Zoey：」整段被当成
# 说话人，而这正是要挡的错拆。英文侧没有这个问题——行首必须是字母开头。
_NAME_CHARS = r"[^\s:：,，。；;！!？?、\[\]（）()「」【】]"
_LATIN_NAME = rf"[A-Za-z][A-Za-z.\-']{{0,{MAX_LATIN_NAME - 1}}}"
# `@` 是显式标记，写在名字前面。它不属于名字本身，捕获后剥掉。
_SPEAKER_LINE = re.compile(
    rf"^[ \t]*(?P<mark>@?)(?P<speaker>{_LATIN_NAME}(?: {_LATIN_NAME})?"
    rf"|{_NAME_CHARS}{{1,{MAX_CJK_NAME}}})"
    r"[ \t]*[:：][ \t]*(?P<text>\S.*)$"
)
EXPLICIT_MARK = "@"

# 我这一侧的称呼。命中这些的行不是第三方，原样留在正文里。
_SELF_NAMES = frozenset(
    {
        "我", "ob", "ombre", "ombrebrain", "ombre brain", "助手", "模型",
        "assistant", "claude", "ai", "gpt", "bot", "deepseek", "chatgpt",
    }
)

# 用户一侧的称呼。同样不算第三方。
_USER_NAMES = frozenset(
    {"你", "用户", "user", "human", "主人", "宿主", "poluz"}
)

# 天天出现在正文行首、后面跟冒号，但根本不是人名的结构词。
# 这份表是误判的主要防线：少一个词，就多一段正文被错标成别人说的话。
_STRUCTURAL_WORDS = frozenset(
    {
        "注意", "结论", "背景", "问题", "原因", "总结", "时间", "地点", "结果",
        "例如", "比如", "说明", "备注", "待办", "目标", "方法", "前提", "影响",
        "风险", "建议", "来源", "链接", "参考", "定义", "现象", "对策", "复盘",
        "输入", "输出", "步骤", "验证", "范围", "边界", "版本", "状态", "进度",
        "优点", "缺点", "取舍", "决定", "分工", "期限", "标题", "摘要", "正文",
        "补充", "更新", "修复", "新增", "删除", "改动", "测试", "部署", "回滚",
        "note", "todo", "fixme", "warning", "error", "info", "debug", "tip",
        "summary", "result", "reason", "input", "output", "status", "step",
        "http", "https", "ftp", "file", "data", "id", "url", "ref",
    }
)


def _normalize_names(values: Iterable[Any] | None) -> set[str]:
    if not values:
        return set()
    if isinstance(values, str):
        values = [values]
    normalized = set()
    for item in values:
        text = str(item or "").strip().lower()
        if text and len(text) <= MAX_SPEAKER_CHARS:
            normalized.add(text)
    return normalized


def _is_third_party(
    speaker: str,
    *,
    self_names: set[str],
    user_names: set[str],
) -> bool:
    key = speaker.strip().lower()
    if not key or len(key) > MAX_SPEAKER_CHARS:
        return False
    if key in self_names or key in user_names:
        return False
    if key in _STRUCTURAL_WORDS:
        return False
    # 纯数字/纯符号不是名字（「1：」这种是列表编号）。
    if not any(ch.isalnum() for ch in key):
        return False
    if key.isdigit():
        return False
    return True


def is_third_party_speaker(
    speaker: str,
    *,
    self_names: Iterable[str] | None = None,
    user_names: Iterable[str] | None = None,
) -> bool:
    """这个署名既不是我、也不是用户吗？

    给引语（`quote_store`）用：那里的 speaker 是模型写入时自己填的，
    不需要再过一遍行首协议，只需要这一道归属判定。
    """
    return _is_third_party(
        speaker,
        self_names=_SELF_NAMES | _normalize_names(self_names),
        user_names=_USER_NAMES | _normalize_names(user_names),
    )


def known_person_names(bucket: Mapping[str, Any] | None) -> set[str]:
    """这个桶里有哪些名字已经被独立认定为「人」。

    两个来源，都不是靠猜出来的：

    - `quotes` 的 `speaker`：写入那一刻我自己署的名
    - 正文里的 `[[双链]]` 目标：`[[Zoey]]` 与 `Zoey：` 同时出现时，
      是两个互相独立的信号撞在一起

    双链目标里当然也有不是人的（概念、地名、项目名）。那不要紧——
    这里只是把候选缩到「桶里点过名的东西」，一个概念名要真的引发错拆，
    还得同时以 `概念：内容` 的形状出现在行首。误判面比一张黑名单窄得多。
    """
    if not isinstance(bucket, Mapping):
        return set()
    names: set[str] = set()
    metadata = bucket.get("metadata")
    if isinstance(metadata, Mapping):
        raw_quotes = metadata.get("quotes")
        if isinstance(raw_quotes, list):
            for item in raw_quotes:
                if isinstance(item, Mapping):
                    speaker = str(item.get("speaker") or "").strip().lower()
                    if speaker:
                        names.add(speaker)
    content = bucket.get("content")
    if isinstance(content, str) and content:
        from utils import extract_wikilinks

        for target in extract_wikilinks(content):
            normalized = target.strip().lower()
            if normalized:
                names.add(normalized)
    return names


def split_third_party_speech(
    content: str,
    *,
    known_names: Iterable[str] | None = None,
    self_names: Iterable[str] | None = None,
    user_names: Iterable[str] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """把正文拆成「剩余正文」和「第三方发言列表」。

    只拆两种：显式 `@名字：`，或名字出现在 ``known_names`` 里。别的原样留下。
    没有 ``known_names`` 时就只认 `@` —— 缺省是最保守的那一档，
    调用方忘了传的后果是漏拆，不是错拆。

    返回的正文里，被拆走的那些行整行移除——留一份在正文里就等于返回了两次，
    而两次里有一次是没有归属标记的，那正是要防的那一次。

    发言按原顺序编号（``order``），顺序在对话里是有意义的，重排会改变意思。
    """
    if not isinstance(content, str) or not content:
        return content if isinstance(content, str) else "", []

    selves = _SELF_NAMES | _normalize_names(self_names)
    users = _USER_NAMES | _normalize_names(user_names)
    known = _normalize_names(known_names)

    kept: list[str] = []
    statements: list[dict[str, Any]] = []
    for line in content.split("\n"):
        match = _SPEAKER_LINE.match(line)
        if match and len(statements) < MAX_STATEMENTS:
            speaker = match.group("speaker").strip()
            text = match.group("text").strip()
            explicit = match.group("mark") == EXPLICIT_MARK
            # `https://x` 会被切成 speaker=https / text=//x，_STRUCTURAL_WORDS
            # 已经挡住 https，这里再挡一次自定义协议头。
            if text.startswith("//"):
                kept.append(line)
                continue
            recognized = explicit or speaker.lower() in known
            if recognized and _is_third_party(
                speaker, self_names=selves, user_names=users
            ):
                statements.append(
                    {
                        "order": len(statements) + 1,
                        "speaker": speaker,
                        "speaker_role": "third_party",
                        "text": text,
                    }
                )
                continue
        kept.append(line)

    if not statements:
        return content, []
    return "\n".join(kept).strip("\n"), statements


_BLOCK_HEADER = (
    "[以下是记忆里第三方说的话，不是用户说的，也不是我说的。"
    "把它当成用户的观点、事实或要求都是错的；归属看 speaker。]"
)


def render_third_party_block(statements: list[dict[str, Any]]) -> str:
    """渲染成一条 JSON。

    一条，不是每人一条——「单独用一条 json 分开」的重点在于它与正文之间有一道
    明确的结构边界，而不是把边界切得更碎。碎成多条反而让模型更容易只读到其中
    一条就往下推断。
    """
    if not statements:
        return ""
    payload = {
        "non_user_speech": statements,
        "attribution_note": "third_party statements; not the user's words or views",
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=False)
    return f"{_BLOCK_HEADER}\n```json\n{encoded}\n```"


def names_from_config(config: Mapping[str, Any] | None) -> dict[str, list[str]]:
    """从配置里读 self/user 的补充称呼，直接摊成 split 的关键字参数。

    读不到就返回空表，走内置称呼表——绝大多数部署不会配这一段，渲染路径不该
    因为配置缺一节就失败。

    breath 与 dream 两条渲染路径都调这里，共用同一份判定依据；各自维护一份的话，
    同一段正文在两处会拆出不同结果。
    """
    section = config.get("attribution") if isinstance(config, Mapping) else None
    if not isinstance(section, Mapping):
        return {"self_names": [], "user_names": []}
    return {
        "self_names": sorted(_normalize_names(section.get("self_names"))),
        "user_names": sorted(_normalize_names(section.get("user_names"))),
    }
