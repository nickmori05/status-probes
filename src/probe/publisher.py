"""Publish the quorum for each configured endpoint as a static page.

``status.json`` is the generated time and, for each endpoint, the status
and the regions that voted. ``index.html`` fetches that file. It does not
embed the status. The handler writes the page first, then the JSON, so a
failed query leaves the previous objects in place. A failed write is
logged and re-raised.

The handler does not invoke the quorum function. It calls the same query
and majority code, once per endpoint in ``ENDPOINTS``.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Sequence
from datetime import UTC, datetime

import boto3
from botocore.config import Config

from probe.handler import parse_endpoints
from probe.quorum import QuorumResult
from probe.reader import read_quorum
from probe.writer import (
    DEFAULT_TABLE_REGION,
    TABLE_NAME_ENV,
    TABLE_REGION_ENV,
    dynamodb_table,
    format_observed_at,
)

logger = logging.getLogger(__name__)

STATUS_KEY = "status.json"
PAGE_KEY = "index.html"
BUCKET_NAME_ENV = "BUCKET_NAME"
JSON_CONTENT_TYPE = "application/json; charset=utf-8"
HTML_CONTENT_TYPE = "text/html; charset=utf-8"
# The StatusPage cache policy holds an object for at most this long.
CACHE_CONTROL = "public, max-age=60"
SSE_ALGORITHM = "AES256"

_CLIENT_CONFIG = Config(
    connect_timeout=2,
    read_timeout=5,
    retries={"total_max_attempts": 3, "mode": "standard"},
)

# The page is static. Endpoint text is assigned with textContent, so a URL
# in the JSON cannot become markup. The fetch is relative: the page and
# the JSON are both at the root of the distribution.
INDEX_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Status</title>
  <style>
    body { font-family: sans-serif; margin: 2rem; color: #222; }
    table { border-collapse: collapse; }
    th, td {
      text-align: left;
      padding: 0.4rem 0.8rem;
      border-bottom: 1px solid #ccc;
    }
    .up { color: #070; }
    .down { color: #900; }
    .unknown { color: #555; }
  </style>
</head>
<body>
  <h1>Status</h1>
  <p id="generated">Loading.</p>
  <table>
    <thead>
      <tr>
        <th>Endpoint</th>
        <th>Status</th>
        <th>Up</th>
        <th>Down</th>
        <th>Missing</th>
      </tr>
    </thead>
    <tbody id="rows"></tbody>
  </table>
  <script>
    fetch("status.json", {cache: "no-store"})
      .then(function (response) {
        if (!response.ok) {
          throw new Error("status.json " + response.status);
        }
        return response.json();
      })
      .then(function (data) {
        var generated = document.getElementById("generated");
        generated.textContent = "Generated " + data.generated_at;
        var rows = document.getElementById("rows");
        (data.endpoints || []).forEach(function (item) {
          var cells = [
            item.endpoint,
            item.status,
            (item.up || []).join(", "),
            (item.down || []).join(", "),
            (item.missing || []).join(", ")
          ];
          var tr = document.createElement("tr");
          cells.forEach(function (text, index) {
            var td = document.createElement("td");
            td.textContent = text == null ? "" : String(text);
            if (index === 1) {
              td.className = text;
            }
            tr.appendChild(td);
          });
          rows.appendChild(tr);
        });
      })
      .catch(function (error) {
        document.getElementById("generated").textContent = String(error);
      });
  </script>
</body>
</html>
"""


def status_document(
    entries: Sequence[tuple[str, QuorumResult]],
    *,
    generated_at: datetime,
) -> dict[str, object]:
    """One page document. ``entries`` are ``(endpoint, QuorumResult)`` pairs.

    Endpoint order is the order of ``entries``. Region lists follow the
    order ``aggregate`` already applied.
    """
    endpoints: list[dict[str, object]] = []
    for endpoint, result in entries:
        endpoints.append(
            {
                "endpoint": endpoint,
                "status": result.status,
                "up": list(result.up),
                "down": list(result.down),
                "missing": list(result.missing),
            }
        )
    return {
        "generated_at": format_observed_at(generated_at),
        "endpoints": endpoints,
    }


def render_status_json(document: dict[str, object]) -> str:
    """Pretty JSON with a trailing newline."""
    return json.dumps(document, indent=2) + "\n"


def collect_status(
    table: object,
    endpoints: Sequence[str],
    *,
    now: datetime,
) -> dict[str, object]:
    """Query the freshness window for each endpoint and build the document.

    A failed query raises before any later endpoint is read. The caller
    has not written yet, so the previous page stays as it was.
    """
    entries = [(endpoint, read_quorum(table, endpoint, now=now)) for endpoint in endpoints]
    return status_document(entries, generated_at=now)


def publish(client: object, bucket: str, document: dict[str, object]) -> None:
    """Write ``index.html``, then ``status.json``.

    The page is the same bytes every time. Writing it first means a failed
    JSON put leaves the previous status in place behind a page that can
    still read it. Both objects are SSE-S3. The bucket blocks ACLs, so
    this does not set one.
    """
    client.put_object(
        Bucket=bucket,
        Key=PAGE_KEY,
        Body=INDEX_HTML.encode("utf-8"),
        ContentType=HTML_CONTENT_TYPE,
        CacheControl=CACHE_CONTROL,
        ServerSideEncryption=SSE_ALGORITHM,
    )
    client.put_object(
        Bucket=bucket,
        Key=STATUS_KEY,
        Body=render_status_json(document).encode("utf-8"),
        ContentType=JSON_CONTENT_TYPE,
        CacheControl=CACHE_CONTROL,
        ServerSideEncryption=SSE_ALGORITHM,
    )


def handler(event: dict, context: object) -> dict[str, object]:
    """Publish the quorum for every endpoint in ``ENDPOINTS``.

    Reads ``probe-results`` using ``TABLE_NAME`` and ``TABLE_REGION``.
    Writes to the bucket named by ``BUCKET_NAME``. The S3 client uses
    ``AWS_REGION`` (the function's region, which is where the bucket is),
    not the table region. Returns the document it wrote. The event is
    ignored: the schedule does not pass an endpoint.
    """
    del event, context
    endpoints = parse_endpoints(os.environ.get("ENDPOINTS", ""))
    if not endpoints:
        raise RuntimeError("ENDPOINTS is required")
    bucket = os.environ.get(BUCKET_NAME_ENV, "").strip()
    if not bucket:
        raise RuntimeError(f"{BUCKET_NAME_ENV} is required")
    table_name = os.environ.get(TABLE_NAME_ENV, "").strip()
    if not table_name:
        raise RuntimeError(f"{TABLE_NAME_ENV} is required")
    table_region = os.environ.get(TABLE_REGION_ENV, "").strip() or DEFAULT_TABLE_REGION
    try:
        table = dynamodb_table(table_name, table_region)
        document = collect_status(table, endpoints, now=datetime.now(UTC))
        publish(_s3_client(), bucket, document)
    except Exception:
        logger.exception("failed to publish status for %s to %s", endpoints, bucket)
        raise
    return document


def _s3_client() -> object:
    """S3 client in the function's region. Falls back to the table region."""
    region = os.environ.get("AWS_REGION", "").strip()
    if not region:
        region = os.environ.get(TABLE_REGION_ENV, "").strip() or DEFAULT_TABLE_REGION
    return boto3.client("s3", region_name=region, config=_CLIENT_CONFIG)
