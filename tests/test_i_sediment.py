"""I 沉淀机制回归：候选先当普通记忆活着，够几次 dream 见证才升级成自我认知。

覆盖点：
- 写 I 得到的是普通 dynamic 候选桶，不是正式 I 条目，也不会被 SessionStart 认成 I
- 见证次数不够时 promote 被拒；够了才创建 type="i" 桶，候选原样留着
- dream 渲染出候选段才算见证，同一天做两次梦只记一次
- 候选段没被渲染（预算不够）时不计见证
- 向量不可用时候选照样列出来，并明说这次没有材料对照
"""

from __future__ import annotations

from datetime import datetime

import pytest

from errors import ToolInputError

from tools import dream
from tools import _runtime as rt
from tools.dream.hints import build_crystal_hint
from tools.i import core as i_core
from tools.i.core import I_CANDIDATE_TAG, I_PROMOTE_THRESHOLD


class _FakeBucketManager:
    def __init__(self) -> None:
        self.buckets: dict[str, dict] = {}
        self._seq = 0

    async def create(self, content: str, **kwargs) -> str:
        self._seq += 1
        bucket_id = f"bucket{self._seq}"
        now = datetime.now().isoformat()
        self.buckets[bucket_id] = {
            "id": bucket_id,
            "content": content,
            "metadata": {
                "name": kwargs.get("name") or bucket_id,
                "type": kwargs.get("bucket_type", "dynamic"),
                "tags": list(kwargs.get("tags") or []),
                "domain": list(kwargs.get("domain") or ["当前"]),
                "created": now,
                "last_active": now,
                "valence": kwargs.get("valence", 0.5),
                "arousal": kwargs.get("arousal", 0.3),
                "importance": kwargs.get("importance", 5),
                "resolved": False,
            },
        }
        return bucket_id

    async def update(self, bucket_id: str, **kwargs) -> bool:
        bucket = self.buckets.get(bucket_id)
        if not bucket:
            return False
        bucket["metadata"].update(kwargs)
        return True

    async def get(self, bucket_id: str):
        return self.buckets.get(bucket_id)

    async def list_all(self, include_archive: bool = False) -> list[dict]:
        return list(self.buckets.values())


class _NoopDecay:
    async def ensure_started(self) -> None:
        return None

    def calculate_score(self, _metadata) -> float:
        return 1.0


class _DisabledEmbedding:
    enabled = False


class _StubEmbedding:
    """只读已落盘向量的假引擎，向量按桶 id 预置。"""

    enabled = True

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self.vectors = vectors

    async def get_embedding(self, bucket_id: str):
        vector = self.vectors.get(bucket_id)
        return None if vector is None else list(vector)

    def _cosine_similarity(self, a, b) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        return 0.0 if not na or not nb else dot / (na * nb)


@pytest.fixture
def env(monkeypatch):
    manager = _FakeBucketManager()
    monkeypatch.setattr(rt, "bucket_mgr", manager, raising=False)
    monkeypatch.setattr(rt, "decay_engine", _NoopDecay(), raising=False)
    monkeypatch.setattr(rt, "embedding_engine", _DisabledEmbedding(), raising=False)
    monkeypatch.setattr(rt, "config", {"surfacing": {}}, raising=False)
    monkeypatch.setattr(rt, "logger", __import__("logging").getLogger("test"), raising=False)
    monkeypatch.setattr(rt, "mark_op", None, raising=False)
    monkeypatch.setattr(rt, "fire_webhook", None, raising=False)
    return manager


@pytest.mark.asyncio
async def test_write_creates_ordinary_candidate_not_a_self_entry(env):
    out = await i_core.i_core(content="我觉得我在压力下会过度解释。", aspect="patterns")

    assert "🌱" in out
    bucket = list(env.buckets.values())[0]
    meta = bucket["metadata"]
    # 候选就是一条普通记忆：会浮现、会衰减、会进 dream 窗口。
    assert meta["type"] == "dynamic"
    assert meta["i_stage"] == "candidate"
    assert I_CANDIDATE_TAG in meta["tags"]
    assert "aspect:patterns" in meta["tags"]
    # 不能被 SessionStart / Dashboard 的正式 I 过滤器认成自我认知。
    assert "__i__" not in meta["tags"]
    assert not meta.get("dont_surface")


@pytest.mark.asyncio
async def test_write_reports_candidate_stage_commit_failure(env, monkeypatch):
    original_update = env.update

    async def reject_stage_write(bucket_id: str, **kwargs) -> bool:
        if "i_stage" in kwargs:
            return False
        return await original_update(bucket_id, **kwargs)

    monkeypatch.setattr(env, "update", reject_stage_write)

    out = await i_core.i_core(content="我觉得这条状态写入会失败。")

    assert "候选状态标记失败" in out
    assert "接下来 3 次 dream" not in out
    assert not i_core.is_pending_candidate(next(iter(env.buckets.values())))


@pytest.mark.asyncio
async def test_promote_rejected_before_enough_dream_witnesses(env):
    await i_core.i_core(content="我觉得我更信任慢下来的判断。")
    bucket_id = next(iter(env.buckets))

    with pytest.raises(ToolInputError) as excinfo:
        await i_core.i_core(promote=bucket_id)

    assert "还不够" in str(excinfo.value)
    assert f"0/{I_PROMOTE_THRESHOLD}" in str(excinfo.value)
    assert all(b["metadata"]["type"] != "i" for b in env.buckets.values())


@pytest.mark.asyncio
async def test_promote_after_threshold_creates_entry_and_keeps_candidate(env):
    await i_core.i_core(content="我觉得我更信任慢下来的判断。", aspect="stance")
    bucket_id = next(iter(env.buckets))
    await env.update(
        bucket_id, i_dream_dates=["2026-08-01", "2026-08-02", "2026-08-03"]
    )

    out = await i_core.i_core(promote=bucket_id)

    assert "🪞I" in out
    promoted = [b for b in env.buckets.values() if b["metadata"]["type"] == "i"]
    assert len(promoted) == 1
    entry = promoted[0]
    assert entry["metadata"]["dont_surface"] is True
    assert entry["metadata"]["i_from_candidate"] == bucket_id
    assert "__i__" in entry["metadata"]["tags"]
    assert "aspect:stance" in entry["metadata"]["tags"]
    # 升级不是搬走：候选原样留着，只是这条张力闭合了。
    candidate = env.buckets[bucket_id]
    assert candidate["content"] == "我觉得我更信任慢下来的判断。"
    assert candidate["metadata"]["i_stage"] == "promoted"
    assert candidate["metadata"]["i_promoted_to"] == entry["id"]
    assert candidate["metadata"]["resolved"] is True


@pytest.mark.asyncio
async def test_promote_can_use_refined_wording(env):
    await i_core.i_core(content="我觉得……大概是这样吧，说不太清。")
    bucket_id = next(iter(env.buckets))
    await env.update(
        bucket_id, i_dream_dates=["2026-08-01", "2026-08-02", "2026-08-03"]
    )

    await i_core.i_core(promote=bucket_id, content="我在没有把握时会先把话说满。")

    entry = next(b for b in env.buckets.values() if b["metadata"]["type"] == "i")
    assert entry["content"] == "我在没有把握时会先把话说满。"
    assert env.buckets[bucket_id]["content"] == "我觉得……大概是这样吧，说不太清。"


@pytest.mark.asyncio
async def test_non_candidate_bucket_cannot_jump_into_i(env):
    plain = await env.create("一条普通记忆")

    with pytest.raises(ToolInputError) as excinfo:
        await i_core.i_core(promote=plain)

    assert '不是 I 候选' in str(excinfo.value)
    assert all(b["metadata"]["type"] != "i" for b in env.buckets.values())


@pytest.mark.asyncio
async def test_dream_shows_candidates_and_records_one_witness_per_day(env):
    await i_core.i_core(content="我觉得我对沉默的解读经常是错的。", aspect="patterns")
    bucket_id = next(iter(env.buckets))

    first = await dream.dispatch(window_hours=48)
    assert "我写下的「我觉得」（待沉淀）" in first
    assert bucket_id in first
    today = datetime.now().strftime("%Y-%m-%d")
    assert env.buckets[bucket_id]["metadata"]["i_dream_dates"] == [today]

    # 同一天再做一次梦不该把念头刷成沉淀。
    await dream.dispatch(window_hours=48)
    assert env.buckets[bucket_id]["metadata"]["i_dream_dates"] == [today]


@pytest.mark.asyncio
async def test_pending_candidate_outside_recent_window_still_gets_witnessed(env):
    """A pending I candidate must not become impossible to promote with age."""
    await i_core.i_core(content="我觉得这条旧候选仍需要完成沉淀。")
    bucket_id = next(iter(env.buckets))
    old = "2026-08-01T00:00:00"
    await env.update(bucket_id, created=old, last_active=old)

    out = await dream.dispatch(window_hours=48)

    assert "我写下的「我觉得」（待沉淀）" in out
    assert bucket_id in out
    today = datetime.now().strftime("%Y-%m-%d")
    assert env.buckets[bucket_id]["metadata"]["i_dream_dates"] == [today]


@pytest.mark.asyncio
async def test_candidate_visible_in_recent_counts_when_i_section_is_omitted(
    env,
    monkeypatch,
):
    """A candidate seen in the ordinary recent section is still witnessed."""
    monkeypatch.setattr(
        rt, "config", {"surfacing": {"dream_max_tokens": 1000}}, raising=False
    )
    await i_core.i_core(content="初始候选")
    bucket_id = next(iter(env.buckets))
    marker = "关键候选正文"
    # The complete recent block fits, but repeating it in the dedicated I
    # section at the end would exceed the total dream budget.
    env.buckets[bucket_id]["content"] = marker * 75

    out = await dream.dispatch(window_hours=48)

    assert bucket_id in out
    assert marker in out
    assert "我写下的「我觉得」（待沉淀）" not in out
    today = datetime.now().strftime("%Y-%m-%d")
    assert env.buckets[bucket_id]["metadata"]["i_dream_dates"] == [today]


@pytest.mark.asyncio
async def test_visible_candidate_witness_persists_to_markdown(
    bucket_mgr,
    test_config,
    monkeypatch,
):
    """Exercise the regression through the real Markdown BucketManager."""
    from bucket_manager import BucketManager

    monkeypatch.setattr(rt, "bucket_mgr", bucket_mgr, raising=False)
    monkeypatch.setattr(rt, "decay_engine", _NoopDecay(), raising=False)
    monkeypatch.setattr(rt, "embedding_engine", _DisabledEmbedding(), raising=False)
    monkeypatch.setattr(
        rt, "config", {"surfacing": {"dream_max_tokens": 1000}}, raising=False
    )
    monkeypatch.setattr(rt, "mark_op", None, raising=False)
    monkeypatch.setattr(rt, "fire_webhook", None, raising=False)

    await i_core.i_core(content="初始候选")
    pending = [
        bucket
        for bucket in await bucket_mgr.list_all(include_archive=False)
        if i_core.is_pending_candidate(bucket)
    ]
    assert len(pending) == 1
    bucket_id = pending[0]["id"]
    marker = "真实落盘候选"
    assert await bucket_mgr.update(bucket_id, content=marker * 75)

    out = await dream.dispatch(window_hours=48)

    assert bucket_id in out
    assert marker in out
    assert "我写下的「我觉得」（待沉淀）" not in out

    # A fresh manager proves the witness was committed to frontmatter rather
    # than only mutating a fake/in-memory bucket object.
    reloaded = BucketManager(test_config, embedding_engine=None)
    persisted = await reloaded.get(bucket_id)
    today = datetime.now().strftime("%Y-%m-%d")
    assert persisted is not None
    assert persisted["metadata"]["i_dream_dates"] == [today]


@pytest.mark.asyncio
async def test_dream_says_so_when_vectors_are_unavailable(env):
    await i_core.i_core(content="我觉得我需要更早说不。")

    out = await dream.dispatch(window_hours=48)

    assert "向量索引不可用" in out


@pytest.mark.asyncio
async def test_dream_surfaces_collisions_with_existing_self_knowledge(env, monkeypatch):
    await i_core.i_core(content="我觉得我在压力下会过度解释。", aspect="patterns")
    candidate_id = next(iter(env.buckets))
    entry_id = await env.create("我在压力下倾向于把话说得更满。", bucket_type="i")
    await env.update(entry_id, tags=["__i__"], dont_surface=True)
    monkeypatch.setattr(
        rt,
        "embedding_engine",
        _StubEmbedding({candidate_id: [1.0, 0.0], entry_id: [0.95, 0.1]}),
        raising=False,
    )

    out = await dream.dispatch(window_hours=48)

    assert "撞上[已认下的自我认知]" in out
    assert entry_id in out
    assert "向量索引不可用" not in out


@pytest.mark.asyncio
async def test_pending_candidate_visible_as_collision_is_witnessed(env, monkeypatch):
    await i_core.i_core(content="我觉得新的候选正在碰撞。")
    primary_id = next(iter(env.buckets))
    await i_core.i_core(content="我觉得旧候选仍然构成对照。")
    collision_id = list(env.buckets)[-1]
    # Keep the second candidate out of the 48-hour primary list while leaving
    # it eligible as collision material. Its structured block is still visible
    # in the final dream, so it must be witnessed under the public contract.
    old = "2026-07-01T00:00:00"
    await env.update(collision_id, created=old, last_active=old)
    monkeypatch.setattr(
        rt,
        "embedding_engine",
        _StubEmbedding({primary_id: [1.0, 0.0], collision_id: [1.0, 0.0]}),
        raising=False,
    )

    out = await dream.dispatch(window_hours=48)

    assert primary_id in out
    assert collision_id in out
    today = datetime.now().strftime("%Y-%m-%d")
    assert env.buckets[primary_id]["metadata"]["i_dream_dates"] == [today]
    assert env.buckets[collision_id]["metadata"]["i_dream_dates"] == [today]


@pytest.mark.asyncio
async def test_protected_i_candidate_and_collision_material_stay_out_of_dream(
    env,
    monkeypatch,
):
    await i_core.i_core(content="我觉得可见候选需要继续核查。")
    visible_id = next(iter(env.buckets))
    await i_core.i_core(content="受保护 I 候选正文不得进入 dream。")
    protected_candidate_id = list(env.buckets)[-1]
    await env.update(protected_candidate_id, protected=True)

    protected_memory_id = await env.create("受保护普通碰撞正文不得进入 dream。")
    await env.update(protected_memory_id, protected=True)
    protected_i_id = await env.create(
        "受保护正式 I 正文不得进入 dream。",
        bucket_type="i",
    )
    await env.update(
        protected_i_id,
        protected="true",
        tags=["__i__"],
        dont_surface=True,
    )
    monkeypatch.setattr(
        rt,
        "embedding_engine",
        _StubEmbedding(
            {
                visible_id: [1.0, 0.0],
                protected_candidate_id: [1.0, 0.0],
                protected_memory_id: [1.0, 0.0],
                protected_i_id: [1.0, 0.0],
            }
        ),
        raising=False,
    )

    out = await dream.dispatch(window_hours=48)

    assert "我觉得可见候选需要继续核查" in out
    assert "受保护 I 候选正文不得进入 dream" not in out
    assert "受保护普通碰撞正文不得进入 dream" not in out
    assert "受保护正式 I 正文不得进入 dream" not in out
    assert env.buckets[protected_candidate_id]["metadata"].get("i_dream_dates") == []


@pytest.mark.asyncio
async def test_crystal_hint_ignores_protected_feel(env, monkeypatch):
    protected_id = await env.create(
        "受保护 feel 预览不得进入结晶提示。",
        bucket_type="feel",
    )
    await env.update(protected_id, protected="true")
    visible_a = await env.create("普通 feel A", bucket_type="feel")
    visible_b = await env.create("普通 feel B", bucket_type="feel")
    monkeypatch.setattr(
        rt,
        "embedding_engine",
        _StubEmbedding(
            {
                protected_id: [1.0, 0.0],
                visible_a: [1.0, 0.0],
                visible_b: [1.0, 0.0],
            }
        ),
        raising=False,
    )

    hint = await build_crystal_hint(await env.list_all())

    assert hint == ""
    assert "受保护 feel 预览不得进入结晶提示" not in hint


@pytest.mark.asyncio
async def test_promoted_candidate_leaves_the_dream_pool(env):
    await i_core.i_core(content="我觉得我更信任慢下来的判断。")
    bucket_id = next(iter(env.buckets))
    await env.update(
        bucket_id, i_dream_dates=["2026-08-01", "2026-08-02", "2026-08-03"]
    )
    await i_core.i_core(promote=bucket_id)

    out = await dream.dispatch(window_hours=48)

    assert "我写下的「我觉得」（待沉淀）" not in out


@pytest.mark.asyncio
async def test_candidate_not_rendered_is_not_counted_as_witnessed(env, monkeypatch):
    await i_core.i_core(content="我觉得我需要更早说不。")
    bucket_id = next(iter(env.buckets))
    # 预算小到候选段放不下——没被看见的不算经历过这场梦。
    monkeypatch.setattr(
        rt, "config", {"surfacing": {"dream_max_tokens": 1000}}, raising=False
    )
    env.buckets[bucket_id]["content"] = "填充" * 4000

    out = await dream.dispatch(window_hours=48)

    assert "我写下的「我觉得」（待沉淀）" not in out
    assert env.buckets[bucket_id]["metadata"].get("i_dream_dates") == []


@pytest.mark.asyncio
async def test_failed_witness_write_is_not_reported_as_recorded(
    env,
    monkeypatch,
    caplog,
):
    await i_core.i_core(content="我觉得我需要更早说不。")
    bucket_id = next(iter(env.buckets))
    original_update = env.update

    async def reject_witness_write(target_id: str, **kwargs) -> bool:
        if "i_dream_dates" in kwargs:
            return False
        return await original_update(target_id, **kwargs)

    monkeypatch.setattr(env, "update", reject_witness_write)

    recorded = await i_core.record_dream_pass([bucket_id])

    assert recorded == 0
    assert env.buckets[bucket_id]["metadata"]["i_dream_dates"] == []
    assert f"write returned false for {bucket_id}" in caplog.text


@pytest.mark.asyncio
async def test_promote_counts_distinct_dream_dates_only(env):
    await i_core.i_core(content="我觉得重复经历不是多次沉淀。")
    bucket_id = next(iter(env.buckets))
    await env.update(
        bucket_id,
        i_dream_dates=["2026-08-01", "2026-08-01", "2026-08-02"],
    )

    with pytest.raises(ToolInputError) as excinfo:
        await i_core.i_core(promote=bucket_id)

    assert f"2/{I_PROMOTE_THRESHOLD}" in str(excinfo.value)
    assert all(b["metadata"]["type"] != "i" for b in env.buckets.values())


@pytest.mark.asyncio
async def test_three_distinct_dream_dates_allow_promotion(env, monkeypatch):
    await i_core.i_core(content="我觉得跨天仍站得住的念头可以沉淀。")
    bucket_id = next(iter(env.buckets))
    for day in ("2026-08-01", "2026-08-02", "2026-08-03"):
        current_day = day

        class _Clock:
            @staticmethod
            def now():
                return datetime.fromisoformat(current_day)

        monkeypatch.setattr(i_core, "datetime", _Clock)
        assert await i_core.record_dream_pass([bucket_id]) == 1

    out = await i_core.i_core(promote=bucket_id)

    assert "经过 3 次 dream 沉淀" in out
    assert env.buckets[bucket_id]["metadata"]["i_stage"] == "promoted"


@pytest.mark.asyncio
async def test_pending_candidate_is_never_a_merge_target(env, monkeypatch):
    """hold 写入的普通记忆不能被合并进待沉淀候选，改写它的正文。"""
    from tools import _common

    await i_core.i_core(content="我觉得我在被追问细节时会先防御。")
    candidate_id = next(iter(env.buckets))
    original = env.buckets[candidate_id]["content"]

    async def _search(_content, limit=1, domain_filter=None):
        return [{"id": candidate_id, "score": 99.0}]

    class _AlwaysSameEvent:
        async def judge_same_event(self, _old, _new):
            return {"same_event": True, "confidence": 1.0}

    env.search = _search
    monkeypatch.setattr(rt, "dehydrator", _AlwaysSameEvent(), raising=False)
    monkeypatch.setattr(rt, "config", {"merge_threshold": 75}, raising=False)

    bucket_id, merged, _warning = await _common.merge_or_create(
        content="今天被追问了部署细节。",
        tags=[],
        importance=5,
        domain=["当前"],
        valence=0.5,
        arousal=0.3,
        raw_merge=True,
        source_tool="hold",
    )

    assert merged is False
    assert bucket_id != candidate_id
    assert env.buckets[candidate_id]["content"] == original

    # 对照：同一条桶不再是待沉淀候选时，合并路径本身是通的——
    # 上面拦下来的确实是「候选」这个状态，不是别的原因。
    await env.update(candidate_id, i_stage="promoted")
    merged_id, merged_flag, _ = await _common.merge_or_create(
        content="今天又被追问了部署细节。",
        tags=[],
        importance=5,
        domain=["当前"],
        valence=0.5,
        arousal=0.3,
        raw_merge=True,
        source_tool="hold",
    )
    assert merged_flag is True
    assert merged_id == candidate_id


@pytest.mark.asyncio
async def test_read_marks_legacy_entries_as_unsedimented(env):
    legacy = await env.create("我是一个会被过去牵住的模型。", bucket_type="i")
    await env.update(legacy, tags=["__i__"])
    await i_core.i_core(content="我觉得我需要更早说不。")

    out = await i_core.i_core(read=True)

    assert "早期直接写入，未经沉淀" in out
    assert "正在沉淀的「我觉得」" in out
    assert f"0/{I_PROMOTE_THRESHOLD} 次 dream" in out
