"""Shared AWS plumbing for the AWS-backed MCP servers and the world simulator.

Target selection, explicit on purpose so a dev laptop never touches real AWS by accident:

    AGENTGUARD_AWS_TARGET=localstack   (default) http://localhost:4566, dummy credentials
    AGENTGUARD_AWS_TARGET=aws          real AWS endpoints + your normal AWS credentials
                                       (tests use this inside moto's mock_aws)

The server code itself is plain boto3; switching targets changes no code.
"""

from __future__ import annotations

import functools
import os
import threading
from typing import Any

import boto3
from botocore.exceptions import ClientError
from mcp.server.mcpserver.exceptions import ToolError

PROJECT_TAG = {"Key": "Project", "Value": "agentguard"}   # every resource we create carries this
METRIC_NAMESPACE = "AgentGuard/App"
LIVE_STATES = ["pending", "running"]

_lock = threading.Lock()


def target() -> str:
    value = os.environ.get("AGENTGUARD_AWS_TARGET", "localstack")
    if value not in ("localstack", "aws"):
        raise ValueError(f"AGENTGUARD_AWS_TARGET must be 'localstack' or 'aws', got {value!r}")
    return value


@functools.cache
def client(service: str) -> Any:
    # boto3's default session isn't thread-safe while creating clients; MCPServer
    # runs sync tools in worker threads. Created clients themselves are thread-safe.
    with _lock:
        if target() == "aws":
            return boto3.client(service)
        return boto3.client(
            service,
            endpoint_url=os.environ.get("LOCALSTACK_URL", "http://localhost:4566"),
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )


def reset_clients() -> None:
    """Tests: drop cached clients so new ones are created inside moto's mock."""
    client.cache_clear()


class AwsError(ToolError):
    """A readable AWS error for the agent, e.g. 'BucketNotEmpty: The bucket you tried...'.

    Must be a ToolError: MCPServer replaces the message of any other exception
    with a generic "Error executing tool", and the model would learn nothing.
    """


def aws_errors(fn):
    """Turn botocore ClientError into a short message the LLM can act on."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ClientError as e:
            err = e.response.get("Error", {})
            raise AwsError(f"{err.get('Code', 'AwsError')}: {err.get('Message', str(e))}") from None
    return wrapper


def tags_of(resource: dict) -> dict[str, str]:
    return {t["Key"]: t["Value"] for t in resource.get("Tags", [])}


def live_instances(service: str | None = None) -> list[dict]:
    """Our pending/running EC2 instances, optionally only those of one service."""
    filters = [
        {"Name": "tag:Project", "Values": [PROJECT_TAG["Value"]]},
        {"Name": "instance-state-name", "Values": LIVE_STATES},
    ]
    if service:
        filters.append({"Name": "tag:Service", "Values": [service]})
    pages = client("ec2").get_paginator("describe_instances").paginate(Filters=filters)
    instances = [i for page in pages for r in page["Reservations"] for i in r["Instances"]]
    # Oldest first. LaunchTime has 1 s resolution, so instances launched together
    # tie; break ties by Name (api-1 < api-2 < api-scaled) so order is deterministic.
    return sorted(instances, key=lambda i: (i["LaunchTime"], tags_of(i).get("Name", ""), i["InstanceId"]))


def log_group(service: str) -> str:
    return f"/agentguard/app/{service}"
