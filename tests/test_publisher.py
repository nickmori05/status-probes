"""Status page rendering and S3 writes. DynamoDB and S3 are moto or fakes."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from probe.checker import ProbeResult
from probe.publisher import (
    CACHE_CONTROL,
    HTML_CONTENT_TYPE,
    INDEX_HTML,
    JSON_CONTENT_TYPE,
    PAGE_KEY,
    SSE_ALGORITHM,
    STATUS_KEY,
    collect_status,
    handler,
    publish,
    render_status_json,
    status_document,
)
from probe.quorum import QuorumResult
from probe.writer import clear_cached_resources, write_observations

NOW = datetime(2026, 11, 18, 13, 20, tzinfo=UTC)
UP = "https://example.com/health"
DOWN = "https://down.example/health"
UNKNOWN = "https://other.example/health"


class _Puts:
    """Records put_object calls and nothing else."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def put_object(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {}


class _Clock:
    @staticmethod
    def now(tz: object = None) -> datetime:
        del tz
        return NOW


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


def _sample_document() -> dict[str, object]:
    return status_document(
        [
            (
                DOWN,
                QuorumResult(
                    "down",
                    (),
                    ("us-west-2", "us-east-1"),
                    ("eu-west-1",),
                ),
            ),
            (
                UP,
                QuorumResult(
                    "up",
                    ("us-west-2", "eu-west-1"),
                    (),
                    ("us-east-1",),
                ),
            ),
        ],
        generated_at=NOW,
    )


def test_status_document_keeps_caller_order_and_region_lists() -> None:
    assert _sample_document() == {
        "generated_at": "2026-11-18T13:20:00.000000Z",
        "endpoints": [
            {
                "endpoint": DOWN,
                "status": "down",
                "up": [],
                "down": ["us-west-2", "us-east-1"],
                "missing": ["eu-west-1"],
            },
            {
                "endpoint": UP,
                "status": "up",
                "up": ["us-west-2", "eu-west-1"],
                "down": [],
                "missing": ["us-east-1"],
            },
        ],
    }


def test_render_status_json_is_pretty_and_stable() -> None:
    assert render_status_json(_sample_document()) == """\
{
  "generated_at": "2026-11-18T13:20:00.000000Z",
  "endpoints": [
    {
      "endpoint": "https://down.example/health",
      "status": "down",
      "up": [],
      "down": [
        "us-west-2",
        "us-east-1"
      ],
      "missing": [
        "eu-west-1"
      ]
    },
    {
      "endpoint": "https://example.com/health",
      "status": "up",
      "up": [
        "us-west-2",
        "eu-west-1"
      ],
      "down": [],
      "missing": [
        "us-east-1"
      ]
    }
  ]
}
"""


def test_index_html_fetches_status_json_and_does_not_embed_it() -> None:
    assert INDEX_HTML.startswith("<!DOCTYPE html>\n")
    assert 'fetch("status.json", {cache: "no-store"})' in INDEX_HTML
    assert "textContent" in INDEX_HTML
    assert "innerHTML" not in INDEX_HTML
    assert "https://" not in INDEX_HTML
    assert "generated_at" in INDEX_HTML


def test_publish_writes_the_page_then_the_status() -> None:
    client = _Puts()
    document = _sample_document()

    publish(client, "status-site", document)

    assert [call["Key"] for call in client.calls] == [PAGE_KEY, STATUS_KEY]
    page, status = client.calls
    assert page["Bucket"] == "status-site"
    assert page["ContentType"] == HTML_CONTENT_TYPE
    assert page["CacheControl"] == CACHE_CONTROL
    assert page["ServerSideEncryption"] == SSE_ALGORITHM
    assert page["Body"] == INDEX_HTML.encode("utf-8")
    assert "ACL" not in page
    assert status["ContentType"] == JSON_CONTENT_TYPE
    assert status["CacheControl"] == CACHE_CONTROL
    assert status["ServerSideEncryption"] == SSE_ALGORITHM
    assert status["Body"] == render_status_json(document).encode("utf-8")
    assert "ACL" not in status


def test_collect_status_uses_the_quorum(aws_credentials: None) -> None:
    when = NOW - timedelta(minutes=1)
    with mock_aws():
        table = _create_results_table()
        _store(table, UP, "us-west-2", when, ok=True)
        _store(table, UP, "us-east-1", when, ok=True)
        _store(table, UP, "eu-west-1", when - timedelta(minutes=10), ok=False)
        _store(table, UNKNOWN, "eu-west-1", when, ok=False)
        _store(table, DOWN, "us-west-2", when, ok=False)
        _store(table, DOWN, "us-east-1", when, ok=False)
        document = collect_status(table, [UP, UNKNOWN, DOWN], now=NOW)

    assert document == {
        "generated_at": "2026-11-18T13:20:00.000000Z",
        "endpoints": [
            {
                "endpoint": UP,
                "status": "up",
                "up": ["us-west-2", "us-east-1"],
                "down": [],
                "missing": ["eu-west-1"],
            },
            {
                "endpoint": UNKNOWN,
                "status": "unknown",
                "up": [],
                "down": ["eu-west-1"],
                "missing": ["us-west-2", "us-east-1"],
            },
            {
                "endpoint": DOWN,
                "status": "down",
                "up": [],
                "down": ["us-west-2", "us-east-1"],
                "missing": ["eu-west-1"],
            },
        ],
    }


def test_handler_publishes_the_configured_endpoints(
    aws_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TABLE_NAME", "probe-results")
    monkeypatch.setenv("TABLE_REGION", "us-east-1")
    monkeypatch.setenv("BUCKET_NAME", "status-site")
    monkeypatch.setenv("ENDPOINTS", f"{UP}, {UNKNOWN}")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    observed = NOW - timedelta(seconds=30)
    with mock_aws():
        clear_cached_resources()
        table = _create_results_table()
        _store(table, UP, "us-west-2", observed, ok=True)
        _store(table, UP, "us-east-1", observed, ok=True)
        _store(table, UNKNOWN, "eu-west-1", observed, ok=False)
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="status-site")
        with patch("probe.publisher.datetime", _Clock):
            payload = handler({"endpoint": "https://not-configured.example"}, None)
        status = s3.get_object(Bucket="status-site", Key=STATUS_KEY)
        page = s3.get_object(Bucket="status-site", Key=PAGE_KEY)

    body = json.loads(status["Body"].read())
    assert payload == body
    assert [entry["endpoint"] for entry in body["endpoints"]] == [UP, UNKNOWN]
    assert [entry["status"] for entry in body["endpoints"]] == ["up", "unknown"]
    assert status["ContentType"] == JSON_CONTENT_TYPE
    assert status["CacheControl"] == CACHE_CONTROL
    assert status["ServerSideEncryption"] == SSE_ALGORITHM
    assert page["Body"].read() == INDEX_HTML.encode("utf-8")
    assert page["ContentType"] == HTML_CONTENT_TYPE
    assert page["CacheControl"] == CACHE_CONTROL


def test_handler_queries_the_table_region_and_writes_the_function_region(
    aws_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TABLE_NAME", "probe-results")
    monkeypatch.setenv("TABLE_REGION", "us-east-1")
    monkeypatch.setenv("BUCKET_NAME", "status-site")
    monkeypatch.setenv("ENDPOINTS", UP)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    seen: dict[str, object] = {}

    def fake_table(name: str, region: str) -> object:
        seen["table"] = (name, region)
        return object()

    def fake_client(service_name: str, **kwargs: object) -> _Puts:
        assert service_name == "s3"
        seen["s3_region"] = kwargs.get("region_name")
        return _Puts()

    with (
        patch("probe.publisher.dynamodb_table", fake_table),
        patch("probe.publisher.boto3.client", fake_client),
        patch("probe.publisher.read_quorum", return_value=QuorumResult("unknown", (), (), ())),
        patch("probe.publisher.datetime", _Clock),
    ):
        handler({}, None)

    assert seen["table"] == ("probe-results", "us-east-1")
    assert seen["s3_region"] == "us-east-1"


def test_handler_does_not_publish_when_the_query_fails(
    aws_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TABLE_NAME", "probe-results")
    monkeypatch.setenv("TABLE_REGION", "us-east-1")
    monkeypatch.setenv("BUCKET_NAME", "status-site")
    monkeypatch.setenv("ENDPOINTS", UP)
    with mock_aws():
        clear_cached_resources()
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="status-site")
        with pytest.raises(Exception):
            handler({}, None)
        listed = s3.list_objects_v2(Bucket="status-site")

    assert listed.get("KeyCount", 0) == 0


@pytest.mark.parametrize(
    ("env", "value"),
    [
        ("ENDPOINTS", ""),
        ("ENDPOINTS", " , "),
        ("BUCKET_NAME", "  "),
        ("TABLE_NAME", ""),
    ],
)
def test_handler_requires_endpoints_bucket_and_table(
    monkeypatch: pytest.MonkeyPatch, env: str, value: str
) -> None:
    monkeypatch.setenv("ENDPOINTS", UP)
    monkeypatch.setenv("BUCKET_NAME", "status-site")
    monkeypatch.setenv("TABLE_NAME", "probe-results")
    monkeypatch.setenv(env, value)
    with (
        patch("probe.publisher.dynamodb_table") as table,
        patch("probe.publisher._s3_client") as client,
    ):
        with pytest.raises(RuntimeError):
            handler({}, None)

    table.assert_not_called()
    client.assert_not_called()
