"""Mock cloud provider as an MCP server. Replaced by LocalStack in milestone 3.

State is in memory, so each process starts with the same small "production":
a slow `api` service, two buckets, one instance.

Run standalone over stdio:  python -m servers.cloud_server
"""

from __future__ import annotations

import itertools

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("cloud", instructions="Mock cloud provider for the AgentGuard demo.")

_ids = itertools.count(1001)
STATE: dict = {}


def reset_state() -> None:
    STATE.clear()
    STATE.update({
        "services": {"api": {"replicas": 2, "p99_ms": 2400}, "db": {"replicas": 1, "p99_ms": 35}},
        "buckets": {"old-logs-2023": 1_200_000, "prod-assets": 54_000},
        "instances": {"i-1000": {"name": "api-1", "type": "t3.medium"}},
    })


reset_state()


@mcp.tool()
def get_logs(service: str, lines: int = 5) -> str:
    """Return recent log lines for a service."""
    svc = STATE["services"].get(service)
    if svc is None:
        return f"no such service: {service}"
    if svc["p99_ms"] > 1000:
        sample = [f"WARN {service} request took {svc['p99_ms']}ms (pool exhausted, {svc['replicas']} replicas)"]
    else:
        sample = [f"INFO {service} healthy p99={svc['p99_ms']}ms"]
    return "\n".join(sample * max(1, min(lines, 20)))


@mcp.tool()
def list_services() -> dict:
    """List services with replica count and p99 latency."""
    return STATE["services"]


@mcp.tool()
def list_instances() -> dict:
    """List running compute instances."""
    return STATE["instances"]


@mcp.tool()
def list_buckets() -> dict:
    """List storage buckets and their size in MB."""
    return STATE["buckets"]


@mcp.tool()
def scale_service(service: str, replicas: int) -> str:
    """Set the replica count of a service. More replicas lower latency."""
    svc = STATE["services"].get(service)
    if svc is None:
        raise ValueError(f"no such service: {service}")
    if not 1 <= replicas <= 20:
        raise ValueError("replicas must be between 1 and 20")
    old = svc["replicas"]
    svc["replicas"] = replicas
    svc["p99_ms"] = max(80, svc["p99_ms"] * old // replicas)
    return f"{service} scaled {old} -> {replicas} replicas; p99 now {svc['p99_ms']}ms"


@mcp.tool()
def create_instance(name: str, instance_type: str = "t3.medium") -> str:
    """Launch a new compute instance. Costs money; not idempotent on its own."""
    instance_id = f"i-{next(_ids)}"
    STATE["instances"][instance_id] = {"name": name, "type": instance_type}
    return f"launched {instance_id} ({name}, {instance_type})"


@mcp.tool()
def delete_bucket(bucket: str) -> str:
    """Permanently delete a storage bucket and everything in it."""
    if bucket not in STATE["buckets"]:
        raise ValueError(f"no such bucket: {bucket}")
    del STATE["buckets"][bucket]
    return f"deleted bucket {bucket}"


@mcp.tool()
def delete_iam_role(role: str) -> str:
    """Delete an IAM role. Exists so policies have something to forbid."""
    return f"deleted IAM role {role}"


if __name__ == "__main__":
    mcp.run()
