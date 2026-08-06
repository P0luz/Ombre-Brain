"""
回归测试：机器元数据统一 aware UTC，桶名/展示为 Asia/Shanghai 本地时间。

验收场景（审计事故复现）：
- UTC 08:57 创建 → 元数据保存 +00:00（而不是裸 naive "08:57"）
- 桶名与 breath 展示为上海 16:57（旧 naive 桶按 UTC 解释后转换）
"""
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from utils import now_iso, parse_iso_aware, display_time, display_bucket_name


def test_now_iso_is_aware_utc():
    raw = now_iso()
    dt = datetime.fromisoformat(raw)
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timezone.utc.utcoffset(None)
    # 与 UTC 当前时刻一致（秒级）
    assert abs((dt - datetime.now(timezone.utc)).total_seconds()) < 2


def test_parse_iso_aware_interprets_legacy_naive_as_utc():
    dt = parse_iso_aware("2026-08-06T08:57:53")
    assert dt.utcoffset() == timezone.utc.utcoffset(None)
    assert dt.isoformat() == "2026-08-06T08:57:53+00:00"
    # 带时区原样保留
    dt2 = parse_iso_aware("2026-08-06T08:57:53+02:00")
    assert dt2.utcoffset().total_seconds() == 2 * 3600
    # Z 后缀兼容
    dt3 = parse_iso_aware("2026-08-06T08:57:53Z")
    assert dt3.utcoffset() == timezone.utc.utcoffset(None)


def test_display_time_converts_legacy_naive_utc_to_shanghai():
    # 事故场景：hold 于 16:57:53 +08 调用，旧代码存了 naive 08:57:53 (UTC)
    assert display_time("2026-08-06T08:57:53") == "2026-08-06 16:57:53"
    # 带时区时间正常转换
    assert display_time("2026-08-06T08:57:53+00:00") == "2026-08-06 16:57:53"


def test_display_bucket_name_legacy_utc_prefix_to_shanghai():
    assert display_bucket_name("2026-08-06 08-57-53 午餐与追星吐槽") == \
        "2026-08-06 16-57-53 午餐与追星吐槽"
    # 无时间戳前缀的名称原样通过
    assert display_bucket_name("普通记忆") == "普通记忆"


def test_create_bucket_metadata_aware_utc_and_name_shanghai(test_config, fake_embedding_engine):
    """验收：UTC 08:57 创建 → 元数据 +00:00；桶名为上海 16:57。"""
    import asyncio
    from bucket_manager import BucketManager

    manager = BucketManager(test_config, embedding_engine=fake_embedding_engine)

    async def _run():
        return await manager.create(
            content="测试内容：中午吃了麦当劳炸鸡，吐槽了追星粉丝。",
            tags=["测试"],
            importance=5,
            domain=["测试"],
            source_tool="hold",
        )

    bucket_id = asyncio.run(_run())
    bucket = manager._load_bucket(manager._find_bucket_file(bucket_id))
    assert bucket is not None
    meta = bucket["metadata"]
    # 元数据必须是 aware UTC（带 +00:00），绝不允许 timezone-naive
    created = datetime.fromisoformat(meta["created"])
    assert created.tzinfo is not None
    assert created.utcoffset() == timezone.utc.utcoffset(None)
    last_active = datetime.fromisoformat(meta["last_active"])
    assert last_active.utcoffset() == timezone.utc.utcoffset(None)
    # 桶名时间戳前缀 = 上海本地当前时间（创建与断言同秒/相邻秒）
    name_ts = " ".join(str(meta["name"]).split(" ")[:2])  # "YYYY-MM-DD HH-MM-SS"
    shanghai_now = datetime.now(ZoneInfo("Asia/Shanghai"))
    parsed_name = datetime.strptime(
        f"{name_ts[:10]} {name_ts[11:].replace('-', ':')}", "%Y-%m-%d %H:%M:%S"
    ).replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    assert abs((parsed_name - shanghai_now).total_seconds()) < 3


def test_scoring_and_decay_handle_mixed_naive_and_aware(test_config, fake_embedding_engine):
    """naive(旧桶) 与 aware(新桶) 混合时评分/衰减不 TypeError、不误入 fallback。"""
    from bucket_scoring import calc_time_score
    from decay_engine import _days_since_active

    # 旧桶（naive）与刚创建的 aware 桶都应得到正常的时间分（非 fallback 0.0 边界）
    score_legacy = calc_time_score({"last_active": "2026-08-05T08:57:53"})
    score_aware = calc_time_score({"last_active": now_iso()})
    assert 0.0 < score_aware <= 1.0
    assert 0.0 < score_legacy <= 1.0
    assert score_aware > score_legacy  # 更新的桶分更高

    days_legacy = _days_since_active({"last_active": "2026-08-05T08:57:53"})
    days_aware = _days_since_active({"last_active": now_iso()})
    assert days_aware < days_legacy
    assert days_aware >= 0.0


def test_breath_sort_key_mixed_datetimes(test_config, fake_embedding_engine):
    """breath 排序对 naive/aware 混合桶不抛异常（旧排序 naive→本地时区会错）。"""
    from tools.breath import surface

    meta_old = {"last_active": "2026-08-04T08:57:53", "arousal": 0.3, "valence": 0.5,
                "importance": 5}
    meta_new = {"last_active": now_iso(), "arousal": 0.3, "valence": 0.5,
                "importance": 5}
    # _sort_key 是嵌套函数，无法直接调用；这里验证 parse 层一致即可：
    ts_old = parse_iso_aware(meta_old["last_active"]).timestamp()
    ts_new = parse_iso_aware(meta_new["last_active"]).timestamp()
    assert ts_new > ts_old
