"""A regional probe: one Lambda and a one-minute EventBridge rule."""

from __future__ import annotations

from pathlib import Path

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
FUNCTION_NAME = "status-probe"


class StatusProbeStack(Stack):
    """Probe Lambda triggered every minute. ``ENDPOINTS`` is a comma-separated list."""

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
        self.probe = lambda_.Function(
            self,
            "Probe",
            function_name=FUNCTION_NAME,
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="probe.handler.handler",
            code=lambda_.Code.from_asset(str(SRC_DIR), exclude=["**/__pycache__/**", "**/*.pyc"]),
            timeout=Duration.seconds(20),
            memory_size=128,
            environment={"ENDPOINTS": endpoints},
            log_group=log_group,
            description="GET each ENDPOINTS URL and return status code and latency.",
        )

        rule = events.Rule(
            self,
            "EveryMinute",
            schedule=events.Schedule.rate(Duration.minutes(1)),
            description="Run the HTTP probe once a minute.",
        )
        rule.add_target(targets.LambdaFunction(self.probe))
