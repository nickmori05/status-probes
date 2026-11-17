"""Synthesized probe stack: the local replica's env vars and write policy. No AWS calls."""

from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from infra.status_probe import StatusProbeStack

ACCOUNT = "123456789012"
PROBE_REGIONS = ("us-west-2", "us-east-1", "eu-west-1")


def _stack(region: str, endpoints: str = "https://example.com") -> Template:
    app = cdk.App()
    stack = StatusProbeStack(
        app,
        f"StatusProbe-{region}",
        endpoints=endpoints,
        env=cdk.Environment(account=ACCOUNT, region=region),
    )
    return Template.from_stack(stack)


def _arn_text(value: object) -> str:
    """Render a synthesized ARN, filling the partition token with ``aws``."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_arn_text(part) for part in value)
    if isinstance(value, dict) and set(value) == {"Fn::Join"}:
        separator, parts = value["Fn::Join"]
        return separator.join(
            "aws" if part == {"Ref": "AWS::Partition"} else _arn_text(part) for part in parts
        )
    raise AssertionError(f"unexpected ARN shape: {value!r}")


def _statements(template: Template) -> list[dict[str, object]]:
    policies = template.find_resources("AWS::IAM::Policy")
    assert len(policies) == 1
    document = next(iter(policies.values()))["Properties"]["PolicyDocument"]
    statement = document["Statement"]
    if isinstance(statement, dict):
        return [statement]
    return list(statement)


@pytest.mark.parametrize("region", PROBE_REGIONS)
def test_probe_writes_the_replica_in_its_own_region(region: str) -> None:
    template = _stack(region, endpoints="https://example.com,https://example.org")

    functions = template.find_resources("AWS::Lambda::Function")
    assert len(functions) == 1
    variables = next(iter(functions.values()))["Properties"]["Environment"]["Variables"]
    assert variables == {
        "ENDPOINTS": "https://example.com,https://example.org",
        "TABLE_NAME": "probe-results",
        "TABLE_REGION": region,
    }

    statements = _statements(template)
    assert len(statements) == 1
    statement = statements[0]
    assert statement["Effect"] == "Allow"
    assert statement["Sid"] == "WriteProbeResults"
    assert sorted(statement["Action"]) == ["dynamodb:BatchWriteItem", "dynamodb:PutItem"]
    assert _arn_text(statement["Resource"]) == (
        f"arn:aws:dynamodb:{region}:{ACCOUNT}:table/probe-results"
    )
    assert "ImportValue" not in json.dumps(template.to_json())


def test_probe_role_keeps_the_basic_execution_policy() -> None:
    template = _stack("us-west-2")
    roles = template.find_resources("AWS::IAM::Role")
    assert len(roles) == 1
    managed = next(iter(roles.values()))["Properties"]["ManagedPolicyArns"]
    assert len(managed) == 1
    assert _arn_text(managed[0]).endswith(":policy/service-role/AWSLambdaBasicExecutionRole")
