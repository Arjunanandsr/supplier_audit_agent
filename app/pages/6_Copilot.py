import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # app/ on the path for "import ui"
import json

import streamlit as st

import ui
from core import ai

ui.setup("Copilot")
st.caption("Ask in plain English. The model only chooses filter values for one fixed function "
           "(filter_suppliers); the app runs the filter itself and never executes model-written code.")

g = ui.visible(ui.golden())
examples = ["Critical or high suppliers with PII access and overdue audits",
            "Suppliers with expired certification and open findings",
            "Eligible suppliers in India with risk score above 60",
            "Which suppliers have data conflicts waiting?"]
cols = st.columns(len(examples))
for c, ex in zip(cols, examples):
    if c.button(ex, width="stretch"):
        st.session_state["copilot_q"] = ex

if "chat" not in st.session_state:
    st.session_state["chat"] = []
q = st.chat_input("Ask about your suppliers…") or st.session_state.pop("copilot_q", None)
if q:
    args, src = ai.copilot(q)
    res = ai.filter_suppliers(g, args)
    st.session_state["chat"].append({"q": q, "args": args, "src": src, "ids": res.golden_id.tolist()})

for turn in reversed(st.session_state["chat"][-6:]):
    with st.chat_message("user"):
        st.write(turn["q"])
    with st.chat_message("assistant"):
        res = g[g.golden_id.isin(turn["ids"])].sort_values("risk_score", ascending=False)
        st.write(f"**{len(res)} suppliers** match. Filters ({turn['src']}): `{json.dumps(turn['args'])}`")
        st.dataframe(res[["golden_id", "legal_name", "country", "risk_score", "risk_tier", "data_access_level",
                          "eligibility_status", "next_due", "overdue", "certification_status"]],
                     hide_index=True, width="stretch")
