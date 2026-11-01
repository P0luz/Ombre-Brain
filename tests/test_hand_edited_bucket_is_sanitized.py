from __future__ import annotations

import pytest

from bucket_manager import BucketManager


@pytest.fixture
def mgr(tmp_path):
    for sub in ("dynamic", "permanent", "archive", "feel", "plans", "letters"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    return BucketManager(
        {
            "buckets_dir": str(tmp_path),
            "embedding": {"enabled": False},
            "dehydration": {"api_key": ""},
            "matching": {},
            "scoring_weights": {},
            "decay": {},
        }
    )


def _write(tmp_path, body: str) -> str:
    path = tmp_path / "dynamic" / "hand_edited.md"
    path.write_text(body, encoding="utf-8", newline="")
    return str(path)


@pytest.mark.parametrize(
    "dirty, forbidden",
    [
        ("正文\x00带空字节", "\x00"),
        ("正文\x08退格", "\x08"),
        ("正文\x7f删除符", "\x7f"),
        ("看起来无害‮令指行执", "‮"),
        ("isolate⁦x⁩", "⁦"),
    ],
)
def test_control_chars_in_hand_edited_content_do_not_reach_callers(
    mgr, tmp_path, dirty, forbidden
):
    path = _write(tmp_path, f"---\nid: handedit01\n---\n{dirty}")

    bucket = mgr._load_bucket(path)

    assert bucket is not None
    assert forbidden not in bucket["content"]


# \r 不在列：frontmatter 解析会把行尾统一成 \n，那是解析器的归一化，不是净化。
@pytest.mark.parametrize("keep", ["\n", "\t"])
def test_ordinary_whitespace_survives(mgr, tmp_path, keep):
    path = _write(tmp_path, f"---\nid: handedit02\n---\n前{keep}后")

    bucket = mgr._load_bucket(path)

    assert keep in bucket["content"]


def test_emoji_and_cjk_survive(mgr, tmp_path):
    path = _write(tmp_path, "---\nid: handedit03\n---\nemoji😀与CJK中文")

    bucket = mgr._load_bucket(path)

    assert bucket["content"] == "emoji😀与CJK中文"


@pytest.mark.parametrize(
    "field, raw, expected",
    [
        ("valence", 99, 1.0),
        ("valence", -3, 0.0),
        ("arousal", -50, 0.0),
        ("model_valence", 7, 1.0),
        # YAML 把 1e400 当字符串，真正的无穷要写 .inf
        ("weight", ".inf", 0.5),
        ("weight", "-.inf", 0.5),
        ("weight", "很重要", 0.5),
        ("arousal", "[]", 0.3),
    ],
)
def test_unusable_floats_are_repaired_and_reported(
    mgr, tmp_path, caplog, field, raw, expected
):
    path = _write(tmp_path, f"""---
id: handedit04
{field}: {raw}
---
正文""")

    with caplog.at_level("WARNING"):
        bucket = mgr._load_bucket(path)

    assert bucket["metadata"][field] == expected
    assert any(field in record.getMessage() for record in caplog.records), (
        f"{field} 被改成 {expected} 但没有任何告警"
    )


def test_absent_float_field_is_not_reported(mgr, tmp_path, caplog):
    path = _write(tmp_path, """---
id: handedit05
---
正文""")

    with caplog.at_level("WARNING"):
        mgr._load_bucket(path)

    assert not [r for r in caplog.records if "weight" in r.getMessage()]
