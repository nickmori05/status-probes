"""Reader queries. DynamoDB is moto, except the page-break test, which is a fake."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from probe.checker import ProbeResult
from probe.quorum import DEFAULT_FRESHNESS
from probe.reader import (
    handler,
    observation_from_item,
    query_observations,
    read_quorum,
    sort_key_bounds,
)
from probe.writer import clear_cached_resources, observation_sort_key, write_observations

NOW = datetime(2026, 11, 16, 13, 20, tzinfo=UTC)
URL = "https://example.com/health"
OTHER = "https://other.example/health"


class _Pages:
    """Just enough of a Table resource to return two query pages."""

    def __init__(self, pages: list[dict[str, object]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, object]] = []

    def query(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return self.pages[len(self.calls) - 1]


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


def _store(table: object, url: str, region: str, when: datetime, *, ok: bool) -> None:
    status_code = 200 if ok else 503
    error = None if ok else "HTTP 503"
    write_observations(
        [ProbeResult(url, status_code, 10.0, ok, error)],
        table,
        region=region,
        observed_at=when,
    )


def test_sort_key_bounds_include_every_region_at_the_edges() -> None:
    start = datetime(2026, 11, 16, 13, 17, tzinfo=UTC)
    end = datetime(2026, 11, 16, 13, 20, tzinfo=UTC)

    lower, upper = sort_key_bounds(start, end)

    assert lower == "2026-11-16T13:17:00.000000Z#"
    assert upper == "2026-11-16T13:20:00.000001Z#"
    inside = (
        "2026-11-16T13:17:00.000000Z#us-west-2",
        "2026-11-16T13:20:00.000000Z#eu-west-1",
    )
    assert all(lower <= key <= upper for key in inside)
    assert "2026-11-16T13:16:59.999999Z#us-east-1" < lower
    assert "2026-11-16T13:20:00.000001Z#us-west-2" > upper


def test_sort_key_bounds_rejects_a_backwards_window() -> None:
    with pytest.raises(ValueError):
        sort_key_bounds(NOW, NOW - timedelta(seconds=1))


def test_query_observations_keeps_the_window_and_the_partition(aws_credentials: None) -> None:
    with mock_aws():
        table = _create_results_table()
        _store(table, URL, "us-west-2", NOW - DEFAULT_FRESHNESS, ok=True)
        too_old = NOW - DEFAULT_FRESHNESS - timedelta(microseconds=1)
        _store(table, URL, "us-east-1", too_old, ok=False)
        _store(table, URL, "eu-west-1", NOW, ok=True)
        _store(table, URL, "us-west-2", NOW + timedelta(microseconds=1), ok=False)
        _store(table, OTHER, "us-east-1", NOW - timedelta(minutes=1), ok=False)
        found = query_observations(table, URL, now=NOW)

    assert [(item.region, item.observed_at, item.ok) for item in found] == [
        ("us-west-2", NOW - DEFAULT_FRESHNESS, True),
        ("eu-west-1", NOW, True),
    ]


def test_read_quorum_uses_the_latest_sample_in_the_window(aws_credentials: None) -> None:
    with mock_aws():
        table = _create_results_table()
        _store(table, URL, "us-west-2", NOW - timedelta(minutes=2), ok=False)
        _store(table, URL, "us-west-2", NOW - timedelta(minutes=1), ok=True)
        _store(table, URL, "us-east-1", NOW - timedelta(minutes=1), ok=True)
        _store(table, URL, "eu-west-1", NOW - timedelta(minutes=1), ok=False)
        result = read_quorum(table, URL, now=NOW)

    assert result.status == "up"
    assert result.up == ("us-west-2", "us-east-1")
    assert result.down == ("eu-west-1",)
    assert result.missing == ()


def test_read_quorum_is_unknown_when_only_one_region_is_fresh(aws_credentials: None) -> None:
    with mock_aws():
        table = _create_results_table()
        _store(table, URL, "us-west-2", NOW - timedelta(minutes=1), ok=False)
        _store(table, URL, "us-east-1", NOW - timedelta(minutes=10), ok=False)
        result = read_quorum(table, URL, now=NOW)

    assert result.status == "unknown"
    assert result.down == ("us-west-2",)
    assert result.up == ()
    assert result.missing == ("us-east-1", "eu-west-1")


def test_query_follows_the_page_break() -> None:
    when = NOW - timedelta(minutes=1)
    first = {"sk": observation_sort_key(when, "us-west-2"), "ok": True}
    second = {"sk": observation_sort_key(when, "us-east-1"), "ok": True}
    third = {"sk": observation_sort_key(when, "eu-west-1"), "ok": False}
    token = {"pk": URL, "sk": first["sk"]}
    table = _Pages(
        [
            {"Items": [first], "LastEvaluatedKey": token},
            {"Items": [second, third]},
        ]
    )

    result = read_quorum(table, URL, now=NOW)

    assert result.status == "up"
    assert result.down == ("eu-west-1",)
    assert result.missing == ()
    assert len(table.calls) == 2
    assert "ExclusiveStartKey" not in table.calls[0]
    assert table.calls[1]["ExclusiveStartKey"] == token
    assert table.calls[0]["KeyConditionExpression"] is table.calls[1]["KeyConditionExpression"]


def test_query_observations_rejects_a_non_positive_window() -> None:
    with pytest.raises(ValueError):
        query_observations(object(), URL, now=NOW, freshness=timedelta(0))


@pytest.mark.parametrize(
    "item",
    [
        {"sk": "2026-11-16T13:20:00.000000Z#us-east-1"},
        {"sk": "not-a-key", "ok": True},
        {"sk": "2026-11-16T13:20:00Z#us-east-1", "ok": True},
        {"ok": True},
    ],
)
def test_observation_from_item_rejects_a_bad_item(item: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        observation_from_item(item)


def test_handler_returns_the_quorum(
    aws_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TABLE_NAME", "probe-results")
    monkeypatch.setenv("TABLE_REGION", "us-east-1")
    observed = datetime.now(UTC) - timedelta(seconds=30)
    with mock_aws():
        clear_cached_resources()
        table = _create_results_table()
        _store(table, URL, "us-west-2", observed, ok=True)
        _store(table, URL, "us-east-1", observed, ok=True)
        _store(table, URL, "eu-west-1", observed, ok=False)
        payload = handler({"endpoint": f"  {URL}  "}, None)

    assert payload == {
        "endpoint": URL,
        "status": "up",
        "up": ["us-west-2", "us-east-1"],
        "down": ["eu-west-1"],
        "missing": [],
    }


def test_handler_queries_the_table_region(
    aws_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TABLE_NAME", "probe-results")
    monkeypatch.setenv("TABLE_REGION", "us-east-1")
    regions: list[str] = []
    with mock_aws():
        clear_cached_resources()
        table = _create_results_table()
        _store(table, URL, "us-east-1", datetime.now(UTC), ok=True)
        import probe.writer as writer

        original = writer.boto3.resource

        def spy(service_name: str, **kwargs: object) -> object:
            region = kwargs.get("region_name")
            if isinstance(region, str):
                regions.append(region)
            return original(service_name, **kwargs)

        with patch.object(writer.boto3, "resource", spy):
            handler({"endpoint": URL}, None)

    assert regions == ["us-east-1"]


@pytest.mark.parametrize("event", [{}, {"endpoint": "   "}, {"endpoint": None}])
def test_handler_requires_an_endpoint(event: dict) -> None:
    with pytest.raises(ValueError):
        handler(event, None)


def test_handler_requires_the_table_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TABLE_NAME", raising=False)
    with patch("probe.reader.dynamodb_table") as table:
        with pytest.raises(RuntimeError):
            handler({"endpoint": URL}, None)

    table.assert_not_called()
