"""Export an AI analysis report to Excel (.xlsx) and PDF.

Both exports are built from the stored report dict, so they match what the UI shows.
The PDF uses reportlab's built-in Helvetica (no font files needed), so text is sanitised to the
Windows-1252 character set.
"""
from __future__ import annotations

import io
import json

import pandas as pd

SEV_HEX = {"Critical": "C0392B", "High": "E07B39", "Medium": "D4A72C", "Low": "5B8BD0", "Info": "999999"}
TIER_HEX = {"Low": "4C9A6A", "Medium": "D4A72C", "High": "E07B39", "Critical": "C0392B"}
RATING_HEX = {"Satisfactory": "4C9A6A", "Needs improvement": "E07B39", "Unsatisfactory": "C0392B",
              "Not applicable": "888888"}
ROLE = {"audit_owner": "Audit team", "supplier_owner": "Supplier owner", "ism": "ISM"}
SEVERITIES = ["Critical", "High", "Medium", "Low", "Info"]


# ---------------------------------------------------------------------------------- tables
def _frames(rep: dict) -> dict[str, pd.DataFrame]:
    m = rep.get("metrics", {})
    si = rep.get("score_impact") or {}
    summary_rows = [
        ("Report ID", rep.get("report_id")), ("Source", rep.get("source_name")),
        ("Document type", rep.get("document_type")), ("Supplier", rep.get("supplier_id") or "Multiple / none"),
        ("Generated", str(rep.get("generated_at", ""))[:16].replace("T", " ")),
        ("Analyst mode", "AI agent (Azure OpenAI)" if rep.get("mode") == "ai-agent" else "Rule-based analyst"),
        ("Overall rating", rep.get("overall_rating")), ("Risk level", rep.get("risk_level")),
        ("Confidence", rep.get("confidence")), ("Accepted into risk model", "Yes" if rep.get("accepted") else "No"),
        ("Findings", m.get("findings")), ("Open findings", m.get("open")),
        ("Critical open", m.get("critical_open")), ("High open", m.get("high_open")),
        ("Control areas affected", m.get("control_areas")),
        ("Evidence verified", f"{m['evidence_verified_pct']}%" if m.get("evidence_verified_pct") is not None else "—"),
        ("Discrepancies with golden record", m.get("discrepancies")),
    ]
    if si:
        summary_rows += [("Current risk score", f"{si['current']['risk_score']} ({si['current']['risk_tier']})"),
                         ("Projected risk score", f"{si['projected']['risk_score']} ({si['projected']['risk_tier']})")]
    mk = rep.get("masking", {})
    summary_rows += [("Masked before AI", f"{mk.get('supplier_names', 0)} supplier names, {mk.get('emails', 0)} emails, "
                                          f"{mk.get('phones', 0)} phones, {mk.get('tax_ids', 0)} tax IDs"),
                     ("Executive summary", rep.get("executive_summary"))]
    out = {"Summary": pd.DataFrame([(k, "" if v is None else str(v)) for k, v in summary_rows],
                                   columns=["Item", "Value"])}

    f = pd.DataFrame(rep.get("findings") or [])
    if len(f):
        cols = {"id": "ID", "supplier_id": "Supplier", "severity": "Severity", "status": "Status",
                "control_area": "Control area", "title": "Finding", "evidence": "Evidence",
                "evidence_verified": "Evidence verified", "impact": "Impact", "recommendation": "Recommendation",
                "owner_role": "Owner", "due_in_days": "Due (days)"}
        f = f[[c for c in cols if c in f.columns]].rename(columns=cols)
        if "Owner" in f:
            f["Owner"] = f["Owner"].map(lambda r: ROLE.get(r, r))
        if "Evidence verified" in f:
            f["Evidence verified"] = f["Evidence verified"].map(lambda v: "Yes" if v else "No - review")
        out["Findings"] = f
        open_f = pd.DataFrame(rep["findings"])
        open_f = open_f[open_f.status == "Open"]
        piv = (open_f.pivot_table(index="control_area", columns="severity", values="title", aggfunc="count",
                                  fill_value=0).reindex(columns=SEVERITIES, fill_value=0) if len(open_f) else pd.DataFrame())
        if len(piv):
            piv["Total"] = piv.sum(axis=1)
            out["By control area"] = piv.sort_values("Total", ascending=False).reset_index().rename(
                columns={"control_area": "Control area"})
    if si:
        ch = si.get("inputs_changed") or {}
        rows = [("Current score", si["current"]["risk_score"], si["current"]["risk_tier"]),
                ("Projected score", si["projected"]["risk_score"], si["projected"]["risk_tier"])]
        impact = pd.DataFrame(rows, columns=["Measure", "Score", "Tier"])
        changes = pd.DataFrame([{"Input": k.replace("_", " "), "System now": json.dumps(v.get("from")),
                                 "From this report": json.dumps(v.get("to"))} for k, v in ch.items()])
        drivers = pd.DataFrame(si["projected"].get("top_factors", []))
        out["Score impact"] = (impact, changes, drivers)
    if rep.get("discrepancies"):
        out["Discrepancies"] = pd.DataFrame(rep["discrepancies"]).rename(columns={
            "field": "Field", "report_value": "Report says", "system_value": "AuditPilot has",
            "implication": "Why it matters"})
    if rep.get("affected_suppliers"):
        a = pd.DataFrame(rep["affected_suppliers"])
        a["signals"] = a["signals"].map(lambda s: ", ".join(s) if isinstance(s, list) else s)
        out["Affected suppliers"] = a.rename(columns={"golden_id": "Supplier", "current_tier": "Current tier",
                                                      "current_score": "Current score", "signals": "Signals",
                                                      "suggested_action": "Suggested action"})
    if rep.get("data_quality"):
        out["Data quality"] = pd.DataFrame(rep["data_quality"]).rename(columns={
            "issue": "Issue", "severity": "Severity", "detail": "Detail"})
    if rep.get("recommendations"):
        r = pd.DataFrame(rep["recommendations"])
        r["owner_role"] = r["owner_role"].map(lambda x: ROLE.get(x, x))
        out["Recommendations"] = r.rename(columns={"priority": "#", "action": "Action", "owner_role": "Owner",
                                                   "due_in_days": "Due (days)"})
    if rep.get("limitations"):
        out["Limitations"] = pd.DataFrame({"Limitation": rep["limitations"]})
    if rep.get("trace"):
        t = pd.DataFrame(rep["trace"])
        t["args"] = t["args"].map(lambda a: json.dumps(a, default=str)[:200])
        out["Agent trace"] = t[["step", "tool", "args", "why", "summary", "status", "ms"]].rename(columns={
            "step": "Step", "tool": "Tool", "args": "Arguments", "why": "Why", "summary": "Result",
            "status": "Status", "ms": "ms"})
    return out


# ---------------------------------------------------------------------------------- Excel
def to_excel(rep: dict) -> bytes:
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    frames = _frames(rep)
    buf = io.BytesIO()
    head_fill = PatternFill("solid", fgColor="1F3864")
    head_font = Font(bold=True, color="FFFFFF")
    thin = Side(style="thin", color="D0D0D0")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    wrap = Alignment(wrap_text=True, vertical="top")
    widths = {"Evidence": 60, "Finding": 45, "Recommendation": 50, "Impact": 35, "Why it matters": 55, "Action": 70,
              "Detail": 70, "Value": 90, "Executive summary": 90, "Limitation": 110, "Arguments": 45, "Result": 40,
              "Why": 40, "Signals": 50, "Suggested action": 35, "Issue": 40}

    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name, df in frames.items():
            if isinstance(df, tuple):  # Score impact: three stacked tables
                row = 0
                for part, title in zip(df, ["Risk score", "Inputs changed by this report", "Top drivers (projected)"]):
                    if not len(part):
                        continue
                    pd.DataFrame({title: []}).to_excel(xw, sheet_name=name, startrow=row, index=False)
                    part.to_excel(xw, sheet_name=name, startrow=row + 1, index=False)
                    row += len(part) + 4
            else:
                df.to_excel(xw, sheet_name=name[:31], index=False)
        wb = xw.book
        for ws in wb.worksheets:
            ws.sheet_view.showGridLines = False
            ws.page_setup.orientation = "landscape"
            ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
            ws.sheet_properties.pageSetUpPr.fitToPage = True
            for row in ws.iter_rows():
                for c in row:
                    if c.value is None:
                        continue
                    c.alignment = wrap
                    c.border = border
            # header rows: any row whose cells are all strings directly above data, plus row 1
            header_rows = {1}
            if ws.title == "Score impact":
                header_rows = {r for r in range(1, ws.max_row + 1)
                               if ws.cell(r, 1).value in ("Risk score", "Inputs changed by this report",
                                                          "Top drivers (projected)")}
                for r in list(header_rows):
                    ws.cell(r, 1).font = Font(bold=True, size=12, color="1F3864")
                    ws.cell(r, 1).border = Border()
                header_rows = {r + 1 for r in header_rows}
            for r in header_rows:
                for c in ws[r]:
                    if c.value is not None:
                        c.fill, c.font = head_fill, head_font
            for i, col in enumerate(ws.iter_cols(min_row=1, max_row=1), start=1):
                hdr = str(col[0].value or "")
                longest = max((len(str(c.value)) for c in ws[get_column_letter(i)] if c.value is not None), default=10)
                ws.column_dimensions[get_column_letter(i)].width = widths.get(hdr, min(max(12, longest + 2), 40))
            if ws.title == "Summary":
                ws.column_dimensions["A"].width = 32
                ws.column_dimensions["B"].width = 95
                for r in range(2, ws.max_row + 1):
                    ws.cell(r, 1).font = Font(bold=True)
                    k, v = ws.cell(r, 1).value, ws.cell(r, 2).value
                    color = (RATING_HEX.get(v) if k == "Overall rating" else TIER_HEX.get(v) if k == "Risk level" else None)
                    if color:
                        ws.cell(r, 2).fill = PatternFill("solid", fgColor=color)
                        ws.cell(r, 2).font = Font(bold=True, color="FFFFFF")
            # colour severity cells
            for row in ws.iter_rows(min_row=2):
                for c in row:
                    if isinstance(c.value, str) and c.value in SEV_HEX and ws.cell(1, c.column).value in (
                            "Severity", "Critical", "High", "Medium", "Low", "Info") or (
                            isinstance(c.value, str) and c.value in SEV_HEX and ws.title in ("Findings", "Data quality")):
                        c.fill = PatternFill("solid", fgColor=SEV_HEX[c.value])
                        c.font = Font(bold=True, color="FFFFFF")
                    if c.value == "No - review":
                        c.fill = PatternFill("solid", fgColor="FBE7C6")
            if ws.title != "Summary":
                ws.freeze_panes = "A2"
                if ws.max_row > 1 and ws.title != "Score impact":
                    ws.auto_filter.ref = ws.dimensions
        wb.active = 0
    return buf.getvalue()


# ---------------------------------------------------------------------------------- PDF
_REPL = {"→": "->", "←": "<-", "≥": ">=", "≤": "<=", "✅": "", "⚠️": "", "⚠": "", "×": "x", "₹": "INR ",
         "‑": "-", " ": " ", "…": "...", "Σ": "Sum"}


def _t(v) -> str:
    s = "" if v is None else str(v)
    for a, b in _REPL.items():
        s = s.replace(a, b)
    s = s.encode("cp1252", errors="ignore").decode("cp1252")
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_pdf(rep: dict) -> bytes:
    from reportlab.graphics.charts.barcharts import HorizontalBarChart
    from reportlab.graphics.shapes import Drawing, String
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)

    buf = io.BytesIO()
    page = landscape(A4)
    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=16 * mm,
                            bottomMargin=14 * mm, title=f"AI analysis {rep.get('report_id', '')}",
                            author="AuditPilot", subject=_t(rep.get("source_name")))
    W = page[0] - 28 * mm
    ss = getSampleStyleSheet()
    navy = colors.HexColor("#1F3864")
    H1 = ParagraphStyle("H1", parent=ss["Title"], alignment=TA_LEFT, fontSize=18, textColor=navy, spaceAfter=4)
    H2 = ParagraphStyle("H2", parent=ss["Heading2"], fontSize=13, textColor=navy, spaceBefore=10, spaceAfter=5,
                        keepWithNext=1)
    B = ParagraphStyle("B", parent=ss["BodyText"], fontSize=9.5, leading=13)
    S = ParagraphStyle("S", parent=B, fontSize=8, leading=10.5)
    SH = ParagraphStyle("SH", parent=S, textColor=colors.white, fontName="Helvetica-Bold")
    G = ParagraphStyle("G", parent=S, textColor=colors.HexColor("#666666"))

    def table(rows, col_w, head=True, sev_col=None, zebra=True):
        data = [[Paragraph(_t(c), SH if (head and i == 0) else S) for c in r] for i, r in enumerate(rows)]
        t = Table(data, colWidths=col_w, repeatRows=1 if head else 0)
        st = [("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D0D0D0")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
              ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
        if head:
            st.append(("BACKGROUND", (0, 0), (-1, 0), navy))
        if zebra:
            for i in range(1 if head else 0, len(rows)):
                if i % 2 == 0:
                    st.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#F5F7FB")))
        if sev_col is not None:
            for i in range(1, len(rows)):
                hexc = SEV_HEX.get(str(rows[i][sev_col]))
                if hexc:
                    st.append(("BACKGROUND", (sev_col, i), (sev_col, i), colors.HexColor("#" + hexc)))
                    data[i][sev_col] = Paragraph(_t(rows[i][sev_col]), SH)
        t.setStyle(TableStyle(st))
        return t

    def badge(text, hexc):
        return f"<font backColor='#{hexc}' color='white'>&nbsp;<b>{_t(text)}</b>&nbsp;</font>"

    m = rep.get("metrics", {})
    si = rep.get("score_impact")
    story = [Paragraph(f"AI analysis: {_t(rep.get('source_name'))}", H1),
             Paragraph(f"{_t(rep.get('report_id'))} &nbsp;·&nbsp; {_t(rep.get('document_type'))} &nbsp;·&nbsp; "
                       f"generated {_t(str(rep.get('generated_at', ''))[:16].replace('T', ' '))} &nbsp;·&nbsp; "
                       f"supplier {_t(rep.get('supplier_id') or 'multiple / none')} &nbsp;·&nbsp; "
                       f"{'AI agent' if rep.get('mode') == 'ai-agent' else 'rule-based analyst'}", G),
             Spacer(1, 5),
             Paragraph(badge(rep.get("overall_rating"), RATING_HEX.get(rep.get("overall_rating"), "888888")) + "&nbsp; "
                       + badge(f"Risk {rep.get('risk_level')}", TIER_HEX.get(rep.get("risk_level"), "888888")) + "&nbsp; "
                       + badge(f"Confidence {rep.get('confidence', '-')}", "5B6B7F")
                       + ("&nbsp; " + badge("Accepted into risk model", "4C9A6A") if rep.get("accepted") else ""), B),
             Spacer(1, 8)]
    tiles = [("Findings", m.get("findings")), ("Open", m.get("open")), ("Critical open", m.get("critical_open")),
             ("High open", m.get("high_open")),
             ("Evidence verified", f"{m['evidence_verified_pct']}%" if m.get("evidence_verified_pct") is not None else "-"),
             ("Discrepancies", m.get("discrepancies"))]
    big = ParagraphStyle("big", parent=B, fontSize=16, leading=19, fontName="Helvetica-Bold", textColor=navy)
    tt = Table([[Paragraph(_t(k), G) for k, _ in tiles], [Paragraph(_t(v), big) for _, v in tiles]],
               colWidths=[W / 6] * 6)
    tt.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D0D0D0")),
                            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E6E6E6")),
                            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F5F7FB"))]))
    story += [tt, Paragraph("Executive summary", H2), Paragraph(_t(rep.get("executive_summary")), B)]

    if si:
        cur, proj = si["current"], si["projected"]
        story += [Paragraph("Impact on the AuditPilot risk score", H2),
                  Paragraph(f"Current <b>{cur['risk_score']}</b> {badge(cur['risk_tier'], TIER_HEX.get(cur['risk_tier'], '888888'))}"
                            f" &nbsp;-&gt;&nbsp; if findings are accepted <b>{proj['risk_score']}</b> "
                            f"{badge(proj['risk_tier'], TIER_HEX.get(proj['risk_tier'], '888888'))} "
                            f"&nbsp;({proj['risk_score'] - cur['risk_score']:+.1f} points)", B), Spacer(1, 4)]
        ch = si.get("inputs_changed") or {}
        if ch:
            story.append(table([["Input", "System now", "From this report"]]
                               + [[k.replace("_", " "), json.dumps(v.get("from")), json.dumps(v.get("to"))]
                                  for k, v in ch.items()], [W * 0.25, W * 0.35, W * 0.40]))
        story.append(Paragraph(_t(si.get("explanation", "")) + " Top drivers: " + _t(", ".join(
            f"{x['factor']} (+{x['contribution']})" for x in proj.get("top_factors", []))), G))

    findings = rep.get("findings") or []
    if findings:
        open_f = [f for f in findings if f.get("status") == "Open"]
        counts = [sum(f["severity"] == s for f in open_f) for s in SEVERITIES]
        d = Drawing(W, 120)
        ch_ = HorizontalBarChart()
        ch_.x, ch_.y, ch_.width, ch_.height = 70, 10, W * 0.45, 100
        ch_.data = [counts[::-1]]
        ch_.categoryAxis.categoryNames = SEVERITIES[::-1]
        ch_.categoryAxis.labels.fontSize = 8
        ch_.valueAxis.valueMin = 0
        ch_.valueAxis.valueMax = max(counts + [1])
        ch_.valueAxis.valueStep = max(1, max(counts + [1]) // 5)
        ch_.valueAxis.labels.fontSize = 7
        ch_.bars.strokeColor = None
        for i, s in enumerate(SEVERITIES[::-1]):
            ch_.bars[(0, i)].fillColor = colors.HexColor("#" + SEV_HEX[s])
        ch_.barLabelFormat = "%d"
        ch_.barLabels.fontSize = 8
        ch_.barLabels.nudge = 7
        d.add(ch_)
        d.add(String(70, 114, "Open findings by severity", fontSize=9, fontName="Helvetica-Bold", fillColor=navy))
        areas = {}
        for f in open_f:
            areas[f["control_area"]] = areas.get(f["control_area"], 0) + 1
        area_tbl = table([["Control area", "Open"]] + [[a, n] for a, n in sorted(areas.items(), key=lambda x: -x[1])],
                         [W * 0.30, W * 0.08])
        story += [Paragraph("Findings", H2), Table([[d, area_tbl]], colWidths=[W * 0.58, W * 0.42],
                                                   style=[("VALIGN", (0, 0), (-1, -1), "TOP")])]
        multi = len({f.get("supplier_id") for f in findings}) > 1
        hdr = ["ID"] + (["Supplier"] if multi else []) + ["Severity", "Status", "Control area", "Finding", "Evidence",
                                                         "Verified", "Recommendation", "Owner", "Due"]
        rows = [hdr]
        for f in findings:
            rows.append([f.get("id")] + ([f.get("supplier_id")] if multi else []) +
                        [f.get("severity"), f.get("status"), f.get("control_area"), f.get("title"), f.get("evidence"),
                         "Yes" if f.get("evidence_verified") else "No - review", f.get("recommendation"),
                         ROLE.get(f.get("owner_role"), f.get("owner_role")), f"{f.get('due_in_days')} d"])
        w = ([0.05] + ([0.07] if multi else []) + [0.07, 0.06, 0.10, 0.16, 0.21, 0.06, 0.17, 0.07, 0.04])
        tot = sum(w)
        story.append(table(rows, [W * x / tot for x in w], sev_col=2 if multi else 1))

    if rep.get("discrepancies"):
        story += [Paragraph("Report vs golden record", H2),
                  table([["Field", "Report says", "AuditPilot has", "Why it matters"]]
                        + [[d["field"], d["report_value"], d["system_value"], d["implication"]]
                           for d in rep["discrepancies"]], [W * 0.2, W * 0.13, W * 0.13, W * 0.54])]
    if rep.get("affected_suppliers"):
        story += [Paragraph("Affected suppliers", H2),
                  table([["Supplier", "Current tier", "Score", "Signals", "Suggested action"]]
                        + [[a["golden_id"], a["current_tier"], a["current_score"],
                            ", ".join(a["signals"]) if isinstance(a["signals"], list) else a["signals"],
                            a["suggested_action"]] for a in rep["affected_suppliers"]],
                        [W * 0.12, W * 0.1, W * 0.08, W * 0.45, W * 0.25])]
    if rep.get("data_quality"):
        story += [Paragraph("Data quality", H2),
                  table([["Severity", "Issue", "Detail"]] + [[d["severity"], d["issue"], d["detail"]]
                                                            for d in rep["data_quality"]],
                        [W * 0.1, W * 0.3, W * 0.6], sev_col=0)]
    if rep.get("recommendations"):
        story += [Paragraph("Recommended actions", H2),
                  table([["#", "Action", "Owner", "Due"]] + [[r["priority"], r["action"], ROLE.get(r["owner_role"],
                                                                                                    r["owner_role"]),
                                                              f"{r['due_in_days']} days"] for r in rep["recommendations"]],
                        [W * 0.04, W * 0.72, W * 0.13, W * 0.11])]
    gov = [Paragraph("Limitations and governance", H2)]
    mk = rep.get("masking", {})
    gov.append(Paragraph(f"Masked before AI: {mk.get('supplier_names', 0)} supplier-name mentions, "
                         f"{mk.get('emails', 0)} emails, {mk.get('phones', 0)} phone numbers, "
                         f"{mk.get('tax_ids', 0)} tax IDs. Evidence quotes are checked against the source; risk scores "
                         "are computed by the AuditPilot risk engine, never by the AI.", B))
    for x in rep.get("limitations", []):
        gov.append(Paragraph("&bull; " + _t(x), B))
    story.append(KeepTogether(gov))
    if rep.get("trace"):
        story += [Paragraph("Agent trace", H2),
                  table([["Step", "Tool", "Why", "Result", "Status"]]
                        + [[t["step"], t["tool"], t.get("why", ""), t.get("summary", ""), t.get("status", "")]
                           for t in rep["trace"]], [W * 0.05, W * 0.17, W * 0.38, W * 0.32, W * 0.08])]

    def footer(canvas, d_):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#777777"))
        canvas.drawString(14 * mm, 8 * mm, "AuditPilot · AI Audit Analyst · SYNTHETIC DATA - for demonstration only")
        canvas.drawRightString(page[0] - 14 * mm, 8 * mm, f"{_t(rep.get('report_id'))} · page {d_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
