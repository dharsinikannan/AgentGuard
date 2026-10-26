"""The eval suite as a regression test: every bad-agent scenario except the documented
gaps must stay stopped, and no benign action may be blocked. A change that weakens a
defence fails here, with the scenario's name."""

import pytest

from evals.harness import Harness
from evals.scenarios import SCENARIOS

KNOWN_GAPS = {"task_rotation_to_evade_budget"}


@pytest.mark.parametrize("sc", SCENARIOS, ids=[s.name for s in SCENARIOS])
async def test_scenario(sc):
    async with Harness(sc.name, guarded=True) as h:
        await sc.run(h)
    failures = [f"{a.label}: harmful={a.harmful} executed={a.executed} verdict={a.verdict}"
                for a in h.attempts if not a.ok]
    if sc.name in KNOWN_GAPS:
        assert failures, "a known gap started passing: update KNOWN_GAPS and the README"
        return
    assert not failures, failures
    assert h.attempts, "scenario recorded nothing"


def test_suite_shape():
    bad = [s for s in SCENARIOS if s.kind == "bad"]
    assert len(bad) == 30 and len(SCENARIOS) - len(bad) == 12
    assert {s.name for s in bad if s.known_gap} == KNOWN_GAPS
