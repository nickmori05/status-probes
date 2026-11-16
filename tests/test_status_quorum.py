"""Synthesized quorum stack: table env vars and the query policy. No AWS calls."""

from __future__ import annotations

import json

import aws_cdk as cdk
from aws_cdk.assertions import Template
from infra.status_quorum import StatusQuorumStack

ACCOUNT = "123456789012"
TABLE_ARN = f"arn:aws:dynamodb:us-east-1:{ACCOUNT}:table/probe-results"


def _template() -> Template:
    app = cdk.App()
    stack = StatusQuorumStack(
        app,
        "StatusQuorum",
        env=cdk.Environment(account=ACCOUNT, region="us-east-1"),
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


def _actions(statement: dict[str, object]) -> list[str]:
    action = statement["Action"]
    if isinstance(action, str):
        return [action]
    return list(action)


def test_quorum_may_only_query_the_table() -> None:
    template = _template()

    functions = template.find_resources("AWS::Lambda::Function")
    assert len(functions) == 1
    function = next(iter(functions.values()))["Properties"]
    assert function["Handler"] == "probe.reader.handler"
    assert function["Runtime"] == "python3.12"
    assert function["Environment"]["Variables"] == {
        "TABLE_NAME": "probe-results",
        "TABLE_REGION": "us-east-1",
    }

    statements = _statements(template)
    assert len(statements) == 1
    statement = statements[0]
    assert statement["Effect"] == "Allow"
    assert statement["Sid"] == "QueryProbeResults"
    assert _actions(statement) == ["dynamodb:Query"]
    assert _arn_text(statement["Resource"]) == TABLE_ARN
    assert template.find_resources("AWS::Events::Rule") == {}
    assert "ImportValue" not in json.dumps(template.to_json())


def test_quorum_role_keeps_the_basic_execution_policy() -> None:
    template = _template()
    roles = template.find_resources("AWS::IAM::Role")
    assert len(roles) == 1
    managed = next(iter(roles.values()))["Properties"]["ManagedPolicyArns"]
    assert len(managed) == 1
    assert _arn_text(managed[0]).endswith(":policy/service-role/AWSLambdaBasicExecutionRole")
