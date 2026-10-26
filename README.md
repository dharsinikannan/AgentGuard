# AgentGuard

A safety gateway for AI agents that operate cloud infrastructure. It sits between
an agent and its MCP tools and stops:

- **runaway loops**: the same call repeated, or too many calls per task
- **runaway spend**: a per-task ₹ budget, reserved atomically before each call
- **unapproved destructive actions**: paused until a human approves that exact call
- **duplicate actions after a dropped connection**: idempotency keys, no blind retries of writes

```
agent ──MCP──► AgentGuard ──MCP──► cloud__*  (EC2, S3)          ─┐
                   │       └─MCP──► obs__*    (CloudWatch logs,   ├─ boto3 → LocalStack (or real AWS)
                   │                           metrics)          ─┘
                 Redis   budgets · loop counters · idempotency · approvals · audit stream
```

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev,agent,aws,ui]"

pytest -q                             # 80 tests, ~90s; 3 live ones skip without docker compose
python demo/run_demo.py               # scripted incident, no Redis needed
python demo/reconnect_demo.py         # dropped connections + gateway restart (part B needs Redis)
```

Run the LangGraph on-call agent. Put `GROQ_API_KEY` (free: https://console.groq.com/keys) and
`LOCALSTACK_AUTH_TOKEN` (free Hobby plan: https://app.localstack.cloud) in `.env` (see `.env.example`).

```powershell
python -m agent.run_agent                         # mock cloud, in-process gateway + fakeredis

docker compose up -d                              # Redis + LocalStack
python -m agent.run_agent --cloud aws --redis-url redis://localhost:6379/0
                                                  # full stack: real gateway, Redis, AWS APIs on LocalStack,
                                                  # world seeded + simulated traffic; approve.py works
python -m servers.world seed                      # reset LocalStack to the incident start

python -m agentguard --transport http --port 8765 # HTTP gateway (Streamable HTTP at /mcp)
python -m agent.run_agent --gateway-url http://127.0.0.1:8765/mcp
```

Human approval: a `require_approval` call pauses the agent (LangGraph `interrupt`); the run
is checkpointed in Redis and resumes once a human decides:

```powershell
streamlit run ui/approvals.py                     # approvals console + audit trail, http://127.0.0.1:8501
python -m agent.run_agent --redis-url redis://localhost:6379/0 --on-approval exit --alert "...delete old-logs-2023"
python scripts/approve.py approve <id>            # or click Approve in the UI
python -m agent.run_agent --resume --task-id <task> --redis-url redis://localhost:6379/0
```

Run the gateway for a real MCP client (needs Redis at `REDIS_URL`, default `redis://localhost:6379/0`):

```powershell
python -m agentguard --policy skills/policies/prod-safe.yaml            # mock cloud
python -m agentguard --policy skills/policies/prod-safe.yaml --cloud aws  # boto3 servers
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
| `agentguard/connection.py` | `SupervisedClient`: self-reconnecting MCP client (gateway → servers, agent → gateway) |
| `agentguard/chaos.py` | Fault injection (response lost after the server acted) |
| `servers/cloud_server.py` | Mock cloud MCP server (fast tests, scripted demo) |
| `servers/aws_cloud_server.py` | Cloud MCP server on EC2 + S3 (boto3) |
| `servers/observability_server.py` | Read-only logs + metrics MCP server on CloudWatch |
| `servers/world.py` | Seeds the incident; simulates traffic (p99 = load ÷ live instances) |
| `servers/aws.py` | Target selection (LocalStack / AWS), error mapping, shared helpers |
| `docker-compose.yml` | Redis + LocalStack |
| `skills/policies/` | `prod-safe`, `read-only-investigation` |
| `scripts/` | `approve.py`, `audit.py` (human side; never MCP tools) |
| `ui/approvals.py` | Streamlit approvals console: pending approvals, approve/reject, audit trail, spend |
| `agent/mcp_bridge.py` | MCP v2 tools → LLM tool specs; tool call → `tools/call` with `_meta` |
| `agent/graph.py` | Hand-written LangGraph ReAct loop (agent ⇄ tools); pauses with `interrupt` for approvals |
| `agent/run_agent.py` | CLI: Groq model → AgentGuard → cloud server |
| `demo/run_demo.py` | Every protection, end to end (scripted) |
| `demo/reconnect_demo.py` | Dropped connection mid-launch; gateway killed and restarted mid-task |

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
- **Policy values are type-checked as well as key names.** A pattern list written as a plain string would be split into single characters, and the `"*"` among them would allow every tool. Every malformed value fails at load time, never during a live call.
- **Own MCP→LangChain bridge instead of `langchain-mcp-adapters`.** The adapter's latest release pins `mcp<2`, and its tools can't attach per-call `_meta`. The bridge is ~60 lines.
- **Idempotency key = the LLM's `tool_call_id`.** Re-executing the *same* tool call (resume after a crash) replays the stored result; the model *deciding again* gets a new key and counts against loop and budget limits.
- **Own tools node instead of the prebuilt `ToolNode`.** Each call needs `_meta`. Tool calls in one model turn run sequentially, so two writes never race and the audit order matches the model's order.
- **Blocked calls go back to the model as `ToolMessage(status="error")` with the reason.** The model can adapt instead of retrying blindly. `recursion_limit` is a second, agent-side step cap; the gateway is the one the agent can't bypass.
- **The model is injected into the graph.** Tests drive the real graph, MCP and gateway with a scripted model: deterministic, no API key.
- **AWS servers are plain boto3; the target is an explicit setting** (`AGENTGUARD_AWS_TARGET=localstack|aws`, default LocalStack with dummy credentials). A dev laptop can't reach real AWS by accident, and `servers.world seed` refuses real AWS without `--allow-real-aws`, because it deletes resources.
- **A "service" is EC2 instances tagged `Service=<name>`.** `scale_service` launches/terminates tagged instances, which is what an Auto Scaling group does underneath (Auto Scaling isn't in LocalStack's free plan). Every resource we create is tagged `Project=agentguard`; teardown touches only those.
- **Telemetry is a separate, read-only MCP server** (`obs__*`), as in real companies: reading telemetry is safe and retryable, changing infrastructure isn't. The policy marks `obs__*` as `safe_to_retry`.
- **A simulator plays production traffic** (`servers/world.py`): the cloud server *acts*, the observability server *reads*, the world *reacts*. Scaling really lowers the latency the agent observes.
- **Gateway idempotency is needed even on AWS.** LocalStack ignores EC2 `ClientToken` (the same token launched two instances), and not every API has one. A test proves the gateway still turns a retried launch into a replay.
- **AWS errors reach the model as `ToolError`.** MCPServer replaces any other exception's message with "Error executing tool", so `BucketNotEmpty` would be invisible to the agent.
- **Metrics are stored at 1-second resolution and read per second.** With 1-minute buckets the pre-fix maximum lingers right after a fix, and the agent concluded scaling hadn't helped. The tool description also warns that metrics lag.
- **Re-seeding writes a `history_reset` marker metric.** CloudWatch can't delete datapoints, so a previous run's latency appeared as "recent"; points older than the marker are hidden.
- **Logs: read the whole window, return the tail, collapse repeats.** `filter_log_events` returns oldest first, so `limit=N` gave stale lines. Identical consecutive lines become `line (xN)`: same information, a fraction of the tokens (the free Groq tier is 8k tokens/min).
- **Simple log filters are re-applied locally.** LocalStack ignores `filterPattern` entirely (moto honours it, so tests didn't catch it at first). Lesson: verify emulator fidelity per feature.
- **Child processes get an explicit env allowlist** (`AGENTGUARD_*`, `AWS_*`, `LOCALSTACK_URL`, `REDIS_URL`). The MCP stdio client passes almost nothing by default, and passing everything would hand every server the Groq key and LocalStack token.
- **HTTP is stateless (MCP 2026-07-28); no session resumption was built.** The protocol has no session to resume: one request in, one response out. Legacy `EventStore` replay only helps old-protocol clients and needs sticky routing. All state AgentGuard needs is already in Redis, keyed by task and idempotency key, so any gateway process can serve any request, and "reconnect without losing place" happens per action. The demo kills the gateway mid-task: budget and loop counts carry on.
- **Allowed calls run detached from the agent's connection.** Over HTTP a client disconnect cancels the request handler. Unshielded, that left the idempotency key `in_progress` for 24 h and the outcome unrecorded, and every retry was refused. Decide + execute + record now run in a shielded scope with their own deadline (`execution_timeout`, 120 s; on timeout the outcome is `unknown`).
- **A retry during the original attaches to it.** Instead of "still in flight", the retry waits (up to 15 s, by polling Redis) and returns the original's result as a replay. Waiting is *not* shielded: if that caller leaves too, nobody needs the answer.
- **The agent resends a lost call with the same key, even writes.** It's safe only because the gateway dedupes by key. The agent's timeout (180 s) is longer than the gateway's (120 + 15 s), so the gateway records `unknown`; the agent never just hangs up first.
- **A lost connection is never a known failure.** The SDK reports a dead connection as `MCPError(CONNECTION_CLOSED)`, synthesized locally. Treating it as "the server answered" recorded writes that may have run as failed, and the agent would redo them. A server could use -32000 itself; we still treat it as lost, erring toward a human check, never a duplicate.
- **`SupervisedClient` unpublishes a dead connection at once** and the supervisor reconnects. An `mcp.Client` is finished for good after its connection dies, and closing it takes time; callers in that window were handed the same dead client. Waiters poll for a live client (50 ms) instead of an Event that could be swapped under them.
- **Approval pauses the graph instead of returning an error.** On `pending_approval` the tools node calls LangGraph `interrupt()`; state is checkpointed (Redis, 24 h TTL), so the process may exit and another one resume it (`--resume`). Proven with a real model: process 1 paused and exited, a human approved from the CLI, process 2 resumed and deleted the bucket once.
- **The gateway decides, not the resume value.** Resuming just re-sends the same call with the same key: approved → the single-use grant is consumed and it runs; rejected → the agent gets the rejection. A forged or stale resume can't approve anything.
- **Resume re-runs the paused node from the top** (LangGraph semantics), so calls made earlier in that turn run again. Their idempotency keys make that a free replay; a test checks the earlier launch isn't repeated.
- **One task = one checkpoint thread** (`thread_id = task_id`), so the guard's state (budget, loops, audit) and the agent's state (messages) share an identity and both survive restarts.
- **`decide()` claims a decision with an atomic `HDEL`.** It used to read then write: Approve and Reject clicked at the same moment both succeeded, and the guard checks the grant first. Now exactly one wins.
- **Redis 8** (`redis:8.10.2-alpine`) because the LangGraph checkpointer needs RedisJSON + RediSearch, built in since Redis 8. RediSearch indexes only work in **database 0**, so checkpoints must live there; `run_agent` refuses another db with a clear message.
- **The approvals UI is outside MCP and bound to 127.0.0.1.** It writes to Redis directly, so the agent has no path to it. It has no login yet and approver names are self-declared.
- **Prompt lesson:** telling the model "destructive actions need approval" made it *skip* the deletion and claim approval was pending. The prompt now says: call the tool; AgentGuard pauses and a human decides. Enforcement lives outside the model; the prompt just mustn't fight it.
- **Every decision and outcome is appended to a Redis Stream** `ag:audit:{task}`: append-only and ordered, with `XRANGE` for replay.

## Future work

- Budget refunds when a write fails definitively (today a reserved cost is kept).
- Stuck `in_progress` keys if the gateway crashes mid-call (today they expire after 24h).
- Redact secrets from audited arguments.
- **Argument-level policy rules**, e.g. `scale_service: require_approval if replicas > 6`, or only for named services. Seen in every real run: the agent scaled `api` 2 → 10–12 and the *healthy* `db` 1 → 4, and the policy allowed each step because it can only judge the tool name.
- **Approvals UI auth** (SSO/OAuth) and verified approver identity; today it's localhost-only with a self-declared name.
- **Gateway crash mid-call**: the detached call dies with the process and the key stays `in_progress` (24 h). Needs a lease (claim with a short TTL, renewed while running) and a reaper that turns expired leases into `unknown`, never into "absent", which would allow a re-run.
- `MCPError(INTERNAL_ERROR)` from an HTTP 5xx is still treated as a known answer; a server that crashed mid-write could have acted. Could be classified `unknown` for writes.
- Real Auto Scaling groups instead of tagged instances (needs a paid LocalStack plan or real AWS).
- More clouds: AgentGuard is cloud-agnostic; Azure/GCP need only another MCP server and `azure__*` / `gcp__*` policy rules.
