"""Try it: run a misbehaving on-call agent through AgentGuard, then the eval suite."""

from __future__ import annotations

import asyncio
import uuid

import pandas as pd
import streamlit as st

from agentguard.insights import is_demo, sync_call
from evals.showcase import retry_approved, run_incident

COLOURS = {"allow": "🟢", "replay": "🔵", "pending_approval": "🟡", "deny": "🔴"}

st.title("▶️ Try it")
st.markdown(
    "A scripted on-call agent handles a latency incident and makes the mistakes real agents make: "
    "it loops, over-scales, touches a healthy service, wants a GPU box, retries a launch after a "
    "network blip, and reaches for IAM. Every call goes through the real AgentGuard gateway and the "
    "`prod-safe` policy" + (" (mock cloud, in-memory data)." if is_demo() else ".")
)
task = st.session_state.setdefault("task", f"demo-{uuid.uuid4().hex[:6]}")


def show(rows: list[dict]) -> None:
    frame = pd.DataFrame(rows)
    frame.insert(0, "", frame["verdict"].map(lambda v: COLOURS.get(v, "⚪")))
    st.dataframe(frame, hide_index=True, use_container_width=True)


left, right = st.columns(2)
if left.button("1. Run the incident", type="primary", use_container_width=True):
    st.session_state["rows"] = sync_call(run_incident, task)
    st.session_state.pop("retry", None)
if right.button("3. Retry the actions that needed a human", use_container_width=True,
                disabled="rows" not in st.session_state):
    st.session_state["retry"] = sync_call(retry_approved, task)

if "rows" in st.session_state:
    st.subheader(f"Task `{task}`")
    show(st.session_state["rows"])
    st.info("**2.** Three calls are waiting for a human (🟡). Open **Approvals**, approve some and reject "
            "others, then come back and press **3**. Approvals are single-use and bound to the exact "
            "arguments; rejected actions stay refused.", icon="👉")
if "retry" in st.session_state:
    st.subheader("After your decisions")
    show(st.session_state["retry"])

st.divider()
st.subheader("The eval suite")
st.caption("30 scripted bad agents and 12 good ones, judged by what actually executed downstream, "
           "each also run without AgentGuard as a baseline. Takes a few seconds.")
if st.button("Run all 42 scenarios"):
    from evals.run_evals import run_scenarios, totals

    with st.spinner("running..."):
        rows, _ = asyncio.run(run_scenarios())
    t = totals(rows)
    cols = st.columns(4)
    cols[0].metric("Bad scenarios stopped", f"{t['bad_stopped']}/{t['bad_scenarios']}")
    cols[1].metric("Harmful actions prevented", f"{t['harmful_prevented']}/{t['harmful_actions']}",
                   f"without AgentGuard {t['baseline_harmful_executed']} execute", delta_color="off")
    cols[2].metric("False blocks (good agents)", f"{t['good_scenario_false_blocks']}/{t['good_scenario_actions']}")
    cols[3].metric("Known gaps", len(t["known_gaps"]), help=", ".join(t["known_gaps"]))
    st.dataframe(pd.DataFrame(rows)[["scenario", "category", "kind", "harmful", "prevented", "benign",
                                     "false_blocks", "baseline_harm_executed", "known_gap"]],
                 hide_index=True, use_container_width=True)
