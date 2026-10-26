# Demo video script (~3 minutes)

Audience: recruiters and interviewers who have 3 minutes and no context. Show the problem,
show it being stopped, show the numbers. Record at 1080p; Windows: **Win + Alt + R** (Xbox
Game Bar) records the active window, or use OBS. Speak over it live or add voice-over after.

**Before recording:** `docker compose up -d --build`, open http://127.0.0.1:8501, run the
agent once off-camera to warm everything up, then `docker compose down -v && docker compose up -d`
for a clean slate. Zoom the terminal font to ~18 pt.

| Time | Screen | Say |
|---|---|---|
| 0:00–0:20 | README top (diagram) | "AI agents are starting to run cloud infrastructure. When they go wrong, they loop, overspend, delete things, or do the same thing twice after a network blip. AgentGuard sits between the agent and its tools and stops that." |
| 0:20–0:35 | Terminal: `docker compose ps` | "Everything runs locally: a real LangGraph agent on an LLM, AWS APIs on LocalStack, Redis, a GitHub server with OAuth, and the gateway. The agent holds no API keys at all; even its LLM calls go through AgentGuard." |
| 0:35–1:20 | Terminal: `docker compose run --rm agent` | Narrate as it scrolls: "Alert: API latency. It reads logs, checks metrics, scales the API service." Point at any `[deny]` / `[pending_approval]`: "Here it tried to scale past 6 replicas: policy says a human decides." |
| 1:20–1:45 | Dashboard → Approvals | Click ✅. "Approval is bound to that exact action and works once. The agent can't approve itself: approvals aren't a tool it can call." Back in the terminal the run resumes. |
| 1:45–2:10 | Dashboard → Overview | "Every decision is audited: what was allowed, blocked and why, and the spend split between tool calls and LLM tokens, all in one budget." |
| 2:10–2:40 | Dashboard → Try it → *Run all 42 scenarios* | "I evaluated it with 30 scripted bad agents and 12 good ones, judged by what actually executed. It stops 29 of 30: 94% of harmful actions, zero false blocks, about 3 milliseconds per call. And it shows the one it can't stop yet: an agent inventing new task ids. That needs authenticated agents, which is next." |
| 2:40–3:00 | README "Design decisions" scrolling | "Idempotency with atomic Redis claims, OAuth with no token passthrough, approvals that survive restarts. Code, tests and the live demo are linked below." |

If something goes differently live (the LLM picks another path), keep it: real agents vary, and
the gateway's answer is what matters. Don't cut the blocks; they are the point.
