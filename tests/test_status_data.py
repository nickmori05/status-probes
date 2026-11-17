"""Synthesized status table: replicas, TTL, and retention. No AWS calls."""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk.assertions import Template
from infra.status_data import StatusDataStack

ACCOUNT = "123456789012"
REPLICA_REGIONS = {"us-east-1", "us-west-2", "eu-west-1"}


def _stack() -> StatusDataStack:
    app = cdk.App()
    return StatusDataStack(
        app,
        "StatusData",
        env=cdk.Environment(account=ACCOUNT, region="us-east-1"),
    )


def _table(template: Template) -> dict[str, object]:
    tables = template.find_resources("AWS::DynamoDB::GlobalTable")
    assert len(tables) == 1
    assert template.find_resources("AWS::DynamoDB::Table") == {}
    return next(iter(tables.values()))


def test_probe_results_has_a_replica_in_each_probe_region() -> None:
    resource = _table(Template.from_stack(_stack()))
    props = resource["Properties"]
    assert props["TableName"] == "probe-results"
    assert props["BillingMode"] == "PAY_PER_REQUEST"
    assert props["MultiRegionConsistency"] == "EVENTUAL"
    assert props["KeySchema"] == [
        {"AttributeName": "pk", "KeyType": "HASH"},
        {"AttributeName": "sk", "KeyType": "RANGE"},
    ]
    assert props["TimeToLiveSpecification"] == {"AttributeName": "ttl", "Enabled": True}
    assert props["StreamSpecification"] == {"StreamViewType": "NEW_AND_OLD_IMAGES"}
    assert "WitnessRegion" not in props

    replicas = props["Replicas"]
    assert {replica["Region"] for replica in replicas} == REPLICA_REGIONS
    for replica in replicas:
        assert replica["DeletionProtectionEnabled"] is True
        assert replica["PointInTimeRecoverySpecification"] == {
            "PointInTimeRecoveryEnabled": True,
        }

    assert resource["DeletionPolicy"] == "Retain"
    assert resource["UpdateReplacePolicy"] == "Retain"


def test_status_data_stack_is_termination_protected() -> None:
    assert _stack().termination_protection is True
