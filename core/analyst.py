"""AI Audit Analyst: an agent that analyses a report or dataset from any source and writes a detailed analysis.

How it works
- The input (PDF, Word, text, CSV, Excel, JSON) is read into text or a table, then masked: supplier names,
  emails, phone numbers and tax IDs are replaced by tokens before anything reaches the model.
- With an LLM configured (Azure OpenAI or the OpenAI API), the model runs a tool-calling loop (read sections, search, profile tables,
  look up the golden record, simulate the risk score) and submits a structured report.
- With no key, or if the model fails, a rule-based analyst runs the same tools in a fixed plan.
- Either way the engine then verifies: every evidence quote is checked against the document, and every score
  is recomputed by the deterministic risk model. The model never sets a score.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import date, datetime

import pandas as pd
from rapidfuzz import fuzz, process

from . import ai, audit_log, db
from .config import ACCESS_ORDER, TIER_ORDER, rules as load_rules, today, weights as load_weights
from .consolidate import norm_country, norm_name, parse_date
from .doc_io import Document
from .risk import score_supplier

db.execute("""CREATE TABLE IF NOT EXISTS analysis_reports(
  report_id TEXT PRIMARY KEY, ts TEXT, actor TEXT, source_name TEXT, source_kind TEXT, golden_id TEXT,
  mode TEXT, overall_rating TEXT, risk_level TEXT, report TEXT, trace TEXT,
  accepted INTEGER DEFAULT 0, accepted_by TEXT, accepted_at TEXT)""")

SEVERITIES = ["Critical", "High", "Medium", "Low", "Info"]
SEV_DUE = {"Critical": 14, "High": 30, "Medium": 60, "Low": 90, "Info": 90}
RATINGS = ["Satisfactory", "Needs improvement", "Unsatisfactory", "Not applicable"]
MAX_STEPS = 14

CONTROL_AREAS = {
    "Access control": r"access review|\bmfa\b|multi-factor|password|privileged|administrator|shared admin|"
                      r"terminated|leaver|logical access|access removal|provisioning",
    "Data protection": r"encrypt|backup|pii|personal data|client data|data loss|retention|masking",
    "Incident response": r"incident|breach|tabletop",
    "Logging & monitoring": r"siem|alert|monitor|logging|log review",
    "Business continuity & DR": r"continuity|disaster|recovery|restore|restoration|rto|rpo",
    "Sub-processor management": r"sub-?processor|sub-?service organi|subcontract|vendor management|third party",
    "Vulnerability management": r"patch|vulnerab|penetration|pen test|scan",
    "Physical security": r"physical|server room|badge|premises|facility",
    "HR security": r"training|awareness|background check|joiner",
    "Change management": r"change management|change control",
    "Governance & compliance": r"certif|iso 27001|soc 2|policy|contract|sanction|financial|adverse media",
}
SOC_AREA = {"CC6": "Access control", "CC7": "Logging & monitoring", "CC8": "Change management",
            "CC9": "Sub-processor management", "A1": "Business continuity & DR", "CC3": "Governance & compliance"}
AREA_OWNER = {"Access control": "ism", "Data protection": "ism", "Incident response": "ism",
              "Logging & monitoring": "ism", "Vulnerability management": "ism", "Physical security": "ism",
              "Business continuity & DR": "supplier_owner", "Sub-processor management": "supplier_owner",
              "HR security": "supplier_owner", "Change management": "ism",
              "Governance & compliance": "audit_owner", "Data quality": "audit_owner"}
AREA_RECO = {
    "Access control": "Enforce MFA and named privileged accounts; run quarterly access reviews.",
    "Data protection": "Encrypt client data at rest, including backups, and restrict access to it.",
    "Incident response": "Test the incident response plan and record lessons learned.",
    "Logging & monitoring": "Staff alert triage so security alerts are reviewed daily.",
    "Business continuity & DR": "Test backup restoration and BCP at least annually.",
    "Sub-processor management": "Approve and contract every sub-processor before sharing client data.",
    "Vulnerability management": "Close overdue penetration test and patching items.",
    "Physical security": "Restrict and log physical access to areas holding client data.",
    "HR security": "Track training and screening completion centrally.",
    "Change management": "Keep change approvals documented.",
    "Governance & compliance": "Obtain current certifications and resolve compliance flags.",
    "Data quality": "Correct the source record and re-run consolidation.",
}
EXTRA_FLAGS = {
    "no_mfa": r"mfa (is )?not (enforced|implemented)|password only|without mfa",
    "shared_admin_accounts": r"shared administrator|shared admin",
    "backups_untested": r"backups? (were |was )?not tested",
    "no_incident_plan": r"incident response plan (exists but )?(not tested|no tabletop)|no formal incident",
    "offshore_storage": r"offshore nas|stored [^.]{0,40}offshore",
}


# ======================================================================================
# Masking
# ======================================================================================
class Masker:
    """Replace supplier names and personal identifiers with tokens before text reaches the model."""

    def __init__(self, golden: pd.DataFrame):
        self.names: dict[str, str] = {}
        pats = []
        for r in golden.itertuples():
            self.names[r.golden_id] = r.legal_name
            base = re.sub(r"\s+(Pvt\.? Ltd\.?|Private Limited|Inc\.?|Ltd\.?|GmbH|Pte\.? Ltd\.?|Limited|Incorporated)$",
                          "", r.legal_name, flags=re.I)
            for v in {r.legal_name, base}:
                pats.append((v, r.golden_id))
        pats.sort(key=lambda x: -len(x[0]))
        self._name_re = [(re.compile(r"\b" + re.escape(v) + r"\b", re.I), gid) for v, gid in pats]
        self._short = {}
        for r in golden.itertuples():
            words = r.legal_name.split()
            self._short[r.golden_id] = [" ".join(words[:2]), words[0]]
        self.seen: set[str] = set()
        self.counts = {"supplier_names": 0, "emails": 0, "phones": 0, "tax_ids": 0}

    def mask(self, text: str) -> str:
        if not isinstance(text, str) or not text:
            return "" if text is None or (not isinstance(text, str) and pd.isna(text)) else str(text)
        for rx, gid in self._name_re:
            text, n = rx.subn(f"[{gid}]", text)
            self.counts["supplier_names"] += n
            if n:
                self.seen.add(gid)
        # Short references ("Nimbus management…") for suppliers already identified in this input
        for gid in self.seen:
            for v in self._short[gid]:
                if len(v) >= 4:
                    text, n = re.subn(r"(?<!\[)\b" + re.escape(v) + r"\b", f"[{gid}]", text, flags=re.I)
                    self.counts["supplier_names"] += n
        text, n = re.subn(r"[\w.+-]+@[\w-]+\.[\w.]+", "[EMAIL]", text)
        self.counts["emails"] += n
        text, n = re.subn(r"\bTX[A-Z]{2}\d{6,}\b", "[TAX_ID]", text)
        self.counts["tax_ids"] += n
        text, n = re.subn(r"(?<![\w+])\+\d{1,3}[\s-]?\(?\d{2,5}\)?[\s-]?\d{3,5}[\s-]?\d{3,5}(?![\w-])", "[PHONE]", text)
        self.counts["phones"] += n
        return text

    def unmask(self, obj):
        if isinstance(obj, str):
            return re.sub(r"\[(GS-\d{4})\]", lambda m: self.names.get(m.group(1), m.group(0)), obj)
        if isinstance(obj, list):
            return [self.unmask(x) for x in obj]
        if isinstance(obj, dict):
            return {k: (v if k in ("golden_id", "supplier_id") else self.unmask(v)) for k, v in obj.items()}
        return obj


# ======================================================================================
# Agent context and tools (shared by the LLM agent and the rule-based agent)
# ======================================================================================
class Context:
    def __init__(self, doc: Document, golden: pd.DataFrame, golden_id: str | None, focus: str, on_step=None):
        self.doc, self.golden, self.focus = doc, golden, focus or ""
        self.on_step = on_step or (lambda *_: None)
        self.masker = Masker(golden)
        self.masked_text = self.masker.mask(doc.text) if doc.kind == "text" else ""
        self.table = doc.table
        if doc.kind == "table":
            self.masked_table = doc.table.copy().fillna("").astype(str).map(self.masker.mask)
        self.mask_summary = dict(self.masker.counts)
        self.golden_id = golden_id
        self.trace: list[dict] = []
        self.pct = golden["contract_value_inr"].astype(float).rank(pct=True) * 100
        self.cache: dict = {}

    def call(self, name: str, args: dict | None = None, why: str = ""):
        args = args or {}
        t0 = time.time()
        try:
            out = TOOLS[name](self, **args)
            status = "ok"
        except Exception as exc:
            out, status = {"error": str(exc)[:300]}, "error"
        step = {"step": len(self.trace) + 1, "tool": name, "args": args, "why": why, "status": status,
                "ms": int((time.time() - t0) * 1000), "summary": _summarize(name, out)}
        self.trace.append(step)
        self.on_step(step)
        return out


def _summarize(name: str, out) -> str:
    if isinstance(out, dict) and "error" in out:
        return "error: " + out["error"]
    if name == "document_overview":
        return f"{out.get('kind')} · {out.get('detected_type')} · {len(out.get('sections', []))} sections"
    if name == "detect_suppliers":
        return ", ".join(f"{c['golden_id']} ({c['score']})" for c in out.get("candidates", [])[:3]) or "none found"
    if name == "extract_findings":
        return f"{len(out.get('findings', []))} findings extracted"
    if name == "simulate_risk":
        return (f"{out.get('current', {}).get('risk_score')} {out.get('current', {}).get('risk_tier')} → "
                f"{out.get('projected', {}).get('risk_score')} {out.get('projected', {}).get('risk_tier')}")
    if name == "profile_table":
        return f"{out.get('rows')} rows · {len(out.get('columns', []))} columns · {out.get('duplicate_rows')} duplicates"
    if name == "cross_check_table":
        return f"{out.get('matched')} of {out.get('rows')} rows matched to golden · {len(out.get('signals', []))} signals"
    if name == "compare_with_golden":
        return f"{len(out.get('discrepancies', []))} discrepancies"
    if name == "search_document":
        return f"{len(out.get('hits', []))} hits"
    if name == "read_section":
        return f"{len(out.get('text', ''))} chars"
    if name == "lookup_supplier":
        return f"{out.get('golden_id')} · {out.get('risk_tier')} {out.get('risk_score')}"
    return "done"


def _detect_type(text: str, name: str) -> str:
    head = (text[:600] + " " + name).lower()
    t = (text[:4000] + " " + name).lower()
    if "audit report" in head or re.search(r"\bfinding\s+f?-?\d", t):
        return "Audit report"
    if re.search(r"soc ?2 type|soc ?2 report", head):
        return "SOC 2 report"
    if "self-assessment" in t or "questionnaire" in t:
        return "Supplier self-assessment"
    if "penetration" in t:
        return "Penetration test report"
    return "General document"


def tool_document_overview(ctx: Context) -> dict:
    d = ctx.doc
    if d.kind == "table":
        return {"kind": "table", "name": ctx.masker.mask(d.name.replace("_", " ")), "detected_type": "Dataset", "rows": len(d.table),
                "columns": list(d.table.columns), "masking": ctx.mask_summary}
    return {"kind": "text", "name": ctx.masker.mask(d.name.replace("_", " ")), "detected_type": _detect_type(d.text, d.name), "chars": len(d.text),
            "sections": [{"index": i, "title": ctx.masker.mask(s["title"]), "chars": s["end"] - s["start"]}
                         for i, s in enumerate(d.sections)],
            "linked_supplier": ctx.golden_id, "masking": ctx.mask_summary, "focus": ctx.focus}


def tool_read_section(ctx: Context, index: int = 0) -> dict:
    s = ctx.doc.sections[int(index)]
    return {"index": index, "title": ctx.masker.mask(s["title"]),
            "text": ctx.masker.mask(ctx.doc.text[s["start"]:s["end"]])[:6000]}


def tool_search_document(ctx: Context, query: str) -> dict:
    words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2]
    hits = []
    for para in re.split(r"\n\s*\n|\n(?=[A-Z])", ctx.masked_text):
        p = para.strip()
        if p and words and sum(w in p.lower() for w in words) >= max(1, len(words) // 2):
            hits.append(p[:500])
    return {"query": query, "hits": hits[:8]}


def tool_detect_suppliers(ctx: Context) -> dict:
    if ctx.doc.kind == "table":
        return {"candidates": []}
    head = ctx.doc.text[:6000]
    tokens = re.findall(r"\[(GS-\d{4})\]", ctx.masker.mask(head))
    cands = {}
    for gid in tokens:
        cands[gid] = cands.get(gid, 0) + 1
    out = [{"golden_id": g, "score": 100, "mentions": n} for g, n in sorted(cands.items(), key=lambda x: -x[1])]
    if not out:  # fuzzy fallback on the first lines
        nh = norm_name(head[:600])
        names = ctx.golden.legal_name.map(norm_name).tolist()
        best = process.extract(nh, names, scorer=fuzz.partial_ratio, limit=3)
        out = [{"golden_id": ctx.golden.iloc[i].golden_id, "score": round(s, 1), "mentions": 0}
               for _n, s, i in best if s >= 85]
    return {"candidates": out}


def tool_lookup_supplier(ctx: Context, golden_id: str) -> dict:
    r = ctx.golden[ctx.golden.golden_id == golden_id]
    if not len(r):
        return {"error": f"{golden_id} not found"}
    r = r.iloc[0]
    keys = ["golden_id", "country", "category_name", "service_type", "data_access_level", "users_with_system_access",
            "criticality_tier", "certification_status", "certification_expiry", "assessment_score",
            "last_audit_date", "last_audit_rating", "open_critical", "open_high", "open_medium",
            "offshore_delivery", "uses_subprocessors", "risk_score", "risk_tier", "eligibility_status",
            "next_due", "overdue", "data_quality"]
    out = {}
    for k in keys:
        v = r[k]
        out[k] = None if (not isinstance(v, str) and pd.isna(v)) else (v.item() if hasattr(v, "item") else v)
    out["scope_modules"] = [m["name"] for m in json.loads(r["scope_modules"] or "[]")]
    out["red_flags"] = json.loads(r["red_flags"] or "[]")
    return out


def tool_get_audit_policy(ctx: Context) -> dict:
    w, rl = load_weights(), load_rules()
    return {"risk_weights": w["weights"], "tiers": w["tiers"], "overrides": w["overrides"],
            "eligibility": rl["eligibility"], "frequency": rl["frequency"],
            "scope_modules": [m["name"] + ": " + m["condition"] for m in rl["scope_modules"]],
            "severity_due_days": SEV_DUE, "flag_codes": sorted(ai.FLAG_RULES)}


def tool_simulate_risk(ctx: Context, golden_id: str, open_critical: int | None = None, open_high: int | None = None,
                       open_medium: int | None = None, red_flags: list | None = None,
                       data_access_level: str | None = None, certification_status: str | None = None,
                       last_audit_rating: str | None = None) -> dict:
    i = ctx.golden.index[ctx.golden.golden_id == golden_id]
    if not len(i):
        return {"error": f"{golden_id} not found"}
    i = i[0]
    row = ctx.golden.loc[i].to_dict()
    cur_flags = json.loads(row.get("red_flags") or "[]")
    pct = ctx.pct.loc[i]
    pct = None if pd.isna(pct) else pct
    w = load_weights()
    cur = score_supplier(row, pct, cur_flags, w)
    new = dict(row)
    for k, v in [("open_critical", open_critical), ("open_high", open_high), ("open_medium", open_medium),
                 ("data_access_level", data_access_level), ("certification_status", certification_status),
                 ("last_audit_rating", last_audit_rating)]:
        if v is not None:
            new[k] = v
    flags = sorted(set(cur_flags) | {f for f in (red_flags or []) if f in ai.FLAG_RULES})
    proj = score_supplier(new, pct, flags, w)
    def same(a, b):
        na = a is None or (not isinstance(a, str) and pd.isna(a))
        nb = b is None or (not isinstance(b, str) and pd.isna(b))
        return (na and nb) or (not na and not nb and a == b)

    changed = {k: {"from": None if same(row.get(k), None) else row.get(k), "to": new.get(k)} for k in
               ("open_critical", "open_high", "open_medium", "data_access_level", "certification_status",
                "last_audit_rating") if not same(row.get(k), new.get(k))}
    if flags != sorted(cur_flags):
        changed["red_flags"] = {"from": cur_flags, "to": flags}
    return {"golden_id": golden_id,
            "current": {"risk_score": cur["risk_score"], "risk_tier": cur["risk_tier"]},
            "projected": {"risk_score": proj["risk_score"], "risk_tier": proj["risk_tier"],
                          "overrides": proj["overrides"],
                          "top_factors": sorted(({"factor": v["label"], "contribution": v["contribution"]}
                                                 for v in proj["factors"].values()),
                                                key=lambda x: -x["contribution"])[:3]},
            "inputs_changed": changed}


# ---------------- deterministic extraction tools ----------------
def _sev_guess(s: str) -> str:
    t = s.lower()
    if re.search(r"unencrypted|breach|exfiltrat|sanction|critical", t):
        return "Critical"
    if re.search(r"shared admin|not enforced|password only|without (client )?approval|retained access|"
                 r"not reviewed|not tested|qualified|terminated|overdue|expired|exception", t):
        return "High"
    if re.search(r"minor|incomplete record|documentation|cosmetic", t):
        return "Low"
    return "Medium"


def _area(s: str) -> str:
    t = s.lower()
    for area, pat in CONTROL_AREAS.items():
        if re.search(pat, t):
            return area
    return "Governance & compliance"


def tool_extract_findings(ctx: Context) -> dict:
    """Rule-based extraction: structured 'Finding X:' blocks, SOC 2 'Exception noted' lines, then keyword scan."""
    text = ctx.masked_text
    findings = []
    lines = text.split("\n")
    heads = [i for i, l in enumerate(lines) if re.match(r"\s*Finding\s+[A-Z]{0,3}-?\d+\s*[:\-–]", l, re.I)]
    for n, i in enumerate(heads):
        block = lines[i:(heads[n + 1] if n + 1 < len(heads) else len(lines))]
        m = re.match(r"\s*Finding\s+([A-Z]{0,3}-?\d+)\s*[:\-–]\s*(.+)", block[0], re.I)
        fields, cur = {}, None
        for l in block[1:]:
            if l.lstrip().startswith("#") or re.match(r"\s*(SECTION\s+\d+|\d{1,2}\.\s+[A-Z])", l):
                break  # next document section: stop the finding here
            km = re.match(r"\s*(Severity|Rating|Control area|Area|Status|Observation|Recommendation|Impact|Risk)\s*:\s*(.*)",
                          l, re.I)
            if km:
                cur = km.group(1).lower()
                fields[cur] = km.group(2).strip()
            elif cur and l.strip() and not l.startswith("#"):
                fields[cur] += " " + l.strip()
        sev = (fields.get("severity") or fields.get("rating") or "").title()
        findings.append({
            "id": m.group(1), "title": m.group(2).strip(),
            "severity": sev if sev in SEVERITIES else _sev_guess(" ".join(block)),
            "control_area": fields.get("control area") or fields.get("area") or _area(" ".join(block)),
            "status": "Closed" if re.search(r"closed|remediated|resolved", fields.get("status", ""), re.I) else "Open",
            "evidence": (fields.get("observation") or m.group(2))[:400],
            "impact": fields.get("impact") or fields.get("risk") or "",
            "recommendation": fields.get("recommendation") or "",
        })
    if not findings:
        for n, mm in enumerate(re.finditer(r"(?:(\b(?:CC|A|PI|C|P)\d(?:\.\d+)?)\s+([^.\n]*)\.\s*)?Exception noted:\s*([^\n]+(?:\n(?!\S*\b(?:CC|A)\d)[^\n]+)*)",
                                           text), start=1):
            ref, ctl, body = mm.group(1) or f"E{n}", (mm.group(2) or "").strip(), " ".join(mm.group(3).split())
            area = SOC_AREA.get(re.sub(r"\..*", "", ref), _area(body))
            findings.append({"id": ref, "title": (ctl + ": " if ctl else "") + body.split(".")[0][:110],
                             "severity": _sev_guess(body), "control_area": area, "status": "Open",
                             "evidence": body[:400], "impact": "", "recommendation": ""})
    if not findings:
        neg = r"\b(not|no|lack|without|overdue|expired|fail|weak|unencrypted|missing|exception)\b"
        for n, sent in enumerate(re.split(r"(?<=[.!?])\s+|\n", text)):
            s = sent.strip()
            if len(s) < 25 or not re.search(neg, s, re.I) or s.endswith("?"):
                continue
            if re.search(r"do not (receive|access|process)|not applicable|no incidents|none reported|"
                         r"no client data|no exceptions", s, re.I):
                continue
            findings.append({"id": f"K-{len(findings) + 1:02d}", "title": s[:110], "severity": _sev_guess(s),
                             "control_area": _area(s), "status": "Open", "evidence": s[:400], "impact": "",
                             "recommendation": ""})
            if len(findings) >= 12:
                break
    for f in findings:
        if not f["recommendation"]:
            f["recommendation"] = AREA_RECO.get(f["control_area"], AREA_RECO["Governance & compliance"])
        f["owner_role"] = AREA_OWNER.get(f["control_area"], "audit_owner")
        f["due_in_days"] = SEV_DUE[f["severity"]]
    flags = sorted(set(ai.keyword_flags(text)) | {k for k, p in EXTRA_FLAGS.items() if re.search(p, text, re.I)})
    om = re.search(r"overall opinion[^\n]*\n+\s*([^\n]+)|opinion is (qualified|unqualified)", text, re.I)
    opinion = (om.group(1) or om.group(2)).strip() if om else None
    return {"findings": findings, "red_flags": flags, "stated_opinion": opinion}


def _facts(text: str) -> dict:
    t = text
    f: dict = {}
    if re.search(r"(do not|does not|don't)\s+(receive|process|access|hold)[^.]{0,40}(client data|personal data|pii)", t, re.I):
        f["data_access_level"] = "none"
    elif re.search(r"(process|processes|host|hosts|receive|receives|contain|containing|contains|store|stored)\b"
                   r"[^.]{0,80}(personal data|\bpii\b)", t, re.I):
        f["data_access_level"] = "pii"
    if re.search(r"iso\s*27001[^\n]{0,200}?\bexpired\b", t, re.I):
        f["certification_status"] = "expired"
    elif re.search(r"iso\s*27001[^\n]{0,120}?(valid until|expires on)", t, re.I):
        f["certification_status"] = "valid"
    if re.search(r"sub-?processor|subcontracted|sub-service organi", t, re.I):
        f["uses_subprocessors"] = True
    if re.search(r"\boffshore\b|support cent(re|er) in|delivery cent(re|er)", t, re.I):
        f["offshore_delivery"] = True
    m = re.search(r"(\d+)\s+[\w\[\]\- ]{0,40}?(staff|employees|users)\s+(hold|have|with)\s+access", t, re.I)
    if m:
        f["users_with_system_access"] = int(m.group(1))
    return f


def tool_compare_with_golden(ctx: Context, golden_id: str) -> dict:
    sup = tool_lookup_supplier(ctx, golden_id)
    if "error" in sup:
        return sup
    facts = _facts(ctx.masked_text)
    ext = ctx.cache.get("extract") or tool_extract_findings(ctx)
    open_f = [f for f in ext["findings"] if f["status"] == "Open"]
    rep = {s: sum(1 for f in open_f if f["severity"] == s) for s in ("Critical", "High", "Medium")}
    out = []

    def add(field, rv, sv, why):
        out.append({"field": field, "report_value": rv, "system_value": sv, "implication": why})

    if "data_access_level" in facts and facts["data_access_level"] != sup["data_access_level"]:
        add("Data access level", facts["data_access_level"], sup["data_access_level"],
            "Risk factor 'data sensitivity' and scope modules depend on this; correct the source system.")
    if facts.get("certification_status") and facts["certification_status"] != sup["certification_status"]:
        add("Certification", facts["certification_status"], sup["certification_status"],
            "Ariba holds an out-of-date certificate status; certification gap and PII floor may apply.")
    if facts.get("uses_subprocessors") and not sup["uses_subprocessors"]:
        add("Uses sub-processors", "yes", "no" if sup["uses_subprocessors"] is not None else "unknown",
            "Sub-processor management module is missing from the audit scope.")
    if facts.get("offshore_delivery") and not sup["offshore_delivery"]:
        add("Offshore delivery", "yes", "no" if sup["offshore_delivery"] is not None else "unknown",
            "Physical and offshore security module is missing from the audit scope.")
    u = facts.get("users_with_system_access")
    su = sup["users_with_system_access"]
    if u is not None and (su is None or abs(u - su) > max(3, 0.2 * (su or 0))):
        add("Users with system access", u, su, "Access exposure factor uses GBAMS counts; reconcile with GBAMS.")
    sysopen = {"Critical": sup["open_critical"], "High": sup["open_high"], "Medium": sup["open_medium"]}
    if open_f and any(rep[k] != sysopen[k] for k in rep):
        add("Open findings (critical/high/medium)", f"{rep['Critical']}/{rep['High']}/{rep['Medium']}",
            f"{sysopen['Critical']}/{sysopen['High']}/{sysopen['Medium']}",
            "Audit history in AuditPilot is behind this report; accept the findings to update the risk model.")
    return {"golden_id": golden_id, "report_facts": facts, "report_open_counts": rep, "discrepancies": out}


# ---------------- table tools ----------------
def _infer_type(s: pd.Series) -> str:
    v = s[s.astype(str).str.strip() != ""].astype(str)
    if not len(v):
        return "empty"
    if pd.to_numeric(v, errors="coerce").notna().mean() > 0.9:
        return "numeric"
    if v.map(lambda x: _parse_any_date(x) is not None).mean() > 0.7:
        return "date"
    return "categorical" if v.nunique() <= max(12, len(v) * 0.2) else "text"


def _parse_any_date(x: str):
    x = str(x).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y", "%m/%d/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(x, fmt).date()
        except ValueError:
            continue
    return None


def _date_format(x: str) -> str:
    x = str(x).strip()
    for fmt, label in (("%Y-%m-%d", "YYYY-MM-DD"), ("%d/%m/%Y", "DD/MM/YYYY"), ("%d-%b-%Y", "DD-Mon-YYYY"),
                       ("%d %b %Y", "DD Mon YYYY"), ("%m/%d/%Y", "MM/DD/YYYY"), ("%d.%m.%Y", "DD.MM.YYYY")):
        try:
            datetime.strptime(x, fmt)
            return label
        except ValueError:
            continue
    return "unparsed"


def tool_profile_table(ctx: Context) -> dict:
    df = ctx.table
    cols = []
    for c in df.columns:
        s = df[c].astype(str)
        blank = (s.str.strip() == "").mean()
        typ = _infer_type(df[c])
        info = {"column": c, "type": typ, "blank_pct": round(blank * 100, 1), "distinct": int(s.nunique())}
        if typ == "numeric":
            n = pd.to_numeric(s, errors="coerce")
            q1, q3 = n.quantile(0.25), n.quantile(0.75)
            iqr = q3 - q1
            info |= {"min": float(n.min()), "max": float(n.max()), "mean": round(float(n.mean()), 2),
                     "outliers": int(((n < q1 - 1.5 * iqr) | (n > q3 + 1.5 * iqr)).sum())}
        elif typ == "date":
            fmts = s[s.str.strip() != ""].map(_date_format).value_counts().to_dict()
            info |= {"formats": fmts}
        elif typ == "categorical":
            info |= {"values": s.value_counts().head(8).to_dict()}
        cols.append(info)
    return {"rows": len(df), "columns": cols, "duplicate_rows": int(df.duplicated().sum())}


def tool_sample_rows(ctx: Context, n: int = 10, offset: int = 0) -> dict:
    return {"rows": ctx.masked_table.iloc[int(offset):int(offset) + min(int(n), 30)].to_dict("records")}


def tool_query_table(ctx: Context, column: str, filter_column: str | None = None, filter_value: str | None = None,
                     top_n: int = 15) -> dict:
    df = ctx.masked_table
    if filter_column:
        df = df[df[filter_column].astype(str).str.lower() == str(filter_value).lower()]
    return {"rows_after_filter": len(df), "value_counts": df[column].value_counts().head(int(top_n)).to_dict()}


def _col(df: pd.DataFrame, *pats) -> str | None:
    for p in pats:
        for c in df.columns:
            if re.search(p, c, re.I):
                return c
    return None


def tool_cross_check_table(ctx: Context) -> dict:
    """Match each row to a golden supplier and compare overlapping signals."""
    df = ctx.table
    ncol = _col(df, r"supplier.*name|vendor.*name|^name$|party|org.*name|supplier$|vendor$")
    if not ncol:
        return {"rows": len(df), "matched": 0, "signals": [], "note": "No supplier name column found."}
    ccol = _col(df, r"country")
    g = ctx.golden.assign(nn=ctx.golden.legal_name.map(norm_name))
    matches, signals, unmatched = [], [], []
    risk_col = _col(df, r"risk.*(score|rating)|external.*score")
    sanc_col = _col(df, r"sanction")
    fin_col = _col(df, r"financ")
    acc_col = _col(df, r"data.*access|access")
    note_col = _col(df, r"note|remark|news|media")
    comment_col = _col(df, r"assessment.*comment|questionnaire|response|comment")
    date_col = _col(df, r"assess.*date|review.*date|last.*(assess|review|audit)")
    for i, r in df.iterrows():
        nm = str(r[ncol])
        iso = norm_country(r[ccol]) if ccol else None
        cand = g[g.country == iso] if iso else g
        best = process.extractOne(norm_name(nm), cand.nn.tolist(), scorer=fuzz.token_sort_ratio) if len(cand) else None
        if not best or best[1] < 85:
            unmatched.append({"row": int(i) + 2, "name": nm})
            continue
        gr = cand.iloc[best[2]]
        gid = gr.golden_id
        matches.append(gid)

        def sig(kind, sev, detail, evidence):
            signals.append({"golden_id": gid, "row": int(i) + 2, "kind": kind, "severity": sev, "detail": detail,
                            "evidence": evidence, "current_tier": gr.risk_tier, "current_score": float(gr.risk_score)})

        if sanc_col and str(r[sanc_col]).strip().upper() in ("Y", "YES", "TRUE", "1"):
            sig("Sanctions screening hit", "Critical", "External feed reports a sanctions screening hit.",
                f"{sanc_col}={r[sanc_col]}")
        if fin_col and str(r[fin_col]).strip().upper() in ("D", "E", "F", "POOR", "WEAK"):
            sig("Weak financial health", "High", f"Financial health rated {r[fin_col]}.", f"{fin_col}={r[fin_col]}")
        if risk_col:
            ext = pd.to_numeric(r[risk_col], errors="coerce")
            if not pd.isna(ext):
                if ext >= 70 and gr.risk_tier in ("Low", "Medium"):
                    sig("External risk higher than AuditPilot", "Medium",
                        f"External score {ext:.0f} vs AuditPilot {gr.risk_score:.1f} ({gr.risk_tier}).",
                        f"{risk_col}={r[risk_col]}")
                elif ext <= 30 and gr.risk_tier in ("High", "Critical"):
                    sig("External risk lower than AuditPilot", "Info",
                        f"External score {ext:.0f} vs AuditPilot {gr.risk_score:.1f} ({gr.risk_tier}).",
                        f"{risk_col}={r[risk_col]}")
        if acc_col:
            v = str(r[acc_col]).strip().lower()
            v = {"pii": "pii", "personal data": "pii", "none": "none", "internal": "internal",
                 "confidential": "confidential"}.get(v)
            if v and v != gr.data_access_level and ACCESS_ORDER.index(v) > ACCESS_ORDER.index(gr.data_access_level or "none"):
                sig("Data access under-recorded", "High",
                    f"Feed declares {v}; golden record has {gr.data_access_level}.", f"{acc_col}={r[acc_col]}")
        if note_col and re.search(r"adverse|breach|sanction|fraud|investigat|incident reported", str(r[note_col]), re.I):
            sig("Adverse note", "High" if re.search(r"breach|incident|fraud", str(r[note_col]), re.I) else "Medium",
                str(r[note_col])[:160], f"{note_col}={str(r[note_col])[:120]}")
        if comment_col and comment_col != note_col:
            fl = ai.keyword_flags(str(r[comment_col]))
            if fl:
                sig("Questionnaire red flags", "Medium" if len(fl) < 2 else "High",
                    "Red flags in responses: " + ", ".join(ai.FLAG_LABEL[x] for x in fl),
                    f"{comment_col}={str(r[comment_col])[:120]}")
        if date_col:
            d = _parse_any_date(r[date_col])
            if d is None and str(r[date_col]).strip() == "":
                sig("Missing assessment date", "Low", "No assessment date recorded.", f"{date_col}=<blank>")
            elif d and (today() - d).days > 365:
                sig("Stale assessment", "Low", f"Last assessed {d.isoformat()} ({(today() - d).days} days ago).",
                    f"{date_col}={r[date_col]}")
    return {"rows": len(df), "name_column": ncol, "matched": len(matches), "unique_suppliers": len(set(matches)),
            "unmatched": unmatched, "signals": signals}


TOOLS = {
    "document_overview": tool_document_overview, "read_section": tool_read_section,
    "search_document": tool_search_document, "detect_suppliers": tool_detect_suppliers,
    "lookup_supplier": tool_lookup_supplier, "get_audit_policy": tool_get_audit_policy,
    "simulate_risk": tool_simulate_risk, "extract_findings": tool_extract_findings,
    "compare_with_golden": tool_compare_with_golden, "profile_table": tool_profile_table,
    "sample_rows": tool_sample_rows, "query_table": tool_query_table, "cross_check_table": tool_cross_check_table,
}


# ======================================================================================
# Rule-based analyst (no key, or model failure)
# ======================================================================================
def _rating_from(findings: list[dict], stated: str | None) -> str:
    if stated:
        s = stated.lower()
        if "unsatisfactory" in s:
            return "Unsatisfactory"
        if "needs improvement" in s or "qualified" == s or "qualified" in s and "unqualified" not in s:
            return "Needs improvement"
        if "satisfactory" in s or "unqualified" in s:
            return "Satisfactory"
    open_f = [f for f in findings if f["status"] == "Open"]
    if any(f["severity"] == "Critical" for f in open_f):
        return "Unsatisfactory"
    if sum(f["severity"] == "High" for f in open_f) >= 1 or len(open_f) >= 4:
        return "Needs improvement"
    return "Satisfactory"


def rule_based_text(ctx: Context) -> dict:
    ov = ctx.call("document_overview", why="Understand what kind of document this is")
    if not ctx.golden_id:
        det = ctx.call("detect_suppliers", why="Identify which supplier the document is about")
        if det["candidates"]:
            ctx.golden_id = det["candidates"][0]["golden_id"]
    if ctx.golden_id:
        ctx.call("lookup_supplier", {"golden_id": ctx.golden_id}, why="Load the golden record for comparison")
    ctx.call("get_audit_policy", why="Load risk weights, tiers and scope rules")
    ext = ctx.call("extract_findings", why="Extract findings, severities, control areas and red flags")
    ctx.cache["extract"] = ext
    if ctx.focus:
        ctx.call("search_document", {"query": ctx.focus}, why="Look for the reviewer's focus area")
    report = {"document_type": ov["detected_type"], "supplier_id": ctx.golden_id, "findings": ext["findings"],
              "red_flags": ext["red_flags"], "discrepancies": [], "limitations": []}
    if ctx.golden_id:
        cmp = ctx.call("compare_with_golden", {"golden_id": ctx.golden_id},
                       why="Compare report facts with the golden record")
        report["discrepancies"] = cmp["discrepancies"]
        facts = cmp["report_facts"]
        rep = cmp["report_open_counts"]
        is_assurance = ov["detected_type"] in ("Audit report", "SOC 2 report", "Penetration test report")
        sim_args = {"golden_id": ctx.golden_id, "red_flags": ext["red_flags"]}
        if is_assurance and ext["findings"]:
            sim_args |= {"open_critical": rep["Critical"], "open_high": rep["High"], "open_medium": rep["Medium"]}
        for k in ("data_access_level", "certification_status"):
            if k in facts:
                sim_args[k] = facts[k]
        ctx.call("simulate_risk", sim_args, why="Recompute the risk score with the report's evidence")
    else:
        report["limitations"].append("No supplier could be identified; link one to compare with the golden record.")
    report["overall_rating"] = _rating_from(ext["findings"], ext["stated_opinion"]) if ext["findings"] or \
        ov["detected_type"] != "General document" else "Not applicable"
    open_f = [f for f in ext["findings"] if f["status"] == "Open"]
    sev = {s: sum(f["severity"] == s for f in open_f) for s in SEVERITIES}
    areas = sorted({f["control_area"] for f in open_f})
    dt = ov["detected_type"] if ov["detected_type"].startswith("SOC") else ov["detected_type"].lower()
    if ext["findings"]:
        summary = (f"This {dt} contains {len(ext['findings'])} findings, {len(open_f)} still "
                   f"open ({sev['Critical']} critical, {sev['High']} high, {sev['Medium']} medium). "
                   f"Open issues sit in {', '.join(areas) or 'no specific area'}.")
    else:
        summary = (f"This {dt} raises no control weaknesses. "
                   "No findings or red flags were detected.")
    if report["discrepancies"]:
        summary += (f" It contradicts the golden record on {len(report['discrepancies'])} point(s): "
                    + ", ".join(d["field"] for d in report["discrepancies"]) + ".")
    report["executive_summary"] = summary
    report["confidence"] = "Medium" if ext["findings"] else "Low"
    report["limitations"].append("Rule-based analysis: findings were extracted with patterns, not read by a model.")
    return report


def rule_based_table(ctx: Context) -> dict:
    ctx.call("document_overview", why="Understand the dataset")
    prof = ctx.call("profile_table", why="Profile columns, blanks, formats and duplicates")
    ctx.call("get_audit_policy", why="Load risk weights and tiers")
    cc = ctx.call("cross_check_table", why="Match rows to golden suppliers and compare signals")
    findings, dq = [], []
    for c in prof["columns"]:
        if c["blank_pct"] >= 5:
            dq.append({"issue": f"Blank values in '{c['column']}'", "severity": "Medium" if c["blank_pct"] >= 20 else "Low",
                       "detail": f"{c['blank_pct']}% of rows are blank."})
        if c["type"] == "date" and len(c.get("formats", {})) > 1:
            dq.append({"issue": f"Mixed date formats in '{c['column']}'", "severity": "Low",
                       "detail": ", ".join(f"{k}: {v}" for k, v in c["formats"].items())})
        if c["type"] == "numeric" and c.get("outliers") and not re.search(r"code|_id$|\bid\b|tax|phone|zip|pin",
                                                                         c["column"], re.I):
            dq.append({"issue": f"Outliers in '{c['column']}'", "severity": "Low",
                       "detail": f"{c['outliers']} values outside 1.5×IQR (range {c['min']}–{c['max']})."})
        if re.search(r"country", c["column"], re.I) and c["type"] in ("categorical", "text"):
            raw = ctx.table[c["column"]].astype(str)
            iso = raw.map(norm_country)
            variants = raw.groupby(iso).nunique()
            if (variants > 1).any():
                dq.append({"issue": f"Inconsistent country spellings in '{c['column']}'", "severity": "Low",
                           "detail": f"{int((variants > 1).sum())} countries written more than one way."})
    if prof["duplicate_rows"]:
        dq.append({"issue": "Duplicate rows", "severity": "Medium", "detail": f"{prof['duplicate_rows']} exact duplicates."})
    if cc.get("unmatched"):
        dq.append({"issue": "Suppliers not in the supplier master", "severity": "Medium",
                   "detail": f"{len(cc['unmatched'])} rows: " + ", ".join(u["name"] for u in cc["unmatched"][:6])})
    seen = set()
    low_kinds = ("Stale assessment", "Missing assessment date")
    for kind in low_kinds:
        ids = sorted({x["golden_id"] for x in cc.get("signals", []) if x["kind"] == kind})
        if ids:
            dq.append({"issue": kind + ("s" if kind.startswith("Stale") else "s"), "severity": "Low",
                       "detail": f"{len(ids)} suppliers: " + ", ".join(f"[{i}]" for i in ids[:8])
                                 + (" …" if len(ids) > 8 else "")})
    for n, s in enumerate(sorted(cc.get("signals", []), key=lambda x: SEVERITIES.index(x["severity"])), start=1):
        key = (s["golden_id"], s["kind"])
        if key in seen or s["kind"] in low_kinds:
            continue
        seen.add(key)
        area = {"Data access under-recorded": "Data protection",
                "Questionnaire red flags": "Access control"}.get(s["kind"], "Governance & compliance")
        findings.append({"id": f"S-{len(findings) + 1:02d}", "title": f"{s['kind']}: [{s['golden_id']}]",
                         "severity": s["severity"], "control_area": area, "status": "Open",
                         "evidence": f"Row {s['row']}: {s['evidence']}", "impact": s["detail"],
                         "recommendation": {"Sanctions screening hit": "Escalate to compliance; pause new orders until cleared.",
                                            "Weak financial health": "Review financial stability and continuity plans.",
                                            "Data access under-recorded": "Correct data access in GBAMS/Ariba and rescore.",
                                            "Adverse note": "Investigate the adverse information and record the outcome.",
                                            "External risk higher than AuditPilot": "Review the gap; consider bringing the audit forward.",
                                            "Stale assessment": "Request a refreshed assessment.",
                                            "Questionnaire red flags": "Ask the supplier for evidence and a remediation date for each red flag.",
                                            "Missing assessment date": "Record the assessment date."}.get(s["kind"],
                                                                                                          "Review."),
                         "owner_role": "audit_owner" if s["kind"] != "Data access under-recorded" else "ism",
                         "due_in_days": SEV_DUE[s["severity"]], "supplier_id": s["golden_id"]})
    affected = {}
    for s in cc.get("signals", []):
        a = affected.setdefault(s["golden_id"], {"golden_id": s["golden_id"], "current_tier": s["current_tier"],
                                                 "current_score": s["current_score"], "signals": []})
        if s["kind"] not in a["signals"]:
            a["signals"].append(s["kind"])
    affected = {k: a for k, a in affected.items() if set(a["signals"]) - set(low_kinds)}
    for a in affected.values():
        worst = min((next(x["severity"] for x in cc["signals"] if x["golden_id"] == a["golden_id"] and x["kind"] == k)
                     for k in a["signals"]), key=SEVERITIES.index)
        a["suggested_action"] = ("Escalate now and re-plan audit" if worst == "Critical" else
                                 "Review risk rating and audit timing" if worst == "High" else "Monitor")
    crit = sum(f["severity"] == "Critical" for f in findings)
    high = sum(f["severity"] == "High" for f in findings)
    summary = (f"The dataset has {prof['rows']} rows; {cc.get('matched', 0)} matched to {cc.get('unique_suppliers', 0)} "
               f"golden suppliers and {len(cc.get('unmatched', []))} are not in the supplier master. "
               f"Cross-checking raised {len(findings)} supplier signals ({crit} critical, {high} high) and "
               f"{len(dq)} data quality issues.")
    return {"document_type": "Dataset", "supplier_id": None, "findings": findings, "data_quality": dq,
            "affected_suppliers": sorted(affected.values(), key=lambda a: -a["current_score"]),
            "discrepancies": [], "red_flags": [], "executive_summary": summary,
            "overall_rating": "Needs improvement" if crit or high else "Satisfactory",
            "confidence": "Medium", "limitations": ["Rule-based analysis of a dataset: columns were matched by name."]}


# ======================================================================================
# LLM agent (tool-calling loop; Azure OpenAI or OpenAI API)
# ======================================================================================
def _fn(name, desc, props=None, required=None):
    return {"type": "function", "function": {"name": name, "description": desc,
                                             "parameters": {"type": "object", "properties": props or {},
                                                            "required": required or []}}}


REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "document_type": {"type": "string"},
        "supplier_id": {"type": ["string", "null"], "description": "Golden ID like GS-0091, or null"},
        "executive_summary": {"type": "string", "description": "4-6 sentences; facts only from tools"},
        "overall_rating": {"type": "string", "enum": RATINGS},
        "confidence": {"type": "string", "enum": ["High", "Medium", "Low"]},
        "findings": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "title": {"type": "string"},
            "severity": {"type": "string", "enum": SEVERITIES},
            "control_area": {"type": "string", "enum": list(CONTROL_AREAS) + ["Data quality"]},
            "status": {"type": "string", "enum": ["Open", "Closed"]},
            "evidence": {"type": "string", "description": "Exact quote from the document or row"},
            "impact": {"type": "string"}, "recommendation": {"type": "string"},
            "owner_role": {"type": "string", "enum": ["audit_owner", "supplier_owner", "ism"]},
            "due_in_days": {"type": "integer"}, "supplier_id": {"type": ["string", "null"]}},
            "required": ["title", "severity", "control_area", "status", "evidence", "recommendation"]}},
        "discrepancies": {"type": "array", "items": {"type": "object", "properties": {
            "field": {"type": "string"}, "report_value": {"type": "string"}, "system_value": {"type": "string"},
            "implication": {"type": "string"}}}},
        "red_flags": {"type": "array", "items": {"type": "string", "enum": sorted(ai.FLAG_RULES)}},
        "data_quality": {"type": "array", "items": {"type": "object", "properties": {
            "issue": {"type": "string"}, "severity": {"type": "string"}, "detail": {"type": "string"}}}},
        "recommendations": {"type": "array", "items": {"type": "object", "properties": {
            "priority": {"type": "integer"}, "action": {"type": "string"},
            "owner_role": {"type": "string", "enum": ["audit_owner", "supplier_owner", "ism"]},
            "due_in_days": {"type": "integer"}}}},
        "limitations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["document_type", "executive_summary", "overall_rating", "findings", "confidence"],
}

LLM_TOOLS = [
    _fn("document_overview", "Kind of input, detected type, section list, masking summary."),
    _fn("read_section", "Read one section of a text document (masked).",
        {"index": {"type": "integer"}}, ["index"]),
    _fn("search_document", "Find paragraphs mentioning the query words.", {"query": {"type": "string"}}, ["query"]),
    _fn("detect_suppliers", "Candidate golden suppliers mentioned in a text document."),
    _fn("lookup_supplier", "Golden record for a supplier (coded values only).",
        {"golden_id": {"type": "string"}}, ["golden_id"]),
    _fn("get_audit_policy", "Risk weights, tiers, override floors, eligibility, scope rules, due days."),
    _fn("extract_findings", "Rule-based first pass at findings and red flags. Check it against the text."),
    _fn("compare_with_golden", "Facts the report states vs the golden record.",
        {"golden_id": {"type": "string"}}, ["golden_id"]),
    _fn("simulate_risk", "Recompute the deterministic risk score with new evidence.",
        {"golden_id": {"type": "string"}, "open_critical": {"type": "integer"}, "open_high": {"type": "integer"},
         "open_medium": {"type": "integer"}, "red_flags": {"type": "array", "items": {"type": "string"}},
         "data_access_level": {"type": "string", "enum": ACCESS_ORDER},
         "certification_status": {"type": "string", "enum": ["valid", "expired", "none"]},
         "last_audit_rating": {"type": "string", "enum": ["Satisfactory", "Needs improvement", "Unsatisfactory"]}},
        ["golden_id"]),
    _fn("profile_table", "Column types, blanks, formats, outliers and duplicates of a table."),
    _fn("sample_rows", "Read table rows (masked).", {"n": {"type": "integer"}, "offset": {"type": "integer"}}),
    _fn("query_table", "Value counts of a column, optionally filtered.",
        {"column": {"type": "string"}, "filter_column": {"type": "string"}, "filter_value": {"type": "string"},
         "top_n": {"type": "integer"}}, ["column"]),
    _fn("cross_check_table", "Match table rows to golden suppliers and compare risk signals."),
    _fn("submit_report", "Submit the final structured analysis. Call exactly once, last.", REPORT_SCHEMA["properties"],
        REPORT_SCHEMA["required"]),
]

SYSTEM = """You are AuditPilot's AI Audit Analyst, working for a supplier audit team.
Analyse the input (an audit/assurance report, questionnaire, or a dataset from any source) and produce a detailed,
evidence-based analysis by calling tools, then call submit_report exactly once.

Rules:
- Use only facts returned by tools. Never invent findings, numbers, dates or names.
- Supplier names are masked as tokens like [GS-0091]. Keep the tokens as-is in your output.
- Every finding needs an evidence quote copied exactly from the document text or a table row.
- Risk scores come only from simulate_risk. Do not compute or guess scores yourself.
- Severity: Critical = client data exposed or sanctions; High = key control missing or not operating;
  Medium = control partly operating; Low = documentation gaps; Info = context only.
- Map each finding to one control area and an owner role (audit_owner, supplier_owner, ism).
- For text documents: overview -> detect/lookup supplier -> read the relevant sections -> extract_findings and
  verify it against the text -> compare_with_golden -> simulate_risk -> submit_report.
- For tables: overview -> profile_table -> cross_check_table -> sample or query rows as needed -> submit_report.
- Prioritise the reviewer's focus if one is given. Recommendations must be specific and actionable."""


def _assistant_msg(msg) -> dict:
    d = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        d["tool_calls"] = [{"id": tc.id, "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                           for tc in msg.tool_calls]
    return d


def llm_agent(ctx: Context) -> dict:
    task = {"input_name": ctx.masker.mask(ctx.doc.name.replace("_", " ")), "kind": ctx.doc.kind, "linked_supplier": ctx.golden_id,
            "reviewer_focus": ctx.focus or None, "today": today().isoformat()}
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": "Analyse this input and submit a report.\n" + json.dumps(task)}]
    for _ in range(MAX_STEPS):
        t0 = time.time()
        resp = ai.chat(temperature=0, tools=LLM_TOOLS, messages=messages)
        msg = resp.choices[0].message
        ai._log("analyst_step", ai._hash(messages[-1]), {"tool_calls": [tc.function.name for tc in (msg.tool_calls or [])]},
                "ok", int((time.time() - t0) * 1000))
        messages.append(_assistant_msg(msg))
        if not msg.tool_calls:
            messages.append({"role": "user", "content": "Call submit_report now with your analysis."})
            continue
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments or "{}")
            if tc.function.name == "submit_report":
                ctx.trace.append({"step": len(ctx.trace) + 1, "tool": "submit_report", "args": {}, "why": "Final report",
                                  "status": "ok", "ms": 0, "summary": f"{len(args.get('findings', []))} findings"})
                ctx.on_step(ctx.trace[-1])
                return args
            if tc.function.name not in TOOLS:
                result = {"error": "unknown tool"}
            else:
                result = ctx.call(tc.function.name, args, why="Model's choice")
                if tc.function.name == "extract_findings":
                    ctx.cache["extract"] = result
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(result, default=str)[:9000]})
    raise RuntimeError("Agent did not submit a report within the step limit.")


# ======================================================================================
# Verification and finalisation
# ======================================================================================
def _verify(evidence: str, haystack: str) -> bool:
    e = re.sub(r"\s+", " ", (evidence or "")).strip().lower()
    if len(e) < 8:
        return False
    return fuzz.partial_ratio(e[:200], haystack) >= 85


def finalize(ctx: Context, rep: dict, mode: str) -> dict:
    hay = re.sub(r"\s+", " ", ctx.masked_text).lower() if ctx.doc.kind == "text" else \
        " ".join(ctx.masked_table.astype(str).apply(lambda r: " ".join(f"{k}={v}" for k, v in r.items()), axis=1)).lower()
    findings = []
    for n, f in enumerate(rep.get("findings") or [], start=1):
        f = dict(f)
        f.setdefault("id", f"F-{n:02d}")
        f["severity"] = f.get("severity") if f.get("severity") in SEVERITIES else _sev_guess(f.get("title", ""))
        f["status"] = f.get("status") if f.get("status") in ("Open", "Closed") else "Open"
        f["control_area"] = f.get("control_area") or _area(f.get("title", ""))
        f["owner_role"] = f.get("owner_role") or AREA_OWNER.get(f["control_area"], "audit_owner")
        f["due_in_days"] = int(f.get("due_in_days") or SEV_DUE[f["severity"]])
        f["recommendation"] = f.get("recommendation") or AREA_RECO.get(f["control_area"], "Review.")
        f.setdefault("impact", "")
        ev = f.get("evidence", "")
        m = re.match(r"\s*row\s+(\d+)\s*:\s*([^=]+?)\s*=\s*(.*)$", ev, re.I | re.S) if ctx.doc.kind == "table" else None
        if m and m.group(2) in ctx.table.columns and 0 <= int(m.group(1)) - 2 < len(ctx.table):
            cell = str(ctx.table.iloc[int(m.group(1)) - 2][m.group(2)]).strip()
            val = m.group(3).strip()
            f["evidence_verified"] = (val == "<blank>" and cell == "") or (
                bool(val) and fuzz.partial_ratio(val.lower(), cell.lower()) >= 90)
        else:
            f["evidence_verified"] = _verify(ev, hay)
        f.setdefault("supplier_id", rep.get("supplier_id") or ctx.golden_id)
        findings.append(f)
    findings.sort(key=lambda f: (f["status"] != "Open", SEVERITIES.index(f["severity"])))
    rep["findings"] = findings
    gid = rep.get("supplier_id") or ctx.golden_id
    if gid and not re.fullmatch(r"GS-\d{4}", str(gid)):
        gid = ctx.golden_id
    rep["supplier_id"] = gid

    # Score impact is always recomputed by the engine, never taken from the model.
    if gid and ctx.doc.kind == "text":
        open_f = [f for f in findings if f["status"] == "Open"]
        facts = _facts(ctx.masked_text)
        dtype = rep.get("document_type", "")
        args = {"golden_id": gid, "red_flags": rep.get("red_flags") or []}
        if any(k in dtype.lower() for k in ("audit", "soc", "penetration")) and findings:
            args |= {"open_critical": sum(f["severity"] == "Critical" for f in open_f),
                     "open_high": sum(f["severity"] == "High" for f in open_f),
                     "open_medium": sum(f["severity"] == "Medium" for f in open_f)}
        for k in ("data_access_level", "certification_status"):
            if k in facts:
                args[k] = facts[k]
        sim = tool_simulate_risk(ctx, **args)
        rep["score_impact"] = sim | {"explanation": (
            "Recomputed by the AuditPilot risk engine using the report's open findings, red flags and stated facts.")}
    rep.setdefault("affected_suppliers", [])
    rep.setdefault("discrepancies", [])
    rep.setdefault("data_quality", [])
    rep.setdefault("limitations", [])
    if not rep.get("recommendations"):
        recs = []
        for f in [f for f in findings if f["status"] == "Open"][:8]:
            recs.append({"priority": len(recs) + 1, "action": f"{f['id']}: {f['recommendation']}",
                         "owner_role": f["owner_role"], "due_in_days": f["due_in_days"]})
        si = rep.get("score_impact")
        if si and TIER_ORDER.index(si["projected"]["risk_tier"]) > TIER_ORDER.index(si["current"]["risk_tier"]):
            recs.insert(0, {"priority": 0, "action": f"Accept findings into the risk model: tier moves "
                                                    f"{si['current']['risk_tier']} → {si['projected']['risk_tier']}; "
                                                    "bring the next audit forward.",
                            "owner_role": "audit_owner", "due_in_days": 7})
        for d in rep["discrepancies"][:4]:
            recs.append({"priority": len(recs) + 1, "action": f"Correct '{d['field']}' in the source system "
                                                              f"(report: {d['report_value']}, system: {d['system_value']}).",
                         "owner_role": "audit_owner", "due_in_days": 14})
        for i, r in enumerate(recs, start=1):
            r["priority"] = i
        rep["recommendations"] = recs
    unver = sum(not f["evidence_verified"] for f in findings)
    if unver:
        rep["limitations"].append(f"{unver} finding(s) have evidence that could not be matched to the source text; "
                                  "review before acting.")
    sev_open = {s: sum(1 for f in findings if f["status"] == "Open" and f["severity"] == s) for s in SEVERITIES}
    si = rep.get("score_impact")
    rep["risk_level"] = (si["projected"]["risk_tier"] if si else
                         "Critical" if sev_open["Critical"] else "High" if sev_open["High"] else
                         "Medium" if sev_open["Medium"] else "Low")
    rep["metrics"] = {"findings": len(findings), "open": sum(sev_open.values()),
                      "critical_open": sev_open["Critical"], "high_open": sev_open["High"],
                      "control_areas": len({f["control_area"] for f in findings if f["status"] == "Open"}),
                      "evidence_verified_pct": round(100 * (1 - unver / len(findings))) if findings else None,
                      "discrepancies": len(rep["discrepancies"])}
    rep["mode"] = mode
    rep["masking"] = ctx.mask_summary
    rep["source_name"] = ctx.doc.name
    rep["source_kind"] = ctx.doc.kind
    rep["generated_at"] = db.now()
    if rep.get("overall_rating") not in RATINGS:
        rep["overall_rating"] = _rating_from(findings, None)
    return ctx.masker.unmask(rep)


def analyze(doc: Document, golden_id: str | None = None, focus: str = "", actor: str = "system",
            on_step=None, force_rules: bool = False) -> dict:
    golden = db.read_table("golden")
    ctx = Context(doc, golden, golden_id, focus, on_step)
    mode, note = "rule-based", None
    rep = None
    if ai.enabled() and not force_rules and time.time() >= ai._paused_until:
        try:
            rep = llm_agent(ctx)
            mode = "ai-agent"
        except Exception as exc:
            note = f"AI agent failed ({str(exc)[:120]}); the rule-based analyst completed the report."
            ctx.trace.append({"step": len(ctx.trace) + 1, "tool": "fallback", "args": {}, "why": note,
                              "status": "error", "ms": 0, "summary": "switched to rule-based analyst"})
            ctx.on_step(ctx.trace[-1])
    if rep is None:
        rep = rule_based_text(ctx) if doc.kind == "text" else rule_based_table(ctx)
    report = finalize(ctx, rep, mode)
    if note:
        report["limitations"].insert(0, note)
    report_id = f"RPT-{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:4]}"
    report["report_id"] = report_id
    db.execute("INSERT INTO analysis_reports(report_id, ts, actor, source_name, source_kind, golden_id, mode,"
               " overall_rating, risk_level, report, trace) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (report_id, db.now(), actor, doc.name, doc.kind, report.get("supplier_id"), mode,
                report["overall_rating"], report["risk_level"], json.dumps(report, default=str),
                json.dumps(ctx.trace, default=str)))
    audit_log.log(actor, "ai_analysis_completed", "analysis_report", report_id, None,
                  {"source": doc.name, "mode": mode, "supplier": report.get("supplier_id"),
                   "findings": report["metrics"]["findings"], "masked": ctx.mask_summary})
    report["trace"] = ctx.trace
    return report


# ======================================================================================
# Report store and actions
# ======================================================================================
def list_reports():
    return db.query("SELECT report_id, ts, actor, source_name, golden_id, mode, overall_rating, risk_level, accepted "
                    "FROM analysis_reports ORDER BY ts DESC")


def load_report(report_id: str) -> dict | None:
    df = db.query("SELECT * FROM analysis_reports WHERE report_id=?", (report_id,))
    if not len(df):
        return None
    r = df.iloc[0]
    rep = json.loads(r["report"])
    rep["trace"] = json.loads(r["trace"] or "[]")
    rep["accepted"] = bool(r["accepted"])
    rep["accepted_by"] = r["accepted_by"]
    return rep


def accept_findings(report_id: str, actor: str) -> dict:
    """Human approval: feed the report's open findings into the risk model, then rerun the pipeline."""
    from .pipeline import run_all
    rep = load_report(report_id)
    gid = rep.get("supplier_id") if rep else None
    if not gid or rep.get("source_kind") != "text":
        return {"ok": False, "message": "Only supplier-specific reports can be accepted into the risk model."}
    before = db.query("SELECT risk_score, risk_tier FROM golden WHERE golden_id=?", (gid,)).iloc[0].to_dict()
    db.execute("UPDATE analysis_reports SET accepted=1, accepted_by=?, accepted_at=? WHERE report_id=?",
               (actor, db.now(), report_id))
    audit_log.log(actor, "ai_findings_accepted", "analysis_report", report_id, before,
                  {"golden_id": gid, "open": rep["metrics"]["open"]})
    run_all(actor=actor)
    after = db.query("SELECT risk_score, risk_tier FROM golden WHERE golden_id=?", (gid,)).iloc[0].to_dict()
    return {"ok": True, "before": before, "after": after,
            "message": f"Accepted. Risk {before['risk_score']:.1f} {before['risk_tier']} → "
                       f"{after['risk_score']:.1f} {after['risk_tier']}."}


def report_overlays() -> dict[str, dict]:
    """Accepted, supplier-specific reports: latest one per supplier overrides open-finding counts."""
    if not db.table_exists("analysis_reports"):
        return {}
    df = db.query("SELECT report_id, golden_id, report FROM analysis_reports WHERE accepted=1 AND golden_id IS NOT NULL "
                  "ORDER BY accepted_at")
    out = {}
    for r in df.itertuples():
        rep = json.loads(r.report)
        if rep.get("source_kind") != "text":
            continue
        open_f = [f for f in rep.get("findings", []) if f.get("status") == "Open"]
        out[r.golden_id] = {
            "report_id": r.report_id,
            "open_critical": sum(f["severity"] == "Critical" for f in open_f),
            "open_high": sum(f["severity"] == "High" for f in open_f),
            "open_medium": sum(f["severity"] == "Medium" for f in open_f),
            "red_flags": rep.get("red_flags") or [],
            "rating": rep.get("overall_rating") if rep.get("overall_rating") in RATINGS[:3] else None,
            "facts": {k: v for k, v in (rep.get("score_impact", {}).get("inputs_changed", {}) or {}).items()
                      if k in ("data_access_level", "certification_status")},
        }
    return out


def create_tasks(report_id: str, actor: str, min_severity: str = "Medium") -> dict:
    rep = load_report(report_id)
    if not rep:
        return {"created": 0}
    cut = SEVERITIES.index(min_severity)
    golden = db.read_table("golden").set_index("golden_id")
    rows, notified = [], {}
    existing = set(db.query("SELECT task_id FROM tasks WHERE plan_id=?", (report_id,)).task_id)
    for f in rep["findings"]:
        gid = f.get("supplier_id") or rep.get("supplier_id")
        if f["status"] != "Open" or SEVERITIES.index(f["severity"]) > cut or gid not in golden.index:
            continue
        g = golden.loc[gid]
        owner = g[f["owner_role"]] if f["owner_role"] in ("audit_owner", "supplier_owner", "ism") else g["audit_owner"]
        tid = f"{report_id}-{f['id']}-{gid}"
        if tid in existing:
            continue
        due = (today() + pd.Timedelta(days=int(f["due_in_days"]))).isoformat()
        rows.append((tid, report_id, gid, len(rows) + 1, f"[{f['severity']}] {f['title'][:120]}", f["owner_role"],
                     owner, due, "Not started", db.now()))
        notified.setdefault((gid, owner), []).append(f["title"][:80])
    db.executemany("INSERT INTO tasks(task_id, plan_id, golden_id, seq, title, owner_role, owner, due_date, status,"
                   " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    for (gid, owner), titles in notified.items():
        notify_remediation(gid, owner, titles, report_id, golden.loc[gid]["legal_name"])
    audit_log.log(actor, "remediation_tasks_created", "analysis_report", report_id, None, {"tasks": len(rows)})
    return {"created": len(rows)}


def notify_remediation(gid: str, owner: str, titles: list[str], report_id: str, name: str) -> None:
    from . import notifier
    subject = f"Remediation actions assigned: {name}"
    body = f"{len(titles)} action(s) from AI analysis {report_id}: " + "; ".join(titles[:4])
    db.execute("INSERT INTO notifications(ts, event, golden_id, recipient, channel, subject, body, status, dedupe_key)"
               " VALUES (?,?,?,?,?,?,?,?,?)",
               (db.now(), "remediation_assigned", gid, owner, "in_app", subject, body, "delivered",
                f"rem:{report_id}:{gid}:{owner}"))
    if "email" in notifier.channels():
        notifier._send_email(notifier.PEOPLE.get(owner, {}).get("email", owner), subject, body, None)


def to_markdown(rep: dict) -> str:
    L = [f"# AI analysis: {rep['source_name']}", "",
         f"Report {rep.get('report_id', '')} · generated {rep.get('generated_at', '')} · mode {rep.get('mode')} · "
         f"supplier {rep.get('supplier_id') or '—'}", "",
         f"**Overall rating:** {rep['overall_rating']} · **Risk level:** {rep['risk_level']} · "
         f"**Confidence:** {rep.get('confidence', '—')}", "", "## Executive summary", "", rep["executive_summary"], ""]
    si = rep.get("score_impact")
    if si:
        L += ["## Score impact", "", f"Current {si['current']['risk_score']} ({si['current']['risk_tier']}) → projected "
              f"{si['projected']['risk_score']} ({si['projected']['risk_tier']}). {si.get('explanation', '')}", ""]
    if rep["findings"]:
        L += ["## Findings", "", "| ID | Severity | Status | Control area | Finding | Evidence verified | Recommendation |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
        for f in rep["findings"]:
            L.append(f"| {f['id']} | {f['severity']} | {f['status']} | {f['control_area']} | {f['title']} | "
                     f"{'yes' if f['evidence_verified'] else 'no'} | {f['recommendation']} |")
        L.append("")
    if rep.get("discrepancies"):
        L += ["## Discrepancies with the golden record", "", "| Field | Report | System | Implication |",
              "| --- | --- | --- | --- |"]
        L += [f"| {d['field']} | {d['report_value']} | {d['system_value']} | {d['implication']} |" for d in rep["discrepancies"]]
        L.append("")
    if rep.get("affected_suppliers"):
        L += ["## Affected suppliers", "", "| Supplier | Current tier | Signals | Suggested action |", "| --- | --- | --- | --- |"]
        L += [f"| {a['golden_id']} | {a['current_tier']} | {', '.join(a['signals'])} | {a['suggested_action']} |"
              for a in rep["affected_suppliers"]]
        L.append("")
    if rep.get("data_quality"):
        L += ["## Data quality", ""] + [f"- **{d['severity']}** {d['issue']}: {d['detail']}" for d in rep["data_quality"]] + [""]
    if rep.get("recommendations"):
        L += ["## Recommended actions", ""] + [f"{r['priority']}. {r['action']} — {r['owner_role']}, within "
                                               f"{r['due_in_days']} days" for r in rep["recommendations"]] + [""]
    if rep.get("limitations"):
        L += ["## Limitations", ""] + [f"- {x}" for x in rep["limitations"]] + [""]
    L.append("_Synthetic data. Scores are computed by the AuditPilot risk engine; AI does not set scores._")
    return "\n".join(L)
