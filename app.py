#!/usr/bin/env python3
"""CDK app: one status table, a probe in each region, a quorum reader, and a status page.

The status page stack also has the topic for a quorum moving between up
and down. Nothing subscribes to it. GithubDeploy is the OIDC role a
manual workflow would assume to deploy these stacks. Synthesis does not
call AWS. Set CDK_DEFAULT_ACCOUNT to the target account; otherwise a
placeholder account is used so `cdk synth` runs with no credentials.
"""

from __future__ import annotations

import os

import aws_cdk as cdk
from infra.github_deploy import GithubDeployStack
from infra.status_data import TABLE_REGION, StatusDataStack
from infra.status_page import StatusPageStack
from infra.status_probe import StatusProbeStack
from infra.status_quorum import StatusQuorumStack

# Placeholder is the AWS documentation account. Synth never looks it up.
ACCOUNT = os.environ.get("CDK_DEFAULT_ACCOUNT", "123456789012")
DATA_REGION = TABLE_REGION
# Region is part of the stack's identity, so the loop interpolates it into
# the stack id on purpose. Resource ids inside the stack do not repeat it.
PROBE_REGIONS = ("us-west-2", "us-east-1", "eu-west-1")
ENDPOINTS = os.environ.get("PROBE_ENDPOINTS", "https://example.com")

app = cdk.App()

StatusDataStack(
    app,
    "StatusData",
    env=cdk.Environment(account=ACCOUNT, region=DATA_REGION),
    description="On-demand probe-results global table. Replicas in us-west-2 and eu-west-1.",
)

for region in PROBE_REGIONS:
    StatusProbeStack(
        app,
        f"StatusProbe-{region}",
        endpoints=ENDPOINTS,
        env=cdk.Environment(account=ACCOUNT, region=region),
        description=f"Minute HTTP probe in {region}.",
    )

StatusQuorumStack(
    app,
    "StatusQuorum",
    env=cdk.Environment(account=ACCOUNT, region=DATA_REGION),
    description="On-demand quorum read of probe-results in us-east-1.",
)

StatusPageStack(
    app,
    "StatusPage",
    endpoints=ENDPOINTS,
    env=cdk.Environment(account=ACCOUNT, region=DATA_REGION),
    description="Static status page in us-east-1. Private bucket, CloudFront, status topic.",
)

GithubDeployStack(
    app,
    "GithubDeploy",
    env=cdk.Environment(account=ACCOUNT, region=DATA_REGION),
    description="GitHub Actions OIDC role. Assumes the CDK bootstrap roles only.",
)

app.synth()
