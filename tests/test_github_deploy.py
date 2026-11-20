"""Synthesized GitHub deploy role: OIDC trust and bootstrap assume permissions."""

from __future__ import annotations

import json

import aws_cdk as cdk
from aws_cdk.assertions import Template
from infra.github_deploy import GithubDeployStack

ACCOUNT = "123456789012"
SUBJECT = "repo:nickmori05@314151628/status-probes@1458305886:environment:production"
# The form GitHub used before 2026-07-15. This repository does not emit it.
NAME_ONLY_SUBJECT = "repo:nickmori05/status-probes:environment:production"
REGIONS = ("us-east-1", "us-west-2", "eu-west-1")
ROLE_KINDS = ("deploy", "file-publishing", "lookup")


def _template() -> Template:
    app = cdk.App()
    stack = GithubDeployStack(
        app,
        "GithubDeploy",
        env=cdk.Environment(account=ACCOUNT, region="us-east-1"),
    )
    return Template.from_stack(stack)


def _one(template: Template, resource_type: str) -> tuple[str, dict[str, object]]:
    found = template.find_resources(resource_type)
    assert len(found) == 1, resource_type
    logical_id, resource = next(iter(found.items()))
    return logical_id, resource


def _statements(document: dict[str, object]) -> list[dict[str, object]]:
    statement = document["Statement"]
    if isinstance(statement, dict):
        return [statement]
    return list(statement)


def _bootstrap_arns() -> list[str]:
    return [
        f"arn:aws:iam::{ACCOUNT}:role/cdk-hnb659fds-{kind}-role-{ACCOUNT}-{region}"
        for kind in ROLE_KINDS
        for region in REGIONS
    ]


def test_provider_is_github_actions_without_a_pinned_thumbprint() -> None:
    template = _template()
    _provider_id, provider = _one(template, "AWS::IAM::OIDCProvider")
    props = provider["Properties"]
    assert props["Url"] == "https://token.actions.githubusercontent.com"
    assert props["ClientIdList"] == ["sts.amazonaws.com"]
    assert "ThumbprintList" not in props
    assert template.find_resources("Custom::AWSCDKOpenIdConnectProvider") == {}


def test_trust_policy_requires_the_immutable_production_subject() -> None:
    template = _template()
    provider_id, _provider = _one(template, "AWS::IAM::OIDCProvider")
    _role_id, role = _one(template, "AWS::IAM::Role")
    props = role["Properties"]
    assert props["RoleName"] == "github-status-probes-deploy"
    assert "ManagedPolicyArns" not in props

    statements = _statements(props["AssumeRolePolicyDocument"])
    assert len(statements) == 1
    trust = statements[0]
    assert trust["Effect"] == "Allow"
    assert trust["Action"] == "sts:AssumeRoleWithWebIdentity"
    assert trust["Principal"] == {"Federated": {"Ref": provider_id}}
    # One operator. StringLike would treat a * as a wildcard, and a wildcard
    # subject also matches a pull request. A * inside StringEquals is literal
    # and matches nothing. ForAllValues matches when the key is absent.
    assert set(trust["Condition"]) == {"StringEquals"}
    assert trust["Condition"]["StringEquals"] == {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": SUBJECT,
        "token.actions.githubusercontent.com:ref": "refs/heads/main",
        "token.actions.githubusercontent.com:environment": "production",
        "token.actions.githubusercontent.com:repository_id": "1458305886",
        "token.actions.githubusercontent.com:repository_owner_id": "314151628",
    }
    assert "*" not in SUBJECT
    rendered = json.dumps(template.to_json())
    assert "StringLike" not in rendered
    assert "ForAllValues" not in rendered
    assert "ForAnyValue" not in rendered
    assert NAME_ONLY_SUBJECT not in rendered
    assert "repo:nickmori05/status-probes:" not in rendered
    # The branch is not part of sub. A job in an environment sends one subject.
    assert ":ref:" not in SUBJECT
    assert SUBJECT.endswith(":environment:production")


def test_role_assumes_only_the_cdk_bootstrap_roles() -> None:
    template = _template()
    _policy_id, policy = _one(template, "AWS::IAM::Policy")
    statements = _statements(policy["Properties"]["PolicyDocument"])
    assert len(statements) == 1
    statement = statements[0]
    assert statement["Sid"] == "AssumeCdkBootstrapRoles"
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == ["sts:AssumeRole", "sts:TagSession"]
    assert statement["Resource"] == _bootstrap_arns()
    assert len(statement["Resource"]) == 9

    rendered = json.dumps(template.to_json())
    assert "AdministratorAccess" not in rendered
    assert "cfn-exec" not in rendered
    assert "image-publishing" not in rendered
    assert '"*"' not in rendered
    assert "iam:*" not in rendered
    assert "cloudformation:" not in rendered
    assert "s3:" not in rendered
