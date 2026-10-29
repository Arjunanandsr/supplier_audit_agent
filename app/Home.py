"""AuditPilot · Overview. Run with:  streamlit run app/Home.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))  # app/ on the path for "import ui"

import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import ui
from core import db
from core.config import ACCESS_LABEL, ACCESS_ORDER, CRIT_ORDER, TIER_COLOR, TIER_ORDER

ui.setup("Overview")
g_all = ui.golden()
g = ui.visible(g_all)
ui.role_note(g_all, g)

conf = db.read_table("conflicts")
pending = int((conf.status == "pending").sum()) if len(conf) else 0
plans = db.read_table("audit_plans")

c = st.columns(6)
c[0].metric("Suppliers", len(g))
c[1].metric("Audit-eligible", int(g.eligible.sum()))
c[2].metric("High + Critical", int(g.risk_tier.isin(["High", "Critical"]).sum()))
c[3].metric("Overdue audits", int(g.overdue.sum()))
c[4].metric("Open data conflicts", pending)
c[5].metric("Avg data quality", f"{g.data_quality.mean():.0%}" if len(g) else "—")

left, right = st.columns([1, 1.2])
with left:
    st.subheader("Suppliers by risk tier")
    counts = g.risk_tier.value_counts().reindex(TIER_ORDER, fill_value=0).reset_index()
    counts.columns = ["tier", "suppliers"]
    fig = px.bar(counts, x="tier", y="suppliers", color="tier", color_discrete_map=TIER_COLOR, text="suppliers")
    fig.update_layout(showlegend=False, height=320, margin=dict(l=10, r=10, t=10, b=10), xaxis_title=None)
    st.plotly_chart(fig, width="stretch")
with right:
    st.subheader("Criticality × data sensitivity")
    pv = (g.assign(acc=g.data_access_level.fillna("none"), crit=g.criticality_tier.fillna("medium"))
          .pivot_table(index="crit", columns="acc", values="golden_id", aggfunc="count", fill_value=0)
          .reindex(index=CRIT_ORDER[::-1], columns=ACCESS_ORDER, fill_value=0))
    fig = go.Figure(go.Heatmap(z=pv.values, x=[ACCESS_LABEL[a] for a in pv.columns],
                               y=[x.title() for x in pv.index], colorscale="OrRd", text=pv.values,
                               texttemplate="%{text}", showscale=False))
    fig.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10))
    st.plotly_chart(fig, width="stretch")

st.subheader("Highest-risk suppliers")
top = g.sort_values("risk_score", ascending=False).head(15).copy()
top["data_access"] = top.data_access_level.map(ui.access_label)
top["planned"] = top.golden_id.isin(plans.golden_id if len(plans) else [])
st.dataframe(
    top[["golden_id", "legal_name", "country", "risk_score", "risk_tier", "data_access", "eligibility_status",
         "next_due", "overdue", "data_quality", "pending_conflicts", "planned"]],
    hide_index=True, width="stretch",
    column_config={
        "risk_score": st.column_config.ProgressColumn("Risk score", min_value=0, max_value=100, format="%.1f"),
        "data_quality": st.column_config.ProgressColumn("Data quality", min_value=0, max_value=1, format="%.2f"),
        "overdue": st.column_config.CheckboxColumn("Overdue"),
        "planned": st.column_config.CheckboxColumn("Plan exists"),
        "legal_name": "Supplier", "golden_id": "ID", "risk_tier": "Tier", "eligibility_status": "Eligibility",
        "next_due": "Next due", "pending_conflicts": "Conflicts", "data_access": "Data access",
    })

last = db.get_meta("last_run") or {}
if last:
    with st.expander("Last pipeline run"):
        st.json(last)
st.caption("All supplier data is synthetic. Scores are rule-based (config/weights.yaml); AI only explains.")
