"""原文入库前的隐私过滤：原文库存的是全量对话，比摘要式记忆更容易把密钥带进去。

- ``<private>…</private>`` 里的内容整段不入库，换成 ``[private]``（照 claude-mem 的约定）；
- 常见凭据格式替换成 ``[REDACTED]``：各家 API key、GitHub token、AWS key、JWT、PEM 私钥、
  ``Bearer …``，以及 ``password=…`` / ``api_key: …`` 这类赋值。

宁可多涂也不要漏：被误涂的只是原文库里的一段字，漏涂的是一把钥匙。
对外暴露：redact(text) -> str。
"""

from __future__ import annotations

import re

_PRIVATE = re.compile(r"<private>.*?(?:</private>|$)", re.IGNORECASE | re.DOTALL)
_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.DOTALL),
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-~+/]{16,}=*"),
]
_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_\-]?key|access[_\-]?key|密码|密钥)(\s*[:=：]\s*)([^\s'\"，。,]{6,})"
)


def redact(text: str) -> str:
    out = _PRIVATE.sub("[private]", str(text or ""))
    for pattern in _PATTERNS:
        out = pattern.sub("[REDACTED]", out)
    return _ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", out)
