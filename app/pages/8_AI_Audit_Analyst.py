import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # app/ on the path for "import ui"
import json

import pandas as pd
import plotly.express as px
import streamlit as st

import ui
from core import ai, analyst, report_export
from core.config import RAW_DIR, ROOT, TIER_COLOR
from core.doc_io import SUPPORTED, excel_sheets, read_bytes, read_path

ui.setup("AI Audit Analyst", icon="🕵️")
SAMPLES = ROOT / "samples"
SEV_ICON = {"Critical": "🔴", "High": "🟠", "Medium": "🟡", "Low": "🔵", "Info": "⚪"}
RATING_COLOR = {"Satisfactory": "#4c9a6a", "Needs improvement": "#e07b39", "Unsatisfactory": "#c0392b",
                "Not applicable": "#888"}
SAMPLE_INFO = {
    "Nimbus_InfoSec_Audit_Report_2026.md": "External information security audit with 6 structured findings",
    "Orion_SOC2_Type2_Summary_2026.txt": "SOC 2 Type II summary with control exceptions (free text)",
    "Lotus_Self_Assessment_2026.txt": "Low-risk supplier self-assessment questionnaire",
    "External_Supplier_Risk_Feed.csv": "Third-party risk feed for 57 suppliers: sanctions, financials, adverse notes",
    "AuditPilot_Sample_Data.xlsx": "Excel workbook: every source system and the risk feed, one tab each",
}

st.caption("Give the agent a report or dataset from any source. It reads it step by step with tools, finds the "
           "supplier, checks every claim against the golden record and simulates the risk impact. Names and "
           "personal identifiers are masked before anything reaches the model; evidence quotes are verified "
           "against the source and scores are recomputed by the risk engine.")
mode_txt = (f"🟢 {ai.provider_label()} agent · {ai.model()} (tool calling)" if ai.enabled()
            else "⚪ Rule-based analyst (no OpenAI or Azure key)")
st.markdown(f"**Analyst mode:** {mode_txt}")

# ---------------- input ----------------
src = st.radio("Input", ["Sample reports", "Upload a file", "AuditPilot source data"], horizontal=True)
doc, err = None, None
try:
    if src == "Upload a file":
        up = st.file_uploader("Report or dataset", type=[e.strip(".") for e in SUPPORTED])
        if up is not None:
            sheet = None
            if up.name.lower().endswith((".xlsx", ".xls")):
                sheets = excel_sheets(up.getvalue())
                if len(sheets) > 1:
                    sheet = st.selectbox("Sheet", sheets, index=next((i for i, x in enumerate(sheets)
                                                                      if x.lower() != "readme"), 0))
            doc = read_bytes(up.name, up.getvalue(), sheet)
    elif src == "Sample reports":
        files = sorted((p for p in SAMPLES.glob("*") if p.is_file()), key=lambda p: (p.suffix in (".csv", ".xlsx"), p.name))
        f = st.selectbox("Synthetic sample", files, format_func=lambda p: f"{p.name} — {SAMPLE_INFO.get(p.name, '')}")
        sheet = None
        if f.suffix == ".xlsx":
            sheets = excel_sheets(f.read_bytes())
            sheet = st.selectbox("Sheet", sheets, index=sheets.index("External_Risk_Feed")
                                 if "External_Risk_Feed" in sheets else 0)
        doc = read_path(f, sheet)
    else:
        files = sorted(RAW_DIR.glob("*.csv"))
        f = st.selectbox("Source extract", files, format_func=lambda p: p.name)
        doc = read_path(f)
except Exception as exc:
    err = str(exc)
if err:
    st.error(f"Could not read the file: {err}")

golden = ui.golden()
if doc is not None:
    with st.expander(f"Preview · {doc.name} · " + (f"{doc.meta.get('words', 0)} words, {len(doc.sections)} sections"
                                                  if doc.kind == "text" else f"{len(doc.table)} rows × "
                                                                               f"{len(doc.table.columns)} columns")):
        if doc.meta.get("warning"):
            st.warning(doc.meta["warning"])
        if doc.kind == "text":
            st.text(doc.text[:4000] + ("…" if len(doc.text) > 4000 else ""))
        else:
            st.dataframe(doc.table.head(50), hide_index=True, width="stretch")
    c1, c2 = st.columns([1, 1.4])
    link = None
    if doc.kind == "text":
        opts = ["auto"] + golden.sort_values("legal_name").golden_id.tolist()
        names = dict(zip(golden.golden_id, golden.legal_name))
        link = c1.selectbox("Supplier", opts, format_func=lambda g: "Auto-detect from the document" if g == "auto"
                            else f"{names[g]} · {g}")
        link = None if link == "auto" else link
    else:
        c1.markdown("**Supplier**  \nMulti-supplier dataset: rows are matched to golden records.")
    focus = c2.text_input("Focus (optional)", placeholder="e.g. access control and sub-processors")
    force_rules = False
    if ai.enabled():
        force_rules = st.toggle("Use rule-based analyst instead of the AI agent", value=False)

    if st.button("🕵️ Run analysis", type="primary"):
        with st.status("Agent working…", expanded=True) as status:
            def on_step(s):
                icon = "✅" if s["status"] == "ok" else "⚠️"
                why = f" — {s['why']}" if s.get("why") else ""
                st.write(f"{icon} **Step {s['step']} · {s['tool']}**{why}  \n↳ {s['summary']}")
            rep = analyst.analyze(doc, golden_id=link, focus=focus, actor=ui.user(), on_step=on_step,
                                  force_rules=force_rules)
            status.update(label=f"Analysis complete · {len(rep['trace'])} steps · {rep['mode']}", state="complete",
                          expanded=False)
        st.session_state["analysis_id"] = rep["report_id"]

# ---------------- history ----------------
hist = analyst.list_reports()
if len(hist):
    with st.sidebar.expander("Past analyses", expanded=False):
        pick = st.selectbox("Open report", hist.report_id.tolist(),
                            format_func=lambda r: f"{hist.set_index('report_id').loc[r, 'source_name']} · "
                                                  f"{hist.set_index('report_id').loc[r, 'ts'][:16]}")
        if st.button("Open"):
            st.session_state["analysis_id"] = pick

rid = st.session_state.get("analysis_id")
rep = analyst.load_report(rid) if rid else None
if not rep:
    st.stop()

# ---------------- report ----------------
st.divider()
gid = rep.get("supplier_id")
sup_name = golden.set_index("golden_id").legal_name.get(gid) if gid else None


def badge(text, color):
    return (f"<span style='background:{color};color:white;padding:3px 12px;border-radius:12px;font-weight:600;"
            f"font-size:0.85em;margin-right:6px'>{text}</span>")


st.markdown(f"## Analysis: {rep['source_name']}")
st.markdown(badge(rep["overall_rating"], RATING_COLOR.get(rep["overall_rating"], "#888"))
            + badge(f"Risk {rep['risk_level']}", TIER_COLOR.get(rep["risk_level"], "#888"))
            + badge(f"Confidence {rep.get('confidence', '—')}", "#5b6b7f")
            + badge("AI agent" if rep["mode"] == "ai-agent" else "Rule-based", "#2f6fdb")
            + (badge("Accepted into risk model", "#4c9a6a") if rep.get("accepted") else ""),
            unsafe_allow_html=True)
st.caption(f"{rep['report_id']} · {rep['document_type']} · generated {rep['generated_at'][:16].replace('T', ' ')}"
           + (f" · supplier {sup_name} ({gid})" if gid else ""))

m = rep["metrics"]
c = st.columns(6)
c[0].metric("Findings", m["findings"])
c[1].metric("Open", m["open"])
c[2].metric("Critical open", m["critical_open"])
c[3].metric("High open", m["high_open"])
c[4].metric("Evidence verified", f"{m['evidence_verified_pct']}%" if m["evidence_verified_pct"] is not None else "—")
c[5].metric("Discrepancies", m["discrepancies"])

st.info(f"**Executive summary.** {rep['executive_summary']}")

si = rep.get("score_impact")
if si:
    st.subheader("Impact on the AuditPilot risk score")
    a, b, d = st.columns([1, 1, 2])
    cur, proj = si["current"], si["projected"]
    a.metric("Current", f"{cur['risk_score']:.1f}", help=cur["risk_tier"])
    b.metric("If findings are accepted", f"{proj['risk_score']:.1f}", f"{proj['risk_score'] - cur['risk_score']:+.1f}",
             delta_color="inverse")
    a.markdown(ui.tier_badge(cur["risk_tier"]), unsafe_allow_html=True)
    b.markdown(ui.tier_badge(proj["risk_tier"]), unsafe_allow_html=True)
    ch = si.get("inputs_changed", {})
    if ch:
        d.dataframe(pd.DataFrame([{"Input": k.replace("_", " "), "System now": json.dumps(v["from"]),
                                   "From this report": json.dumps(v["to"])} for k, v in ch.items()]),
                    hide_index=True, width="stretch")
    d.caption(si.get("explanation", "") + " Top drivers: "
              + ", ".join(f"{t['factor']} (+{t['contribution']})" for t in proj.get("top_factors", [])))

if rep["findings"]:
    st.subheader("Findings")
    fdf = pd.DataFrame(rep["findings"])
    left, right = st.columns(2)
    sev = fdf[fdf.status == "Open"].groupby("severity").size().reindex(analyst.SEVERITIES, fill_value=0).reset_index()
    sev.columns = ["severity", "open"]
    fig = px.bar(sev, x="severity", y="open", color="severity", text="open",
                 color_discrete_map={"Critical": "#c0392b", "High": "#e07b39", "Medium": "#d4a72c", "Low": "#5b8bd0",
                                     "Info": "#aaa"})
    fig.update_layout(showlegend=False, height=240, margin=dict(l=10, r=10, t=30, b=10), title="Open findings by severity",
                      xaxis_title=None, yaxis_title=None)
    left.plotly_chart(fig, width="stretch")
    area = fdf[fdf.status == "Open"].groupby("control_area").size().sort_values().reset_index(name="open")
    fig = px.bar(area, x="open", y="control_area", orientation="h", text="open")
    fig.update_traces(marker_color="#2f6fdb")
    fig.update_layout(height=240, margin=dict(l=10, r=10, t=30, b=10), title="Open findings by control area",
                      xaxis_title=None, yaxis_title=None)
    right.plotly_chart(fig, width="stretch")
    view = pd.DataFrame({
        "ID": fdf["id"], "Severity": fdf.severity.map(lambda s: f"{SEV_ICON.get(s, '')} {s}"), "Status": fdf.status,
        "Control area": fdf.control_area, "Finding": fdf.title,
        "Evidence": fdf.evidence, "Verified": fdf.evidence_verified.map(lambda v: "✅ in source" if v else "⚠️ not found"),
        "Recommendation": fdf.recommendation, "Owner": fdf.owner_role.map(ui.ROLE_LABELS),
        "Due (days)": fdf.due_in_days})
    if fdf.get("supplier_id") is not None and fdf.supplier_id.nunique() > 1:
        view.insert(1, "Supplier", fdf.supplier_id)
    st.dataframe(view, hide_index=True, width="stretch",
                 column_config={"Evidence": st.column_config.TextColumn(width="large"),
                                "Finding": st.column_config.TextColumn(width="medium")})

if rep.get("discrepancies"):
    st.subheader("Report vs golden record")
    st.dataframe(pd.DataFrame(rep["discrepancies"]).rename(columns={
        "field": "Field", "report_value": "Report says", "system_value": "AuditPilot has", "implication": "Why it matters"}),
        hide_index=True, width="stretch")

if rep.get("affected_suppliers"):
    st.subheader("Affected suppliers")
    af = pd.DataFrame(rep["affected_suppliers"])
    af.insert(1, "supplier", af.golden_id.map(golden.set_index("golden_id").legal_name))
    af["signals"] = af.signals.map(", ".join)
    st.dataframe(af, hide_index=True, width="stretch")

if rep.get("data_quality"):
    st.subheader("Data quality")
    for d in rep["data_quality"]:
        st.markdown(f"- {SEV_ICON.get(d['severity'], '')} **{d['issue']}** — {d['detail']}")

if rep.get("recommendations"):
    st.subheader("Recommended actions")
    for r in rep["recommendations"]:
        st.markdown(f"{r['priority']}. {r['action']}  \n<span style='color:#888'>Owner: "
                    f"{ui.ROLE_LABELS.get(r['owner_role'], r['owner_role'])} · within {r['due_in_days']} days</span>",
                    unsafe_allow_html=True)

with st.expander("Limitations, governance and agent trace"):
    mk = rep.get("masking", {})
    st.markdown(f"**Masked before AI:** {mk.get('supplier_names', 0)} supplier-name mentions, {mk.get('emails', 0)} "
                f"emails, {mk.get('phones', 0)} phone numbers, {mk.get('tax_ids', 0)} tax IDs.")
    for x in rep.get("limitations", []):
        st.markdown(f"- {x}")
    st.dataframe(pd.DataFrame(rep.get("trace", []))[["step", "tool", "why", "summary", "status", "ms"]]
                 if rep.get("trace") else pd.DataFrame(), hide_index=True, width="stretch")

st.subheader("Actions")
a1, a2 = st.columns(2)
is_team = ui.role() == "Audit team"
can_accept = is_team and gid and rep["source_kind"] == "text" and not rep.get("accepted") and rep["findings"]
if a1.button("✅ Accept findings into risk model", disabled=not can_accept,
             help="Human approval: the report's open findings replace the audit-history counts and the pipeline reruns"):
    out = analyst.accept_findings(rep["report_id"], ui.user())
    st.session_state["flash_a"] = out["message"]
    st.rerun()
if a2.button("🧩 Create remediation tasks", disabled=not (is_team and rep["findings"]),
             help="One task per open finding of Medium severity or above, assigned by control area"):
    out = analyst.create_tasks(rep["report_id"], ui.user())
    st.session_state["flash_a"] = f"Created {out['created']} remediation tasks; owners notified."
    st.rerun()
st.markdown("**Download the report**")
d1, d2, d3, d4 = st.columns(4)
try:
    d1.download_button("📄 PDF", report_export.to_pdf(rep), file_name=f"{rep['report_id']}.pdf",
                       mime="application/pdf", type="primary", width="stretch")
except Exception as exc:
    d1.error(f"PDF export failed: {exc}")
try:
    d2.download_button("📊 Excel", report_export.to_excel(rep), file_name=f"{rep['report_id']}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", type="primary",
                       width="stretch")
except Exception as exc:
    d2.error(f"Excel export failed: {exc}")
d3.download_button("Markdown", analyst.to_markdown(rep), file_name=f"{rep['report_id']}.md", mime="text/markdown",
                   width="stretch")
d4.download_button("JSON", json.dumps({k: v for k, v in rep.items() if k != "trace"}, indent=2, default=str),
                   file_name=f"{rep['report_id']}.json", mime="application/json", width="stretch")
st.caption("PDF: formatted report with severity chart and all sections. Excel: one tab per section "
           "(Summary, Findings, By control area, Score impact, Discrepancies, Data quality, Recommendations, "
           "Agent trace) with filters.")
if not is_team:
    st.caption("Only the audit team can accept findings or create tasks.")
if st.session_state.get("flash_a"):
    st.success(st.session_state.pop("flash_a"))
