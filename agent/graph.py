"""A ReAct agent as an explicit LangGraph: think -> act -> observe -> think ...

    START -> agent --(tool calls?)--> tools -> agent -> ... -> END

We write the tools node ourselves instead of using langgraph's prebuilt ToolNode,
because each MCP call must carry `_meta` (task id + idempotency key), and
ToolNode only passes tool arguments.

The model is injected, so tests can drive the graph with a scripted fake model.

Human approval: when AgentGuard answers "pending_approval", the tools node calls
LangGraph's interrupt(). The run pauses and its state is saved by the checkpointer
(Redis in production), so the process may even exit. Whoever resumes it, possibly a
new process, re-sends the same call with the same idempotency key: approved, it
runs once; rejected, the agent gets the rejection. The agent cannot approve anything
itself. Approval lives outside MCP (UI / CLI writing to Redis).

On resume LangGraph re-runs the paused node from the top, so calls that already ran
earlier in that node run again. Their idempotency keys make that a free replay.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import interrupt

from agent.mcp_bridge import call_mcp_tool

SYSTEM_PROMPT = """You are an on-call SRE agent. An alert has fired; find the cause and fix it.

How to work:
- Investigate before acting: read logs and list services/instances first.
- Prefer the smallest reversible fix (e.g. scaling a service) over launching or deleting things.
- Never repeat a call with identical arguments; if a result didn't help, try a different tool.
- Every tool goes through AgentGuard. If a result starts with "[AgentGuard]", read the reason
  and adapt: a loop block means change approach; a budget block means stop spending.
- If a destructive action (e.g. deleting) is the right fix, call the tool. Don't ask for
  permission yourself and don't stop early: AgentGuard pauses the run, a human approves or
  rejects, and you then see the real result. If a human rejected it, don't retry it; find
  another way or report it in your summary.
- When the incident is handled (or you can do no more), stop calling tools and reply with a
  short summary: cause, actions taken, anything waiting for a human."""


def build_agent(model: Any, client: Any, tool_specs: list[dict[str, Any]], checkpointer: Any = None):
    """`model`: any LangChain chat model (bind_tools + ainvoke). `client`: a SupervisedClient
    or mcp.Client connected to AgentGuard. With a `checkpointer`, the run pauses for human
    approval (interrupt); without one, a pending approval comes back to the model as an error."""
    llm = model.bind_tools(tool_specs)

    async def agent_node(state: MessagesState) -> dict:
        response = await llm.ainvoke([SystemMessage(SYSTEM_PROMPT), *state["messages"]])
        return {"messages": [response]}

    async def tools_node(state: MessagesState, config: RunnableConfig) -> dict:
        task_id = config["configurable"]["task_id"]
        # Sequential on purpose: two writes from one model turn must not race,
        # and the audit trail stays in the order the model asked for them.
        results = []
        for tc in state["messages"][-1].tool_calls:
            message = await call_mcp_tool(client, tc, task_id)
            if checkpointer is not None and _verdict(message) == "pending_approval":
                # Pause here. On resume this node re-runs from the top, so the call
                # above re-sends with the same key: approved, the gateway runs it;
                # rejected, it says so. interrupt() only returns (instead of pausing)
                # if we were resumed while still pending; then the model gets the
                # pending message as an error. The gateway decides, never the runner.
                interrupt({
                    "approval_id": message.artifact["agentguard"].get("agentguard/approval_id"),
                    "task_id": task_id, "tool": tc["name"], "args": tc["args"], "reason": message.content,
                })
            results.append(message)
        return {"messages": results}

    def route(state: MessagesState) -> str:
        return "tools" if getattr(state["messages"][-1], "tool_calls", None) else END

    graph = StateGraph(MessagesState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route, ["tools", END])
    graph.add_edge("tools", "agent")
    return graph.compile(checkpointer=checkpointer)


def _verdict(message: Any) -> str | None:
    return ((getattr(message, "artifact", None) or {}).get("agentguard") or {}).get("agentguard/verdict")
