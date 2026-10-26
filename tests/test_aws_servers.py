"""AWS-backed MCP servers, tested with moto (in-process AWS mock, no Docker).

One test at the bottom runs against live LocalStack and skips itself when
LocalStack isn't running.
"""

import json
import time
import urllib.request

import pytest
from moto import mock_aws
from mcp import Client

from agentguard.env import child_env
from agentguard.gateway import Downstream, Gateway
from servers import aws, aws_cloud_server, observability_server, world


@pytest.fixture
def moto_world(monkeypatch):
    monkeypatch.setenv("AGENTGUARD_AWS_TARGET", "aws")   # real AWS endpoints, intercepted by moto
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    for var in ("AWS_PROFILE", "AWS_ENDPOINT_URL", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    aws.reset_clients()
    with mock_aws():
        world.seed(allow_real_aws=True)  # "real" = moto here
        yield
    aws.reset_clients()


def text(result) -> str:
    return result.content[0].text


async def test_seed_builds_the_incident_start(moto_world):
    async with Client(aws_cloud_server.mcp) as cloud:
        services = json.loads(text(await cloud.call_tool("list_services", {})))
        buckets = json.loads(text(await cloud.call_tool("list_buckets", {})))
    assert {s: v["instances"] for s, v in services.items()} == {"api": 2, "db": 1}
    assert buckets["old-logs-2023"]["objects"] == 3
    assert world.tick()["api"] == 2400


async def test_scaling_lowers_observed_latency(moto_world):
    async with Client(aws_cloud_server.mcp) as cloud, Client(observability_server.mcp) as obs:
        r = await cloud.call_tool("scale_service", {"service": "api", "replicas": 6})
        assert text(r) == "api scaled 2 -> 6 instances"
        world.tick(at=time.time() + 2)
        metric = json.loads(text(await obs.call_tool("get_metric", {"service": "api"})))
        logs = text(await obs.call_tool("get_logs", {"service": "api", "pattern": "INFO"}))
    assert metric["latest"] == 800
    assert "healthy p99=800ms" in logs


async def test_scale_down_terminates_newest_first(moto_world):
    async with Client(aws_cloud_server.mcp) as cloud:
        await cloud.call_tool("scale_service", {"service": "api", "replicas": 4})
        await cloud.call_tool("scale_service", {"service": "api", "replicas": 1})
    remaining = aws.live_instances("api")
    assert [aws.tags_of(i)["Name"] for i in remaining] == ["api-1"]  # scaled-out ones went first


async def test_aws_errors_reach_the_model_readably(moto_world):
    # Failure demo: real AWS refuses to delete a non-empty bucket. The message must
    # survive MCP (only ToolError messages do) so the agent can explain it.
    async with Client(aws_cloud_server.mcp) as cloud:
        r = await cloud.call_tool("delete_bucket", {"bucket": "old-logs-2023"})
        bad = await cloud.call_tool("scale_service", {"service": "api", "replicas": 99})
    assert r.is_error and "BucketNotEmpty" in text(r)
    assert bad.is_error and "between 1 and 20" in text(bad)


async def test_logs_for_unknown_service_say_not_found(moto_world):
    async with Client(observability_server.mcp) as obs:
        r = await obs.call_tool("get_logs", {"service": "nope"})
    assert r.is_error and "NotFound" in text(r)


async def test_gateway_dedupes_even_though_backend_does_not(moto_world, guard):
    # LocalStack ignores EC2 ClientToken; AgentGuard's idempotency key still
    # turns a retried launch into a replay.
    gateway = Gateway(guard, [Downstream("cloud", aws_cloud_server.mcp)])
    meta = {"agentguard/task_id": "t1", "agentguard/idempotency_key": "launch-1"}
    async with gateway.running(), Client(gateway.server) as client:
        r1 = await client.call_tool("cloud__create_instance", {"name": "extra"}, meta=meta)
        r2 = await client.call_tool("cloud__create_instance", {"name": "extra"}, meta=meta)
    assert r2.meta["agentguard/verdict"] == "replay" and text(r1) == text(r2)
    assert sum(aws.tags_of(i).get("Name") == "extra" for i in aws.live_instances()) == 1


def test_seed_refuses_real_aws_without_explicit_flag(monkeypatch):
    monkeypatch.setenv("AGENTGUARD_AWS_TARGET", "aws")
    with pytest.raises(RuntimeError, match="refusing"):
        world.seed()


def test_child_env_passes_aws_settings_but_not_secrets(monkeypatch):
    monkeypatch.setenv("AGENTGUARD_AWS_TARGET", "localstack")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_secret")
    monkeypatch.setenv("LOCALSTACK_AUTH_TOKEN", "ls-secret")
    monkeypatch.setenv("AGENTGUARD_TOKEN_KEY", "fernet-key")
    env = child_env()
    assert env["AGENTGUARD_AWS_TARGET"] == "localstack"
    assert "GROQ_API_KEY" not in env and "LOCALSTACK_AUTH_TOKEN" not in env
    assert "AGENTGUARD_TOKEN_KEY" not in env                      # tool servers never get it
    assert child_env(secrets=("AGENTGUARD_TOKEN_KEY",))["AGENTGUARD_TOKEN_KEY"] == "fernet-key"  # the gateway does


def _localstack_up() -> bool:
    try:
        urllib.request.urlopen("http://localhost:4566/_localstack/health", timeout=1)
        return True
    except OSError:
        return False


@pytest.mark.localstack
@pytest.mark.skipif(not _localstack_up(), reason="LocalStack not running (docker compose up -d)")
async def test_live_localstack_scale_and_observe(monkeypatch):
    monkeypatch.setenv("AGENTGUARD_AWS_TARGET", "localstack")
    aws.reset_clients()
    try:
        world.seed()
        async with Client(aws_cloud_server.mcp) as cloud, Client(observability_server.mcp) as obs:
            await cloud.call_tool("scale_service", {"service": "api", "replicas": 4})
            world.tick(at=time.time() + 2)
            metric = json.loads(text(await obs.call_tool("get_metric", {"service": "api"})))
            info = text(await obs.call_tool("get_logs", {"service": "api", "pattern": "INFO"}))
        assert metric["latest"] == 1200
        assert "WARN" not in info  # LocalStack ignores filterPattern; we filter locally
    finally:
        aws.reset_clients()


def test_repeated_log_lines_are_collapsed():
    from servers.observability_server import collapse_repeats
    assert collapse_repeats(["WARN a", "WARN a", "WARN a", "INFO b", "WARN a"]) == [
        "WARN a  (x3)", "INFO b", "WARN a",  # only consecutive repeats merge; order is kept
    ]


def test_simple_filter_patterns_are_reapplied_locally():
    from servers.observability_server import matches_filter
    assert matches_filter("WARN api pool exhausted", "WARN")
    assert matches_filter("WARN api pool exhausted", "pool exhausted")   # all terms, any order
    assert not matches_filter("INFO api healthy", "WARN")
    assert not matches_filter("warn api", "WARN")                       # case-sensitive, like CloudWatch
    assert matches_filter("anything", '{ $.level = "WARN" }')           # complex syntax: backend's job


async def test_get_logs_returns_the_newest_lines(moto_world):
    # Regression: filter_log_events returns oldest first; passing limit=N to AWS
    # gave the agent stale lines from the start of the window.
    logs = aws.client("logs")
    now = int(time.time() * 1000)
    logs.put_log_events(logGroupName=aws.log_group("api"), logStreamName="app",
                        logEvents=[{"timestamp": now + 2000 + i, "message": f"line {i}"} for i in range(30)])  # after the seed tick (+1 s)
    async with Client(observability_server.mcp) as obs:
        out = text(await obs.call_tool("get_logs", {"service": "api", "limit": 3}))
    assert out.splitlines() == ["line 27", "line 28", "line 29"]


async def test_metrics_from_before_the_last_seed_are_hidden(moto_world):
    # Regression: CloudWatch can't delete datapoints, so a previous run's latency
    # showed up as "recent" after re-seeding.
    from datetime import datetime, timezone
    aws.client("cloudwatch").put_metric_data(Namespace=aws.METRIC_NAMESPACE, MetricData=[{
        "MetricName": "p99_latency_ms", "Dimensions": [{"Name": "Service", "Value": "api"}],
        "Value": 9999, "StorageResolution": 1,
        "Timestamp": datetime.fromtimestamp(time.time() - 120, timezone.utc),
    }])
    async with Client(observability_server.mcp) as obs:
        metric = json.loads(text(await obs.call_tool("get_metric", {"service": "api"})))
    assert 9999 not in [p["value"] for p in metric["recent"]]
    assert metric["latest"] == 2400
