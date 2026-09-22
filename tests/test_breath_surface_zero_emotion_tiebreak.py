"""breath surface 排序不该把 valence/arousal=0.0 当成缺失值回退默认值。

找茬会话发现的 bug：tools/breath/surface.py 的 _sort_key 用
`meta.get("arousal") or 0.3`。Python 里 0.0 是 falsy，而
_clamp_unit（bucket_manager.py）允许 valence/arousal 落在 [0,1] 闭区间，
0.0 是合法存储值（比如效价/唤醒度恰好是极端值的记忆）——这类记忆的
情感强度 tiebreak 会被悄悄换成默认值 0.3×0.5=0.15，而不是真实的 0.0，
排序因此失真。

本测试把 decay_score 和 last_active 都强行拉平（monkeypatch
calculate_score 恒定返回同一个值、把两个桶的时间戳改成完全相同），
让 av（arousal×valence）成为唯一还生效的排序维度，直接验证修复后
真正为 0.0 的桶会排在情感强度真的是 0.1 的桶后面（按 bug 触发前的
逻辑，0.0 会被误当成 0.15，反而排到 0.1 前面）。
"""
from datetime import datetime
from unittest.mock import MagicMock

import frontmatter
import pytest

import tools._runtime as rt
from tools.breath.surface import surface_default


class EchoDehydrator:
    async def dehydrate(self, content, meta=None):
        return content


class EmptyEmbedding:
    enabled = False


def install_runtime(bucket_mgr, decay_eng):
    rt.config = {"surfacing": {}}
    rt.bucket_mgr = bucket_mgr
    rt.decay_engine = decay_eng
    rt.dehydrator = EchoDehydrator()
    rt.embedding_engine = EmptyEmbedding()
    rt.logger = MagicMock()
    rt.fire_webhook = None
    rt.mark_op = None


def _pin_identical_timestamp(bucket_mgr, bucket_id: str, ts: str) -> None:
    fpath = bucket_mgr._find_bucket_file(bucket_id)
    post = frontmatter.load(fpath)
    post["created"] = ts
    post["last_active"] = ts
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(frontmatter.dumps(post))


@pytest.mark.asyncio
async def test_zero_emotion_bucket_sorts_below_a_genuinely_low_but_nonzero_one(
    bucket_mgr, decay_eng, monkeypatch,
):
    # decay_score 拉平：唯一还起作用的排序维度就是 av（arousal × valence）
    monkeypatch.setattr(decay_eng, "calculate_score", lambda meta: 1.0)
    install_runtime(bucket_mgr, decay_eng)

    zero_id = await bucket_mgr.create_internal(
        content="效价唤醒度都恰好为零的记忆", importance=5,
        valence=0.0, arousal=0.0,
    )
    low_id = await bucket_mgr.create_internal(
        content="效价唤醒度真实偏低但不为零的记忆", importance=5,
        valence=0.4, arousal=0.25,  # av = 0.1
    )

    same_ts = datetime(2026, 1, 1).isoformat()
    _pin_identical_timestamp(bucket_mgr, zero_id, same_ts)
    _pin_identical_timestamp(bucket_mgr, low_id, same_ts)

    result = await surface_default(max_results=10, max_tokens=10000, tag_filter=[])

    zero_pos = result.find("效价唤醒度都恰好为零的记忆")
    low_pos = result.find("效价唤醒度真实偏低但不为零的记忆")
    assert zero_pos != -1 and low_pos != -1, f"两条记忆都应该浮现，实际输出:\n{result}"
    assert low_pos < zero_pos, (
        "av=0.1 的记忆应该排在 av=0.0 的记忆前面；"
        "如果 0.0 被 `or` 误当成缺失值换成默认 0.3×0.5=0.15，顺序会反过来"
    )
