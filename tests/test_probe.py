"""Checker and handler tests. HTTP is mocked; nothing talks to AWS."""

from __future__ import annotations

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
    with patch("probe.checker.urllib.request.urlopen", return_value=_Response(200)):
        result = check_url("https://example.com/health")

    assert result.url == "https://example.com/health"
    assert result.status_code == 200
    assert result.ok is True
    assert result.error is None
    assert result.latency_ms >= 0


def test_check_url_http_error_is_not_ok() -> None:
    with patch("probe.checker.urllib.request.urlopen", side_effect=_http_error(503)):
        result = check_url("https://example.com/health")

    assert result.status_code == 503
    assert result.ok is False
    assert result.error == "HTTP 503"
    assert result.latency_ms >= 0


def test_check_url_network_error_has_no_status() -> None:
    with patch(
        "probe.checker.urllib.request.urlopen",
        side_effect=urllib.error.URLError("timed out"),
    ):
        result = check_url("https://example.com/health")

    assert result.status_code is None
    assert result.ok is False
    assert result.error == "timed out"
    assert result.latency_ms >= 0


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
