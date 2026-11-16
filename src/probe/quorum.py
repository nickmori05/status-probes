"""Decide up, down, or unknown from the three regional probes.

An endpoint is up or down only when a majority of us-west-2, us-east-1,
and eu-west-1 have a fresh observation and agree. One region cannot flip
the status. A region with no observation inside the freshness window does
not vote, so stale or missing data stays unknown unless the other two agree.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

# The probe regions, in the order votes are reported. app.py deploys the
# same three. This module does not import the CDK app: synth runs app.py
# from the repo root, and the probe package lives under src/.
PROBE_REGIONS: tuple[str, ...] = ("us-west-2", "us-east-1", "eu-west-1")

# Probes run once a minute. Three minutes keeps a region that missed one
# or two runs, and drops a region that has been silent longer than that.
DEFAULT_FRESHNESS = timedelta(minutes=3)

Status = Literal["up", "down", "unknown"]


@dataclass(frozen=True)
class Observation:
    """One region's sample. ``ok`` is that region's vote."""

    region: str
    observed_at: datetime
    ok: bool


@dataclass(frozen=True)
class QuorumResult:
    """The majority vote, and the regions on each side.

    ``up`` and ``down`` are the fresh votes, in ``PROBE_REGIONS`` order.
    ``missing`` did not vote: no observation inside the freshness window.
    ``status`` is ``unknown`` unless one side has a majority of the regions.
    """

    status: Status
    up: tuple[str, ...]
    down: tuple[str, ...]
    missing: tuple[str, ...]


def aggregate(
    observations: Sequence[Observation],
    *,
    now: datetime,
    freshness: timedelta = DEFAULT_FRESHNESS,
) -> QuorumResult:
    """Take each region's latest fresh observation and apply the majority.

    An observation is fresh when its time is inside ``[now - freshness, now]``.
    A later sample replaces an earlier one for the same region. When two
    samples share a timestamp, the later one in ``observations`` wins.
    Regions outside ``PROBE_REGIONS`` are ignored. Naive datetimes are UTC.
    """
    if freshness <= timedelta(0):
        raise ValueError("freshness must be positive")

    now_utc = _as_utc(now)
    earliest = now_utc - freshness
    allowed = set(PROBE_REGIONS)
    latest: dict[str, Observation] = {}
    for observation in observations:
        if observation.region not in allowed:
            continue
        observed_at = _as_utc(observation.observed_at)
        if observed_at < earliest or observed_at > now_utc:
            continue
        current = latest.get(observation.region)
        if current is None or observed_at >= current.observed_at:
            latest[observation.region] = Observation(
                observation.region, observed_at, observation.ok
            )

    up = tuple(region for region in PROBE_REGIONS if region in latest and latest[region].ok)
    down = tuple(region for region in PROBE_REGIONS if region in latest and not latest[region].ok)
    missing = tuple(region for region in PROBE_REGIONS if region not in latest)
    # More than half of the three regions: two must agree, and one cannot.
    needed = len(PROBE_REGIONS) // 2 + 1
    if len(up) >= needed:
        status: Status = "up"
    elif len(down) >= needed:
        status = "down"
    else:
        status = "unknown"
    return QuorumResult(status=status, up=up, down=down, missing=missing)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
