import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # app/ on the path for "import ui"
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import ui
from core import db
from core.config import TIER_COLOR, TIER_ORDER, rules
from core.planner import generate_plan

ui.setup("Audit planner")
p = rules()["planner"]
st.caption(f"Risk-ordered, capacity-aware schedule: {p['auditors']} auditors × {p['audits_per_auditor_per_month']} "
           f"audits/month = {p['auditors'] * p['audits_per_auditor_per_month']} per month, "
           f"{p['horizon_months']}-month horizon. Edit config/rules.yaml to change.")

sched = db.read_table("schedule")
cap = db.read_table("capacity")
plans = db.read_table("audit_plans")
vis_ids = set(ui.visible(ui.golden()).golden_id)
sched = sched[sched.golden_id.isin(vis_ids)]

c = st.columns(4)
c[0].metric("Scheduled in horizon", int((sched.status == "Scheduled").sum()))
c[1].metric("Capacity breaches", int((sched.status == "Capacity breach").sum()))
c[2].metric("Beyond horizon", int((sched.status == "Beyond horizon").sum()))
c[3].metric("Plans generated", len(plans))

st.subheader("Monthly load vs capacity")
fig = go.Figure()
fig.add_bar(x=cap.month, y=cap.planned, name="Planned audits", marker_color="#2f6fdb")
fig.add_scatter(x=cap.month, y=cap.capacity, name="Capacity", mode="lines", line=dict(color="#c0392b", dash="dash"))
fig.update_layout(height=280, margin=dict(l=10, r=10, t=10, b=10), legend=dict(orientation="h"))
st.plotly_chart(fig, width="stretch")

st.subheader("Audit timeline")
s = sched.dropna(subset=["audit_date"]).copy()
if len(s):
    s["audit_date"] = pd.to_datetime(s.audit_date)
    s["has_plan"] = s.golden_id.isin(plans.golden_id if len(plans) else [])
    fig = px.scatter(s, x="audit_date", y="risk_tier", color="risk_tier", symbol="has_plan",
                     color_discrete_map=TIER_COLOR, category_orders={"risk_tier": TIER_ORDER[::-1]},
                     hover_data=["legal_name", "golden_id", "risk_score", "next_due", "status"],
                     symbol_map={True: "diamond", False: "circle"})
    fig.update_traces(marker=dict(size=11, opacity=0.8))
    fig.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10), yaxis_title=None, xaxis_title=None)
    st.plotly_chart(fig, width="stretch")
    st.caption("Diamonds = audit plan already generated.")

st.subheader("Schedule")
st.dataframe(sched[["golden_id", "legal_name", "risk_tier", "risk_score", "next_due", "scheduled_month",
                    "audit_date", "status", "overdue"]].sort_values(["audit_date", "risk_score"],
                                                                    ascending=[True, False]),
             hide_index=True, width="stretch")

if ui.role() == "Audit team":
    st.subheader("Bulk-generate plans")
    months = sorted(sched.scheduled_month.dropna().unique())
    if months:
        upto = st.select_slider("Generate plans for audits scheduled up to", months, value=months[min(1, len(months) - 1)])
        todo = sched[(sched.scheduled_month <= upto) & (sched.status != "Beyond horizon")
                     & ~sched.golden_id.isin(plans.golden_id if len(plans) else [])]
        st.write(f"{len(todo)} suppliers without a plan in that window.")
        if st.button(f"⚡ Generate {len(todo)} plans", disabled=not len(todo), type="primary"):
            bar = st.progress(0.0)
            for i, gid in enumerate(todo.golden_id):
                generate_plan(gid, ui.user(), use_ai_checklist=False)
                bar.progress((i + 1) / len(todo))
            st.success(f"Created {len(todo)} plans with tasks; owners notified.")
            st.rerun()
