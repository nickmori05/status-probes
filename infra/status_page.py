"""A private status page in us-east-1.

The publisher runs once a minute, queries probe-results, and writes
``index.html`` and ``status.json``. The bucket is not public. CloudFront
reads it with origin access control.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import ArnFormat, Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from constructs import Construct

from infra.status_data import TABLE_NAME, TABLE_REGION

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
FUNCTION_NAME = "status-publisher"
# The same keys and max-age as probe.publisher. This stack does not import
# that module: synth runs app.py, and the probe package lives under src/.
STATUS_KEY = "status.json"
PAGE_KEY = "index.html"
CACHE_SECONDS = 60


class StatusPageStack(Stack):
    """Scheduled publisher, a private bucket, and a CloudFront distribution.

    ``ENDPOINTS`` is the same comma-separated list the probes use. The role
    may call ``s3:PutObject`` on ``status.json`` and ``index.html`` only,
    and ``dynamodb:Query`` on the us-east-1 replica of ``probe-results``.
    The table ARN is built from the name, this stack's account, and
    us-east-1, so the stack does not import the table. The bucket blocks
    public access, encrypts with an S3-managed key, and denies requests
    that are not SSL. CloudFront is the only reader, and it may only
    ``s3:GetObject``.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        endpoints: str,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        bucket = s3.Bucket(
            self,
            "StatusSite",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            removal_policy=RemovalPolicy.DESTROY,
        )
        # min 0 so an object can say "do not cache". max 60 so CloudFront
        # never holds the page longer than the publish interval.
        cache_policy = cloudfront.CachePolicy(
            self,
            "StatusCache",
            comment="Hold the status page for at most one minute.",
            min_ttl=Duration.seconds(0),
            default_ttl=Duration.seconds(CACHE_SECONDS),
            max_ttl=Duration.seconds(CACHE_SECONDS),
            enable_accept_encoding_gzip=True,
            enable_accept_encoding_brotli=True,
        )
        self.distribution = cloudfront.Distribution(
            self,
            "StatusDistribution",
            comment="Static status page. The bucket is private.",
            default_root_object=PAGE_KEY,
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(
                    bucket,
                    origin_access_levels=[cloudfront.AccessLevel.READ],
                ),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cache_policy,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD,
            ),
        )

        log_group = logs.LogGroup(
            self,
            "PublisherLogs",
            log_group_name=f"/aws/lambda/{FUNCTION_NAME}",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )
        table_arn = self.format_arn(
            service="dynamodb",
            region=TABLE_REGION,
            resource="table",
            resource_name=TABLE_NAME,
            arn_format=ArnFormat.SLASH_RESOURCE_NAME,
        )
        self.publisher = lambda_.Function(
            self,
            "Publisher",
            function_name=FUNCTION_NAME,
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="probe.publisher.handler",
            code=lambda_.Code.from_asset(str(SRC_DIR), exclude=["**/__pycache__/**", "**/*.pyc"]),
            timeout=Duration.seconds(20),
            memory_size=128,
            environment={
                "ENDPOINTS": endpoints,
                "TABLE_NAME": TABLE_NAME,
                "TABLE_REGION": TABLE_REGION,
                "BUCKET_NAME": bucket.bucket_name,
            },
            log_group=log_group,
            description="Query probe-results and publish status.json and index.html.",
        )
        # SSE-S3 needs no KMS grant. PutObject does not cover the bucket
        # ARN, only the two keys the handler writes.
        self.publisher.add_to_role_policy(
            iam.PolicyStatement(
                sid="PublishStatusObjects",
                actions=["s3:PutObject"],
                resources=[
                    bucket.arn_for_objects(STATUS_KEY),
                    bucket.arn_for_objects(PAGE_KEY),
                ],
            )
        )
        self.publisher.add_to_role_policy(
            iam.PolicyStatement(
                sid="QueryProbeResults",
                actions=["dynamodb:Query"],
                resources=[table_arn],
            )
        )

        rule = events.Rule(
            self,
            "EveryMinute",
            schedule=events.Schedule.rate(Duration.minutes(1)),
            description="Publish the status page once a minute.",
        )
        rule.add_target(targets.LambdaFunction(self.publisher))
