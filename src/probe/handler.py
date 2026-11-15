"""Lambda entry point. Checks each endpoint and returns the observations.

Results are not written anywhere yet. Persistence belongs to a later change.
"""

from __future__ import annotations

import os
from dataclasses import asdict

from probe.checker import check_url


def parse_endpoints(raw: str) -> list[str]:
    """Split a comma-separated endpoint list, dropping blanks."""
    return [part.strip() for part in raw.split(",") if part.strip()]


def handler(event: dict, context: object) -> dict:
    """Probe every URL in the ENDPOINTS environment variable."""
    del event, context
    endpoints = parse_endpoints(os.environ.get("ENDPOINTS", ""))
    results = [check_url(url) for url in endpoints]
    return {
        "region": os.environ.get("AWS_REGION", "local"),
        "results": [asdict(result) for result in results],
    }
