"""Lambda entry point. Checks each endpoint, stores the observations, and returns them.

Storage runs when ``TABLE_NAME`` is set. A failed write is logged with the
observations and re-raised: the log keeps the probe results, and the error
keeps a broken table from looking like a healthy run. The success path still
returns the observations.

A check that failed, or that needed more than one attempt, is logged. A
first-attempt success is not. The line is written before the table write,
so a broken table does not drop it.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict

from probe.checker import ProbeResult, check_url
from probe.writer import store_results

logger = logging.getLogger(__name__)


def parse_endpoints(raw: str) -> list[str]:
    """Split a comma-separated endpoint list, dropping blanks."""
    return [part.strip() for part in raw.split(",") if part.strip()]


def handler(event: dict, context: object) -> dict:
    """Probe every URL in the ENDPOINTS environment variable."""
    del event, context
    region = os.environ.get("AWS_REGION", "local")
    endpoints = parse_endpoints(os.environ.get("ENDPOINTS", ""))
    results = [check_url(url) for url in endpoints]
    for result in results:
        _log_result(result)
    store_results(results, region=region)
    return {
        "region": region,
        "results": [asdict(result) for result in results],
    }


def _log_result(result: ProbeResult) -> None:
    """One line for a failure or a retry. A first-attempt success stays quiet."""
    if result.ok and result.attempts == 1:
        return
    logger.info(
        "probe %s ok=%s status=%s error=%s attempts=%d latency_ms=%s",
        result.url,
        result.ok,
        result.status_code,
        result.error,
        result.attempts,
        result.latency_ms,
    )
