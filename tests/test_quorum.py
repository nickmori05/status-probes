"""Quorum votes. No AWS and no clock: ``now`` is passed in."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from probe.quorum import DEFAULT_FRESHNESS, PROBE_REGIONS, Observation, aggregate

NOW = datetime(2026, 11, 16, 13, 20, tzinfo=UTC)


def _obs(
    region: str,
    *,
    ok: bool = True,
    minutes_ago: int = 1,
    delta: timedelta = timedelta(0),
) -> Observation:
    return Observation(region, NOW - timedelta(minutes=minutes_ago) + delta, ok)


def test_two_up_holds_when_one_region_is_down() -> None:
    result = aggregate(
        [
            _obs("eu-west-1", ok=False),
            _obs("us-east-1", ok=True),
            _obs("us-west-2", ok=True),
        ],
        now=NOW,
    )

    assert result.status == "up"
    assert result.up == ("us-west-2", "us-east-1")
    assert result.down == ("eu-west-1",)
    assert result.missing == ()


def test_two_down_is_down() -> None:
    result = aggregate(
        [_obs("us-west-2", ok=False), _obs("us-east-1", ok=False), _obs("eu-west-1", ok=True)],
        now=NOW,
    )

    assert result.status == "down"
    assert result.up == ("eu-west-1",)
    assert result.down == ("us-west-2", "us-east-1")
    assert result.missing == ()


def test_one_region_alone_is_unknown() -> None:
    up = aggregate([_obs("us-west-2", ok=True)], now=NOW)
    down = aggregate([_obs("eu-west-1", ok=False)], now=NOW)

    assert up.status == "unknown"
    assert up.up == ("us-west-2",)
    assert up.missing == ("us-east-1", "eu-west-1")
    assert down.status == "unknown"
    assert down.down == ("eu-west-1",)
    assert down.missing == ("us-west-2", "us-east-1")


def test_split_with_one_region_missing_is_unknown() -> None:
    result = aggregate([_obs("us-west-2", ok=True), _obs("us-east-1", ok=False)], now=NOW)

    assert result.status == "unknown"
    assert result.up == ("us-west-2",)
    assert result.down == ("us-east-1",)
    assert result.missing == ("eu-west-1",)


def test_no_observations_leaves_every_region_missing() -> None:
    result = aggregate([], now=NOW)

    assert result.status == "unknown"
    assert result.up == ()
    assert result.down == ()
    assert result.missing == PROBE_REGIONS


def test_stale_region_does_not_vote() -> None:
    result = aggregate(
        [
            _obs("us-west-2", ok=True),
            _obs("us-east-1", ok=True),
            _obs("eu-west-1", ok=False, minutes_ago=3, delta=timedelta(microseconds=-1)),
        ],
        now=NOW,
    )

    assert result.status == "up"
    assert result.up == ("us-west-2", "us-east-1")
    assert result.down == ()
    assert result.missing == ("eu-west-1",)


def test_observation_exactly_at_the_freshness_boundary_votes() -> None:
    boundary = NOW - DEFAULT_FRESHNESS
    result = aggregate(
        [
            Observation("us-west-2", boundary, True),
            Observation("us-east-1", boundary, True),
            Observation("eu-west-1", NOW, False),
        ],
        now=NOW,
    )

    assert result.status == "up"
    assert result.up == ("us-west-2", "us-east-1")
    assert result.down == ("eu-west-1",)


def test_latest_observation_in_the_window_wins() -> None:
    result = aggregate(
        [
            _obs("us-west-2", ok=False, minutes_ago=2),
            _obs("us-west-2", ok=True, minutes_ago=1),
            _obs("us-east-1", ok=True),
            _obs("eu-west-1", ok=False),
        ],
        now=NOW,
    )

    assert result.status == "up"
    assert result.up == ("us-west-2", "us-east-1")
    assert result.down == ("eu-west-1",)


def test_equal_timestamps_keep_the_later_observation() -> None:
    when = NOW - timedelta(minutes=1)
    result = aggregate(
        [
            Observation("us-west-2", when, False),
            Observation("us-east-1", when, True),
            Observation("eu-west-1", when, True),
            Observation("us-west-2", when, True),
        ],
        now=NOW,
    )

    assert result.status == "up"
    assert result.up == PROBE_REGIONS
    assert result.down == ()


def test_future_observation_does_not_vote() -> None:
    result = aggregate(
        [
            Observation("us-west-2", NOW + timedelta(seconds=1), False),
            _obs("us-east-1", ok=True),
            _obs("eu-west-1", ok=True),
        ],
        now=NOW,
    )

    assert result.status == "up"
    assert result.missing == ("us-west-2",)
    assert result.down == ()


def test_regions_outside_the_electorate_do_not_vote() -> None:
    result = aggregate(
        [
            _obs("us-west-2", ok=True),
            _obs("us-east-1", ok=True),
            _obs("ap-southeast-2", ok=False),
        ],
        now=NOW,
    )

    assert result.status == "up"
    assert result.missing == ("eu-west-1",)
    assert result.down == ()


def test_shorter_freshness_drops_an_older_sample() -> None:
    result = aggregate(
        [_obs("us-west-2"), _obs("us-east-1"), _obs("eu-west-1", minutes_ago=2)],
        now=NOW,
        freshness=timedelta(minutes=1),
    )

    assert result.status == "up"
    assert result.missing == ("eu-west-1",)


def test_naive_datetimes_are_read_as_utc() -> None:
    naive_now = NOW.replace(tzinfo=None)
    result = aggregate(
        [
            Observation("us-west-2", naive_now - timedelta(minutes=1), True),
            Observation("us-east-1", naive_now - timedelta(minutes=1), True),
            Observation("eu-west-1", naive_now - timedelta(minutes=1), False),
        ],
        now=naive_now,
    )

    assert result.status == "up"
    assert result.down == ("eu-west-1",)


def test_aware_datetime_from_another_zone_is_compared_in_utc() -> None:
    eastern = timezone(timedelta(hours=-5))
    local_now = NOW.astimezone(eastern)
    result = aggregate(
        [
            Observation("us-west-2", local_now - timedelta(minutes=1), True),
            Observation("us-east-1", local_now - timedelta(minutes=1), True),
        ],
        now=local_now,
    )

    assert result.status == "up"
    assert result.missing == ("eu-west-1",)


def test_freshness_must_be_positive() -> None:
    with pytest.raises(ValueError):
        aggregate([], now=NOW, freshness=timedelta(0))
