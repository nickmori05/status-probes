"""The single status table. Probes in every region write their observations here."""

from __future__ import annotations

from aws_cdk import RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from constructs import Construct

TABLE_NAME = "probe-results"
TABLE_REGION = "us-east-1"
TTL_ATTRIBUTE = "ttl"


class StatusDataStack(Stack):
    """On-demand ``probe-results`` table in us-east-1, with a TTL attribute.

    ``pk`` is the endpoint, ``sk`` is the observation time and region, and
    ``ttl`` is the epoch second DynamoDB expires. Probe stacks name this
    table themselves. They do not import it, so this stack can be deployed
    in any order relative to them.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, termination_protection=True, **kwargs)

        self.table = dynamodb.Table(
            self,
            "ProbeResults",
            table_name=TABLE_NAME,
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute=TTL_ATTRIBUTE,
            removal_policy=RemovalPolicy.RETAIN,
            deletion_protection=True,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
        )
