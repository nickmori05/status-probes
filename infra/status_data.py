"""The status table. Each region writes the replica in that region."""

from __future__ import annotations

from aws_cdk import RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from constructs import Construct

TABLE_NAME = "probe-results"
TABLE_REGION = "us-east-1"
# The other probe regions. us-east-1 is this stack's own replica.
REPLICA_REGIONS = ("us-west-2", "eu-west-1")
TTL_ATTRIBUTE = "ttl"


def _point_in_time_recovery() -> dynamodb.PointInTimeRecoverySpecification:
    return dynamodb.PointInTimeRecoverySpecification(
        point_in_time_recovery_enabled=True,
    )


class StatusDataStack(Stack):
    """On-demand ``probe-results`` global table.

    The stack is in us-east-1. Replicas in us-west-2 and eu-west-1 belong
    to the same table. ``pk`` is the endpoint, ``sk`` is the observation
    time and region, and ``ttl`` is the epoch second DynamoDB expires.
    Deletion protection and point-in-time recovery are set on every
    replica: DynamoDB does not copy those two settings from one region to
    another. The consistency mode is eventual. Probe stacks name this
    table themselves. They do not import it, so this stack can be deployed
    in any order relative to them.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, termination_protection=True, **kwargs)

        # TableV2 synthesizes AWS::DynamoDB::GlobalTable. The previous
        # construct synthesized AWS::DynamoDB::Table. Changing the resource
        # type replaces the table. Nothing is deployed, so there is no data
        # to move.
        self.table = dynamodb.TableV2(
            self,
            "ProbeResults",
            table_name=TABLE_NAME,
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing=dynamodb.Billing.on_demand(),
            time_to_live_attribute=TTL_ATTRIBUTE,
            removal_policy=RemovalPolicy.RETAIN,
            deletion_protection=True,
            point_in_time_recovery_specification=_point_in_time_recovery(),
            multi_region_consistency=dynamodb.MultiRegionConsistency.EVENTUAL,
            replicas=[_replica(region) for region in REPLICA_REGIONS],
        )


def _replica(region: str) -> dynamodb.ReplicaTableProps:
    """One replica, with the settings DynamoDB does not synchronize."""
    return dynamodb.ReplicaTableProps(
        region=region,
        deletion_protection=True,
        point_in_time_recovery_specification=_point_in_time_recovery(),
    )
