"""Cloud MCP server on real AWS APIs (boto3). Runs against LocalStack by default.

Same tool names as the mock (servers/cloud_server.py), so policies and the agent
don't change; only the behaviour behind them becomes real:
  - S3 refuses to delete a non-empty bucket (BucketNotEmpty)
  - EC2 instances have real-format ids and states (pending -> running)

A "service" is the set of EC2 instances tagged Service=<name>. scale_service
launches or terminates tagged instances to reach the target count, which is what
an Auto Scaling group does underneath (Auto Scaling isn't in LocalStack's free plan).

Like real AWS, this server has no safety logic. That's AgentGuard's job.

Run standalone over stdio:  python -m servers.aws_cloud_server
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from servers.aws import PROJECT_TAG, AwsError, aws_errors, client, live_instances, tags_of

mcp = MCPServer("cloud", instructions="Cloud provider (AWS API). Instances, services, buckets.")

MAX_REPLICAS = 20


def _describe(instance: dict) -> dict:
    tags = tags_of(instance)
    return {
        "name": tags.get("Name", ""),
        "service": tags.get("Service", ""),
        "type": instance["InstanceType"],
        "state": instance["State"]["Name"],
    }


def _tags(**extra: str) -> list[dict]:
    tags = [PROJECT_TAG, *({"Key": k, "Value": v} for k, v in extra.items())]
    return [{"ResourceType": "instance", "Tags": tags}]


@mcp.tool()
@aws_errors
def list_services() -> dict:
    """List services with their instance count and instance ids."""
    services: dict[str, dict] = {}
    for inst in live_instances():
        name = tags_of(inst).get("Service")
        if name:
            svc = services.setdefault(name, {"instances": 0, "instance_ids": []})
            svc["instances"] += 1
            svc["instance_ids"].append(inst["InstanceId"])
    return services


@mcp.tool()
@aws_errors
def list_instances() -> dict:
    """List running and pending compute instances."""
    return {i["InstanceId"]: _describe(i) for i in live_instances()}


@mcp.tool()
@aws_errors
def list_buckets() -> dict:
    """List storage buckets with object count and size in MB."""
    s3 = client("s3")
    result = {}
    for bucket in s3.list_buckets()["Buckets"]:
        name = bucket["Name"]
        objects = [o for page in s3.get_paginator("list_objects_v2").paginate(Bucket=name)
                   for o in page.get("Contents", [])]
        result[name] = {"objects": len(objects), "size_mb": round(sum(o["Size"] for o in objects) / 1e6, 2)}
    return result


@mcp.tool()
@aws_errors
def scale_service(service: str, replicas: int) -> str:
    """Set how many instances a service runs. More instances lower latency."""
    if not 1 <= replicas <= MAX_REPLICAS:
        raise AwsError(f"ValidationError: replicas must be between 1 and {MAX_REPLICAS}")
    current = live_instances(service)
    if not current:
        raise AwsError(f"NotFound: no such service: {service}")
    before = len(current)
    ec2 = client("ec2")
    if replicas > before:
        # New instances copy an existing one, like a launch template would.
        template = current[0]
        ec2.run_instances(
            ImageId=template["ImageId"], InstanceType=template["InstanceType"],
            MinCount=replicas - before, MaxCount=replicas - before,
            TagSpecifications=_tags(Service=service, Name=f"{service}-scaled"),
        )
    elif replicas < before:
        newest_first = [i["InstanceId"] for i in reversed(current)]
        ec2.terminate_instances(InstanceIds=newest_first[: before - replicas])
    return f"{service} scaled {before} -> {replicas} instances"


@mcp.tool()
@aws_errors
def create_instance(name: str, instance_type: str = "t3.micro") -> str:
    """Launch a new compute instance. Costs money; not idempotent on its own."""
    existing = live_instances()
    if not existing:
        raise AwsError("NotFound: no base image available (run: python -m servers.world seed)")
    response = client("ec2").run_instances(
        ImageId=existing[0]["ImageId"], InstanceType=instance_type, MinCount=1, MaxCount=1,
        TagSpecifications=_tags(Name=name),
    )
    instance_id = response["Instances"][0]["InstanceId"]
    return f"launched {instance_id} ({name}, {instance_type})"


@mcp.tool()
@aws_errors
def delete_bucket(bucket: str) -> str:
    """Permanently delete a storage bucket. AWS refuses if the bucket is not empty."""
    client("s3").delete_bucket(Bucket=bucket)
    return f"deleted bucket {bucket}"


@mcp.tool()
@aws_errors
def delete_iam_role(role: str) -> str:
    """Delete an IAM role. Exists so policies have something to forbid."""
    client("iam").delete_role(RoleName=role)
    return f"deleted IAM role {role}"


if __name__ == "__main__":
    mcp.run()
