import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # app/ on the path for "import ui"
import pandas as pd
import plotly.express as px
import streamlit as st

import ui
from core import ai, db
from core.config import RAW_DIR
from core.pipeline import decide_match, resolve_conflict
from core.risk import what_if

ui.setup("Data consolidation")
st.caption("Ingest → normalize → match → survive → publish. Four source systems become one golden record per "
           "supplier, with the source of every field.")

g = ui.golden()
matches = db.read_table("matches")
conf = db.read_table("conflicts")
last = db.get_meta("last_run") or {}

c = st.columns(5)
rows = last.get("source_rows", {})
c[0].metric("Ariba rows", rows.get("ARIBA", 0), help=f"{last.get('ariba_duplicates_removed', 0)} exact duplicates removed")
c[1].metric("VMS rows", rows.get("VMS", 0))
c[2].metric("GBAMS rows", rows.get("GBAMS", 0))
c[3].metric("PCM rows", rows.get("PCM", 0))
c[4].metric("Golden records", len(g))

tab_cov, tab_conf, tab_match, tab_lin, tab_raw = st.tabs(
    ["Source coverage", f"Conflict queue ({int((conf.status == 'pending').sum()) if len(conf) else 0})",
     f"Match review ({int(matches.status.isin(['review', 'unmatched']).sum()) if len(matches) else 0})",
     "Field lineage", "Raw sources"])

with tab_cov:
    m = matches.groupby(["source", "status"]).size().reset_index(name="records")
    fig = px.bar(m, x="source", y="records", color="status", barmode="stack",
                 color_discrete_map={"auto": "#4c9a6a", "approved": "#2f6fdb", "review": "#d4a72c",
                                     "unmatched": "#c0392b", "rejected": "#888"})
    fig.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10))
    st.plotly_chart(fig, width="stretch")
    cov = pd.DataFrame({"Present in": ["Ariba", "VMS", "GBAMS", "PCM"],
                        "Golden records": [len(g), int(g.in_vms.sum()), int(g.in_gbams.sum()), int(g.in_pcm.sum())]})
    cov["Coverage"] = (cov["Golden records"] / max(len(g), 1)).map("{:.0%}".format)
    st.dataframe(cov, hide_index=True)
    st.markdown("**Matching rules:** exact tax ID first; otherwise fuzzy name + country (rapidfuzz "
                "`token_sort_ratio`). ≥ 90 auto-merge · 80–89 human review · < 80 separate record.")

with tab_conf:
    if not len(conf):
        st.success("No conflicts.")
    else:
        status_f = st.radio("Show", ["pending", "resolved", "all"], horizontal=True)
        view = conf if status_f == "all" else conf[conf.status == status_f]
        st.dataframe(view[["golden_id", "legal_name", "field", "ariba_value", "other_source", "other_value",
                           "rule_value", "rule", "status", "resolved_value"]],
                     hide_index=True, width="stretch")
        pend = conf[conf.status == "pending"]
        if len(pend):
            st.markdown("#### Review a conflict")
            label = {f"{r.golden_id}|{r.field}": f"{r.legal_name} · {r.field} · Ariba '{r.ariba_value}' vs "
                                                 f"{r.other_source} '{r.other_value}'" for r in pend.itertuples()}
            keys = list(label)
            default = next((k for k in keys if st.session_state.get("selected") and
                            k.startswith(st.session_state["selected"])), keys[0])
            pick = st.selectbox("Conflict", keys, index=keys.index(default), format_func=label.get)
            gid, field = pick.split("|")
            row = pend[(pend.golden_id == gid) & (pend.field == field)].iloc[0].to_dict()

            a, b = st.columns(2)
            for col, src, val in [(a, "Ariba", row["ariba_value"]), (b, row["other_source"], row["other_value"])]:
                res = what_if(g, gid, field, val)
                col.markdown(f"**{src} says:** `{val}`")
                col.markdown(f"Score if used: **{res['risk_score']:.1f}** {ui.tier_badge(res['risk_tier'])}",
                             unsafe_allow_html=True)
            exp = ai.explain_conflict(row)
            st.info(f"**{'AI' if exp['source'] == 'ai' else 'Rule'} recommendation:** `{exp['recommended_value']}` — "
                    f"{exp['reason']}")
            choice = st.radio("Resolve to", [row["rule_value"]] + [v for v in (row["ariba_value"], row["other_value"])
                                                                   if v != row["rule_value"]], horizontal=True)
            if st.button("✅ Approve decision", type="primary"):
                decision = "Approved rule value" if choice == row["rule_value"] else "Overridden"
                out = resolve_conflict(gid, field, choice, decision, ui.user())
                st.session_state["flash"] = (f"Resolved. {gid} tier {out['before']['risk_tier']} → "
                                             f"{out['after']['risk_tier']} (score {out['before']['risk_score']:.1f} → "
                                             f"{out['after']['risk_score']:.1f}). Logged to audit trail.")
                st.rerun()
    if st.session_state.get("flash"):
        st.success(st.session_state.pop("flash"))

with tab_match:
    q = matches[matches.status.isin(["review", "unmatched", "approved", "rejected"])]
    st.dataframe(q[["source", "source_id", "source_name", "ariba_name", "score", "method", "status"]],
                 hide_index=True, width="stretch")
    todo = q[q.status.isin(["review", "unmatched"]) & (q.ariba_name != "")]
    if len(todo):
        opts = {f"{r.source}|{r.source_id}": f"{r.source} {r.source_id}: '{r.source_name}' → '{r.ariba_name}' "
                                             f"({r.score})" for r in todo.itertuples()}
        k = st.selectbox("Decide a match", list(opts), format_func=opts.get)
        c1, c2 = st.columns(2)
        s, sid = k.split("|")
        if c1.button("Approve match"):
            decide_match(s, sid, "approve", ui.user())
            st.rerun()
        if c2.button("Reject: keep separate"):
            decide_match(s, sid, "reject", ui.user())
            st.rerun()
    st.caption("Records with no candidate (e.g. vendors in VMS never onboarded in Ariba) stay unmatched and are "
               "flagged for onboarding.")

with tab_lin:
    gid = ui.supplier_picker(ui.visible(g), key="lin_pick")
    if gid:
        lin = db.query("SELECT field, value, source, rule FROM lineage WHERE golden_id=?", (gid,))
        st.dataframe(lin, hide_index=True, width="stretch")

with tab_raw:
    files = sorted(RAW_DIR.glob("*.csv"))
    f = st.selectbox("File", files, format_func=lambda p: p.name)
    df = pd.read_csv(f, dtype=str, keep_default_na=False)
    st.caption(f"{len(df)} rows · every row is labelled SYNTHETIC")
    st.dataframe(df.head(200), hide_index=True, width="stretch")
