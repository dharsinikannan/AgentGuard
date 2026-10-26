"""Approvals: the human side of require_approval.

Talks to Redis directly and is never exposed as an MCP tool: the agent has no
path to it, so it cannot approve its own actions. Each approval is bound to one
task + tool + exact arguments and is single-use (see agentguard/guard.py).

Binds to 127.0.0.1 (.streamlit/config.toml) because it has no login yet; the
approver name is self-declared. Real deployments put it behind SSO.
"""

from __future__ import annotations

import getpass
import json
import time

import streamlit as st

from agentguard.guard import decide, list_pending
from agentguard.insights import is_demo, sync_call

st.title("✅ Approvals")
approver = st.sidebar.text_input("Your name (recorded in the audit trail)",
                                 value="visitor" if is_demo() else getpass.getuser())


def _decide(aid: str, approve: bool) -> None:
    try:
        req = sync_call(decide, aid, approve, who=f"ui:{approver or 'unknown'}")
        st.toast(f"{'Approved' if approve else 'Rejected'} {req['tool']} for {req['task_id']}")
    except KeyError as e:  # someone else decided first, or it expired
        st.toast(str(e).strip("'"))


@st.fragment(run_every="2s")
def pending_panel() -> None:
    try:
        pending = sync_call(list_pending)
    except Exception as e:  # Redis down: say so instead of a stack trace
        st.error(f"Cannot reach Redis: {e}")
        return
    st.subheader(f"Waiting for a decision ({len(pending)})")
    if not pending:
        st.caption("Nothing to approve. Paused agents show up here within 2 seconds"
                   + (" (in the demo: run the incident on 'Try it')." if is_demo() else "."))
    for aid, req in sorted(pending.items(), key=lambda kv: kv[1]["requested_at"]):
        with st.container(border=True):
            left, right = st.columns([4, 1])
            age = int(time.time() - req["requested_at"])
            left.markdown(f"**`{req['tool']}`** · task `{req['task_id']}` · waiting {age}s · id `{aid}`")
            if req.get("reason"):
                left.caption(f"why approval: {req['reason']}")
            left.code(json.dumps(req["args"], indent=2), language="json")
            if right.button("✅ Approve", key=f"approve-{aid}", use_container_width=True):
                _decide(aid, True)
            if right.button("❌ Reject", key=f"reject-{aid}", use_container_width=True):
                _decide(aid, False)


pending_panel()
