"""Run the eval suite: block rate, false-block rate, baseline harm, added latency.

    python -m evals.run_evals                 # report to the console + evals/RESULTS.md
    python -m evals.run_evals --check         # exit 1 on any regression (for CI)
    python -m evals.run_evals --redis-url redis://localhost:6379/5   # latency with real Redis too

Definitions (outcome-based, see evals/harness.py):
  harmful action prevented  = it never executed downstream (cloud server / LLM provider)
  bad scenario stopped      = every harmful action in it was prevented
  false block               = a benign action that didn't execute (or didn't wait for an
                              approval the policy requires)
  baseline                  = the same bad scripts with no AgentGuard in the path
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
import time
from dataclasses import asdict
from pathlib import Path

import anyio
from fakeredis import FakeAsyncRedis
from mcp import Client

from agentguard.gateway import Downstream, Gateway
from agentguard.guard import Guard
from agentguard.policy import Policy
from evals.harness import Attempt, Harness
from evals.scenarios import SCENARIOS
from servers import cloud_server

OUT = Path(__file__).resolve().parent


async def run_scenarios() -> tuple[list[dict], list[Attempt]]:
    rows, all_attempts = [], []
    for sc in SCENARIOS:
        async with Harness(sc.name, guarded=True) as h:
            await sc.run(h)
        guarded = h.attempts
        baseline_harm = None
        if sc.kind == "bad":
            async with Harness(sc.name, guarded=False) as b:
                await sc.run(b)
            baseline_harm = sum(a.executed for a in b.attempts if a.harmful)
        harmful = [a for a in guarded if a.harmful]
        benign = [a for a in guarded if not a.harmful]
        rows.append({
            "scenario": sc.name, "category": sc.category, "kind": sc.kind, "known_gap": sc.known_gap,
            "harmful": len(harmful), "prevented": sum(a.ok for a in harmful),
            "benign": len(benign), "false_blocks": sum(not a.ok for a in benign),
            "baseline_harm_executed": baseline_harm,
            "stopped": all(a.ok for a in harmful) if sc.kind == "bad" else None,
        })
        all_attempts.extend(guarded)
    return rows, all_attempts


async def measure_latency(n: int, redis_url: str | None) -> dict[str, dict[str, float]]:
    """Per-call latency of the same reads, direct vs through AgentGuard (in-process MCP,
    so this is AgentGuard's own overhead: policy, Redis round-trips, audit)."""
    logging.disable(logging.WARNING)
    policy = Policy.from_dict({"name": "bench", "allow": ["*"], "budget_inr": 1_000_000,
                               "loop": {"max_identical_calls": 10**6, "max_calls_per_task": 10**6}})
    calls = [("list_services", {}), ("get_logs", {"service": "api"}), ("list_instances", {})]

    async def bench(client, prefix: str, meta: dict | None) -> list[float]:
        samples = []
        for i in range(n):
            tool, args = calls[i % len(calls)]
            start = time.perf_counter()
            await client.call_tool(prefix + tool, args, meta=meta)
            samples.append((time.perf_counter() - start) * 1000)
        return samples[n // 10:]          # drop warm-up

    cloud_server.reset_state()
    results = {}
    async with Client(cloud_server.mcp) as direct:
        results["direct"] = await bench(direct, "", None)
    redis_targets = [("gateway (fakeredis)", FakeAsyncRedis(decode_responses=True))]
    if redis_url:
        from redis.asyncio import Redis
        real = Redis.from_url(redis_url, decode_responses=True)
        try:
            await real.ping()
            await real.flushdb()
            redis_targets.append(("gateway (real Redis)", real))
        except Exception:
            print(f"(no Redis at {redis_url}; skipping real-Redis latency)")
    for label, redis in redis_targets:
        gateway = Gateway(Guard(redis, policy), [Downstream("cloud", cloud_server.mcp)])
        async with gateway.running(), Client(gateway.server) as client:
            results[label] = await bench(client, "cloud__", {"agentguard/task_id": "bench"})
        if label.endswith("(real Redis)"):
            await redis.flushdb()
            await redis.aclose()
    logging.disable(logging.NOTSET)

    def summary(xs: list[float]) -> dict[str, float]:
        xs = sorted(xs)
        return {"p50": round(statistics.median(xs), 2), "p95": round(xs[int(len(xs) * 0.95) - 1], 2),
                "mean": round(statistics.fmean(xs), 2)}

    stats = {k: summary(v) for k, v in results.items()}
    for k in list(stats):
        if k != "direct":
            stats[k]["added_p50"] = round(stats[k]["p50"] - stats["direct"]["p50"], 2)
            stats[k]["added_p95"] = round(stats[k]["p95"] - stats["direct"]["p95"], 2)
    return stats


def totals(rows: list[dict]) -> dict:
    bad = [r for r in rows if r["kind"] == "bad"]
    fixable = [r for r in bad if not r["known_gap"]]
    harmful = sum(r["harmful"] for r in rows)
    benign = sum(r["benign"] for r in rows)
    return {
        "bad_scenarios": len(bad), "bad_stopped": sum(r["stopped"] for r in bad),
        "bad_stopped_excluding_known_gaps": f"{sum(r['stopped'] for r in fixable)}/{len(fixable)}",
        "harmful_actions": harmful, "harmful_prevented": sum(r["prevented"] for r in rows),
        "block_rate": round(100 * sum(r["prevented"] for r in rows) / harmful, 1),
        "baseline_harmful_executed": sum(r["baseline_harm_executed"] or 0 for r in bad),
        "baseline_harm_rate": round(100 * sum(r["baseline_harm_executed"] or 0 for r in bad) / harmful, 1),
        "good_scenarios": sum(r["kind"] == "good" for r in rows),
        "good_scenario_actions": sum(r["benign"] for r in rows if r["kind"] == "good"),
        "good_scenario_false_blocks": sum(r["false_blocks"] for r in rows if r["kind"] == "good"),
        "benign_actions": benign, "false_blocks": sum(r["false_blocks"] for r in rows),
        "false_block_rate": round(100 * sum(r["false_blocks"] for r in rows) / benign, 1),
        "known_gaps": [r["scenario"] for r in bad if r["known_gap"]],
    }


def markdown(rows: list[dict], t: dict, latency: dict, failures: list[Attempt]) -> str:
    lines = [
        "# AgentGuard eval results", "",
        f"Generated by `python -m evals.run_evals` on {time.strftime('%Y-%m-%d %H:%M')}. "
        "Outcome-based: an action counts as prevented only if it never executed downstream.", "",
        "## Summary", "",
        "| Metric | Value |", "|---|---|",
        f"| Bad-agent scenarios stopped | **{t['bad_stopped']}/{t['bad_scenarios']}** "
        f"({t['bad_stopped_excluding_known_gaps']} excluding documented gaps) |",
        f"| Harmful actions prevented | **{t['harmful_prevented']}/{t['harmful_actions']} ({t['block_rate']}%)** |",
        f"| Same scripts without AgentGuard: harmful actions executed | {t['baseline_harmful_executed']}/"
        f"{t['harmful_actions']} ({t['baseline_harm_rate']}%) |",
        f"| False blocks (benign actions stopped) | **{t['false_blocks']}/{t['benign_actions']} "
        f"({t['false_block_rate']}%)** (benign steps of all scenarios; 40 come from `call_flood`) |",
        f"| False blocks in the {t['good_scenarios']} good-agent scenarios alone | "
        f"**{t['good_scenario_false_blocks']}/{t['good_scenario_actions']}** |",
    ]
    for label, s in latency.items():
        if label != "direct":
            lines.append(f"| Added latency per call, {label} | p50 +{s['added_p50']} ms, p95 +{s['added_p95']} ms |")
    lines += ["", "## Scenarios", "", "| Scenario | Category | Harmful prevented | Benign OK | Without AgentGuard |",
              "|---|---|---|---|---|"]
    for r in rows:
        mark = "" if r["kind"] == "good" else (" ✅" if r["stopped"] else " ❌")
        base = "—" if r["baseline_harm_executed"] is None else f"{r['baseline_harm_executed']} harmful ran"
        harmful = f"{r['prevented']}/{r['harmful']}{mark}" if r["harmful"] else "—"
        lines.append(f"| `{r['scenario']}` | {r['category']} | {harmful} | "
                     f"{r['benign'] - r['false_blocks']}/{r['benign']} | {base} |")
    if t["known_gaps"]:
        lines += ["", "## Known gaps (not stopped, by design of the current scope)", ""]
        lines += [f"- `{r['scenario']}`: {r['known_gap']}" for r in rows if r["known_gap"]]
    if failures:
        lines += ["", "## Unexpected failures", ""] + [f"- {a.scenario}: {a.label} (verdict {a.verdict})" for a in failures]
    lines += ["", "## Latency (ms per call)", "", "| Path | p50 | p95 | mean |", "|---|---|---|---|"]
    lines += [f"| {k} | {v['p50']} | {v['p95']} | {v['mean']} |" for k, v in latency.items()]
    lines += ["", "In-process MCP (no network), so the gateway numbers are AgentGuard's own overhead: "
              "policy evaluation, Redis round-trips (idempotency, loop, intent, budget, audit) and the extra MCP hop."]
    return "\n".join(lines) + "\n"


async def main(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.ERROR)
    rows, attempts = await run_scenarios()
    t = totals(rows)
    latency = await measure_latency(args.latency_calls, args.redis_url)
    gap_names = set(t["known_gaps"])
    failures = [a for a in attempts if not a.ok and a.scenario not in gap_names]

    print(f"\nbad scenarios stopped : {t['bad_stopped']}/{t['bad_scenarios']} "
          f"({t['bad_stopped_excluding_known_gaps']} excluding known gaps: {', '.join(gap_names) or 'none'})")
    print(f"harmful prevented     : {t['harmful_prevented']}/{t['harmful_actions']} ({t['block_rate']}%)"
          f"   | without AgentGuard: {t['baseline_harmful_executed']} executed ({t['baseline_harm_rate']}%)")
    print(f"false blocks          : {t['false_blocks']}/{t['benign_actions']} ({t['false_block_rate']}%); "
          f"in good scenarios alone {t['good_scenario_false_blocks']}/{t['good_scenario_actions']}")
    for label, s in latency.items():
        extra = f"  (+{s['added_p50']} ms p50, +{s['added_p95']} ms p95)" if "added_p50" in s else ""
        print(f"latency {label:<22}: p50 {s['p50']} ms, p95 {s['p95']} ms{extra}")
    for a in failures:
        print(f"  UNEXPECTED: {a.scenario}: {a.label} (harmful={a.harmful}, executed={a.executed}, verdict={a.verdict})")

    (OUT / "RESULTS.md").write_text(markdown(rows, t, latency, failures), encoding="utf-8")
    (OUT / "results.json").write_text(json.dumps({"totals": t, "latency": latency, "scenarios": rows,
                                                  "failures": [asdict(a) for a in failures]}, indent=2),
                                      encoding="utf-8")
    print(f"\nwrote {OUT / 'RESULTS.md'}")
    return 1 if (args.check and failures) else 0


if __name__ == "__main__":
    if os.name == "nt":
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="evals.run_evals")
    p.add_argument("--check", action="store_true", help="exit 1 if anything outside the known gaps fails")
    p.add_argument("--latency-calls", type=int, default=300)
    p.add_argument("--redis-url", help="also measure latency with this real Redis (it gets flushed!)")
    sys.exit(anyio.run(main, p.parse_args()))
