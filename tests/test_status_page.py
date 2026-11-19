"""Synthesized status page: private bucket, OAC, the topic, and the publisher role."""

from __future__ import annotations

import json

import aws_cdk as cdk
from aws_cdk.assertions import Template
from infra.status_page import StatusPageStack

ACCOUNT = "123456789012"
TABLE_ARN = f"arn:aws:dynamodb:us-east-1:{ACCOUNT}:table/probe-results"


def _template() -> Template:
    app = cdk.App()
    stack = StatusPageStack(
        app,
        "StatusPage",
        endpoints="https://example.com,https://example.org",
        env=cdk.Environment(account=ACCOUNT, region="us-east-1"),
    )
    return Template.from_stack(stack)


def _text(value: object) -> str:
    """Render a synthesized token, filling the partition with ``aws``."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_text(part) for part in value)
    if isinstance(value, dict) and set(value) == {"Fn::Join"}:
        separator, parts = value["Fn::Join"]
        return separator.join(_text(part) for part in parts)
    if isinstance(value, dict) and set(value) == {"Fn::GetAtt"}:
        target = value["Fn::GetAtt"]
        if isinstance(target, list):
            return "${" + ".".join(str(part) for part in target) + "}"
        return "${" + str(target) + "}"
    if isinstance(value, dict) and set(value) == {"Ref"}:
        ref = value["Ref"]
        if ref == "AWS::Partition":
            return "aws"
        return "${" + str(ref) + "}"
    raise AssertionError(f"unexpected token shape: {value!r}")


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


def _actions(statement: dict[str, object]) -> list[str]:
    action = statement["Action"]
    if isinstance(action, str):
        return [action]
    return list(action)


def test_bucket_is_private_encrypted_and_ssl_only() -> None:
    template = _template()
    bucket_id, bucket = _one(template, "AWS::S3::Bucket")
    props = bucket["Properties"]

    assert props["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }
    encryption = props["BucketEncryption"]["ServerSideEncryptionConfiguration"]
    assert encryption == [{"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
    assert props["OwnershipControls"] == {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}
    assert "WebsiteConfiguration" not in props
    assert bucket["DeletionPolicy"] == "Delete"
    assert template.find_resources("AWS::KMS::Key") == {}

    _, policy = _one(template, "AWS::S3::BucketPolicy")
    statements = _statements(policy["Properties"]["PolicyDocument"])
    assert [statement["Effect"] for statement in statements] == ["Deny", "Allow"]
    deny, allow = statements
    assert deny["Action"] == "s3:*"
    assert deny["Principal"] == {"AWS": "*"}
    assert deny["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
    assert _actions(allow) == ["s3:GetObject"]
    assert allow["Principal"] == {"Service": "cloudfront.amazonaws.com"}
    assert _text(allow["Resource"]) == f"${{{bucket_id}.Arn}}/*"
    distribution_id, _distribution = _one(template, "AWS::CloudFront::Distribution")
    source = allow["Condition"]["StringEquals"]["AWS:SourceArn"]
    assert _text(source) == (
        f"arn:aws:cloudfront::${{AWS::AccountId}}:distribution/${{{distribution_id}}}"
    )
    public_allows = [
        statement
        for statement in statements
        if statement["Effect"] == "Allow" and statement["Principal"] in ("*", {"AWS": "*"})
    ]
    assert public_allows == []
    assert "ImportValue" not in json.dumps(template.to_json())


def test_distribution_reads_the_bucket_with_origin_access_control() -> None:
    template = _template()
    bucket_id, _bucket = _one(template, "AWS::S3::Bucket")
    oac_id, oac = _one(template, "AWS::CloudFront::OriginAccessControl")
    cache_id, cache = _one(template, "AWS::CloudFront::CachePolicy")
    _distribution_id, distribution = _one(template, "AWS::CloudFront::Distribution")

    oac_config = oac["Properties"]["OriginAccessControlConfig"]
    assert oac_config["OriginAccessControlOriginType"] == "s3"
    assert oac_config["SigningBehavior"] == "always"
    assert oac_config["SigningProtocol"] == "sigv4"
    assert template.find_resources("AWS::CloudFront::CloudFrontOriginAccessIdentity") == {}

    config = distribution["Properties"]["DistributionConfig"]
    assert config["DefaultRootObject"] == "index.html"
    assert config["Enabled"] is True
    assert config["PriceClass"] == "PriceClass_100"
    assert len(config["Origins"]) == 1
    origin = config["Origins"][0]
    assert origin["DomainName"] == {"Fn::GetAtt": [bucket_id, "RegionalDomainName"]}
    assert origin["OriginAccessControlId"] == {"Fn::GetAtt": [oac_id, "Id"]}
    assert origin["S3OriginConfig"] == {"OriginAccessIdentity": ""}
    assert "CustomOriginConfig" not in origin

    behavior = config["DefaultCacheBehavior"]
    assert behavior["ViewerProtocolPolicy"] == "redirect-to-https"
    assert behavior["AllowedMethods"] == ["GET", "HEAD"]
    assert behavior["CachePolicyId"] == {"Ref": cache_id}
    cache_config = cache["Properties"]["CachePolicyConfig"]
    assert cache_config["MinTTL"] == 0
    assert cache_config["DefaultTTL"] == 60
    assert cache_config["MaxTTL"] == 60
    forwarded = cache_config["ParametersInCacheKeyAndForwardedToOrigin"]
    assert forwarded["CookiesConfig"] == {"CookieBehavior": "none"}
    assert forwarded["HeadersConfig"] == {"HeaderBehavior": "none"}
    assert forwarded["QueryStringsConfig"] == {"QueryStringBehavior": "none"}
    assert forwarded["EnableAcceptEncodingGzip"] is True
    assert forwarded["EnableAcceptEncodingBrotli"] is True


def test_publisher_role_is_limited_to_the_page_the_table_and_the_topic() -> None:
    template = _template()
    bucket_id, _bucket = _one(template, "AWS::S3::Bucket")
    topic_id, _topic = _one(template, "AWS::SNS::Topic")
    _function_id, function = _one(template, "AWS::Lambda::Function")
    props = function["Properties"]
    assert props["Handler"] == "probe.publisher.handler"
    assert props["Runtime"] == "python3.12"
    assert props["Timeout"] == 20
    assert props["MemorySize"] == 128
    assert props["Environment"]["Variables"] == {
        "ENDPOINTS": "https://example.com,https://example.org",
        "TABLE_NAME": "probe-results",
        "TABLE_REGION": "us-east-1",
        "BUCKET_NAME": {"Ref": bucket_id},
        "TOPIC_ARN": {"Ref": topic_id},
    }

    _policy_id, policy = _one(template, "AWS::IAM::Policy")
    statements = _statements(policy["Properties"]["PolicyDocument"])
    by_sid = {statement["Sid"]: statement for statement in statements}
    assert set(by_sid) == {
        "PublishStatusObjects",
        "QueryProbeResults",
        "RecordStatusTransition",
        "PublishStatusChange",
    }

    publish = by_sid["PublishStatusObjects"]
    assert publish["Effect"] == "Allow"
    assert _actions(publish) == ["s3:PutObject"]
    assert sorted(_text(resource) for resource in publish["Resource"]) == [
        f"${{{bucket_id}.Arn}}/index.html",
        f"${{{bucket_id}.Arn}}/status.json",
    ]

    query = by_sid["QueryProbeResults"]
    assert query["Effect"] == "Allow"
    assert _actions(query) == ["dynamodb:Query"]
    assert _text(query["Resource"]) == TABLE_ARN

    record = by_sid["RecordStatusTransition"]
    assert record["Effect"] == "Allow"
    assert _actions(record) == ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
    assert _text(record["Resource"]) == TABLE_ARN

    change = by_sid["PublishStatusChange"]
    assert change["Effect"] == "Allow"
    assert _actions(change) == ["sns:Publish"]
    assert change["Resource"] == {"Ref": topic_id}

    _role_id, role = _one(template, "AWS::IAM::Role")
    managed = role["Properties"]["ManagedPolicyArns"]
    assert len(managed) == 1
    assert _text(managed[0]).endswith(":policy/service-role/AWSLambdaBasicExecutionRole")


def test_status_change_topic_has_no_subscription() -> None:
    template = _template()
    topic_id, topic = _one(template, "AWS::SNS::Topic")
    assert topic["Properties"]["TopicName"] == "status-changes"
    assert "KmsMasterKeyId" not in topic["Properties"]
    assert template.find_resources("AWS::SNS::Subscription") == {}
    assert template.find_resources("AWS::SNS::TopicPolicy") == {}

    _function_id, function = _one(template, "AWS::Lambda::Function")
    assert function["Properties"]["Environment"]["Variables"]["TOPIC_ARN"] == {"Ref": topic_id}
    _policy_id, policy = _one(template, "AWS::IAM::Policy")
    statements = _statements(policy["Properties"]["PolicyDocument"])
    by_sid = {statement["Sid"]: statement for statement in statements}
    publish = by_sid["PublishStatusChange"]
    assert _actions(publish) == ["sns:Publish"]
    assert publish["Resource"] == {"Ref": topic_id}
    other_sns = [
        action
        for statement in statements
        if statement["Sid"] != "PublishStatusChange"
        for action in _actions(statement)
        if action.startswith("sns:")
    ]
    assert other_sns == []


def test_publisher_runs_once_a_minute() -> None:
    template = _template()
    function_id, _function = _one(template, "AWS::Lambda::Function")
    _rule_id, rule = _one(template, "AWS::Events::Rule")
    props = rule["Properties"]
    assert props["ScheduleExpression"] == "rate(1 minute)"
    assert props["State"] == "ENABLED"
    assert props["Targets"] == [
        {"Arn": {"Fn::GetAtt": [function_id, "Arn"]}, "Id": "Target0"},
    ]

    _permission_id, permission = _one(template, "AWS::Lambda::Permission")
    assert permission["Properties"]["Principal"] == "events.amazonaws.com"
    assert permission["Properties"]["Action"] == "lambda:InvokeFunction"
    assert permission["Properties"]["FunctionName"] == {"Fn::GetAtt": [function_id, "Arn"]}
