"""Check one URL and report the status code and the time to headers."""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import dataclass

USER_AGENT = "status-probes/0.1"
DEFAULT_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class ProbeResult:
    """One HTTP observation. ``status_code`` is None when the request never completed."""

    url: str
    status_code: int | None
    latency_ms: float
    ok: bool
    error: str | None = None


def check_url(url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> ProbeResult:
    """GET ``url`` and return the status code plus latency to response headers.

    A response in the 2xx or 3xx range is ``ok``. HTTP errors keep their status
    code. Timeouts and connection failures leave ``status_code`` empty.
    """
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": USER_AGENT})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            latency_ms = _elapsed_ms(started)
            status_code = response.status
            return ProbeResult(
                url=url,
                status_code=status_code,
                latency_ms=latency_ms,
                ok=200 <= status_code < 400,
            )
    except urllib.error.HTTPError as exc:
        return ProbeResult(
            url=url,
            status_code=exc.code,
            latency_ms=_elapsed_ms(started),
            ok=False,
            error=f"HTTP {exc.code}",
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return ProbeResult(
            url=url,
            status_code=None,
            latency_ms=_elapsed_ms(started),
            ok=False,
            error=str(reason),
        )


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)
