"""Item shape and handler writes. DynamoDB is moto; HTTP is mocked."""

from __future__ import annotations

import logging
import re
import urllib.error
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from probe.checker import ProbeResult
from probe.handler import handler
from probe.writer import clear_cached_resources, to_item

_SORT_KEY = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z#(?P<region>[a-z0-9-]+)")


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> bool:
        return False


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


def test_to_item_sets_pk_sk_and_ttl() -> None:
    observed = datetime(2026, 11, 15, 8, 20, 1, 250000, tzinfo=timezone(timedelta(hours=-5)))
    result = ProbeResult(
        url="https://example.com/health",
        status_code=None,
        latency_ms=12.5,
        ok=False,
        error="timed out",
    )

    item = to_item(result, region="eu-west-1", observed_at=observed)

    assert item["pk"] == "https://example.com/health"
    assert item["sk"] == "2026-11-15T13:20:01.250000Z#eu-west-1"
    assert item["ttl"] == int(observed.timestamp()) + 7 * 24 * 60 * 60
    assert item["status_code"] is None
    assert item["latency_ms"] == Decimal("12.5")
    assert item["ok"] is False
    assert item["error"] == "timed out"
    assert item["region"] == "eu-west-1"


def test_to_item_retention_days_moves_ttl() -> None:
    observed = datetime(2026, 11, 15, 13, 20, tzinfo=UTC)
    result = ProbeResult(url="https://example.com/health", status_code=204, latency_ms=1, ok=True)

    item = to_item(result, region="us-east-1", observed_at=observed, retention_days=1)

    assert item["ttl"] == int(observed.timestamp()) + 24 * 60 * 60
    assert item["status_code"] == 204
    assert item["error"] is None


def test_handler_writes_one_observation(
    aws_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TABLE_NAME", "probe-results")
    monkeypatch.setenv("TABLE_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setenv("RESULT_RETENTION_DAYS", "1")
    monkeypatch.setenv("ENDPOINTS", "https://example.com/health")
    with mock_aws():
        clear_cached_resources()
        table = _create_results_table()
        before = int(datetime.now(UTC).timestamp())
        with patch("probe.checker.urllib.request.urlopen", return_value=_Response(204)):
            payload = handler({}, None)
        after = int(datetime.now(UTC).timestamp())
        items = table.scan()["Items"]

    assert payload["region"] == "eu-west-1"
    assert payload["results"][0]["status_code"] == 204
    assert len(items) == 1
    item = items[0]
    assert item["pk"] == "https://example.com/health"
    match = _SORT_KEY.fullmatch(item["sk"])
    assert match is not None
    assert match.group("region") == "eu-west-1"
    assert item["region"] == "eu-west-1"
    assert int(item["status_code"]) == 204
    assert item["ok"] is True
    assert item["error"] is None
    assert before + 86400 <= int(item["ttl"]) <= after + 86400


def test_handler_writes_each_endpoint(
    aws_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TABLE_NAME", "probe-results")
    monkeypatch.setenv("TABLE_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("ENDPOINTS", "https://a.example/health, https://b.example/health")
    with mock_aws():
        clear_cached_resources()
        table = _create_results_table()
        before = int(datetime.now(UTC).timestamp())
        with patch(
            "probe.checker.urllib.request.urlopen",
            side_effect=[_Response(200), urllib.error.URLError("timed out")],
        ):
            payload = handler({}, None)
        after = int(datetime.now(UTC).timestamp())
        stored = {item["pk"]: item for item in table.scan()["Items"]}

    assert [item["url"] for item in payload["results"]] == [
        "https://a.example/health",
        "https://b.example/health",
    ]
    ok_item = stored["https://a.example/health"]
    assert ok_item["sk"].endswith("#us-west-2")
    assert int(ok_item["status_code"]) == 200
    assert ok_item["ok"] is True
    assert before + 7 * 86400 <= int(ok_item["ttl"]) <= after + 7 * 86400

    failed = stored["https://b.example/health"]
    assert failed["status_code"] is None
    assert failed["ok"] is False
    assert failed["error"] == "timed out"
    assert failed["region"] == "us-west-2"


def test_handler_targets_the_table_region(
    aws_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TABLE_NAME", "probe-results")
    monkeypatch.setenv("TABLE_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setenv("ENDPOINTS", "https://example.com/health")
    regions: list[str] = []
    with mock_aws():
        clear_cached_resources()
        _create_results_table()
        import probe.writer as writer

        original = writer.boto3.resource

        def spy(service_name: str, **kwargs: object) -> object:
            region = kwargs.get("region_name")
            if isinstance(region, str):
                regions.append(region)
            return original(service_name, **kwargs)

        with patch.object(writer.boto3, "resource", spy):
            with patch("probe.checker.urllib.request.urlopen", return_value=_Response(200)):
                handler({}, None)

    assert regions == ["us-east-1"]


def test_handler_logs_observations_and_reraises_when_the_write_fails(
    aws_credentials: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("TABLE_NAME", "probe-results")
    monkeypatch.setenv("TABLE_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("ENDPOINTS", "https://example.com/health")
    error = ClientError(
        {"Error": {"Code": "InternalServerError", "Message": "unavailable"}},
        "PutItem",
    )
    with patch("probe.checker.urllib.request.urlopen", return_value=_Response(503)):
        with patch("probe.writer.dynamodb_table", return_value=object()):
            with patch("probe.writer.write_observations", side_effect=error):
                with caplog.at_level(logging.ERROR, logger="probe.writer"):
                    with pytest.raises(ClientError):
                        handler({}, None)

    assert "https://example.com/health" in caplog.text
    assert "probe-results" in caplog.text


def test_handler_without_table_name_does_not_call_dynamodb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TABLE_NAME", raising=False)
    monkeypatch.setenv("ENDPOINTS", "https://example.com/health")
    with patch("probe.checker.urllib.request.urlopen", return_value=_Response(200)):
        with patch("probe.writer.dynamodb_table") as table:
            payload = handler({}, None)

    table.assert_not_called()
    assert payload["results"][0]["ok"] is True


def test_handler_with_no_endpoints_does_not_call_dynamodb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TABLE_NAME", "probe-results")
    monkeypatch.setenv("ENDPOINTS", "")
    with patch("probe.writer.dynamodb_table") as table:
        payload = handler({}, None)

    table.assert_not_called()
    assert payload["results"] == []
