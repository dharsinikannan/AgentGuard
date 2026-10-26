"""Overview: every task AgentGuard has seen, what it allowed, blocked and charged."""

from __future__ import annotations

import time

import pandas as pd
import streamlit as st

from agentguard.insights import is_demo, overview, sync_call

SERIES_1 = "#2a78d6"   # one series -> one hue (validated default palette, slot 1)

st.title("🛡️ AgentGuard")
st.caption("A safety gateway between AI agents and their tools: loops, spend, approvals, duplicates.")
if is_demo():
    st.info("Public demo: everything runs in this process on in-memory data with a mock cloud. "
            "No API keys, no real infrastructure. Generate activity on **Try it**.", icon="ℹ️")


@st.fragment(run_every="3s")
def dashboard() -> None:
    try:
        data = sync_call(overview)
    except Exception as e:
        st.error(f"Cannot reach Redis: {e}")
        return
    tasks = data["tasks"]
    if not tasks:
        st.caption("No activity yet.")
        return

    decisions = sum(t["decisions"] for t in tasks)
    blocked = sum(t["blocked"] for t in tasks)
    cols = st.columns(5)
    cols[0].metric("Tasks", len(tasks))
    cols[1].metric("Tool decisions", decisions)
    cols[2].metric("Blocked", blocked, help="denied by policy, loops, budget, ownership, idempotency...")
    cols[3].metric("Waiting for a human", data["pending_approvals"])
    cols[4].metric("Spend (tools + LLM)",
                   f"₹{sum(t['spend_tools_inr'] + t['spend_llm_inr'] for t in tasks):,.2f}")

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Tasks")
        frame = pd.DataFrame(tasks).assign(
            last_event=lambda d: d["last_event"].map(lambda ts: time.strftime("%H:%M:%S", time.localtime(ts))))
        st.dataframe(frame, hide_index=True, use_container_width=True, column_config={
            "spend_tools_inr": st.column_config.NumberColumn("tools ₹", format="%.2f"),
            "spend_llm_inr": st.column_config.NumberColumn("LLM ₹", format="%.2f"),
        })
    with right:
        st.subheader("Blocked actions, by check")
        by_check = data["blocked_by_check"]
        if by_check:
            chart = pd.DataFrame({"check": list(by_check), "blocked": list(by_check.values())})
            st.bar_chart(chart, x="check", y="blocked", color=SERIES_1, horizontal=True,
                         height=40 + 36 * len(by_check))
            st.dataframe(chart, hide_index=True, use_container_width=True)   # the same numbers, exactly
        else:
            st.caption("Nothing blocked yet.")

    st.subheader("Latest events")
    recent = pd.DataFrame(data["recent"])
    recent["ts"] = recent["ts"].map(lambda ts: time.strftime("%H:%M:%S", time.localtime(float(ts))))
    keep = [c for c in ("ts", "task", "event", "verdict", "check", "tool", "reason", "cost") if c in recent]
    st.dataframe(recent[keep], hide_index=True, use_container_width=True)


dashboard()
