"""The role a GitHub Actions job assumes to deploy this app.

The provider is GitHub's OIDC issuer. The role trusts one subject: this
repository's production environment. It may assume the CDK bootstrap
roles and nothing else. Nothing here is deployed.
"""

from __future__ import annotations

from aws_cdk import Stack
from aws_cdk import aws_iam as iam
from constructs import Construct

GITHUB_OIDC_URL = "https://token.actions.githubusercontent.com"
GITHUB_OIDC_HOST = "token.actions.githubusercontent.com"
# The audience configure-aws-credentials requests. It is also the provider's client id.
GITHUB_AUDIENCE = "sts.amazonaws.com"

# Created 2026-11-15, after GitHub's 2026-07-15 cutoff, so the subject is the
# immutable form. The API reports use_immutable_subject and this prefix.
# repo:nickmori05/status-probes:... is a different string and does not match.
GITHUB_OWNER = "nickmori05"
GITHUB_OWNER_ID = "314151628"
GITHUB_REPO = "status-probes"
GITHUB_REPO_ID = "1458305886"
GITHUB_ENVIRONMENT = "production"
GITHUB_SUBJECT = (
    f"repo:{GITHUB_OWNER}@{GITHUB_OWNER_ID}/{GITHUB_REPO}@{GITHUB_REPO_ID}"
    f":environment:{GITHUB_ENVIRONMENT}"
)
# A job that names an environment does not put the branch in sub.
# ref is a separate claim. Both are required, and they are not alternatives.
GITHUB_REF = "refs/heads/main"

ROLE_NAME = "github-status-probes-deploy"
# DefaultStackSynthesizer.DEFAULT_QUALIFIER. Bootstrap role names use it.
CDK_QUALIFIER = "hnb659fds"
# Zip assets, not images. The CloudFormation execution role is not here:
# CloudFormation assumes that one, and a normal bootstrap makes it an admin.
BOOTSTRAP_ROLE_KINDS = ("deploy", "file-publishing", "lookup")
DEPLOY_REGIONS = ("us-east-1", "us-west-2", "eu-west-1")


class GithubDeployStack(Stack):
    """OIDC provider and the deploy role for this repository.

    The trust policy is ``StringEquals`` on the audience and on one subject,
    ``repo:nickmori05@314151628/status-probes@1458305886:environment:production``.
    ``StringLike`` is not used. A ``*`` inside ``StringEquals`` is a literal
    character, so it matches no token. ``StringLike`` would treat that ``*``
    as a wildcard, and ``repo:<repo>:*`` also matches a pull request. A job
    in the production environment does not send ``ref:refs/heads/main`` as
    its subject. The branch is the separate ``ref`` claim, and the policy
    requires ``refs/heads/main``. ``repository_id`` and ``repository_owner_id``
    are the same ids the subject carries. ``ForAllValues`` is not used: it
    matches when the key is missing.

    The role may ``sts:AssumeRole`` and ``sts:TagSession`` on the CDK
    deploy, file-publishing, and lookup roles in us-east-1, us-west-2, and
    eu-west-1. The qualifier is ``hnb659fds``. It may not assume the
    CloudFormation execution role or the image-publishing role. The provider
    does not pin a certificate thumbprint. IAM reads the current intermediate
    and checks this issuer with its trusted roots.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, **kwargs)

        provider = iam.OidcProviderNative(
            self,
            "GitHubActions",
            url=GITHUB_OIDC_URL,
            client_ids=[GITHUB_AUDIENCE],
        )
        # OpenIdConnectPrincipal stores aud under StringEquals, then spreads
        # this map over that one. A StringEquals passed here replaces aud,
        # so aud is included again in the same map.
        principal = iam.OpenIdConnectPrincipal(
            provider,
            {
                "StringEquals": {
                    f"{GITHUB_OIDC_HOST}:aud": GITHUB_AUDIENCE,
                    f"{GITHUB_OIDC_HOST}:sub": GITHUB_SUBJECT,
                    f"{GITHUB_OIDC_HOST}:ref": GITHUB_REF,
                    f"{GITHUB_OIDC_HOST}:environment": GITHUB_ENVIRONMENT,
                    f"{GITHUB_OIDC_HOST}:repository_id": GITHUB_REPO_ID,
                    f"{GITHUB_OIDC_HOST}:repository_owner_id": GITHUB_OWNER_ID,
                }
            },
        )
        self.role = iam.Role(
            self,
            "DeployRole",
            role_name=ROLE_NAME,
            description="Assumed by GitHub Actions to deploy this repo. CDK bootstrap roles only.",
            assumed_by=principal,
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="AssumeCdkBootstrapRoles",
                actions=["sts:AssumeRole", "sts:TagSession"],
                resources=[
                    (
                        f"arn:aws:iam::{self.account}:role/"
                        f"cdk-{CDK_QUALIFIER}-{kind}-role-{self.account}-{region}"
                    )
                    for kind in BOOTSTRAP_ROLE_KINDS
                    for region in DEPLOY_REGIONS
                ],
            )
        )
