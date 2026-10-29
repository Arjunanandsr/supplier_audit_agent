import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # app/ on the path for "import ui"
import streamlit as st

import ui
from core import audit_log, db

ui.setup("Audit log & governance")
st.markdown("""
| Control | How it is enforced here |
| --- | --- |
| Synthetic data only | Seeded generator; every source row labelled `SYNTHETIC` |
| Masking before AI | Only golden IDs and coded values reach the LLM (OpenAI API or Azure OpenAI); notification text uses a `{supplier}` placeholder |
| Role-based access | Sidebar role: supplier owners see their suppliers, ISMs their security-scoped suppliers |
| Human in the loop | Conflict resolutions, match decisions and audit plans need an explicit click |
| Audit trail | Append-only log below: who, when, action, before → after, rule/model version |
| Policy as config | `config/weights.yaml` and `config/rules.yaml`, versioned and stamped on every result |
""")

tab_log, tab_ai = st.tabs(["Audit trail", "AI call log"])
with tab_log:
    log = audit_log.read(1000)
    actions = sorted(log.action.unique()) if len(log) else []
    pick = st.multiselect("Action", actions)
    if pick:
        log = log[log.action.isin(pick)]
    st.dataframe(log, hide_index=True, width="stretch")
with tab_ai:
    calls = db.query("SELECT ts, kind, prompt_version, deployment, input_hash, status, latency_ms, output "
                     "FROM ai_calls ORDER BY call_id DESC LIMIT 500")
    if not len(calls):
        st.info("No live AI calls yet. The app is using deterministic fallbacks (no OpenAI or Azure key set) or cached answers.")
    st.dataframe(calls, hide_index=True, width="stretch")
