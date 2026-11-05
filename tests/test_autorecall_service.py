"""Claude Code 自动召回 v1：原文库 + 唤醒召回 + 开局接续。"""

import asyncio
import sqlite3
from datetime import datetime, timedelta

import pytest

from ombrebrain.autorecall import AutoRecallService, TranscriptStore, validate_transcript_snapshot_file
from ombrebrain.autorecall.lexical import BM25Index, tokens


class FakeBuckets:
    def __init__(self, buckets):
        self.buckets = {b["id"]: b for b in buckets}
        self.list_calls = []

    async def list_all(self, include_archive=False):
        self.list_calls.append(include_archive)
        return list(self.buckets.values())

    async def get(self, bucket_id):
        return self.buckets.get(bucket_id)


class FakeEngine:
    enabled = True

    def __init__(self, ranking=None, error=None, delay=0.0):
        self.ranking, self.error, self.delay, self.calls = ranking or [], error, delay, []

    async def search_similar_strict(self, query, top_k=10, allowed_bucket_ids=None):
        self.calls.append((query, allowed_bucket_ids))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return [(b, 0.9 - i * 0.01) for i, b in enumerate(self.ranking)][:top_k]


def _b(bid, content, **meta):
    return {"id": bid, "content": content, "metadata": {"name": bid, "type": "dynamic", **meta}}


def _svc(tmp_path, buckets=(), *, engine=None, dream=None, today="2026-10-14", names=None, **cfg):
    config = {"autorecall": {"enabled": True, **cfg}}
    return AutoRecallService(
        FakeBuckets(list(buckets)), str(tmp_path), config,
        embedding_engine=engine, dream_fn=dream, today_fn=lambda: today,
        names_fn=(lambda: list(names)) if names is not None else None,
    )


def _turns(n, start=0, prefix="说点工作上的事"):
    out = []
    for i in range(start, start + n):
        out.append({"key": f"u{i}", "role": "user", "text": f"{prefix} 第{i}句"})
        out.append({"key": f"a{i}", "role": "assistant", "text": f"回复第{i}句"})
    return out


# ---------------- 分词 / BM25 ----------------

def test_bigram_tokens():
    assert tokens("我想吃火锅") == ["我想", "想吃", "吃火", "火锅"]
    assert tokens("好") == ["好"]
    assert tokens("The Ombre brain 记忆") == ["ombre", "brain", "记忆"]


def test_bm25_prefers_matching_doc():
    scores = BM25Index(["周末去爬山看日出", "今天的火锅很辣", "写代码到半夜"]).scores("火锅辣不辣")
    assert max(scores, key=scores.get) == 1


# ---------------- 开关 ----------------

@pytest.mark.asyncio
async def test_disabled_does_nothing(tmp_path):
    svc = AutoRecallService(FakeBuckets([_b("m1", "火锅很辣")]), str(tmp_path), {})
    assert await svc.on_prompt("s", "还记得那家火锅吗") == ""
    assert await svc.on_stop("s", _turns(1)) == {"added": 0, "links": 0}
    assert await svc.on_session_start("s2", "startup") == ""
    assert not svc.store.exists
    assert svc.bucket_mgr.list_calls == []


# ---------------- 唤醒召回 ----------------

@pytest.mark.asyncio
async def test_no_wake_word_no_recall(tmp_path):
    svc = _svc(tmp_path, [_b("hotpot", "周五和朋友吃了重庆火锅")], agree_top=0)
    assert await svc.recall("火锅辣不辣") == []
    svc2 = _svc(tmp_path, [_b("hotpot", "周五和朋友吃了重庆火锅")], agree_top=0, wake_on_names=False)
    assert await svc2.recall("火锅辣不辣") == []
    assert svc2.bucket_mgr.list_calls == []  # 关了名字唤醒、又没有语气词，连索引都不建


@pytest.mark.asyncio
async def test_wake_word_returns_top1_and_includes_digested(tmp_path):
    svc = _svc(tmp_path, [
        _b("hotpot", "周五和朋友吃了重庆火锅，辣到流泪", digested=True),
        _b("hike", "周末去爬山看日出"),
    ], agree_top=0)
    hits = await svc.recall("还记得上次那家火锅吗")
    assert [b["id"] for b in hits] == ["hotpot"]
    assert set(svc.bucket_mgr.list_calls) == {False}  # 不碰归档全量解析


@pytest.mark.asyncio
async def test_hidden_kinds_never_surface(tmp_path):
    svc = _svc(tmp_path, [
        _b("letter", "火锅 火锅 火锅", type="letter"),
        _b("letter2", "火锅 火锅", tags=["__letter__"]),
        _b("feel", "火锅 火锅", type="feel"),
        _b("plan", "火锅 火锅", type="plan"),
        _b("core", "火锅 火锅", pinned=True),
        _b("perm", "火锅 火锅", type="permanent"),
        _b("quiet", "火锅 火锅", dont_surface=True),
        _b("ok", "那家火锅"),
    ], agree_top=0)
    assert [b["id"] for b in await svc.recall("记得火锅吗", top_k=5)] == ["ok"]


@pytest.mark.asyncio
async def test_no_match_returns_nothing_not_recent_filler(tmp_path):
    svc = _svc(tmp_path, [_b("hike", "周末去爬山看日出")])
    assert await svc.recall("还记得量子物理期末考试吗") == []


@pytest.mark.asyncio
async def test_vector_fuses_with_lexical_and_respects_visibility(tmp_path):
    engine = FakeEngine(ranking=["snake", "feel"])
    svc = _svc(tmp_path, [
        _b("snake", "十月九号决定养一条玉米蛇"),
        _b("noise", "那天开会讨论排期"),
        _b("feel", "蛇蛇好可爱", type="feel"),
    ], engine=engine, agree_top=0)
    hits = await svc.recall("还记得我说过的宠物吗")
    assert [b["id"] for b in hits] == ["snake"]
    assert "feel" not in engine.calls[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", [FakeEngine(error=RuntimeError("down")), FakeEngine(ranking=["x"], delay=5)])
async def test_vector_failure_falls_back_to_lexical(tmp_path, engine):
    svc = _svc(tmp_path, [_b("hotpot", "重庆火锅"), _b("x", "别的")], engine=engine, vector_timeout=0.5, agree_top=0)
    assert [b["id"] for b in await svc.recall("记得火锅吗")] == ["hotpot"]
    gated = _svc(tmp_path, [_b("hotpot", "重庆火锅"), _b("x", "别的")], engine=engine, vector_timeout=0.5)
    assert await gated.recall("记得火锅吗") == []  # 开着反向门、向量又不通：这一轮不注入


@pytest.mark.asyncio
async def test_index_rebuilds_when_bucket_changes(tmp_path):
    svc = _svc(tmp_path, [_b("m1", "周末去爬山")], agree_top=0)
    assert await svc.recall("记得火锅吗") == []
    svc.bucket_mgr.buckets["m2"] = _b("m2", "晚上吃火锅")
    assert [b["id"] for b in await svc.recall("记得火锅吗")] == ["m2"]


@pytest.mark.asyncio
async def test_custom_wake_words_from_panel(tmp_path):
    svc = _svc(tmp_path, [_b("ws", "阿树今天又改了代码")], wake_words="阿树，小林", agree_top=0, wake_on_names=False)
    assert [b["id"] for b in await svc.recall("我在和阿树迭代代码")] == ["ws"]
    assert await svc.recall("还记得吗") == []  # 默认词被面板上的词表替换


@pytest.mark.asyncio
async def test_known_names_wake_without_recall_words(tmp_path):
    svc = _svc(tmp_path, [
        _b("snake", "[[Mochi]] 是一条玉米蛇，十月十号起的名"),
        _b("ws", "[[阿树]] 用 Codex 跑了四批工程"),
        _b("work", "[[工作]] 区要整理"),
    ], agree_top=0, names=["阿树", "Mochi"])
    assert [b["id"] for b in await svc.recall("我在和阿树迭代代码")] == ["ws"]
    assert [b["id"] for b in await svc.recall("Mochi吃了吗")] == ["snake"]
    assert await svc.recall("mochi 是什么") == []  # 英文名按整词、区分大小写
    assert await svc.recall("今天好冷") == []
    assert await svc.recall("帮我整理一下工作区") == []  # [[工作]] 不在名册里，不再当名字


@pytest.mark.asyncio
async def test_unwoken_prompt_never_touches_vault(tmp_path):
    svc = _svc(tmp_path, [_b("x", "火锅")], names=["阿树"])
    assert await svc.recall("今天好冷") == []
    assert svc.bucket_mgr.list_calls == []


@pytest.mark.asyncio
async def test_self_i_anchor_never_surface(tmp_path):
    svc = _svc(tmp_path, [
        _b("self", "火锅 火锅", type="self"),
        _b("i", "火锅 火锅", type="i"),
        _b("cand", "火锅 火锅", i_stage="candidate"),
        _b("cand2", "火锅 火锅", tags=["__i_candidate__"]),
        _b("anchor", "火锅 火锅", anchor=True),
        _b("prot", "那家火锅", protected=True),
    ], agree_top=0)
    assert [b["id"] for b in await svc.recall("记得火锅吗", top_k=9)] == ["prot"]


@pytest.mark.asyncio
async def test_agreement_gate(tmp_path):
    buckets = [_b("snake", "玉米蛇 Mochi 的饲养记录"), _b("sleep", "那天很晚才睡"), _b("x", "无关")]
    agree = _svc(tmp_path, buckets, engine=FakeEngine(ranking=["snake", "x"]))
    assert [b["id"] for b in await agree.recall("记得玉米蛇吗")] == ["snake"]  # 两路前 2 都有它
    disagree = _svc(tmp_path, buckets, engine=FakeEngine(ranking=["sleep", "x"]))
    assert await disagree.recall("记得玉米蛇吗") == []  # 字面说蛇、向量说睡觉：不注入


# ---------------- 存 + hold 提醒 ----------------

@pytest.mark.asyncio
async def test_append_is_idempotent_and_session_end_backfills(tmp_path):
    svc = _svc(tmp_path)
    assert (await svc.on_stop("s", _turns(1)))["added"] == 2
    assert (await svc.on_stop("s", _turns(1)))["added"] == 0
    out = await svc.on_session_end("s", _turns(3))  # 整段补存：前 1 轮已有，补 2 轮
    assert out["added"] == 4 and out["user_turns"] == 3
    assert [t["seq"] for t in svc.store.session_turns("s")] == [1, 2, 3, 4, 5, 6]


@pytest.mark.asyncio
async def test_hold_reminder_every_n_user_turns(tmp_path):
    svc = _svc(tmp_path, reminder_every=3)
    outs = [await svc.on_prompt("s", f"继续写第{i}段", key=f"u:{i}") for i in range(1, 8)]
    flags = ["存档提醒" in o for o in outs]
    assert flags == [False, False, True, False, False, True, False]
    assert "第1段" not in outs[2] and "hold" in outs[2]  # 提醒里不带原文


@pytest.mark.asyncio
async def test_prompt_stores_user_and_stop_stores_assistant(tmp_path):
    svc = _svc(tmp_path)
    await svc.on_prompt("s", "今天想吃火锅", key="u:p1")
    await svc.on_prompt("s", "今天想吃火锅", key="u:p1")  # 同一条重复投递不重复记
    await svc.on_stop("s", [{"key": "a:p1", "role": "assistant", "text": "好呀"}])
    assert [(t["role"], t["text"]) for t in svc.store.session_turns("s")] == [
        ("user", "今天想吃火锅"), ("assistant", "好呀"),
    ]


# ---------------- 开局接续 + dream ----------------

@pytest.mark.asyncio
async def test_session_start_restores_previous_last_rounds(tmp_path):
    svc = _svc(tmp_path, restore_rounds=2)
    await svc.on_stop("old", _turns(5, prefix="上一窗口"), cwd="/proj/a")
    text = await svc.on_session_start("new", "startup", cwd="/proj/a")
    assert "接续" in text and "第4句" in text and "第3句" in text and "第2句" not in text
    assert await svc.on_session_start("new", "resume", cwd="/proj/a") == ""
    assert await svc.on_session_start("new", "fork", cwd="/proj/a") == ""


@pytest.mark.asyncio
async def test_restore_never_crosses_projects(tmp_path):
    svc = _svc(tmp_path)
    await svc.on_stop("a1", _turns(2, prefix="A 项目"), cwd="/proj/a")
    await svc.on_stop("b1", _turns(2, prefix="B 项目"), cwd="/proj/b")
    text = await svc.on_session_start("a2", "clear", cwd="/proj/a")
    assert "A 项目" in text and "B 项目" not in text
    assert await svc.on_session_start("x", "startup", cwd="") == ""  # 不知道目录就不猜


@pytest.mark.asyncio
async def test_restore_keeps_newest_rounds_when_over_budget(tmp_path):
    svc = _svc(tmp_path, restore_rounds=50, max_chars=1500, dream_on_start=False)
    await svc.on_stop("old", [{"key": f"k{i}", "role": "user", "text": f"第{i}句" + "长" * 200} for i in range(50)],
                      cwd="/p")
    text = await svc.on_session_start("new", "startup", cwd="/p")
    assert "第49句" in text and "第0句" not in text


@pytest.mark.asyncio
async def test_backfilled_turn_sorts_by_time(tmp_path):
    svc = _svc(tmp_path, restore_rounds=3)
    await svc.on_stop("old", [{"key": "u1", "role": "user", "text": "第一句", "at": "2026-10-14T01:00:00Z"},
                              {"key": "u3", "role": "user", "text": "第三句", "at": "2026-10-14T03:00:00Z"}], cwd="/p")
    await svc.on_session_end("old", [{"key": "u2", "role": "user", "text": "第二句", "at": "2026-10-14T02:00:00Z"}],
                             cwd="/p")
    assert [t["text"] for t in svc.store.session_turns("old")] == ["第一句", "第二句", "第三句"]


@pytest.mark.asyncio
async def test_compact_restores_same_session(tmp_path):
    svc = _svc(tmp_path, restore_rounds=1)
    await svc.on_stop("old", _turns(2, prefix="别的窗口"), cwd="/p")
    await svc.on_stop("cur", _turns(2, prefix="当前窗口"), cwd="/p")
    text = await svc.on_session_start("cur", "compact", cwd="/p")
    assert "当前窗口 第1句" in text and "别的窗口" not in text


@pytest.mark.asyncio
async def test_dream_once_per_day_and_released_on_failure(tmp_path):
    calls = []

    async def dream():
        calls.append(1)
        return "" if len(calls) == 1 else "近 48 小时的回顾"

    svc = _svc(tmp_path, dream=dream)
    assert "dream" not in await svc.on_session_start("a", "startup")  # 第一次失败，不占当天额度
    assert "近 48 小时的回顾" in await svc.on_session_start("b", "startup")
    assert "近 48 小时的回顾" not in await svc.on_session_start("c", "startup")
    assert len(calls) == 2
    svc.today_fn = lambda: "2026-10-15"
    assert "近 48 小时的回顾" in await svc.on_session_start("d", "clear")


@pytest.mark.asyncio
@pytest.mark.parametrize("behaviour", ["failure_text", "timeout", "raises"])
async def test_bad_dream_keeps_restore_and_returns_quota(tmp_path, behaviour):
    async def dream():
        if behaviour == "failure_text":
            return "记忆系统暂时无法访问。"
        if behaviour == "timeout":
            await asyncio.sleep(10)
        raise RuntimeError("boom")

    svc = _svc(tmp_path, dream=dream, dream_timeout=2)
    await svc.on_stop("old", _turns(1, prefix="上一窗口"), cwd="/p")
    text = await svc.on_session_start("new", "startup", cwd="/p")
    assert "上一窗口" in text and "今日 dream" not in text and "无法访问" not in text
    assert not svc.store.daily_used("dream_injected", "2026-10-14")


@pytest.mark.asyncio
async def test_compact_does_not_take_daily_dream(tmp_path):
    calls = []

    async def dream():
        calls.append(1)
        return "回顾"

    svc = _svc(tmp_path, dream=dream)
    await svc.on_stop("cur", _turns(1), cwd="/p")
    assert "回顾" not in await svc.on_session_start("cur", "compact", cwd="/p")
    assert calls == []
    assert "回顾" in await svc.on_session_start("new", "startup", cwd="/p")


@pytest.mark.asyncio
async def test_metadata_change_invalidates_index(tmp_path):
    svc = _svc(tmp_path, [_b("hotpot", "重庆火锅")], agree_top=0)
    assert [b["id"] for b in await svc.recall("记得火锅吗")] == ["hotpot"]
    svc.bucket_mgr.buckets["hotpot"]["metadata"]["pinned"] = True
    assert await svc.recall("记得火锅吗") == []
    svc.bucket_mgr.buckets["hotpot"]["metadata"].update(pinned=False, locked_by="human", tags=["__letter__"])
    assert await svc.recall("记得火锅吗") == []


@pytest.mark.asyncio
async def test_recall_failure_does_not_swallow_reminder(tmp_path):
    svc = _svc(tmp_path, reminder_every=1)

    async def broken(*_a, **_k):
        raise RuntimeError("index down")

    svc.recall = broken
    assert "存档提醒" in await svc.on_prompt("s", "还记得吗", key="u:1")


@pytest.mark.asyncio
async def test_injection_stays_within_budget(tmp_path):
    async def dream():
        return "梦" * 20000

    svc = _svc(tmp_path, dream=dream, max_chars=3000)
    await svc.on_stop("old", [{"key": f"k{i}", "role": "user", "text": "长" * 4000} for i in range(10)], cwd="/p")
    text = await svc.on_session_start("new", "startup", cwd="/p")
    assert "接续" in text and len(text.encode("utf-16-le")) // 2 <= 3000


# ---------------- 原文 ↔ 桶 连线 ----------------

@pytest.mark.asyncio
async def test_links_only_buckets_created_during_session(tmp_path):
    now = datetime.now()
    svc = _svc(tmp_path, [
        _b("fresh", "用户想给 OB 做 Claude Code 自动召回，Stop 钩子存原文", created=now.isoformat()),
        _b("old", "Claude Code 自动召回 Stop 钩子", created=(now - timedelta(days=3)).isoformat()),
    ])
    out = await svc.on_stop("s", [
        {"key": "1", "role": "user", "text": "我想给 ob 加个自动召回"},
        {"key": "2", "role": "user", "text": "用 Stop 钩子存原文"},
        {"key": "3", "role": "user", "text": "晚饭吃什么"},
    ])
    assert out["links"] >= 1
    linked = {r["text"] for r in svc.store.links_for_bucket("fresh")}
    assert "用 Stop 钩子存原文" in linked and "晚饭吃什么" not in linked
    assert all(r["score"] > 0 for r in svc.store.links_for_bucket("fresh"))
    assert svc.store.links_for_bucket("old") == []


def test_snapshot_validator(tmp_path):
    store = TranscriptStore(tmp_path)
    store.append("s", [{"key": "1", "role": "user", "text": "一句正常的话"}])
    validate_transcript_snapshot_file(store.path)
    bad = tmp_path / "bad.sqlite3"
    conn = sqlite3.connect(bad)
    conn.execute("CREATE TABLE evil(x)")
    conn.commit()
    conn.close()
    with pytest.raises(Exception):
        validate_transcript_snapshot_file(bad)


def _gateway_request(history, roles=None, **extra):
    import hashlib
    return {"session_id": "gateway-test", "cwd": "/work", "client": "gateway", "prompt": "还记得火锅吗",
            "injection": {"version": 2, "mode": "gateway", "history": [hashlib.sha256(x.encode()).hexdigest() for x in history],
                          "roles": roles or ["user" if i % 2 == 0 else "assistant" for i in range(len(history))],
                          "policy": "a" * 64}, **extra}


def _hook_request(key, delivered=(), epoch="first", **extra):
    return {"session_id": "hook-test", "cwd": "/work", "client": "claude", "prompt_id": key,
            "prompt": "还记得火锅吗", "injection": {"version": 2, "mode": "hook", "epoch": epoch,
            "request_id": key, "delivered": list(delivered)}, **extra}


@pytest.mark.asyncio
async def test_v2_gateway_replay_dedupe_trim_and_restart(tmp_path):
    svc = _svc(tmp_path, [_b("hotpot", "重庆火锅")], agree_top=0, restore_rounds=0)
    first = await svc.prepare_injection(_gateway_request(["u1"]), "prompt")
    assert "重庆火锅" in first["context"]
    second = await svc.prepare_injection(_gateway_request(["u1", "a1", "u2"]), "prompt")
    assert second["context"] == ""
    assert second["placements"][0]["context"] == first["context"]
    svc = _svc(tmp_path, [_b("hotpot", "已经改写的火锅")], agree_top=0, restore_rounds=0)
    retry = await svc.prepare_injection(_gateway_request(["u1"]), "prompt")
    assert retry["context"] == first["context"]  # 重启和记忆改版都不能改写重试快照。
    cropped = await svc.prepare_injection(_gateway_request(["u2", "a2", "u3"]), "prompt")
    assert "已经改写的火锅" in cropped["context"]  # 旧注入已退出实际窗口。
    assert cropped["placements"][0]["context"] == ""


@pytest.mark.asyncio
async def test_v2_tool_continuation_replays_without_storing_prompt(tmp_path):
    svc = _svc(tmp_path, [_b("hotpot", "重庆火锅")], agree_top=0)
    first = await svc.prepare_injection(_gateway_request(["u"]), "prompt")
    count = svc.store.counts()
    out = await svc.prepare_injection(_gateway_request(["u", "call", "result"], ["user", "assistant", "tool"]), "prompt")
    assert out["placements"][0]["context"] == first["context"]
    assert svc.store.counts() == count


@pytest.mark.asyncio
async def test_v2_hook_only_delivered_receipts_cover_and_compact_releases(tmp_path):
    svc = _svc(tmp_path, [_b("hotpot", "重庆火锅")], agree_top=0)
    first = await svc.prepare_injection(_hook_request("p1"), "prompt")
    lost = await svc.prepare_injection(_hook_request("p2"), "prompt")
    assert "重庆火锅" in lost["context"]  # p1 没有回执，不能假定已经送到。
    assert (await svc.prepare_injection(_hook_request("p3", [first["receipt_id"]]), "prompt"))["context"] == ""
    assert "重庆火锅" in (await svc.prepare_injection(_hook_request("p4", [first["receipt_id"]], "after-compact"), "prompt"))["context"]
    assert "重庆火锅" in (await svc.prepare_injection(_hook_request("p5", [first["receipt_id"]], cwd="/other"), "prompt"))["context"]
    assert (await svc.prepare_injection(_hook_request("p1"), "prompt"))["context"] == first["context"]


@pytest.mark.asyncio
async def test_v2_changes_and_ambiguous_suffix_do_not_replay_wrong_branch(tmp_path):
    svc = _svc(tmp_path, [_b("hotpot", "重庆火锅")], agree_top=0)
    await svc.prepare_injection(_gateway_request(["old-root", "a", "same"]), "prompt")
    svc.bucket_mgr.buckets["hotpot"]["content"] = "成都火锅"
    await svc.prepare_injection(_gateway_request(["new-root", "b", "same"]), "prompt")
    out = await svc.prepare_injection(_gateway_request(["same", "reply", "next"]), "prompt")
    assert [p["index"] for p in out["placements"]] == [2]  # 无法辨别分支，不猜历史位置。
    assert "成都火锅" in out["context"]


@pytest.mark.asyncio
async def test_v2_combined_budget_and_failed_save_dont_claim_delivery(tmp_path, monkeypatch):
    from ombrebrain.autorecall.service import _units
    async def dream():
        return "梦😀" * 4000
    svc = _svc(tmp_path, [_b("hotpot", "火锅😀" * 1000)], agree_top=0, dream=dream, max_chars=1000)
    out = await svc.prepare_injection(_gateway_request(["u1"]), "prompt")
    assert _units(out["context"]) <= 1000
    original = svc.store.save_injection_state
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(svc.store, "save_injection_state", fail)
    with pytest.raises(OSError):
        await svc.prepare_injection(_hook_request("p1"), "prompt")
    monkeypatch.setattr(svc.store, "save_injection_state", original)
    assert "火锅" in (await svc.prepare_injection(_hook_request("p1"), "prompt"))["context"]


def test_v2_snapshot_is_backup_compatible_and_rejects_stale_commit(tmp_path):
    store = TranscriptStore(tmp_path)
    state = store.injection_state("a")
    store.save_injection_state("a", state)
    validate_transcript_snapshot_file(store.path)
    with pytest.raises(Exception, match="changed"):
        store.save_injection_state("a", state)
    assert store.injection_state("a")["revision"] == 1


@pytest.mark.asyncio
async def test_v2_concurrent_retry_and_new_memory_version(tmp_path):
    svc = _svc(tmp_path, [_b("hotpot", "重庆火锅")], agree_top=0)
    request = _hook_request("p1")
    first, retry = await asyncio.gather(svc.prepare_injection(request, "prompt"), svc.prepare_injection(request, "prompt"))
    assert first == retry and svc.store.counts()["turns"] == 1
    svc.bucket_mgr.buckets["hotpot"]["metadata"]["last_active"] = "以后"
    assert (await svc.prepare_injection(_hook_request("p2", [first["receipt_id"]]), "prompt"))["context"] == ""
    svc.bucket_mgr.buckets["hotpot"]["content"] = "成都火锅"
    changed = await svc.prepare_injection(_hook_request("p3", [first["receipt_id"]]), "prompt")
    assert "成都火锅" in changed["context"] and "新版本" in changed["context"]
    assert (await svc.prepare_injection(request, "prompt"))["context"] == first["context"]


def test_v2_retention_does_not_delete_transcripts(tmp_path):
    store = TranscriptStore(tmp_path)
    store.append("s", [{"role": "user", "text": "需要保留的原文"}])
    state = {"revision": 0, "records": [{"id": str(i), "context": "x" * 20000} for i in range(150)]}
    store.save_injection_state("a", state)
    saved = store.injection_state("a")
    assert len(saved["records"]) < 128 and saved["records"][-1]["id"] == "149"
    assert store.session_turns("s")[0]["text"] == "需要保留的原文"


@pytest.mark.asyncio
async def test_v2_independent_session_does_not_wait_for_slow_recall(tmp_path):
    svc = _svc(tmp_path, restore_rounds=0)
    entered, release = asyncio.Event(), asyncio.Event()
    original = svc._prompt_content

    async def slow(sid, *args, **kwargs):
        if sid == "slow":
            entered.set()
            await release.wait()
        return await original(sid, *args, **kwargs)

    svc._prompt_content = slow
    task = asyncio.create_task(svc.prepare_injection(_hook_request("p1", session_id="slow"), "prompt"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        other = await asyncio.wait_for(svc.prepare_injection(_hook_request("p1", session_id="other"), "prompt"), 2)
        assert other["protocol"] == 2 and not task.done()
    finally:
        release.set()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["gateway", "hook"])
async def test_v2_failed_snapshot_returns_dream_quota(tmp_path, monkeypatch, mode):
    async def dream():
        return "尚未交付的今日回顾"

    svc = _svc(tmp_path, dream=dream, restore_rounds=0)
    request = _gateway_request(["u1"]) if mode == "gateway" else _hook_request("start", source="startup")
    event = "prompt" if mode == "gateway" else "session-start"
    original = svc.store.save_injection_state

    def fail(*args):
        raise OSError("模拟快照写盘失败")

    monkeypatch.setattr(svc.store, "save_injection_state", fail)
    with pytest.raises(OSError):
        await svc.prepare_injection(request, event)
    assert not svc.store.daily_used("dream_injected", "2026-10-14")
    monkeypatch.setattr(svc.store, "save_injection_state", original)
    result = await svc.prepare_injection(request, event)
    assert "尚未交付的今日回顾" in result["context"]
    assert svc.store.daily_used("dream_injected", "2026-10-14")
    assert (await svc.prepare_injection(request, event))["context"] == result["context"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_write", [False, True])
async def test_v2_cancel_during_snapshot_waits_for_actual_commit(tmp_path, monkeypatch, fail_write):
    import threading

    async def dream():
        return "取消时仍需保留的回顾"

    svc = _svc(tmp_path, dream=dream, restore_rounds=0)
    entered, release = threading.Event(), threading.Event()
    original = svc.store.save_injection_state
    request = _gateway_request(["u1"])

    def blocked(*args):
        entered.set()
        assert release.wait(5)
        if fail_write:
            raise OSError("模拟取消后写盘失败")
        return original(*args)

    monkeypatch.setattr(svc.store, "save_injection_state", blocked)
    task = asyncio.create_task(svc.prepare_injection(request, "prompt"))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
    finally:
        release.set()
    with pytest.raises((asyncio.CancelledError, OSError)):
        await task
    assert svc.store.daily_used("dream_injected", "2026-10-14") is (not fail_write)
    monkeypatch.setattr(svc.store, "save_injection_state", original)
    retry = await svc.prepare_injection(request, "prompt")
    assert "取消时仍需保留的回顾" in retry["context"]
    assert svc.store.daily_used("dream_injected", "2026-10-14")


@pytest.mark.asyncio
async def test_v2_cancel_during_dream_releases_own_quota(tmp_path):
    entered = asyncio.Event()

    async def dream():
        entered.set()
        await asyncio.Event().wait()

    svc = _svc(tmp_path, dream=dream, restore_rounds=0)
    task = asyncio.create_task(svc.prepare_injection(_gateway_request(["u1"]), "prompt"))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not svc.store.daily_used("dream_injected", "2026-10-14")


@pytest.mark.asyncio
async def test_v2_failed_other_session_does_not_release_successful_dream(tmp_path, monkeypatch):
    async def dream():
        return "已交付的回顾"

    svc = _svc(tmp_path, dream=dream, restore_rounds=0)
    await svc.prepare_injection(_gateway_request(["u1"]), "prompt")

    def fail(*args):
        raise OSError("另一会话写盘失败")

    monkeypatch.setattr(svc.store, "save_injection_state", fail)
    with pytest.raises(OSError):
        await svc.prepare_injection(_gateway_request(["u2"], session_id="other"), "prompt")
    assert svc.store.daily_used("dream_injected", "2026-10-14")


@pytest.mark.asyncio
async def test_v2_cancel_during_quota_claim_does_not_leak_quota(tmp_path, monkeypatch):
    import threading

    async def dream():
        pytest.fail("已取消的请求不应继续生成 dream")

    svc = _svc(tmp_path, dream=dream, restore_rounds=0)
    entered, release = threading.Event(), threading.Event()
    original = svc.store.claim_daily

    def blocked(*args):
        acquired = original(*args)
        entered.set()
        assert release.wait(5)
        return acquired

    monkeypatch.setattr(svc.store, "claim_daily", blocked)
    task = asyncio.create_task(svc.prepare_injection(_gateway_request(["u1"]), "prompt"))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not svc.store.daily_used("dream_injected", "2026-10-14")


@pytest.mark.asyncio
async def test_v2_empty_dream_does_not_consume_quota_with_saved_snapshot(tmp_path):
    async def dream():
        return ""

    svc = _svc(tmp_path, dream=dream, restore_rounds=0)
    result = await svc.prepare_injection(_gateway_request(["u1"]), "prompt")
    assert result["receipt_id"]
    assert not svc.store.daily_used("dream_injected", "2026-10-14")


@pytest.mark.asyncio
async def test_v2_empty_turns_preserve_live_snapshot_and_empty_retry(tmp_path):
    svc = _svc(tmp_path, [_b("hotpot", "重庆火锅")], agree_top=0, restore_rounds=0,
               dream_on_start=False, reminder_every=0)
    history = []
    for turn in range(130):
        if history:
            history.append(f"a{turn}")
        history.append(f"u{turn}")
        request = _gateway_request(list(history))
        result = await svc.prepare_injection(request, "prompt")
        if turn == 0:
            first = result["context"]
            assert "重庆火锅" in first
        else:
            assert result["context"] == "", f"第 {turn + 1} 轮重复注入"
            assert result["placements"][0]["context"] == first
        if turn == 100:
            empty_retry = request
    # 重启、记忆更新后重试旧的空结果，不能把新内容补进旧请求。
    restarted = _svc(tmp_path, [_b("hotpot", "成都火锅")], agree_top=0)
    assert (await restarted.prepare_injection(empty_retry, "prompt"))["context"] == ""


def test_v2_compact_state_shares_prefixes_and_reads_legacy(tmp_path):
    import hashlib
    import json

    store = TranscriptStore(tmp_path)
    path = [hashlib.sha256(str(i).encode()).hexdigest() for i in range(200)]
    records = [{"id": str(i), "context": "记忆" if i == 0 else "", "refs": ["m@v"] if i == 0 else [],
                "path": path[:i + 1], "policy": "p"} for i in range(100)]
    legacy = {"revision": 1, "records": records}
    connection = store._connect(create=True)
    connection.execute("INSERT INTO module_state VALUES (?, ?)", ("injection:v2:legacy", json.dumps(legacy)))
    connection.close()
    state = store.injection_state("legacy")
    assert state == legacy
    store.save_injection_state("legacy", state)
    restored = TranscriptStore(tmp_path).injection_state("legacy")
    assert restored["records"] == records
    raw = store._read("SELECT value FROM module_state WHERE key = ?", ("injection:v2:legacy",))[0]["value"]
    assert len(raw.encode()) < len(json.dumps(legacy).encode()) / 3
    validate_transcript_snapshot_file(store.path)


def test_v2_snapshot_and_empty_retry_limits_are_independent(tmp_path):
    store = TranscriptStore(tmp_path)
    live = {"id": "live", "context": "仍在窗口里的记忆", "refs": ["m@v"], "path": ["root"], "policy": "p"}
    empty = [{"id": str(i), "context": "", "refs": [], "path": ["root", str(i)], "policy": "p"}
             for i in range(300)]
    store.save_injection_state("a", {"revision": 0, "records": [live, *empty]})
    saved = store.injection_state("a")["records"]
    assert saved[0] == live
    assert len(saved) == 129 and saved[1]["id"] == "172" and saved[-1]["id"] == "299"


def test_v2_compact_paths_keep_branches_distinct(tmp_path):
    store = TranscriptStore(tmp_path)
    records = [{"id": str(i), "context": "记忆", "refs": [], "policy": "p", "path": path}
               for i, path in enumerate([["root", "a", "same"], ["root", "b", "same"], ["other", "a", "same"]])]
    store.save_injection_state("branches", {"revision": 0, "records": records})
    assert store.injection_state("branches")["records"] == records
