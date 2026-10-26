"""Environment passed to child processes (gateway, downstream MCP servers).

An explicit allowlist: the MCP stdio client passes only a minimal safe set by
default, so AWS settings would be lost; passing everything would hand every
child secrets it doesn't need (the Groq key, the LocalStack token).
"""

from __future__ import annotations

import os

PREFIXES = ("AGENTGUARD_", "AWS_")
NAMES = ("LOCALSTACK_URL", "REDIS_URL")
# Matched by the prefixes above but only for processes that need them: the token
# encryption key goes to the gateway, never to the tool servers it spawns.
SECRETS = ("AGENTGUARD_TOKEN_KEY",)


def child_env(*, secrets: tuple[str, ...] = ()) -> dict[str, str]:
    return {k: v for k, v in os.environ.items()
            if (k.startswith(PREFIXES) or k in NAMES) and (k not in SECRETS or k in secrets)}
