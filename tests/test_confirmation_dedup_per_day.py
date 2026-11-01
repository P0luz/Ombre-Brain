from __future__ import annotations

import pytest

from ombrebrain.you.models import POLICY_VERSION, Scope, YouClaim
from ombrebrain.them.models import ThemClaim
from ombrebrain.them.service import THEM_POLICY_VERSION

DAY1 = "2026-09-01T08:00:00+00:00"
DAY1_LATER = "2026-09-01T23:59:59+00:00"
DAY2 = "2026-09-02T00:00:01+00:00"
DAY3 = "2026-09-03T12:00:00+00:00"


def _scope():
    return Scope(
        owner_instance_id="owner_" + "a" * 32,
        observer_role_id="role_" + "b" * 32,
        subject_user_id="user_" + "c" * 32,
    )


_COMMON = dict(
    concept_key="preferred_address",
    concept_value="Lin",
    content="他喜欢被叫 Lin。",
    aspect="preferred_address",
    evidence=(),
)


def _claim(cls=YouClaim):
    if cls is ThemClaim:
        return ThemClaim.new_for(
            scope=_scope(), person_id="person_" + "d" * 32, **_COMMON
        )
    return YouClaim.new(scope=_scope(), recall_policy="contextual", **_COMMON)


@pytest.fixture(params=[(YouClaim, POLICY_VERSION), (ThemClaim, THEM_POLICY_VERSION)],
                ids=["you", "them"])
def claim_and_version(request):
    cls, version = request.param
    return _claim(cls), version


def test_a_second_confirmation_on_the_same_day_is_not_recorded(claim_and_version):
    claim, version = claim_and_version

    once = claim.with_confirmation(version, DAY1)
    twice = once.with_confirmation(version, DAY1_LATER)

    assert len(once.review_receipts) == 1
    assert len(twice.review_receipts) == 1
    assert twice is once


def test_three_distinct_days_produce_three_receipts(claim_and_version):
    claim, version = claim_and_version

    claim = claim.with_confirmation(version, DAY1)
    claim = claim.with_confirmation(version, DAY2)
    claim = claim.with_confirmation(version, DAY3)

    assert len(claim.review_receipts) == 3
    assert claim.review_date_count == 3


def test_hammering_one_day_never_reaches_the_threshold(claim_and_version):
    claim, version = claim_and_version

    for _ in range(20):
        claim = claim.with_confirmation(version, DAY1)

    assert claim.review_date_count == 1


def test_the_receipt_timestamp_is_the_one_that_was_passed_in(claim_and_version):
    claim, version = claim_and_version

    confirmed = claim.with_confirmation(version, DAY2)

    assert confirmed.review_receipts[0].reviewed_at == DAY2
    assert confirmed.review_receipts[0].review_date == DAY2[:10]


def test_the_policy_version_is_the_one_that_was_passed_in(claim_and_version):
    claim, version = claim_and_version

    confirmed = claim.with_confirmation(version, DAY1)

    assert confirmed.review_receipts[0].policy_version == version


def test_you_and_them_use_different_policy_versions():
    assert POLICY_VERSION != THEM_POLICY_VERSION


def test_the_original_claim_is_not_mutated(claim_and_version):
    claim, version = claim_and_version

    claim.with_confirmation(version, DAY1)

    assert claim.review_receipts == ()


def test_them_inherits_the_rule_from_you():
    assert ThemClaim.with_confirmation is YouClaim.with_confirmation
