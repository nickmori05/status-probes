"""The monthly estimate. Prices are the constants in cost.estimate; AWS is not called."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from cost.estimate import (
    Assumptions,
    at_timeout,
    estimate,
    format_usd,
    observation_item_bytes,
    render_report,
    significant_digits,
)

from probe.publisher import INDEX_HTML, render_status_json, status_document
from probe.quorum import QuorumResult


def test_observation_is_one_write_unit() -> None:
    size = observation_item_bytes()
    # pk, sk, region, a trimmed status code and latency, a 10-digit ttl, ok, null error.
    assert significant_digits("200") == 1
    assert significant_digits("150.0") == 2
    assert size == 118
    assert size < 1024


def test_page_sizes_match_the_publisher() -> None:
    assumptions = Assumptions()
    assert len(INDEX_HTML.encode()) == assumptions.html_bytes
    document = status_document(
        [
            (
                "https://example.com",
                QuorumResult(
                    status="up",
                    up=("us-west-2", "us-east-1", "eu-west-1"),
                    down=(),
                    missing=(),
                ),
            )
        ],
        generated_at=datetime(2026, 11, 21, 13, 23, 1, 250000, tzinfo=UTC),
    )
    assert len(render_status_json(document).encode()) == assumptions.json_bytes


def test_default_month_matches_the_rate_table() -> None:
    result = estimate()
    by_name = {line.name: line for line in result.lines}
    runs = 30 * 24 * 60
    assert runs == 43_200

    requests = 4 * runs
    seconds = Decimal(3) * Decimal("0.3") + Decimal("0.4")
    gb_seconds = Decimal(128) / Decimal(1024) * Decimal(runs) * seconds
    lambda_list = Decimal(requests) * Decimal("0.0000002") + gb_seconds * Decimal("0.0000166667")
    assert by_name["Lambda"].list_usd == lambda_list
    assert by_name["Lambda"].after_usd == 0

    observations = 3 * runs
    rwrus = observations + observations * 2 // 3
    assert by_name["DynamoDB writes us-east-1"].list_usd == Decimal(rwrus) * Decimal("0.000000625")
    assert by_name["DynamoDB writes eu-west-1"].list_usd == Decimal(rwrus) * Decimal("0.000000705")
    # On-demand request units are not the provisioned 25 WCU allowance.
    writes = by_name["DynamoDB writes us-east-1"]
    assert writes.after_usd == writes.list_usd

    reads = Decimal("1.5") * Decimal(runs)
    assert by_name["DynamoDB reads us-east-1"].list_usd == reads * Decimal("0.000000125")

    # Storage is inside 25 GB, so the allowance removes it. PITR stays.
    storage = by_name["DynamoDB storage us-east-1"]
    assert storage.after_usd < storage.list_usd
    assert storage.after_usd > 0

    puts = Decimal(2 * runs) * Decimal("0.000005")
    gets = Decimal(2000) * Decimal("0.0000004")
    assert by_name["S3"].list_usd > puts + gets
    assert by_name["S3"].after_usd == by_name["S3"].list_usd

    assert by_name["CloudFront"].list_usd > 0
    assert by_name["CloudFront"].after_usd == 0
    assert by_name["SNS"].list_usd == 0


def test_allowances_are_not_applied_twice_and_timeout_is_only_lambda() -> None:
    base = estimate()
    ceiling = estimate(at_timeout())
    assert ceiling.list_usd > base.list_usd
    base_lambda = next(line for line in base.lines if line.name == "Lambda")
    ceiling_lambda = next(line for line in ceiling.lines if line.name == "Lambda")
    assert ceiling_lambda.list_usd > base_lambda.list_usd
    other = [
        (left, right)
        for left, right in zip(base.lines, ceiling.lines, strict=True)
        if left.name != "Lambda"
    ]
    assert all(left.list_usd == right.list_usd for left, right in other)
    # 20 seconds is enough GB-seconds to leave the 400,000 GB-second allowance.
    assert ceiling_lambda.after_usd > 0


def test_a_status_change_is_one_sns_request_with_no_delivery() -> None:
    result = estimate(Assumptions(status_changes=4))
    sns = next(line for line in result.lines if line.name == "SNS")
    assert sns.list_usd == Decimal(4) * Decimal("0.0000005")
    assert sns.after_usd == 0


def test_europe_viewers_use_the_other_price_class_100_rate() -> None:
    united_states = estimate(Assumptions(viewer="us"))
    europe = estimate(Assumptions(viewer="europe"))
    us_cdn = next(line for line in united_states.lines if line.name == "CloudFront")
    eu_cdn = next(line for line in europe.lines if line.name == "CloudFront")
    assert eu_cdn.list_usd > us_cdn.list_usd
    assert eu_cdn.after_usd == 0


def test_cost_doc_quotes_the_report() -> None:
    doc = Path("docs/COST.md").read_text()
    assert render_report() in doc


def test_format_usd_keeps_a_tenth_of_a_cent() -> None:
    assert format_usd(Decimal("0.43205")) == "$0.4321"
    assert format_usd(Decimal("0")) == "$0.0000"
