"""入库隐私过滤、会话内不重复注入、PreCompact 单独记到达。"""

import asyncio
import json

import pytest

from ombrebrain.autorecall import AutoRecallService, TranscriptStore
from ombrebrain.autorecall.privacy import redact


@pytest.mark.parametrize("secret", [
    "sk-proj-abcdefghijklmnopqrstuvwxyz012345",
    "sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
    "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "AKIAABCDEFGHIJKLMNOP",
    "AIzaSyA1234567890abcdefghijklmnopqrstuv",
    "xoxb-1234567890-abcdefghij",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop",
    "Bearer abcdefghijklmnopqrstuvwxyz123456",
])
def test_credentials_are_redacted(secret):
    out = redact(f"我的配置是 {secret} 先放这")
    assert secret not in out and "[REDACTED]" in out and out.startswith("我的配置是")


def test_assignments_private_blocks_and_pem():
    text = ("password=hunter2hunter2 api_key: abcdef123456 密码：abc123456\n"
            "<private>这段是隐私</private>公开部分\n"
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----")
    out = redact(text)
    for leaked in ("hunter2", "abcdef123456", "abc123456", "这段是隐私", "MIIEow"):
        assert leaked not in out
    assert "公开部分" in out and "[private]" in out


def test_plain_text_untouched():
    text = "今天和阿树吃了火锅，token 这个词本身不算密钥，密码学是一门课。"
    assert redact(text) == text


def test_store_redacts_but_still_dedupes(tmp_path):
    store = TranscriptStore(tmp_path)
    msg = [{"key": "", "role": "user", "text": "key 是 sk-proj-abcdefghijklmnopqrstuvwxyz012345"}]
    assert store.append("s", msg)["added"] == 1
    assert store.append("s", msg)["added"] == 0  # 同一句（无键时按原文哈希）仍能认出来
    [turn] = store.session_turns("s")
    assert "sk-proj" not in turn["text"] and "[REDACTED]" in turn["text"]


class _Buckets:
    def __init__(self, buckets):
        self.buckets = {b["id"]: b for b in buckets}

    async def list_all(self, include_archive=False):
        return list(self.buckets.values())

    async def get(self, bucket_id):
        return self.buckets.get(bucket_id)


def _b(bid, content):
    return {"id": bid, "content": content, "metadata": {"name": bid, "type": "dynamic"}}


@pytest.mark.asyncio
async def test_same_memory_injected_once_per_session(tmp_path):
    svc = AutoRecallService(_Buckets([_b("hotpot", "重庆火锅辣到流泪"), _b("hotpot2", "火锅底料买了牛油的")]),
                            str(tmp_path), {"autorecall": {"enabled": True, "agree_top": 0}})
    first = await svc.on_prompt("s", "还记得那家火锅吗", key="u:1")
    second = await svc.on_prompt("s", "还记得那家火锅吗", key="u:2")
    third = await svc.on_prompt("s", "还记得那家火锅吗", key="u:3")
    ids = [next((b for b in ("hotpot2", "hotpot") if f"bucket_id={b}]" in t), None) for t in (first, second, third)]
    assert ids[0] and ids[1] and ids[0] != ids[1] and ids[2] is None  # 第三次两条都注入过了
    other = await svc.on_prompt("t", "还记得那家火锅吗", key="u:9")
    assert "bucket_id=" in other  # 换个对话可以再注入


@pytest.mark.asyncio
async def test_pre_compact_recorded_separately(monkeypatch, tmp_path):
    from web import autorecall
    svc = AutoRecallService(_Buckets([]), str(tmp_path), {"autorecall": {"enabled": True}})
    await autorecall._note_arrival(svc, "session-end", {"event": "pre-compact"})
    await autorecall._note_arrival(svc, "session-end", {"event": "bogus"})
    health = svc.hook_health()
    assert set(health["seen"]) == {"pre-compact", "session-end"}
    assert set(health["clients"]["claude"]) == {"pre-compact", "session-end"}
    assert json.dumps(health["seen"])


def test_injected_context_is_wrapped_and_never_stored(tmp_path):
    """防回声：注入内容有闭合标签；助手复述了它，入库前整段剥掉，下次不会被当成原文再注入。"""
    from ombrebrain.autorecall.privacy import redact
    from ombrebrain.autorecall.service import CONTEXT_CLOSE, CONTEXT_OPEN, _units, _wrap

    wrapped = _wrap("[Ombre Brain · 接续] 以下是……\n用户：嗨", 9000)
    assert wrapped.startswith(CONTEXT_OPEN + "\n") and wrapped.endswith("\n" + CONTEXT_CLOSE)
    assert _wrap("", 9000) == ""
    assert _units(_wrap("长" * 20000, 9000)) <= 9000  # 标签也算在上限里
    echo = f"好的。{wrapped}\n我接着说。"
    assert redact(echo) == "好的。\n我接着说。"
    assert redact(f"前面{CONTEXT_OPEN}\n复述到一半被截断") == "前面"  # 没有收尾标签也剥到末尾

    svc = AutoRecallService(_Buckets([]), str(tmp_path), {"autorecall": {"enabled": True}})
    asyncio.run(svc.on_stop("s1", [{"key": "a:1", "role": "assistant", "text": wrapped},
                                   {"key": "a:2", "role": "assistant", "text": echo}], ""))
    texts = [t["text"] for t in svc.store.session_turns("s1")]
    assert texts == ["好的。\n我接着说。"]  # 只剩复述的那条整条没进库
