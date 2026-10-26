"""A ReAct agent as an explicit LangGraph: think -> act -> observe -> think ...

    START -> agent --(tool calls?)--> tools -> agent -> ... -> END

We write the tools node ourselves instead of using langgraph's prebuilt ToolNode,
because each MCP call must carry `_meta` (task id + idempotency key), and
ToolNode only passes tool arguments.

The model is injected, so tests can drive the graph with a scripted fake model.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, MessagesState, StateGraph

from agent.mcp_bridge import call_mcp_tool

SYSTEM_PROMPT = """You are an on-call SRE agent. An alert has fired; find the cause and fix it.

How to work:
- Investigate before acting: read logs and list services/instances first.
- Prefer the smallest reversible fix (e.g. scaling a service) over launching or deleting things.
- Never repeat a call with identical arguments; if a result didn't help, try a different tool.
- Every tool goes through AgentGuard. If a result starts with "[AgentGuard]", read the reason
  and adapt: a loop block means change approach; a budget block means stop spending;
  "requires human approval" means continue with other work and mention it in your summary.
- When the incident is handled (or you can do no more), stop calling tools and reply with a
  short summary: cause, actions taken, anything waiting for a human."""


def build_agent(model: Any, client: Any, tool_specs: list[dict[str, Any]]):
    """`model`: any LangChain chat model (bind_tools + ainvoke). `client`: a SupervisedClient
    or mcp.Client connected to AgentGuard."""
    llm = model.bind_tools(tool_specs)

    async def agent_node(state: MessagesState) -> dict:
        response = await llm.ainvoke([SystemMessage(SYSTEM_PROMPT), *state["messages"]])
        return {"messages": [response]}

    async def tools_node(state: MessagesState, config: RunnableConfig) -> dict:
        task_id = config["configurable"]["task_id"]
        # Sequential on purpose: two writes from one model turn must not race,
        # and the audit trail stays in the order the model asked for them.
        results = [await call_mcp_tool(client, tc, task_id) for tc in state["messages"][-1].tool_calls]
        return {"messages": results}

    def route(state: MessagesState) -> str:
        return "tools" if getattr(state["messages"][-1], "tool_calls", None) else END

    graph = StateGraph(MessagesState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route, ["tools", END])
    graph.add_edge("tools", "agent")
    return graph.compile()
