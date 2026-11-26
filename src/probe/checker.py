"""Check one URL and report the status code and the time to headers.

A timeout or a connection failure is classified and tried again. An HTTP
response, including an error status, is not. The returned observation is
the last attempt.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

USER_AGENT = "status-probes/0.1"
DEFAULT_TIMEOUT_SECONDS = 5.0
# One retry. Two attempts that both hit the timeout, plus the wait below,
# stay inside the probe function's 20 second timeout for a single endpoint.
DEFAULT_ATTEMPTS = 2
DEFAULT_BACKOFF_SECONDS = 0.25

ErrorKind = Literal["timeout", "connection", "http"]


@dataclass(frozen=True)
class ProbeResult:
    """One HTTP observation. ``status_code`` is None when the request never completed.

    ``attempts`` counts GETs, including a failed one that was tried again.
    ``error_kind`` is set when the last attempt failed. ``error`` is the
    stored form of that failure: ``timeout``, ``connection:`` and the reason,
    or ``HTTP`` and the status code.
    """

    url: str
    status_code: int | None
    latency_ms: float
    ok: bool
    error: str | None = None
    error_kind: ErrorKind | None = None
    attempts: int = 1


def check_url(
    url: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    backoff: float = DEFAULT_BACKOFF_SECONDS,
    sleep: Callable[[float], None] | None = None,
) -> ProbeResult:
    """GET ``url`` and return the status code plus latency to response headers.

    A response in the 2xx or 3xx range is ``ok``. An HTTP error keeps its
    status code and is not tried again. A timeout or a connection failure
    is tried again, up to ``attempts``. The wait before the second attempt
    is ``backoff`` seconds, and it doubles before each attempt after that.
    The result is the last attempt. Its latency is that attempt's time to
    headers and does not include earlier attempts or the wait.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if backoff < 0:
        raise ValueError("backoff must be zero or positive")

    pause = time.sleep if sleep is None else sleep
    result = _one_attempt(url, timeout, 1)
    for attempt in range(1, attempts):
        if result.status_code is not None:
            return result
        pause(backoff * (2 ** (attempt - 1)))
        result = _one_attempt(url, timeout, attempt + 1)
    return result


def _one_attempt(url: str, timeout: float, attempt: int) -> ProbeResult:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": USER_AGENT})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status_code = response.status
            return ProbeResult(
                url=url,
                status_code=status_code,
                latency_ms=_elapsed_ms(started),
                ok=200 <= status_code < 400,
                attempts=attempt,
            )
    except urllib.error.HTTPError as exc:
        return ProbeResult(
            url=url,
            status_code=exc.code,
            latency_ms=_elapsed_ms(started),
            ok=False,
            error=f"HTTP {exc.code}",
            error_kind="http",
            attempts=attempt,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        kind = _transport_kind(exc)
        return ProbeResult(
            url=url,
            status_code=None,
            latency_ms=_elapsed_ms(started),
            ok=False,
            error=_transport_error(kind, exc),
            error_kind=kind,
            attempts=attempt,
        )


def _transport_kind(exc: BaseException) -> Literal["timeout", "connection"]:
    """``timeout`` when the failure is a timeout, otherwise ``connection``."""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if _is_timeout(reason):
        return "timeout"
    return "connection"


def _is_timeout(reason: object) -> bool:
    if isinstance(reason, TimeoutError):
        return True
    text = str(reason).strip().lower()
    return text == "timeout" or "timed out" in text


def _transport_error(kind: Literal["timeout", "connection"], exc: BaseException) -> str:
    if kind == "timeout":
        return "timeout"
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    return f"connection: {reason}"


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)
