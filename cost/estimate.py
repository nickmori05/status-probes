"""Monthly cost of the stacks in this repository.

The unit prices were read from AWS's public pricing pages on CHECKED.
Nothing in this repository is deployed, and this module does not call AWS.

Run from the repository root:

    python -m cost.estimate
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

CHECKED = date(2026, 11, 21)

# Pages the prices come from. Regional DynamoDB, Lambda, S3, and SNS figures
# that the page renders from its rate table were read from that table.
DYNAMODB_PRICING = "https://aws.amazon.com/dynamodb/pricing/"
DYNAMODB_RATES = (
    "https://b0.p.awsstatic.com/pricing/2.0/meteredUnitMaps/dynamodb/USD/current/dynamodb.json"
)
DYNAMODB_BILLING = (
    "https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/global-tables-billing.html"
)
DYNAMODB_SIZE = (
    "https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/CapacityUnitCalculations.html"
)
DYNAMODB_TTL = "https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/TTL.html"
LAMBDA_PRICING = "https://aws.amazon.com/lambda/pricing/"
LAMBDA_RATES = (
    "https://b0.p.awsstatic.com/pricing/2.0/meteredUnitMaps/lambda/USD/current/lambda.json"
)
S3_PRICING = "https://aws.amazon.com/s3/pricing/"
S3_RATES = "https://b0.p.awsstatic.com/pricing/2.0/meteredUnitMaps/s3/USD/current/s3.json"
CLOUDFRONT_PRICING = "https://aws.amazon.com/cloudfront/pricing/pay-as-you-go/"
SNS_PRICING = "https://aws.amazon.com/sns/pricing/"
SNS_RATES = "https://b0.p.awsstatic.com/pricing/2.0/meteredUnitMaps/sns/USD/current/sns.json"
EVENTBRIDGE_PRICING = "https://aws.amazon.com/eventbridge/pricing/"

# USD per request unit, from the rate tables above. us-east-1 and us-west-2
# match. eu-west-1 is the Ireland row, which the DynamoDB page labels EU (Ireland).
RWRU_USD = {
    "us-east-1": Decimal("0.0000006250"),
    "us-west-2": Decimal("0.0000006250"),
    "eu-west-1": Decimal("0.0000007050"),
}
RRU_USD = {
    "us-east-1": Decimal("0.0000001250"),
    "us-west-2": Decimal("0.0000001250"),
    "eu-west-1": Decimal("0.0000001415"),
}
STORAGE_USD_PER_GB = {
    "us-east-1": Decimal("0.25"),
    "us-west-2": Decimal("0.25"),
    "eu-west-1": Decimal("0.283"),
}
PITR_USD_PER_GB = {
    "us-east-1": Decimal("0.20"),
    "us-west-2": Decimal("0.20"),
    "eu-west-1": Decimal("0.22"),
}

# x86, first duration tier. The same figure in all three regions.
LAMBDA_REQUEST_USD = Decimal("0.0000002000")
LAMBDA_GB_SECOND_USD = Decimal("0.0000166667")
LAMBDA_FREE_REQUESTS = 1_000_000
LAMBDA_FREE_GB_SECONDS = Decimal(400_000)

# Standard class, us-east-1, where the bucket is. Per request, and per GB-month.
S3_PUT_USD = Decimal("0.000005")
S3_GET_USD = Decimal("0.0000004")
S3_STORAGE_USD_PER_GB = Decimal("0.023")

# Price Class 100 groups. Per 10,000 HTTPS requests, and per GB to the internet
# for the first paid 9 TB. The always-free allowance is account-wide.
CLOUDFRONT_HTTPS_PER_10K = {
    "us": Decimal("0.0100"),
    "europe": Decimal("0.0120"),
}
CLOUDFRONT_DATA_USD_PER_GB = Decimal("0.085")
CLOUDFRONT_FREE_REQUESTS = 10_000_000
CLOUDFRONT_FREE_GB = Decimal(1024)

# Standard topic API request, us-east-1. First 1 million requests a month are free.
SNS_REQUEST_USD = Decimal("0.0000005")
SNS_FREE_REQUESTS = 1_000_000

# DynamoDB Standard storage allowance, per Region, for the payer account.
DYNAMODB_FREE_STORAGE_GB = Decimal(25)

# Storage overhead on top of the attribute bytes. Request units do not add it.
ITEM_OVERHEAD_BYTES = 100
GLOBAL_TABLE_OVERHEAD_BYTES = 48

GIB = 1024**3
WRITE_BYTES = 1024
READ_BYTES = 4096
CENTS = Decimal("0.0001")


@dataclass(frozen=True)
class Assumptions:
    """Workload inputs. Fields that the CDK code fixes are noted in COST.md.

    Durations, page views, and status changes are not in the code. Nothing
    is deployed, so there is no measured duration and no traffic.
    """

    endpoints: int = 1
    regions: tuple[str, ...] = ("us-west-2", "us-east-1", "eu-west-1")
    days: int = 30
    schedule_minutes: int = 1
    memory_mb: int = 128
    probe_duration_ms: int = 300
    publisher_duration_ms: int = 400
    quorum_invocations: int = 0
    quorum_duration_ms: int = 0
    retention_days: int = 7
    freshness_minutes: int = 3
    page_views: int = 1000
    html_bytes: int = 2009
    json_bytes: int = 272
    status_changes: int = 0
    # Viewer edge group inside Price Class 100. "europe" uses the other rate.
    viewer: str = "us"
    # A quiet page: each view misses the 60-second cache and fetches both objects.
    origin_fetches_per_view: int = 2

    def invocations(self) -> int:
        """How many times one one-minute rule fires in the month."""
        return self.days * 24 * (60 // self.schedule_minutes)


@dataclass(frozen=True)
class Line:
    """One priced row. ``after_usd`` is the list price minus a documented allowance."""

    name: str
    list_usd: Decimal
    after_usd: Decimal


@dataclass(frozen=True)
class Estimate:
    assumptions: Assumptions
    lines: tuple[Line, ...]
    observation_bytes: int
    stored_observation_bytes: int

    @property
    def list_usd(self) -> Decimal:
        return sum((line.list_usd for line in self.lines), Decimal(0))

    @property
    def after_usd(self) -> Decimal:
        return sum((line.after_usd for line in self.lines), Decimal(0))


def at_timeout(assumptions: Assumptions | None = None) -> Assumptions:
    """Same workload, with every scheduled invocation billed for its full timeout.

    The probe and the publisher are configured for 20 seconds. The quorum
    function is not on a schedule, so its invocation count stays zero.
    """
    assumptions = assumptions or Assumptions()
    return replace(assumptions, probe_duration_ms=20_000, publisher_duration_ms=20_000)


def number_bytes(significant_digits: int) -> int:
    """Value bytes for a DynamoDB number. The name is not included."""
    return (significant_digits + 1) // 2 + 1


def significant_digits(value: str) -> int:
    """Digits left after DynamoDB trims leading and trailing zeroes."""
    return len(Decimal(value).normalize().as_tuple().digits)


def observation_item_bytes(
    *,
    url: str = "https://example.com",
    region: str = "us-east-1",
    status_code: str = "200",
    latency: str = "150.0",
    sort_key: str = "2026-11-21T13:23:01.250000Z#us-east-1",
) -> int:
    """Attribute bytes of one successful observation. Under 1 KB, so one write unit.

    ``error`` is null on success. The names and types are ``probe.writer.to_item``.
    """
    strings = {
        "pk": url,
        "sk": sort_key,
        "region": region,
    }
    size = sum(len(name) + len(value) for name, value in strings.items())
    size += len("status_code") + number_bytes(significant_digits(status_code))
    size += len("latency_ms") + number_bytes(significant_digits(latency))
    size += len("ttl") + number_bytes(10)
    size += len("ok") + 1
    size += len("error") + 1
    return size


def estimate(assumptions: Assumptions | None = None) -> Estimate:
    """Price one steady month of the default stack.

    A steady month has no status transition. The first up or down is stored
    and not published, and a later minute with the same status does not write
    that item again and does not publish. Reads still happen every minute.
    """
    assumptions = assumptions or Assumptions()
    if assumptions.viewer not in CLOUDFRONT_HTTPS_PER_10K:
        raise ValueError(f"viewer must be one of {sorted(CLOUDFRONT_HTTPS_PER_10K)}")
    item_bytes = observation_item_bytes()
    if item_bytes > WRITE_BYTES:
        raise ValueError("observation is over 1 KB; the write-unit count would change")
    stored = item_bytes + ITEM_OVERHEAD_BYTES + GLOBAL_TABLE_OVERHEAD_BYTES
    runs = assumptions.invocations()
    regions = assumptions.regions
    observations = len(regions) * assumptions.endpoints * runs

    lines = [
        _lambda_line(assumptions, runs),
        *_dynamodb_lines(assumptions, observations, stored),
        _s3_line(assumptions, runs),
        _cloudfront_line(assumptions),
        _sns_line(assumptions),
    ]
    return Estimate(
        assumptions=assumptions,
        lines=tuple(lines),
        observation_bytes=item_bytes,
        stored_observation_bytes=stored,
    )


def format_usd(amount: Decimal) -> str:
    """Dollars to a tenth of a cent, so a sub-cent line does not print as zero."""
    quantized = amount.quantize(CENTS, rounding=ROUND_HALF_UP)
    return f"${quantized}"


def render_report(result: Estimate | None = None) -> str:
    """The tables docs/COST.md quotes. Keep that file in step with this text."""
    result = result or estimate()
    assumptions = result.assumptions
    runs = assumptions.invocations()
    rows = [
        ("Probe count", f"{len(assumptions.regions)} functions, one per region"),
        ("Publisher", "1 function in us-east-1, same schedule"),
        ("Schedule", f"every {assumptions.schedule_minutes} minute, {assumptions.days}-day month"),
        ("Invocations per scheduled function", f"{runs:,}"),
        ("Memory", f"{assumptions.memory_mb} MB, x86"),
        ("Probe duration", f"{assumptions.probe_duration_ms} ms, assumed"),
        ("Publisher duration", f"{assumptions.publisher_duration_ms} ms, assumed"),
        ("Quorum invocations", f"{assumptions.quorum_invocations}, it has no schedule"),
        ("Endpoints", str(assumptions.endpoints)),
        ("Observation item", f"{result.observation_bytes} bytes, under 1 KB"),
        ("Stored observation", f"{result.stored_observation_bytes} bytes, with overhead"),
        ("Retention", f"{assumptions.retention_days} days"),
        ("Page views", f"{assumptions.page_views:,} a month, assumed"),
        ("Viewer group", f"{assumptions.viewer}, inside Price Class 100"),
        (
            "Page objects",
            (
                f"index.html {assumptions.html_bytes} bytes, "
                f"status.json {assumptions.json_bytes} bytes"
            ),
        ),
        ("S3 writes", "2 puts a minute, the page and the JSON"),
        (
            "S3 reads",
            f"{assumptions.origin_fetches_per_view} gets a page view, assumed cache misses",
        ),
        ("Status changes", f"{assumptions.status_changes} a month, assumed, no subscription"),
        ("Regions", ", ".join(assumptions.regions)),
    ]
    table = ["| Input | Value |", "| --- | --- |"]
    table.extend(f"| {name} | {value} |" for name, value in rows)
    priced = [
        "| Line | List price | After allowances |",
        "| --- | ---: | ---: |",
    ]
    for line in result.lines:
        priced.append(
            f"| {line.name} | {format_usd(line.list_usd)} | {format_usd(line.after_usd)} |"
        )
    list_total = format_usd(result.list_usd)
    after_total = format_usd(result.after_usd)
    priced.append(f"| **Total** | **{list_total}** | **{after_total}** |")
    ceiling = estimate(at_timeout(assumptions))
    return "\n".join(
        [
            f"Prices checked {CHECKED.isoformat()}.",
            "",
            *table,
            "",
            *priced,
            "",
            (
                f"If every scheduled invocation ran for its 20 second timeout, "
                f"the list price would be {format_usd(ceiling.list_usd)} "
                f"and the price after allowances would be {format_usd(ceiling.after_usd)}."
            ),
        ]
    )


def _lambda_line(assumptions: Assumptions, runs: int) -> Line:
    probes = len(assumptions.regions)
    requests = probes * runs + runs + assumptions.quorum_invocations
    gb = Decimal(assumptions.memory_mb) / Decimal(1024)
    probe_seconds = Decimal(assumptions.probe_duration_ms) / Decimal(1000)
    publisher_seconds = Decimal(assumptions.publisher_duration_ms) / Decimal(1000)
    quorum_seconds = Decimal(assumptions.quorum_duration_ms) / Decimal(1000)
    gb_seconds = gb * (
        Decimal(probes * runs) * probe_seconds
        + Decimal(runs) * publisher_seconds
        + Decimal(assumptions.quorum_invocations) * quorum_seconds
    )
    list_usd = Decimal(requests) * LAMBDA_REQUEST_USD + gb_seconds * LAMBDA_GB_SECOND_USD
    billable_requests = max(0, requests - LAMBDA_FREE_REQUESTS)
    billable_gb = max(Decimal(0), gb_seconds - LAMBDA_FREE_GB_SECONDS)
    after = Decimal(billable_requests) * LAMBDA_REQUEST_USD + billable_gb * LAMBDA_GB_SECOND_USD
    return Line("Lambda", list_usd, after)


def _dynamodb_lines(
    assumptions: Assumptions,
    observations: int,
    stored_bytes: int,
) -> list[Line]:
    # Each observation is one rWRU in every replica. A TTL delete is free in
    # the region where it expires and one rWRU in each other replica. This
    # assumes the delete starts in the region that wrote the item.
    others = len(assumptions.regions) - 1
    writes: list[Line] = []
    storage_lines: list[Line] = []
    kept = assumptions.retention_days * 24 * (60 // assumptions.schedule_minutes)
    items = len(assumptions.regions) * assumptions.endpoints * kept + assumptions.endpoints
    gb = Decimal(items * stored_bytes) / Decimal(GIB)
    for region in assumptions.regions:
        replicated_deletes = observations * others // len(assumptions.regions)
        rwrus = observations + replicated_deletes
        writes.append(
            Line(
                f"DynamoDB writes {region}",
                Decimal(rwrus) * RWRU_USD[region],
                Decimal(rwrus) * RWRU_USD[region],
            )
        )
        storage_list = gb * STORAGE_USD_PER_GB[region] + gb * PITR_USD_PER_GB[region]
        billable_gb = max(Decimal(0), gb - DYNAMODB_FREE_STORAGE_GB)
        storage_after = billable_gb * STORAGE_USD_PER_GB[region] + gb * PITR_USD_PER_GB[region]
        storage_lines.append(Line(f"DynamoDB storage {region}", storage_list, storage_after))

    reads = _read_rrus(assumptions)
    read_region = "us-east-1"
    read = Line(
        "DynamoDB reads us-east-1",
        reads * RRU_USD[read_region],
        reads * RRU_USD[read_region],
    )
    return [*writes, read, *storage_lines]


def _read_rrus(assumptions: Assumptions) -> Decimal:
    """Publisher queries and the consistent status read, once a minute per endpoint.

    The query is eventually consistent. Nine small items round up to one 4 KB
    page, which is half a read unit. The status read is strongly consistent
    and is one read unit. The quorum function is not on a schedule.
    """
    per_query = assumptions.freshness_minutes * len(assumptions.regions)
    per_query_bytes = Decimal(per_query * observation_item_bytes())
    # Round up to a whole 4 KB page, then half a unit for an eventual read.
    pages = (per_query_bytes + Decimal(READ_BYTES) - 1) // Decimal(READ_BYTES)
    eventual = pages * Decimal("0.5")
    consistent = Decimal(1)
    per_run = eventual + consistent
    return per_run * Decimal(assumptions.endpoints * assumptions.invocations())


def _s3_line(assumptions: Assumptions, runs: int) -> Line:
    puts = 2 * runs
    gets = assumptions.page_views * assumptions.origin_fetches_per_view
    stored = Decimal(assumptions.html_bytes + assumptions.json_bytes) / Decimal(GIB)
    amount = (
        Decimal(puts) * S3_PUT_USD
        + Decimal(gets) * S3_GET_USD
        + stored * S3_STORAGE_USD_PER_GB
    )
    return Line("S3", amount, amount)


def _cloudfront_line(assumptions: Assumptions) -> Line:
    requests = assumptions.page_views * 2
    body = assumptions.html_bytes + assumptions.json_bytes
    gb = Decimal(assumptions.page_views * body) / Decimal(GIB)
    list_usd = (
        Decimal(requests) * CLOUDFRONT_HTTPS_PER_10K[assumptions.viewer] / Decimal(10_000)
        + gb * CLOUDFRONT_DATA_USD_PER_GB
    )
    billable_requests = max(0, requests - CLOUDFRONT_FREE_REQUESTS)
    billable_gb = max(Decimal(0), gb - CLOUDFRONT_FREE_GB)
    after = (
        Decimal(billable_requests) * CLOUDFRONT_HTTPS_PER_10K[assumptions.viewer] / Decimal(10_000)
        + billable_gb * CLOUDFRONT_DATA_USD_PER_GB
    )
    return Line("CloudFront", list_usd, after)


def _sns_line(assumptions: Assumptions) -> Line:
    # One Publish is one request while the body is under 64 KB. No subscription,
    # so there is no delivery charge.
    requests = assumptions.status_changes
    list_usd = Decimal(requests) * SNS_REQUEST_USD
    billable = max(0, requests - SNS_FREE_REQUESTS)
    return Line("SNS", list_usd, Decimal(billable) * SNS_REQUEST_USD)


def main() -> None:
    print(render_report())


if __name__ == "__main__":
    main()
