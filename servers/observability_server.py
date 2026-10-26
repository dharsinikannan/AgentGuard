"""Logs and metrics MCP server on CloudWatch. Read-only by design.

Separate from the cloud server because in a real company these are different
systems with different permissions: reading telemetry is safe and retryable,
changing infrastructure is not. The gateway exposes them as `obs__*` and `cloud__*`.

Run standalone over stdio:  python -m servers.observability_server
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from botocore.exceptions import ClientError
from mcp.server.mcpserver import MCPServer

from servers.aws import METRIC_NAMESPACE, AwsError, aws_errors, client, log_group

mcp = MCPServer("obs", instructions="Read-only logs (CloudWatch Logs) and metrics (CloudWatch).")

METRICS = ("p99_latency_ms",)
MAX_SCANNED_EVENTS = 2000
HISTORY_RESET_METRIC = "history_reset"   # written by servers.world.seed; see get_metric


@mcp.tool()
@aws_errors
def get_logs(service: str, pattern: str = "", minutes: int = 15, limit: int = 20) -> str:
    """Recent log lines for a service, newest last. `pattern` is a CloudWatch filter, e.g. "WARN"."""
    minutes, limit = max(1, min(minutes, 180)), max(1, min(limit, 100))
    kwargs = {"logGroupName": log_group(service), "startTime": int((time.time() - minutes * 60) * 1000)}
    if pattern:
        kwargs["filterPattern"] = pattern
    try:
        # Events come oldest first, so read the whole window (capped) and keep the tail.
        # Passing `limit` to AWS would return the *oldest* N lines.
        pages = client("logs").get_paginator("filter_log_events").paginate(
            **kwargs, PaginationConfig={"MaxItems": MAX_SCANNED_EVENTS})
        lines = [e["message"] for page in pages for e in page["events"]]
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise  # aws_errors turns it into a readable message
        raise AwsError(f"NotFound: no logs for service {service}") from None
    if pattern:
        lines = [line for line in lines if matches_filter(line, pattern)]
    if not lines:
        return f"no log lines for {service} matching {pattern!r} in the last {minutes} min"
    return "\n".join(collapse_repeats(lines[-limit:]))


def matches_filter(line: str, pattern: str) -> bool:
    """Re-apply simple CloudWatch term patterns ("WARN", "pool exhausted": every term,
    case-sensitive) on our side. LocalStack ignores filterPattern entirely, and on real
    AWS this is a no-op. Complex syntax ({ }, [ ], quotes, ?) is left to the backend."""
    if any(c in pattern for c in '{}[]"?'):
        return True
    return all(term in line for term in pattern.split())


def collapse_repeats(lines: list[str]) -> list[str]:
    """Merge consecutive identical lines: 20 copies of one WARN cost the LLM 20x the
    tokens and tell it nothing more than one line with a count."""
    out: list[tuple[str, int]] = []
    for line in lines:
        if out and out[-1][0] == line:
            out[-1] = (line, out[-1][1] + 1)
        else:
            out.append((line, 1))
    return [f"{line}  (x{n})" if n > 1 else line for line, n in out]


@mcp.tool()
@aws_errors
def get_metric(service: str, metric: str = "p99_latency_ms", minutes: int = 15) -> dict:
    """A service metric: the latest value plus recent history (per-second maximum).

    Metrics lag: right after a change (e.g. scaling), `latest` can still show the
    value from before it. Check `recent` for the trend, or re-check shortly, before
    concluding a change didn't help.
    """
    if metric not in METRICS:
        raise AwsError(f"ValidationError: unknown metric {metric}; available: {', '.join(METRICS)}")
    cw = client("cloudwatch")
    now = datetime.now(timezone.utc)
    points = cw.get_metric_statistics(
        Namespace=METRIC_NAMESPACE, MetricName=metric,
        Dimensions=[{"Name": "Service", "Value": service}],
        StartTime=now - timedelta(minutes=max(1, min(minutes, 180))), EndTime=now + timedelta(seconds=10),
        # 1 s buckets (the metric is stored at high resolution). With 1-minute buckets
        # the pre-fix maximum lingers right after a fix, and the agent would wrongly
        # conclude the fix didn't help and escalate.
        Period=1, Statistics=["Maximum"],
    )["Datapoints"]
    reset = history_reset_time(cw, now)
    if reset is not None:
        points = [p for p in points if p["Timestamp"] >= reset]
    points.sort(key=lambda p: p["Timestamp"])
    return {
        "service": service,
        "metric": metric,
        "latest": points[-1]["Maximum"] if points else None,
        "recent": [{"time": p["Timestamp"].strftime("%H:%M:%S"), "value": p["Maximum"]} for p in points[-12:]],
    }


def history_reset_time(cw, now: datetime) -> datetime | None:
    """When the environment was last re-seeded, if it ever was.

    CloudWatch has no API to delete datapoints, so a re-seeded environment would
    still show the previous run's latency. seed() writes a marker whose value is
    its unix time; we ignore points older than the newest marker. Real AWS never
    has a marker, so nothing is filtered there.
    """
    marks = cw.get_metric_statistics(
        Namespace=METRIC_NAMESPACE, MetricName=HISTORY_RESET_METRIC,
        StartTime=now - timedelta(hours=3), EndTime=now + timedelta(seconds=10),
        Period=3 * 3600, Statistics=["Maximum"],
    )["Datapoints"]
    if not marks:
        return None
    return datetime.fromtimestamp(max(p["Maximum"] for p in marks), timezone.utc)


if __name__ == "__main__":
    mcp.run()
