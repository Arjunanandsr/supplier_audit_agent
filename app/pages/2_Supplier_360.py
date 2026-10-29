import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # app/ on the path for "import ui"
import json
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import ui
from core import ai, db
from core.config import TIER_COLOR
from core.notifier import make_ics
from core.planner import active_plan, generate_plan

ui.setup("Supplier 360")
g_all = ui.golden()
g = ui.visible(g_all)
ui.role_note(g_all, g)
gid = ui.supplier_picker(g)
if not gid:
    st.stop()
r = {k: (None if not isinstance(v, (list, dict)) and pd.isna(v) else v)
     for k, v in g[g.golden_id == gid].iloc[0].to_dict().items()}
factors = json.loads(r["factors"])
overrides = ui.jl(r["overrides"])
flags = ui.jl(r["red_flags"])

st.markdown(f"## {r['legal_name']} &nbsp; {ui.tier_badge(r['risk_tier'])}", unsafe_allow_html=True)
st.caption(f"{gid} · Ariba {r['ariba_id']} · {r['country']} · {r['category_name']} · "
           f"audit owner {r['audit_owner']} · supplier owner {r['supplier_owner']} · ISM {r['ism']}")
if r["pending_conflicts"]:
    st.warning(f"{int(r['pending_conflicts'])} data conflict(s) awaiting review. The value used now follows the "
               "conflict rule (most restrictive wins); see Data consolidation.")
if r["low_confidence"]:
    st.warning(f"Low confidence: data quality {r['data_quality']:.0%} is below 60%.")

top = st.columns([1, 1.6])
with top[0]:
    fig = go.Figure(go.Indicator(
        mode="gauge+number", value=r["risk_score"], number={"suffix": " / 100"},
        gauge={"axis": {"range": [0, 100]}, "bar": {"color": TIER_COLOR[r["risk_tier"]]},
               "steps": [{"range": [0, 40], "color": "#eef6f0"}, {"range": [40, 60], "color": "#fbf3dc"},
                         {"range": [60, 80], "color": "#fbe7da"}, {"range": [80, 100], "color": "#f6dcd9"}]}))
    fig.update_layout(height=230, margin=dict(l=20, r=20, t=20, b=0))
    st.plotly_chart(fig, width="stretch")
    st.caption(f"Weighted sum {r['raw_score']:.1f}" + (f" · override: {'; '.join(overrides)}" if overrides else "")
               + f" · weights v{r['weights_version']} · data quality {r['data_quality']:.0%}")
with top[1]:
    fdf = pd.DataFrame(factors.values()).sort_values("contribution")
    fig = go.Figure(go.Bar(x=fdf.contribution, y=fdf.label, orientation="h", marker_color="#2f6fdb",
                           text=[f"+{v:.1f}" for v in fdf.contribution], textposition="outside",
                           customdata=fdf[["score", "weight", "detail"]].values,
                           hovertemplate="%{y}: factor %{customdata[0]} × weight %{customdata[1]}"
                                         "<br>%{customdata[2]}<extra></extra>"))
    fig.update_layout(height=260, margin=dict(l=10, r=30, t=10, b=10), xaxis_title="Points contributed")
    st.plotly_chart(fig, width="stretch")

key = f"narr_{gid}_{r['risk_score']}"
if key not in st.session_state:
    st.session_state[key] = ai.risk_narrative(gid, r["risk_score"], r["risk_tier"], factors, overrides,
                                              r["data_quality"])
text, src = st.session_state[key]
st.info(f"**Why this score** ({'AI narrative, grounded on stored factors' if src == 'ai' else 'template'}): {text}")

left, right = st.columns(2)
with left:
    st.subheader("Eligibility & scope")
    st.markdown(f"**{r['eligibility_status']}**" + (f" · {r['audit_type']} every {int(r['interval_months'])} months"
                                                     if r["eligible"] else ""))
    for reason in ui.jl(r["eligibility_reasons"]):
        st.markdown(f"- {reason}")
    if r["eligible"]:
        due_txt = f"Next due **{r['next_due']}**" + (" · 🔴 **Overdue**" if r["overdue"] else "")
        st.markdown(due_txt)
        st.markdown("**Scope modules**")
        for m in ui.jl(r["scope_modules"]):
            st.markdown(f"- {m['name']} — _{m['reason']}_")
with right:
    st.subheader("Golden record")
    rec = {
        "Data access": ui.access_label(r["data_access_level"]),
        "Users with system access": "—" if r["users_with_system_access"] is None else int(r["users_with_system_access"]),
        "Criticality": r["criticality_tier"], "Annual contract value": ui.inr(r["contract_value_inr"]),
        "Contract": f"{r['contract_start'] or '—'} → {r['contract_end'] or '—'}",
        "Service type": r["service_type"] or "—", "Certifications": r["certification_detail"] or "None on file",
        "Assessment score": "—" if r["assessment_score"] is None else int(r["assessment_score"]),
        "AI red flags": ", ".join(ai.FLAG_LABEL.get(f, f) for f in flags) or "None",
        "Last audit": f"{r['last_audit_date']} ({r['last_audit_rating']})" if r["last_audit_date"] else "Never audited",
        "Open findings": f"{int(r['open_critical'])} critical · {int(r['open_high'])} high · {int(r['open_medium'])} medium",
        "Offshore delivery": {1: "Yes", 0: "No"}.get(r["offshore_delivery"], "—"),
        "Uses sub-processors": {1: "Yes", 0: "No"}.get(r["uses_subprocessors"], "—"),
        "In sources": " · ".join(s for s, f in [("Ariba", 1), ("VMS", r["in_vms"]), ("GBAMS", r["in_gbams"]),
                                                 ("PCM", r["in_pcm"])] if f),
    }
    st.dataframe(pd.DataFrame(rec.items(), columns=["Attribute", "Value"]).astype(str), hide_index=True,
                 width="stretch")
    with st.expander("Assessment comments"):
        st.write(r["assessment_comments"])

st.subheader("Audit plan")
plan = active_plan(gid)
if len(plan):
    p = plan.iloc[0]
    st.success(f"Plan **{p['plan_id']}** · audit on **{p['audit_date']}** · {p['audit_type']} · status {p['status']}")
    tasks = db.query("SELECT seq, title, owner, due_date, status FROM tasks WHERE plan_id=? ORDER BY seq",
                     (p["plan_id"],))
    st.dataframe(tasks, hide_index=True, width="stretch")
    cl = json.loads(p["checklist"] or "{}")
    with st.expander(f"Audit checklist ({'AI-drafted' if cl.get('source') == 'ai' else 'standard'})"):
        names = {m["id"]: m["name"] for m in json.loads(p["scope"])}
        for mid, items in cl.get("items", {}).items():
            st.markdown(f"**{names.get(mid, mid)}**")
            for it in items:
                st.markdown(f"- [ ] {it}")
    st.download_button("📅 Download calendar invite (.ics)",
                       make_ics(f"Audit: {r['legal_name']}", date.fromisoformat(p["audit_date"]), p["audit_type"]),
                       file_name=f"{p['plan_id']}.ics", mime="text/calendar")
elif r["eligible"]:
    sched = db.query("SELECT * FROM schedule WHERE golden_id=?", (gid,))
    if len(sched) and sched.iloc[0]["audit_date"]:
        st.write(f"Scheduler slot: **{sched.iloc[0]['audit_date']}** ({sched.iloc[0]['status']}).")
    if ui.role() == "Audit team":
        if st.button("⚡ Generate audit plan", type="primary"):
            with st.spinner("Creating tasks, checklist and notifications…"):
                out = generate_plan(gid, ui.user())
            st.session_state["flash360"] = out["message"]
            st.rerun()
    else:
        st.caption("Only the audit team can generate audit plans.")
else:
    st.write("No audit needed while the supplier is not eligible.")
if st.session_state.get("flash360"):
    st.success(st.session_state.pop("flash360") + " Owners notified (see Tasks & notifications).")

reps = db.query("SELECT report_id, ts, source_name, mode, overall_rating, risk_level, accepted FROM analysis_reports "
                "WHERE golden_id=? ORDER BY ts DESC", (gid,)) if db.table_exists("analysis_reports") else pd.DataFrame()
with st.expander(f"AI analyses of this supplier ({len(reps)})"):
    if len(reps):
        st.dataframe(reps, hide_index=True, width="stretch")
        st.caption("Open a report in AI Audit Analyst → sidebar → Past analyses.")
    else:
        st.caption("No reports analysed yet. Upload one on the AI Audit Analyst page.")

hist = pd.read_csv(ui.ROOT / "data" / "raw" / "audit_history.csv", dtype=str)
hist = hist[hist.supplier_ref == r["ariba_id"]].sort_values("audit_date", ascending=False)
with st.expander(f"Audit history ({len(hist)})"):
    st.dataframe(hist.drop(columns=["data_label"]), hide_index=True, width="stretch")
