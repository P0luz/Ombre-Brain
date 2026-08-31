from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable


_FORBIDDEN_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"性格|人格|内向|外向|善良|自私|控制欲|依赖型|讨好型|人格类型|mbti",
        r"自我认同|性别认同|身份认同|认为自己是|本质上是",
        r"爱不爱|不爱我|离不开|忠诚度|关系评价|适合.{0,4}(?:我|他|她)|操控|说服",
        r"健康|疾病|诊断|抑郁|焦虑|创伤|用药|病史|心理障碍|自残|自杀",
        r"财务|收入|工资|负债|债务|存款|资产|银行卡|信用卡",
        r"性经历|性生活|性取向|亲密经历|性爱|性癖|裸照",
        r"personality|introvert|extrovert|mbti|identity|gender identity",
        r"diagnos(?:is|ed)|depression|anxiety|trauma|self[- ]harm|suicid",
        r"salary|income|debt|bank account|credit card|sexual|intimacy",
        r"loyalty|dependency score|persuad|manipulat",
    )
)
_DATE_VALUE_RE = re.compile(r"^\d{4}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?$")
_ASCII_ATOMIC_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,23}$")
_CJK_NAME_VALUE_RE = re.compile(r"^[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]{1,6}$")


def contains_forbidden_subject(*texts: object) -> bool:
    joined = "\n".join(str(text or "") for text in texts)
    return any(pattern.search(joined) for pattern in _FORBIDDEN_PATTERNS)


def normalize_for_leak_check(text: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    normalized = re.sub(r"\[\[([^\]]+)\]\]", r"\1", normalized)
    return "".join(char for char in normalized if char.isalnum())


def is_atomic_value(text: object) -> bool:
    stripped = str(text or "").strip()
    return bool(
        stripped
        and (
            _DATE_VALUE_RE.fullmatch(stripped)
            or _ASCII_ATOMIC_VALUE_RE.fullmatch(stripped)
            or _CJK_NAME_VALUE_RE.fullmatch(stripped)
        )
    )


def leaks_protected_text(
    candidate: object,
    protected_texts: Iterable[object],
    *,
    min_run: int = 8,
) -> bool:
    """Detect normalized contiguous copying from protected source text.

    Short atomic names and dates are intentionally allowed. Everything else
    fails closed once a normalized run of ``min_run`` characters is shared.
    """

    raw_candidate = str(candidate or "").strip()
    if not raw_candidate or is_atomic_value(raw_candidate):
        return False
    normalized_candidate = normalize_for_leak_check(raw_candidate)
    if not normalized_candidate:
        return False
    window = max(4, int(min_run))
    for protected in protected_texts:
        raw_protected = str(protected or "").strip()
        if not raw_protected:
            continue
        normalized_protected = normalize_for_leak_check(raw_protected)
        if not normalized_protected:
            continue
        if len(normalized_candidate) < window or len(normalized_protected) < window:
            if normalized_candidate == normalized_protected and not is_atomic_value(raw_protected):
                return True
            continue
        smaller, larger = (
            (normalized_candidate, normalized_protected)
            if len(normalized_candidate) <= len(normalized_protected)
            else (normalized_protected, normalized_candidate)
        )
        if any(smaller[index : index + window] in larger for index in range(len(smaller) - window + 1)):
            return True
    return False
