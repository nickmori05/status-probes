"""Query recent probe-results for one endpoint and aggregate them.

The query is one partition: ``pk`` is the endpoint, and ``sk`` is bounded to
the freshness window. ``probe.quorum`` decides the status and does not know
about DynamoDB.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from boto3.dynamodb.conditions import Key

from probe.quorum import DEFAULT_FRESHNESS, Observation, QuorumResult, aggregate
from probe.writer import (
    DEFAULT_TABLE_REGION,
    TABLE_NAME_ENV,
    TABLE_REGION_ENV,
    dynamodb_table,
    format_observed_at,
)


def sort_key_bounds(start: datetime, end: datetime) -> tuple[str, str]:
    """Inclusive ``sk`` bounds for a Query covering ``[start, end]``.

    The upper bound is one microsecond past ``end`` and has no region. A
    stored key is ``<time>#<region>``, which sorts before that bound when
    the time is ``end`` and after it when the time is the next microsecond.
    """
    lower_time = format_observed_at(start)
    end_time = format_observed_at(end)
    if end_time < lower_time:
        raise ValueError("end is before start")
    upper_time = format_observed_at(end + timedelta(microseconds=1))
    return f"{lower_time}#", f"{upper_time}#"


def observation_from_item(item: dict[str, object]) -> Observation:
    """One stored item. ``sk`` supplies the time and the region."""
    sk = item.get("sk")
    ok = item.get("ok")
    if not isinstance(sk, str) or not isinstance(ok, bool):
        raise ValueError("probe item needs a string sk and a bool ok")
    observed_at, region = _parse_sort_key(sk)
    return Observation(region=region, observed_at=observed_at, ok=ok)


def query_observations(
    table: object,
    endpoint: str,
    *,
    now: datetime,
    freshness: timedelta = DEFAULT_FRESHNESS,
) -> list[Observation]:
    """Return this endpoint's observations inside the freshness window.

    Pages are followed until DynamoDB stops returning ``LastEvaluatedKey``.
    The range is ``[now - freshness, now]``, matching ``aggregate``.
    """
    if freshness <= timedelta(0):
        raise ValueError("freshness must be positive")
    lower, upper = sort_key_bounds(now - freshness, now)
    condition = Key("pk").eq(endpoint) & Key("sk").between(lower, upper)
    items: list[dict[str, object]] = []
    start_key: dict[str, object] | None = None
    while True:
        kwargs: dict[str, object] = {"KeyConditionExpression": condition}
        if start_key is not None:
            kwargs["ExclusiveStartKey"] = start_key
        response = table.query(**kwargs)
        items.extend(response.get("Items", ()))
        start_key = response.get("LastEvaluatedKey")
        if not start_key:
            break
    return [observation_from_item(item) for item in items]


def read_quorum(
    table: object,
    endpoint: str,
    *,
    now: datetime,
    freshness: timedelta = DEFAULT_FRESHNESS,
) -> QuorumResult:
    """Query the window and return the quorum for ``endpoint``."""
    observations = query_observations(table, endpoint, now=now, freshness=freshness)
    return aggregate(observations, now=now, freshness=freshness)


def handler(event: dict, context: object) -> dict:
    """Return the quorum for ``event["endpoint"]``.

    Reads ``probe-results`` using ``TABLE_NAME`` and ``TABLE_REGION``.
    The function does not write. It runs only when something invokes it.
    """
    del context
    endpoint = _endpoint(event)
    table_name = os.environ.get(TABLE_NAME_ENV, "").strip()
    if not table_name:
        raise RuntimeError(f"{TABLE_NAME_ENV} is required")
    table_region = os.environ.get(TABLE_REGION_ENV, "").strip() or DEFAULT_TABLE_REGION
    table = dynamodb_table(table_name, table_region)
    result = read_quorum(table, endpoint, now=datetime.now(UTC))
    return _payload(endpoint, result)


def _endpoint(event: dict) -> str:
    raw = event.get("endpoint")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("endpoint is required")
    return raw.strip()


def _payload(endpoint: str, result: QuorumResult) -> dict[str, object]:
    return {
        "endpoint": endpoint,
        "status": result.status,
        "up": list(result.up),
        "down": list(result.down),
        "missing": list(result.missing),
    }


def _parse_sort_key(sk: str) -> tuple[datetime, str]:
    timestamp, separator, region = sk.partition("#")
    if separator != "#" or not region or not timestamp:
        raise ValueError(f"invalid observation sort key: {sk!r}")
    try:
        observed_at = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError(f"invalid observation sort key: {sk!r}") from exc
    return observed_at, region
