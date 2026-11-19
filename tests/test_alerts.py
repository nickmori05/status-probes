"""Status transitions. DynamoDB is moto. SNS and the lost update are fakes."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from probe.alerts import STATUS_SORT_KEY, notify_transitions
from probe.checker import ProbeResult
from probe.reader import read_quorum
from probe.writer import clear_cached_resources, write_observations

NOW = datetime(2026, 11, 19, 13, 20, tzinfo=UTC)
URL = "https://example.com/health"
OTHER = "https://other.example/health"
TOPIC = "arn:aws:sns:us-east-1:123456789012:status-changes"
SEEDED_AT = "2026-11-19T13:00:00.000000Z"


class _Topic:
    """Records publish calls. ``fail`` raises before a message would be sent."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.fail = False

    def publish(self, **kwargs: object) -> dict[str, str]:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("sns unavailable")
        return {"MessageId": "1"}


class _LostUpdate:
    """The item is up, and the conditional update loses the race."""

    def get_item(self, **kwargs: object) -> dict[str, object]:
        del kwargs
        return {"Item": {"pk": URL, "sk": STATUS_SORT_KEY, "status": "up"}}

    def update_item(self, **kwargs: object) -> None:
        del kwargs
        raise ClientError(
            {
                "Error": {
                    "Code": "ConditionalCheckFailedException",
                    "Message": "The conditional request failed",
                }
            },
            "UpdateItem",
        )


@pytest.fixture
def aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    clear_cached_resources()


def _create_results_table() -> object:
    resource = boto3.resource("dynamodb", region_name="us-east-1")
    resource.create_table(
        TableName="probe-results",
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return resource.Table("probe-results")


def _seed(table: object, endpoint: str, status: str) -> None:
    table.put_item(
        Item={
            "pk": endpoint,
            "sk": STATUS_SORT_KEY,
            "status": status,
            "updated_at": SEEDED_AT,
        }
    )


def _stored(table: object, endpoint: str) -> dict[str, object]:
    response = table.get_item(
        Key={"pk": endpoint, "sk": STATUS_SORT_KEY},
        ConsistentRead=True,
    )
    return response["Item"]


def _store_observation(table: object, url: str, region: str, when: datetime, *, ok: bool) -> None:
    status_code = 200 if ok else 503
    error = None if ok else "HTTP 503"
    write_observations(
        [ProbeResult(url, status_code, 10.0, ok, error)],
        table,
        region=region,
        observed_at=when,
    )


def test_status_sort_key_sorts_before_an_observation_key() -> None:
    assert STATUS_SORT_KEY < "2026-11-19T13:20:00.000000Z#us-east-1"


def test_first_observation_is_recorded_and_not_published(aws_credentials: None) -> None:
    topic = _Topic()
    with mock_aws():
        table = _create_results_table()
        published = notify_transitions(table, topic, TOPIC, [(URL, "up")], now=NOW)
        item = _stored(table, URL)
        again = notify_transitions(table, topic, TOPIC, [(URL, "up")], now=NOW)

    assert published == []
    assert again == []
    assert topic.calls == []
    assert item["status"] == "up"
    assert item["updated_at"] == "2026-11-19T13:20:00.000000Z"


def test_same_status_does_not_write_or_publish(aws_credentials: None) -> None:
    topic = _Topic()
    with mock_aws():
        table = _create_results_table()
        _seed(table, URL, "up")
        published = notify_transitions(table, topic, TOPIC, [(URL, "up")], now=NOW)
        item = _stored(table, URL)

    assert published == []
    assert topic.calls == []
    assert item["status"] == "up"
    assert item["updated_at"] == SEEDED_AT


def test_unknown_does_not_publish_or_replace_the_stored_status(aws_credentials: None) -> None:
    topic = _Topic()
    with mock_aws():
        table = _create_results_table()
        _seed(table, URL, "up")
        skipped = notify_transitions(table, topic, TOPIC, [(URL, "unknown")], now=NOW)
        item = _stored(table, URL)
        published = notify_transitions(table, topic, TOPIC, [(URL, "down")], now=NOW)

    assert skipped == []
    assert item["status"] == "up"
    assert item["updated_at"] == SEEDED_AT
    assert [(change.previous, change.status) for change in published] == [("up", "down")]
    assert len(topic.calls) == 1


@pytest.mark.parametrize(("previous", "status"), [("up", "down"), ("down", "up")])
def test_a_change_between_up_and_down_is_published_once(
    aws_credentials: None, previous: str, status: str
) -> None:
    topic = _Topic()
    with mock_aws():
        table = _create_results_table()
        _seed(table, URL, previous)
        _seed(table, OTHER, previous)
        published = notify_transitions(
            table,
            topic,
            TOPIC,
            [(URL, status), (OTHER, previous)],
            now=NOW,
        )
        retry = notify_transitions(table, topic, TOPIC, [(URL, status)], now=NOW)
        item = _stored(table, URL)

    assert [(change.endpoint, change.previous, change.status) for change in published] == [
        (URL, previous, status)
    ]
    assert retry == []
    assert len(topic.calls) == 1
    message = topic.calls[0]
    assert message["TopicArn"] == TOPIC
    assert message["Subject"] == f"status {previous} -> {status}"
    assert json.loads(message["Message"]) == {
        "endpoint": URL,
        "previous": previous,
        "status": status,
        "at": "2026-11-19T13:20:00.000000Z",
    }
    assert item["status"] == status


def test_a_failed_publish_restores_the_previous_status(aws_credentials: None) -> None:
    topic = _Topic()
    topic.fail = True
    with mock_aws():
        table = _create_results_table()
        _seed(table, URL, "up")
        with pytest.raises(RuntimeError, match="sns unavailable"):
            notify_transitions(table, topic, TOPIC, [(URL, "down")], now=NOW)
        assert _stored(table, URL)["status"] == "up"

        topic.fail = False
        published = notify_transitions(table, topic, TOPIC, [(URL, "down")], now=NOW)
        third = notify_transitions(table, topic, TOPIC, [(URL, "down")], now=NOW)
        assert _stored(table, URL)["status"] == "down"

    assert [(change.previous, change.status) for change in published] == [("up", "down")]
    assert third == []
    assert [call["Subject"] for call in topic.calls] == [
        "status up -> down",
        "status up -> down",
    ]


def test_a_lost_conditional_update_does_not_publish() -> None:
    topic = _Topic()
    published = notify_transitions(
        _LostUpdate(),
        topic,
        TOPIC,
        [(URL, "down")],
        now=NOW,
    )

    assert published == []
    assert topic.calls == []


def test_status_item_is_not_a_probe_observation(aws_credentials: None) -> None:
    when = NOW - timedelta(minutes=1)
    with mock_aws():
        table = _create_results_table()
        _store_observation(table, URL, "us-west-2", when, ok=True)
        _store_observation(table, URL, "us-east-1", when, ok=True)
        _seed(table, URL, "down")
        result = read_quorum(table, URL, now=NOW)
        stored = _stored(table, URL)["status"]

    assert result.status == "up"
    assert stored == "down"
