# AgentGuard

A safety gateway for AI agents that operate cloud infrastructure. It sits between
an agent and its MCP tools and stops:

- **runaway loops**: the same call repeated, or too many calls per task
- **runaway spend**: a per-task ₹ budget, reserved atomically before each call
- **unapproved destructive actions**: paused until a human approves that exact call
- **duplicate actions after a dropped connection**: idempotency keys, no blind retries of writes

```
agent ──MCP──► AgentGuard ──MCP──► cloud server (· github · logs, later)
                   │
                 Redis   budgets · loop counters · idempotency · approvals · audit stream
```

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"

pytest -q                             # 33 tests, ~1s, no Redis needed (fakeredis)
python demo/run_demo.py               # scripted incident, no Redis needed
```

Run the gateway for a real MCP client (needs Redis at `REDIS_URL`, default `redis://localhost:6379/0`):

```powershell
python -m agentguard --policy skills/policies/prod-safe.yaml
python scripts/approve.py list
python scripts/approve.py approve <approval_id>
python scripts/audit.py <task_id>
```

Each call must carry `_meta`: `agentguard/task_id` (required) and
`agentguard/idempotency_key` (optional, recommended for writes).

## Layout

| Path | What |
|---|---|
| `agentguard/policy.py` | YAML policy packs: allow / deny / require_approval / costs / loop limits / safe_to_retry |
| `agentguard/guard.py` | Decision engine: policy → idempotency → loop → budget → approval → allow. No MCP imports. |
| `agentguard/gateway.py` | MCP proxy: namespaced tools `<server>__<tool>`, filters `tools/list`, enforces decisions |
| `agentguard/chaos.py` | Fault injection (response lost after the server acted) |
| `servers/cloud_server.py` | Mock cloud MCP server |
| `skills/policies/` | `prod-safe`, `read-only-investigation` |
| `scripts/` | `approve.py`, `audit.py` (human side; never MCP tools) |
| `demo/run_demo.py` | Every protection, end to end |

## Design decisions

- **The guard has no MCP imports.** All safety logic is pure Python + Redis, unit-tested with fakeredis. The gateway only translates MCP ↔ guard.
- **Check order is cheapest/most absolute first**: policy → idempotency → loop → budget → approval. A replay is returned before loop and budget, so retries are free and don't count as loops.
- **Side effects are rolled back on refusal.** An idempotency claim or budget reservation made by an earlier check is undone if a later check refuses the call. A refused call costs nothing and doesn't leave a key stuck.
- **Unknown tools are denied and invisible.** Precedence is deny > require_approval > allow > implicit deny. `tools/list` is filtered, so the model never plans around a tool it can't use.
- **Policy typos fail at load time.** Unknown YAML keys raise an error. A misspelt `require_aproval` must not silently mean "no approval".
- **Money is integer paise.** No float rounding, and Redis `INCRBY` is atomic on integers.
- **Budget uses reserve-then-check (`INCRBY`, compare, roll back).** It never overspends under concurrency. The trade-off: two racing calls near the limit may both be refused when one would have fit.
- **Idempotency is an atomic claim (`SET NX`), never check-then-set.** Of N concurrent duplicates exactly one executes. Reusing a key with different args is refused rather than replaying the wrong result.
- **Writes are never auto-retried after a dropped connection.** The key is marked `unknown` and the agent is told to inspect state. Only tools in `safe_to_retry` (reads) are retried, on a fresh connection.
- **An MCP error response counts as a known outcome.** The server answered, so it is stored for replay. Only a transport failure counts as "unknown".
- **Approval is not an MCP tool.** It is bound to `sha256(task + tool + canonical args)` and is single-use (`GETDEL`). Approving deletion of bucket A doesn't approve bucket B, or a second deletion. A pending call reserves no budget.
- **`task_id` and idempotency keys are validated** against a strict charset, because they become parts of Redis keys.
- **One supervisor task per downstream owns its connection.** anyio needs a client to be exited in the task that entered it. A failed call marks the connection broken and the supervisor reconnects.
- **stdout is the MCP channel in stdio mode,** so all logging goes to stderr.
- **Every decision and outcome is appended to a Redis Stream** `ag:audit:{task}`: append-only and ordered, with `XRANGE` for replay.

## Future work

- Budget refunds when a write fails definitively (today a reserved cost is kept).
- Stuck `in_progress` keys if the gateway crashes mid-call (today they expire after 24h).
- Redact secrets from audited arguments.
