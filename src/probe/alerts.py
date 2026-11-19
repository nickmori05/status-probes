"""Publish when a quorum moves between up and down.

The last up or down for an endpoint is one item in ``probe-results``.
Its sort key sorts before every observation key, so the freshness query
does not return it. The item has no ``ttl``, so the table does not expire
it. Unknown is not stored and does not clear a stored up or down.

The first up or down is stored and not published. The same status later
is not published. A change is a conditional update that succeeds only
while the stored status is still the previous one. Only that caller
publishes. A retry finds the new status, or loses the condition, and
does not publish again. If publishing raises, the previous status is
written back under the same kind of condition, and the exception
propagates so the retry can send the change.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from botocore.exceptions import ClientError

from probe.writer import format_observed_at

logger = logging.getLogger(__name__)

# ``#`` sorts before the digit an observation key starts with. The
# freshness query bounds are observation times, so this item is outside
# that range.
STATUS_SORT_KEY = "#status"
_ALERT_STATUSES = ("up", "down")
_CONDITION_FAILED = "ConditionalCheckFailedException"


@dataclass(frozen=True)
class Transition:
    """One up/down change this caller claimed and should publish."""

    endpoint: str
    previous: str
    status: str
    at: datetime


def claim_transition(
    table: object,
    endpoint: str,
    status: str,
    *,
    now: datetime,
) -> Transition | None:
    """Store ``status`` and return a change only when this caller claimed it.

    Unknown returns None without reading. A missing item is inserted and
    returns None: there is no previous up or down to compare. A matching
    stored status returns None without writing. A conditional failure
    means another invocation wrote first, so this one does not publish.
    """
    if status not in _ALERT_STATUSES:
        return None
    if not endpoint:
        raise ValueError("endpoint is required")

    current = _read_status(table, endpoint)
    if current is None:
        wrote = _conditional_write(table, endpoint, expected=None, status=status, now=now)
        if wrote:
            return None
        current = _read_status(table, endpoint)
        if current is None:
            raise RuntimeError(f"status item for {endpoint} disappeared during the write")
    if current == status:
        return None
    if current not in _ALERT_STATUSES:
        raise RuntimeError(f"stored status for {endpoint} is {current!r}")
    if not _conditional_write(table, endpoint, expected=current, status=status, now=now):
        return None
    return Transition(endpoint=endpoint, previous=current, status=status, at=_as_utc(now))


def publish_transition(client: object, topic_arn: str, transition: Transition) -> None:
    """Send one change. The subject stays short; the endpoint is in the body."""
    client.publish(
        TopicArn=topic_arn,
        Subject=f"status {transition.previous} -> {transition.status}",
        Message=_message(transition),
    )


def notify_transitions(
    table: object,
    client: object,
    topic_arn: str,
    entries: Sequence[tuple[str, str]],
    *,
    now: datetime,
) -> list[Transition]:
    """Claim each status and publish the changes, in ``entries`` order.

    ``entries`` are ``(endpoint, status)`` pairs. A failed publish restores
    the previous status and raises. Earlier entries in this call that
    already published stay published. Later entries are left for the retry.
    """
    published: list[Transition] = []
    for endpoint, status in entries:
        transition = claim_transition(table, endpoint, status, now=now)
        if transition is None:
            continue
        try:
            publish_transition(client, topic_arn, transition)
        except Exception:
            try:
                _conditional_write(
                    table,
                    endpoint,
                    expected=transition.status,
                    status=transition.previous,
                    now=transition.at,
                )
            except Exception:
                logger.exception(
                    "failed to restore %s to %s after the publish failed",
                    transition.endpoint,
                    transition.previous,
                )
            raise
        published.append(transition)
    return published


def _message(transition: Transition) -> str:
    payload = {
        "endpoint": transition.endpoint,
        "previous": transition.previous,
        "status": transition.status,
        "at": format_observed_at(transition.at),
    }
    return json.dumps(payload, indent=2) + "\n"


def _read_status(table: object, endpoint: str) -> str | None:
    # Consistent read so a retry in this region sees the write it just made.
    response = table.get_item(
        Key={"pk": endpoint, "sk": STATUS_SORT_KEY},
        ConsistentRead=True,
    )
    item = response.get("Item")
    if not item:
        return None
    status = item.get("status")
    if not isinstance(status, str):
        raise RuntimeError(f"stored status for {endpoint} is {status!r}")
    return status


def _conditional_write(
    table: object,
    endpoint: str,
    *,
    expected: str | None,
    status: str,
    now: datetime,
) -> bool:
    """Set ``status`` if the item still matches ``expected``.

    ``expected`` None means the item must not exist. Returns False when
    the condition fails. Any other error propagates.
    """
    updated_at = format_observed_at(now)
    try:
        if expected is None:
            table.put_item(
                Item={
                    "pk": endpoint,
                    "sk": STATUS_SORT_KEY,
                    "status": status,
                    "updated_at": updated_at,
                },
                ConditionExpression="attribute_not_exists(pk)",
            )
        else:
            table.update_item(
                Key={"pk": endpoint, "sk": STATUS_SORT_KEY},
                UpdateExpression="SET #s = :status, updated_at = :at",
                ConditionExpression="#s = :expected",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":status": status,
                    ":expected": expected,
                    ":at": updated_at,
                },
            )
    except ClientError as exc:
        if _error_code(exc) != _CONDITION_FAILED:
            raise
        return False
    return True


def _error_code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
