"""AgentGuard dashboard.

    streamlit run ui/app.py                          # reads REDIS_URL (default localhost)
    AGENTGUARD_DEMO=1 streamlit run ui/app.py        # public demo: in-memory, no secrets
"""

import streamlit as st

st.set_page_config(page_title="AgentGuard", page_icon="🛡️", layout="wide")
st.navigation([
    st.Page("overview.py", title="Overview", icon="📊", default=True),
    st.Page("try_it.py", title="Try it", icon="▶️"),
    st.Page("approvals.py", title="Approvals", icon="✅"),
    st.Page("audit.py", title="Audit trail", icon="📜"),
]).run()
st.sidebar.caption("[Source on GitHub](https://github.com/dharsinikannan/AgentGuard)")
