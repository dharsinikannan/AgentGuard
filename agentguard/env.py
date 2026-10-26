"""Environment passed to child processes (gateway, downstream MCP servers).

An explicit allowlist: the MCP stdio client passes only a minimal safe set by
default, so AWS settings would be lost; passing everything would hand every
child secrets it doesn't need (the Groq key, the LocalStack token).
"""

from __future__ import annotations

import os

PREFIXES = ("AGENTGUARD_", "AWS_")
NAMES = ("LOCALSTACK_URL", "REDIS_URL")


def child_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k.startswith(PREFIXES) or k in NAMES}
