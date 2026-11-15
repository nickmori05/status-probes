"""Lambda entry point. Checks each endpoint, stores the observations, and returns them.

Storage runs when ``TABLE_NAME`` is set. A failed write is logged with the
observations and re-raised: the log keeps the probe results, and the error
keeps a broken table from looking like a healthy run. The success path still
returns the observations.
"""

from __future__ import annotations

import os
from dataclasses import asdict

from probe.checker import check_url
from probe.writer import store_results


def parse_endpoints(raw: str) -> list[str]:
    """Split a comma-separated endpoint list, dropping blanks."""
    return [part.strip() for part in raw.split(",") if part.strip()]


def handler(event: dict, context: object) -> dict:
    """Probe every URL in the ENDPOINTS environment variable."""
    del event, context
    region = os.environ.get("AWS_REGION", "local")
    endpoints = parse_endpoints(os.environ.get("ENDPOINTS", ""))
    results = [check_url(url) for url in endpoints]
    store_results(results, region=region)
    return {
        "region": region,
        "results": [asdict(result) for result in results],
    }
