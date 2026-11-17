"""A quorum reader in us-east-1. It queries probe-results and does not run on a timer."""

from __future__ import annotations

from pathlib import Path

from aws_cdk import ArnFormat, Duration, RemovalPolicy, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

from infra.status_data import TABLE_NAME, TABLE_REGION

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
FUNCTION_NAME = "status-quorum"


class StatusQuorumStack(Stack):
    """Lambda that returns the quorum for one endpoint.

    The caller passes ``endpoint`` in the event. The role may call
    ``dynamodb:Query`` on the us-east-1 replica of ``probe-results`` and
    nothing else. The table ARN is built from the name, this stack's
    account, and us-east-1, so the stack does not import the table. Writes
    from the other regions are visible here after DynamoDB replicates
    them. There is no schedule: a timer would have nothing to publish to
    yet.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, **kwargs)

        log_group = logs.LogGroup(
            self,
            "QuorumLogs",
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
        self.reader = lambda_.Function(
            self,
            "Quorum",
            function_name=FUNCTION_NAME,
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="probe.reader.handler",
            code=lambda_.Code.from_asset(str(SRC_DIR), exclude=["**/__pycache__/**", "**/*.pyc"]),
            timeout=Duration.seconds(10),
            memory_size=128,
            environment={
                "TABLE_NAME": TABLE_NAME,
                "TABLE_REGION": TABLE_REGION,
            },
            log_group=log_group,
            description="Query probe-results and return whether a majority of regions agree.",
        )
        self.reader.add_to_role_policy(
            iam.PolicyStatement(
                sid="QueryProbeResults",
                actions=["dynamodb:Query"],
                resources=[table_arn],
            )
        )
