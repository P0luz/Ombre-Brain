from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ombrebrain.storage.letter_lock import letter_lock_state

PAST = "2020-01-01T00:00:00"
FUTURE = "2099-01-01T00:00:00"


def _state(unlock_date, caller="ai", locked_by="user"):
    return letter_lock_state(
        {
            "id": "x",
            "metadata": {
                "lock_type": "timed",
                "unlock_date": unlock_date,
                "locked_by": locked_by,
            },
        },
        caller,
    )


@pytest.mark.parametrize(
    "unlock_date", [PAST, PAST + "Z", PAST + "+00:00", PAST + "+08:00", PAST + "-05:00"]
)
def test_a_past_unlock_date_expires_with_or_without_a_timezone(unlock_date):
    state = _state(unlock_date)
    assert state["expired"] is True
    assert state["locked"] is False


@pytest.mark.parametrize(
    "unlock_date",
    [FUTURE, FUTURE + "Z", FUTURE + "+00:00", FUTURE + "+08:00", FUTURE + "-05:00"],
)
def test_a_future_unlock_date_stays_locked_with_or_without_a_timezone(unlock_date):
    state = _state(unlock_date)
    assert state["expired"] is False
    assert state["locked"] is True


def test_the_owner_reads_their_own_locked_letter():
    assert _state(FUTURE, caller="user", locked_by="user")["locked"] is False


def test_a_permanent_lock_is_unaffected():
    state = letter_lock_state(
        {"id": "x", "metadata": {"lock_type": "permanent", "locked_by": "user"}}, "ai"
    )
    assert state["locked"] is True
    assert state["expired"] is False


def test_a_malformed_unlock_date_keeps_the_letter_locked():
    state = _state("不是时间")
    assert state["expired"] is False
    assert state["locked"] is True


def test_an_explicit_now_is_honoured():
    boundary = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    before = letter_lock_state(
        {"id": "x", "metadata": {"lock_type": "timed",
                                 "unlock_date": "2026-06-01T12:00:00+00:00",
                                 "locked_by": "user"}},
        "ai",
        now=boundary - timedelta(seconds=1),
    )
    after = letter_lock_state(
        {"id": "x", "metadata": {"lock_type": "timed",
                                 "unlock_date": "2026-06-01T12:00:00+00:00",
                                 "locked_by": "user"}},
        "ai",
        now=boundary,
    )
    assert before["locked"] is True
    assert after["locked"] is False


@pytest.mark.asyncio
async def test_letter_read_returns_a_letter_whose_naive_lock_has_passed(bucket_mgr):
    import frontmatter
    from pathlib import Path

    from tests.test_letter_read_regression import install_letter_runtime
    from tools.plan.core import letter_read

    history = Path(bucket_mgr.letter_dir) / "history"
    history.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(
        "一封早就该解锁的信",
        id="oldlock01",
        name="2026-04-12 08-30-00 T",
        tags=["__letter__"],
        domain=["letter"],
        type="letter",
        importance=10,
        valence=0.5,
        arousal=0.3,
        created="2026-04-12T08:30:00+00:00",
        last_active="2026-04-12T08:30:00+00:00",
        activation_count=0,
        source_tool="letter",
        author="user",
        user_name="U",
        title="T",
        letter_date="2026-04-12",
        lock_type="timed",
        unlock_date=PAST,
        locked_by="user",
    )
    (history / "old.md").write_text(frontmatter.dumps(post), encoding="utf-8")
    install_letter_runtime(bucket_mgr)

    assert "oldlock01" in await letter_read()
