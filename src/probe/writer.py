"""Turn probe observations into items in the ``probe-results`` table.

``pk`` is the endpoint URL. ``sk`` is the ISO-8601 UTC observation time, a
``#``, and the probe region. ``ttl`` is the epoch second the item expires.
``TABLE_REGION`` is the replica this client writes. A deployed probe sets
it to the region the function runs in. When the variable is unset, the
client uses us-east-1.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import boto3
from botocore.config import Config

from probe.checker import ProbeResult

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 7
DEFAULT_TABLE_REGION = "us-east-1"
TABLE_NAME_ENV = "TABLE_NAME"
TABLE_REGION_ENV = "TABLE_REGION"
RETENTION_DAYS_ENV = "RESULT_RETENTION_DAYS"

# One resource per table region. Lambda reuses the execution environment,
# and a client is safe to share once it has been created.
_resources: dict[str, object] = {}

_CLIENT_CONFIG = Config(
    connect_timeout=2,
    read_timeout=5,
    retries={"total_max_attempts": 3, "mode": "standard"},
)


def format_observed_at(observed_at: datetime) -> str:
    """ISO-8601 UTC, with a ``Z`` suffix so every sort key uses one form."""
    return _as_utc(observed_at).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def observation_sort_key(observed_at: datetime, region: str) -> str:
    """``<ISO-8601 UTC observation time>#<region>``."""
    return f"{format_observed_at(observed_at)}#{region}"


def ttl_epoch(observed_at: datetime, retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
    """Epoch seconds at which DynamoDB may expire the item."""
    expires = _as_utc(observed_at) + timedelta(days=retention_days)
    return int(expires.timestamp())


def to_item(
    result: ProbeResult,
    *,
    region: str,
    observed_at: datetime,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> dict[str, object]:
    """One observation as a DynamoDB item. Numbers stay exact (``Decimal``)."""
    return {
        "pk": result.url,
        "sk": observation_sort_key(observed_at, region),
        "status_code": result.status_code,
        "latency_ms": Decimal(str(result.latency_ms)),
        "ok": result.ok,
        "error": result.error,
        "region": region,
        "ttl": ttl_epoch(observed_at, retention_days),
    }


def write_observations(
    results: Sequence[ProbeResult],
    table: object,
    *,
    region: str,
    observed_at: datetime,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> None:
    """Put each observation. One item uses PutItem; more use the batch writer."""
    items = [
        to_item(
            result,
            region=region,
            observed_at=observed_at,
            retention_days=retention_days,
        )
        for result in results
    ]
    if not items:
        return
    if len(items) == 1:
        table.put_item(Item=items[0])
        return
    with table.batch_writer() as batch:
        for item in items:
            batch.put_item(Item=item)


def store_results(results: Sequence[ProbeResult], *, region: str) -> None:
    """Write ``results`` when ``TABLE_NAME`` is set.

    A failed write is logged with the observations and re-raised. The log
    line is what keeps the probe results visible; the exception is what
    keeps a broken table from looking like a healthy run. Callers that
    catch nothing still surface the failure, and the observations are in
    the same log record. When ``TABLE_NAME`` is unset, this returns without
    calling DynamoDB so a local run can still report what it probed.
    """
    table_name = os.environ.get(TABLE_NAME_ENV, "").strip()
    if not table_name or not results:
        return
    table_region = os.environ.get(TABLE_REGION_ENV, "").strip() or DEFAULT_TABLE_REGION
    observed_at = datetime.now(UTC)
    try:
        table = dynamodb_table(table_name, table_region)
        write_observations(
            results,
            table,
            region=region,
            observed_at=observed_at,
            retention_days=_retention_days(),
        )
    except Exception:
        logger.exception(
            "failed to write %d probe observations to %s: %s",
            len(results),
            table_name,
            [asdict(result) for result in results],
        )
        raise


def dynamodb_table(name: str, region: str) -> object:
    """Return the shared DynamoDB Table resource for ``region``."""
    resource = _resources.get(region)
    if resource is None:
        resource = boto3.resource("dynamodb", region_name=region, config=_CLIENT_CONFIG)
        _resources[region] = resource
    return resource.Table(name)


def clear_cached_resources() -> None:
    """Drop cached resources. Tests use this so a mock is not bypassed."""
    _resources.clear()


def _retention_days() -> int:
    raw = os.environ.get(RETENTION_DAYS_ENV, "").strip()
    if not raw:
        return DEFAULT_RETENTION_DAYS
    days = int(raw)
    if days < 1:
        raise ValueError(f"{RETENTION_DAYS_ENV} must be a positive number of days")
    return days


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
