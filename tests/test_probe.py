"""Checker and handler tests. HTTP is mocked; nothing talks to AWS."""

from __future__ import annotations

import logging
import urllib.error
from http.client import HTTPMessage
from unittest.mock import patch

import pytest

from probe.checker import check_url
from probe.handler import handler


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://example.com/health",
        code=code,
        msg="error",
        hdrs=HTTPMessage(),
        fp=None,
    )


def test_check_url_records_status_and_latency() -> None:
    pauses: list[float] = []
    with patch("probe.checker.urllib.request.urlopen", return_value=_Response(200)) as opened:
        result = check_url("https://example.com/health", sleep=pauses.append)

    assert opened.call_count == 1
    assert pauses == []
    assert result.url == "https://example.com/health"
    assert result.status_code == 200
    assert result.ok is True
    assert result.error is None
    assert result.error_kind is None
    assert result.attempts == 1
    assert result.latency_ms >= 0


def test_check_url_http_error_is_not_ok() -> None:
    pauses: list[float] = []
    with patch(
        "probe.checker.urllib.request.urlopen", side_effect=_http_error(503)
    ) as opened:
        result = check_url("https://example.com/health", sleep=pauses.append)

    assert opened.call_count == 1
    assert pauses == []
    assert result.status_code == 503
    assert result.ok is False
    assert result.error == "HTTP 503"
    assert result.error_kind == "http"
    assert result.attempts == 1
    assert result.latency_ms >= 0


def test_check_url_timeout_is_classified_and_retried() -> None:
    pauses: list[float] = []
    with patch(
        "probe.checker.urllib.request.urlopen",
        side_effect=urllib.error.URLError("timed out"),
    ) as opened:
        result = check_url("https://example.com/health", sleep=pauses.append)

    assert opened.call_count == 2
    assert pauses == [0.25]
    assert result.status_code is None
    assert result.ok is False
    assert result.error == "timeout"
    assert result.error_kind == "timeout"
    assert result.attempts == 2
    assert result.latency_ms >= 0


def test_timeout_exception_is_the_same_kind() -> None:
    with patch(
        "probe.checker.urllib.request.urlopen",
        side_effect=urllib.error.URLError(TimeoutError("The read operation timed out")),
    ):
        result = check_url("https://example.com/health", attempts=1, sleep=lambda _delay: None)

    assert result.error == "timeout"
    assert result.error_kind == "timeout"
    assert result.attempts == 1


def test_connection_failure_is_classified_and_retried() -> None:
    reason = ConnectionRefusedError(111, "Connection refused")
    pauses: list[float] = []
    with patch(
        "probe.checker.urllib.request.urlopen",
        side_effect=urllib.error.URLError(reason),
    ) as opened:
        result = check_url("https://example.com/health", sleep=pauses.append)

    assert opened.call_count == 2
    assert pauses == [0.25]
    assert result.status_code is None
    assert result.ok is False
    assert result.error == f"connection: {reason}"
    assert result.error_kind == "connection"
    assert result.attempts == 2


def test_timeout_then_success_keeps_the_second_attempt() -> None:
    pauses: list[float] = []
    with patch(
        "probe.checker.urllib.request.urlopen",
        side_effect=[urllib.error.URLError(TimeoutError("timed out")), _Response(204)],
    ) as opened:
        result = check_url("https://example.com/health", sleep=pauses.append)

    assert opened.call_count == 2
    assert pauses == [0.25]
    assert result.status_code == 204
    assert result.ok is True
    assert result.error is None
    assert result.error_kind is None
    assert result.attempts == 2


def test_backoff_doubles_before_a_third_attempt() -> None:
    pauses: list[float] = []
    with patch(
        "probe.checker.urllib.request.urlopen",
        side_effect=urllib.error.URLError("timed out"),
    ):
        result = check_url("https://example.com/health", attempts=3, sleep=pauses.append)

    assert pauses == [0.25, 0.5]
    assert result.attempts == 3
    assert result.error_kind == "timeout"


def test_latency_is_the_last_attempt_only() -> None:
    clocks = iter([0.0, 5.0, 5.25, 5.30])
    with patch("probe.checker.time.perf_counter", side_effect=lambda: next(clocks)):
        with patch(
            "probe.checker.urllib.request.urlopen",
            side_effect=[urllib.error.URLError("timed out"), _Response(200)],
        ):
            result = check_url("https://example.com/health", sleep=lambda _delay: None)

    assert result.ok is True
    assert result.latency_ms == 50.0
    assert result.attempts == 2


def test_check_url_rejects_a_non_positive_budget() -> None:
    with pytest.raises(ValueError, match="attempts"):
        check_url("https://example.com/health", attempts=0)
    with pytest.raises(ValueError, match="timeout"):
        check_url("https://example.com/health", timeout=0)
    with pytest.raises(ValueError, match="backoff"):
        check_url("https://example.com/health", backoff=-0.1)


def test_handler_logs_a_retry_and_a_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("TABLE_NAME", raising=False)
    monkeypatch.setenv("ENDPOINTS", "https://a.example/health, https://b.example/health")
    with caplog.at_level(logging.INFO, logger="probe.handler"):
        with patch("probe.checker.time.sleep"):
            with patch(
                "probe.checker.urllib.request.urlopen",
                side_effect=[
                    urllib.error.URLError("timed out"),
                    _Response(200),
                    urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")),
                    urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")),
                ],
            ):
                payload = handler({}, None)

    assert payload["results"][0]["attempts"] == 2
    assert payload["results"][0]["ok"] is True
    assert payload["results"][1]["error_kind"] == "connection"
    messages = [record.getMessage() for record in caplog.records if record.name == "probe.handler"]
    assert len(messages) == 2
    assert messages[0].startswith("probe https://a.example/health ok=True ")
    assert "error=None" in messages[0]
    assert "attempts=2" in messages[0]
    assert messages[1].startswith("probe https://b.example/health ok=False ")
    assert "error_kind" not in messages[1]
    assert "attempts=2" in messages[1]
    assert "connection:" in messages[1]


def test_handler_does_not_log_a_first_attempt_success(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("TABLE_NAME", raising=False)
    monkeypatch.setenv("ENDPOINTS", "https://a.example/health")
    with caplog.at_level(logging.INFO, logger="probe.handler"):
        with patch("probe.checker.urllib.request.urlopen", return_value=_Response(204)):
            handler({}, None)

    assert [record for record in caplog.records if record.name == "probe.handler"] == []


def test_handler_checks_each_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENDPOINTS", "https://a.example/health, https://b.example/health")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    with patch("probe.checker.urllib.request.urlopen", return_value=_Response(204)) as opened:
        payload = handler({}, None)

    assert opened.call_count == 2
    assert payload["region"] == "eu-west-1"
    assert [item["url"] for item in payload["results"]] == [
        "https://a.example/health",
        "https://b.example/health",
    ]
    assert all(item["ok"] and item["status_code"] == 204 for item in payload["results"])


def test_handler_ignores_blank_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENDPOINTS", "https://a.example/health, ,")
    with patch("probe.checker.urllib.request.urlopen", return_value=_Response(200)) as opened:
        payload = handler({}, None)

    assert opened.call_count == 1
    assert len(payload["results"]) == 1


def test_handler_with_no_endpoints_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENDPOINTS", raising=False)
    with patch("probe.checker.urllib.request.urlopen") as opened:
        payload = handler({}, None)

    opened.assert_not_called()
    assert payload["results"] == []
