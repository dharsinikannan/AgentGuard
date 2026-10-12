from pathlib import Path

import pytest
from fakeredis import FakeAsyncRedis

from agentguard.guard import Guard
from agentguard.policy import Policy

POLICIES = Path(__file__).parent.parent / "skills" / "policies"


@pytest.fixture
async def redis():
    r = FakeAsyncRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.fixture
def prod_policy() -> Policy:
    return Policy.from_yaml(POLICIES / "prod-safe.yaml")


@pytest.fixture
def guard(redis, prod_policy) -> Guard:
    return Guard(redis, prod_policy)
