"""The "world" around the agent: the incident's starting state, and fake production traffic.

    python -m servers.world seed            reset LocalStack to the incident start
    python -m servers.world run             emit metrics + logs every 5 s (Ctrl+C to stop)
    python -m servers.world tick            emit one round

Why a simulator: in production, services emit their own latency metrics and logs.
Here nothing would, so this plays that role. p99 latency = load / live instances,
so scaling the api service really lowers the latency the agent observes.

The cloud server *acts*, the observability server *reads*, this module is the *world*.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone

from botocore.exceptions import ClientError

from servers.aws import METRIC_NAMESPACE, PROJECT_TAG, client, live_instances, log_group, target

LOAD = {"api": 4800, "db": 35}               # api: 2 instances -> 2400 ms, the alert
SEED_INSTANCES = {"api": 2, "db": 1}
SEED_BUCKETS = {
    "old-logs-2023": ["2023/01/app.log", "2023/02/app.log", "2023/03/app.log"],
    "prod-assets": ["img/logo.png", "css/site.css"],
}
SLOW_MS = 1000


def seed(*, allow_real_aws: bool = False) -> None:
    """Delete everything we created before, then build the incident's starting state."""
    if target() == "aws" and not allow_real_aws:
        raise RuntimeError("refusing to seed real AWS (it deletes resources); pass allow_real_aws=True")
    ec2, s3, logs = client("ec2"), client("s3"), client("logs")

    # --- teardown: only resources tagged/named as ours
    old = [i["InstanceId"] for i in live_instances()]
    if old:
        ec2.terminate_instances(InstanceIds=old)
    existing = {b["Name"] for b in s3.list_buckets()["Buckets"]}
    for bucket in SEED_BUCKETS:
        if bucket in existing:
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
                for obj in page.get("Contents", []):
                    s3.delete_object(Bucket=bucket, Key=obj["Key"])
            s3.delete_bucket(Bucket=bucket)
    for group in logs.describe_log_groups(logGroupNamePrefix=log_group(""))["logGroups"]:
        logs.delete_log_group(logGroupName=group["logGroupName"])

    # --- build
    ami = os.environ.get("AGENTGUARD_AMI") or ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    for service, count in SEED_INSTANCES.items():
        for n in range(1, count + 1):
            ec2.run_instances(
                ImageId=ami, InstanceType="t3.micro", MinCount=1, MaxCount=1,
                TagSpecifications=[{"ResourceType": "instance", "Tags": [
                    PROJECT_TAG, {"Key": "Service", "Value": service}, {"Key": "Name", "Value": f"{service}-{n}"},
                ]}],
            )
    for bucket, keys in SEED_BUCKETS.items():
        s3.create_bucket(Bucket=bucket)
        for key in keys:
            s3.put_object(Bucket=bucket, Key=key, Body=f"seed data for {key}\n".encode())

    # CloudWatch can't delete datapoints; mark where this environment's history
    # begins so the observability server hides the previous run's metrics.
    seeded_at = time.time()
    client("cloudwatch").put_metric_data(Namespace=METRIC_NAMESPACE, MetricData=[{
        "MetricName": "history_reset", "Value": int(seeded_at),
        "Timestamp": datetime.fromtimestamp(seeded_at, timezone.utc),
    }])
    tick(at=seeded_at + 1)  # strictly after the marker (it's stored at whole seconds)


def tick(at: float | None = None) -> dict[str, int | None]:
    """One round of traffic: a latency metric and a log line per service.

    `at` (unix seconds) lets tests place ticks in distinct seconds; metrics are
    aggregated per second, so two ticks in the same second would merge.
    """
    cw, logs = client("cloudwatch"), client("logs")
    at = time.time() if at is None else at
    now_ms = int(at * 1000)
    observed: dict[str, int | None] = {}
    for service, load in LOAD.items():
        live = len(live_instances(service))
        p99 = load // live if live else None
        observed[service] = p99
        if p99 is None:
            message = f"ERROR {service} no live instances; all requests failing"
        else:
            cw.put_metric_data(Namespace=METRIC_NAMESPACE, MetricData=[{
                "MetricName": "p99_latency_ms", "Dimensions": [{"Name": "Service", "Value": service}],
                "Value": p99, "Unit": "Milliseconds",
                "Timestamp": datetime.fromtimestamp(at, timezone.utc),
                # High resolution (1 s storage) so readers can query 10 s buckets and see a
                # fix take effect at once; with 1-minute buckets the old value lingers.
                "StorageResolution": 1,
            }])
            if p99 > SLOW_MS:
                message = f"WARN {service} p99={p99}ms: connection pool exhausted ({live} instances)"
            else:
                message = f"INFO {service} healthy p99={p99}ms ({live} instances)"
        _ensure_log_stream(logs, service)
        logs.put_log_events(logGroupName=log_group(service), logStreamName="app",
                            logEvents=[{"timestamp": now_ms, "message": message}])
    return observed


def _ensure_log_stream(logs, service: str) -> None:
    for create in (lambda: logs.create_log_group(logGroupName=log_group(service)),
                   lambda: logs.create_log_stream(logGroupName=log_group(service), logStreamName="app")):
        try:
            create()
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
                raise


def main() -> None:
    parser = argparse.ArgumentParser(prog="servers.world")
    parser.add_argument("command", choices=["seed", "run", "tick"])
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--allow-real-aws", action="store_true")
    args = parser.parse_args()
    print(f"target={target()}", file=sys.stderr)
    if args.command == "seed":
        seed(allow_real_aws=args.allow_real_aws)
        print("seeded:", {s: len(live_instances(s)) for s in SEED_INSTANCES}, "instances;",
              "buckets:", list(SEED_BUCKETS))
    elif args.command == "tick":
        print(tick())
    else:
        while True:
            print(time.strftime("%H:%M:%S"), tick(), flush=True)
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
