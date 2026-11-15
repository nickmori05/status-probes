"""A regional probe: one Lambda and a one-minute EventBridge rule."""

from __future__ import annotations

from pathlib import Path

from aws_cdk import ArnFormat, Duration, RemovalPolicy, Stack
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

from infra.status_data import TABLE_NAME, TABLE_REGION

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
FUNCTION_NAME = "status-probe"


class StatusProbeStack(Stack):
    """Probe Lambda triggered every minute. ``ENDPOINTS`` is a comma-separated list.

    The function writes each observation to ``probe-results``. The table ARN
    is built from the table name, this stack's account, and us-east-1, so
    the stack does not import the table and can be deployed on its own.
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

        log_group = logs.LogGroup(
            self,
            "ProbeLogs",
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
        self.probe = lambda_.Function(
            self,
            "Probe",
            function_name=FUNCTION_NAME,
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="probe.handler.handler",
            code=lambda_.Code.from_asset(str(SRC_DIR), exclude=["**/__pycache__/**", "**/*.pyc"]),
            timeout=Duration.seconds(20),
            memory_size=128,
            environment={
                "ENDPOINTS": endpoints,
                "TABLE_NAME": TABLE_NAME,
                "TABLE_REGION": TABLE_REGION,
            },
            log_group=log_group,
            description=(
                "GET each ENDPOINTS URL, return the observation, and write it to probe-results."
            ),
        )
        # PutItem covers a single observation. BatchWriteItem covers the rest.
        # The table uses an AWS owned key, so this role has no KMS grant.
        self.probe.add_to_role_policy(
            iam.PolicyStatement(
                sid="WriteProbeResults",
                actions=["dynamodb:PutItem", "dynamodb:BatchWriteItem"],
                resources=[table_arn],
            )
        )

        rule = events.Rule(
            self,
            "EveryMinute",
            schedule=events.Schedule.rate(Duration.minutes(1)),
            description="Run the HTTP probe once a minute.",
        )
        rule.add_target(targets.LambdaFunction(self.probe))
