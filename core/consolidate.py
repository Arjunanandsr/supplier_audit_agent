"""Ingest -> normalize -> match -> survive -> publish.

Builds one golden record per supplier, with field lineage and a conflict log.
"""
from __future__ import annotations

import re
from datetime import date, datetime

import pandas as pd
from rapidfuzz import fuzz, process

from . import db
from .config import ACCESS_ORDER, CRIT_ORDER, PEOPLE, RAW_DIR, people_with_role, today
from .generate_data import CATEGORY_NAMES, COUNTRIES

SUFFIXES = {"PVT", "PRIVATE", "LTD", "LIMITED", "INC", "INCORPORATED", "LLC", "CORP",
            "CORPORATION", "CO", "GMBH", "PTE", "PLC"}
COUNTRY_MAP = {}
for iso, (name, _s, _a, spellings) in COUNTRIES.items():
    for sp in spellings + [name, iso]:
        COUNTRY_MAP[sp.upper()] = iso

AUTO_MERGE = 90
REVIEW_MIN = 80

SOURCES = {
    "VMS": dict(file="vms_vendors.csv", id="vendor_id", name="vendor_name"),
    "GBAMS": dict(file="gbams_associates.csv", id="associate_id", name="associate_org_name"),
    "PCM": dict(file="pcm_contracts.csv", id="contract_id", name="contract_party"),
}


# ---------- normalization ----------
def norm_name(v) -> str:
    s = str(v).upper().replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    return " ".join(t for t in s.split() if t not in SUFFIXES)


def norm_country(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return COUNTRY_MAP.get(str(v).strip().upper())


def parse_date(v):
    if v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == "":
        return None
    s = str(v).strip()
    try:
        if "/" in s:
            return datetime.strptime(s, "%d/%m/%Y").date()
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def parse_certs(v, ref: date):
    """'ISO27001:2027-01-01|SOC2:2026-12-01' -> (status, detail, latest_expiry)."""
    if v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == "":
        return "none", "None on file", None
    items = []
    for part in str(v).split("|"):
        if ":" in part:
            name, exp = part.split(":", 1)
            items.append((name.strip(), parse_date(exp)))
    valid = [e for _, e in items if e and e >= ref]
    latest = max((e for _, e in items if e), default=None)
    detail = ", ".join(f"{n} (exp {e.isoformat() if e else '?'})" for n, e in items)
    return ("valid" if valid else "expired"), detail, latest


def load_sources() -> dict[str, pd.DataFrame]:
    files = {"ARIBA": "ariba_suppliers.csv", **{k: v["file"] for k, v in SOURCES.items()},
             "HISTORY": "audit_history.csv"}
    return {k: pd.read_csv(RAW_DIR / f, dtype=str, keep_default_na=False) for k, f in files.items()}


def _num(v, default=None):
    try:
        if v is None or str(v).strip() == "":
            return default
        return float(v)
    except ValueError:
        return default


def _owner(golden_id: str, role: str) -> str:
    pool = people_with_role(role)
    return pool[int(golden_id.split("-")[1]) % len(pool)]


# ---------- main ----------
def consolidate() -> dict:
    t = today()
    src = load_sources()
    stats: dict = {"source_rows": {k: len(v) for k, v in src.items() if k != "HISTORY"}}

    ariba = src["ARIBA"]
    before = len(ariba)
    ariba = ariba.drop_duplicates().reset_index(drop=True)
    stats["ariba_duplicates_removed"] = before - len(ariba)
    ariba["norm_name"] = ariba["supplier_name"].map(norm_name)
    ariba["iso"] = ariba["country"].map(norm_country)
    tax_index = {r.tax_id: r.supplier_id for r in ariba.itertuples() if r.tax_id}

    decisions = db.read_table("match_decisions")
    dec = {(d.source, d.source_id): d.decision for d in decisions.itertuples()} if len(decisions) else {}

    match_rows, matched = [], {k: {} for k in SOURCES}
    for sname, cfg in SOURCES.items():
        df = src[sname]
        for row in df.to_dict("records"):
            sid, raw_name = row[cfg["id"]], row[cfg["name"]]
            nn, iso = norm_name(raw_name), norm_country(row.get("country"))
            method, score, aid = "fuzzy", 0.0, None
            if row.get("tax_id") and row["tax_id"] in tax_index:
                method, score, aid = "tax_id", 100.0, tax_index[row["tax_id"]]
            else:
                cand = ariba[ariba["iso"] == iso] if iso else ariba
                best = process.extractOne(nn, cand["norm_name"].tolist(), scorer=fuzz.token_sort_ratio)
                if best:
                    score = round(best[1], 1)
                    aid = cand.iloc[best[2]]["supplier_id"]
            if score >= AUTO_MERGE:
                status = "auto"
            elif score >= REVIEW_MIN:
                status = "review"
            else:
                status, aid = "unmatched", (aid if score >= 60 else None)
            d = dec.get((sname, sid))
            if d == "approve" and status in ("review", "unmatched") and aid:
                status = "approved"
            elif d == "reject":
                status = "rejected"
            best_name = ariba.loc[ariba.supplier_id == aid, "supplier_name"].iloc[0] if aid else ""
            match_rows.append(dict(source=sname, source_id=sid, source_name=raw_name, ariba_id=aid or "",
                                   ariba_name=best_name, score=score, method=method, status=status))
            if status in ("auto", "review", "approved"):
                prev = matched[sname].get(aid)
                if prev is None or prev[1] < score:
                    matched[sname][aid] = (row, score)
    matches = pd.DataFrame(match_rows)
    stats["match_status"] = matches.groupby(["source", "status"]).size().unstack(fill_value=0).to_dict("index")

    # audit history per supplier
    hist = src["HISTORY"].copy()
    for c in ["findings_critical", "findings_high", "findings_medium", "open_critical", "open_high", "open_medium"]:
        hist[c] = hist[c].astype(int)
    hist["d"] = hist["audit_date"].map(parse_date)
    hist = hist.sort_values("d")
    last = hist.groupby("supplier_ref").tail(1).set_index("supplier_ref")
    counts = hist.groupby("supplier_ref").size()

    cd = db.read_table("conflict_decisions")
    cdec = {(r.golden_id, r.field): r for r in cd.itertuples()} if len(cd) else {}

    golden, lineage, conflicts = [], [], []
    for a in ariba.to_dict("records"):
        aid = a["supplier_id"]
        gid = "GS-" + aid.split("-")[1]
        v = matched["VMS"].get(aid, (None,))[0]
        g = matched["GBAMS"].get(aid, (None,))[0]
        p = matched["PCM"].get(aid, (None,))[0]
        rec: dict = {"golden_id": gid, "ariba_id": aid}

        def put(field, value, source, rule):
            rec[field] = value
            lineage.append(dict(golden_id=gid, field=field,
                                value=None if value is None else str(value), source=source, rule=rule))

        put("legal_name", a["supplier_name"].strip(), "Ariba", "Ariba is master for legal name")
        put("country", a["iso"], "Ariba", "Ariba wins; normalized to ISO 3166 alpha-2")
        put("category_code", a["category_code"], "Ariba", "Ariba wins")
        rec["category_name"] = CATEGORY_NAMES.get(a["category_code"], "Other")
        put("service_type", v["service_type"] if v else None, "VMS" if v else "missing",
            "VMS wins" if v else "Not in VMS")
        put("contractor_headcount", _num(v["contractor_headcount"]) if v else None, "VMS" if v else "missing",
            "VMS wins" if v else "Not in VMS")
        put("offshore_delivery", (v["offshore_delivery"] == "Y") if v else None, "VMS" if v else "missing",
            "VMS wins" if v else "Not in VMS")

        # Data access: most restrictive wins
        declared = a["data_access_declared"] or None
        recorded = g["data_access_level"] if g else None
        cands = {k: x for k, x in {"Ariba": declared, "GBAMS": recorded}.items() if x}
        access = max(cands.values(), key=ACCESS_ORDER.index) if cands else None
        src_name = next((k for k, x in cands.items() if x == access), "missing")
        put("data_access_level", access, src_name, "Most restrictive value across sources wins")
        if declared and recorded and declared != recorded:
            conflicts.append(dict(golden_id=gid, field="data_access_level", ariba_value=declared,
                                  other_source="GBAMS", other_value=recorded, rule_value=access,
                                  rule="Most restrictive wins"))

        put("users_with_system_access", _num(g["users_with_system_access"]) if g else None,
            "GBAMS" if g else "missing", "Highest count wins" if g else "Not in GBAMS")

        crit_decl = a["criticality_declared"] or None
        crit_pcm = p["criticality_tier"] if p else None
        if crit_pcm:
            put("criticality_tier", crit_pcm, "PCM", "PCM wins")
        else:
            put("criticality_tier", crit_decl, "Ariba", "PCM missing: Ariba self-declared tier used")
        if crit_decl and crit_pcm and crit_decl != crit_pcm:
            conflicts.append(dict(golden_id=gid, field="criticality_tier", ariba_value=crit_decl,
                                  other_source="PCM", other_value=crit_pcm, rule_value=crit_pcm,
                                  rule="PCM wins"))

        put("contract_value_inr", _num(p["contract_value_inr"]) if p else None, "PCM" if p else "missing",
            "PCM wins" if p else "Not in PCM")
        cs, ce = (parse_date(p["contract_start"]), parse_date(p["contract_end"])) if p else (None, None)
        put("contract_start", cs.isoformat() if cs else None, "PCM" if p else "missing", "PCM wins")
        put("contract_end", ce.isoformat() if ce else None, "PCM" if p else "missing", "PCM wins")
        put("uses_subprocessors", (p["uses_subprocessors"] == "Y") if p else None, "PCM" if p else "missing",
            "PCM wins" if p else "Not in PCM")

        status, detail, expiry = parse_certs(a["certifications"], t)
        put("certification_status", status, "Ariba", "Latest valid expiry wins")
        rec["certification_detail"] = detail
        rec["certification_expiry"] = expiry.isoformat() if expiry else None
        put("assessment_score", _num(a["assessment_score"]), "Ariba", "Ariba questionnaire")
        rec["assessment_comments"] = a["assessment_comments"]
        od = parse_date(a["onboarding_date"])
        rec["onboarding_date"] = od.isoformat() if od else None

        if aid in last.index:
            L = last.loc[aid]
            put("last_audit_date", L["d"].isoformat(), "Audit history", "Most recent audit")
            rec["last_audit_rating"] = L["rating"]
            rec["open_critical"], rec["open_high"], rec["open_medium"] = (
                int(L["open_critical"]), int(L["open_high"]), int(L["open_medium"]))
            rec["audits_count"] = int(counts[aid])
        else:
            put("last_audit_date", None, "Audit history", "Never audited")
            rec["last_audit_rating"] = None
            rec["open_critical"] = rec["open_high"] = rec["open_medium"] = 0
            rec["audits_count"] = 0
        rec["in_vms"], rec["in_gbams"], rec["in_pcm"] = v is not None, g is not None, p is not None
        rec["audit_owner"] = _owner(gid, "audit_owner")
        rec["supplier_owner"] = _owner(gid, "supplier_owner")
        rec["ism"] = _owner(gid, "ism")
        golden.append(rec)

    golden = pd.DataFrame(golden)
    lineage = pd.DataFrame(lineage)
    conflicts = pd.DataFrame(conflicts)

    # Apply human decisions recorded earlier (only if source values are unchanged)
    statuses, resolved = [], []
    for c in conflicts.to_dict("records"):
        sig = f"{c['ariba_value']}|{c['other_value']}"
        d = cdec.get((c["golden_id"], c["field"]))
        if d is not None and d.values_sig == sig:
            statuses.append("resolved")
            resolved.append(d.resolved_value)
            idx = golden.index[golden.golden_id == c["golden_id"]][0]
            golden.at[idx, c["field"]] = d.resolved_value
            lineage.loc[(lineage.golden_id == c["golden_id"]) & (lineage.field == c["field"]),
                        ["value", "source", "rule"]] = [d.resolved_value, "Human review",
                                                        f"{d.decision} by {d.reviewer} on {d.ts[:10]}"]
        else:
            statuses.append("pending")
            resolved.append(None)
    if len(conflicts):
        conflicts["values_sig"] = conflicts["ariba_value"] + "|" + conflicts["other_value"]
        conflicts["status"] = statuses
        conflicts["resolved_value"] = resolved
        conflicts = conflicts.merge(golden[["golden_id", "legal_name"]], on="golden_id", how="left")

    # Data quality: share of 15 critical attributes that are filled and conflict-free
    critical = ["golden_id", "legal_name", "country", "category_code", "service_type", "data_access_level",
                "users_with_system_access", "criticality_tier", "contract_value_inr", "contract_end",
                "certification_status", "assessment_score", "last_audit_date", "open_critical",
                "uses_subprocessors"]
    pending = set()
    if len(conflicts):
        pending = {(c.golden_id, c.field) for c in conflicts.itertuples() if c.status == "pending"}

    def dq(row):
        ok = 0
        for f in critical:
            val = row[f]
            filled = not (val is None or (isinstance(val, float) and pd.isna(val)))
            if f == "last_audit_date":
                filled = True  # "never audited" is a known value
            if filled and (row["golden_id"], f) not in pending:
                ok += 1
        return round(ok / len(critical), 2)

    golden["data_quality"] = golden.apply(dq, axis=1)
    golden["pending_conflicts"] = golden["golden_id"].map(
        lambda gid: sum(1 for (g, _f) in pending if g == gid))
    stats["golden_records"] = len(golden)
    stats["conflicts_pending"] = int((conflicts["status"] == "pending").sum()) if len(conflicts) else 0
    stats["conflicts_total"] = len(conflicts)
    return dict(golden=golden, lineage=lineage, conflicts=conflicts, matches=matches, stats=stats)


def access_rank(v) -> int:
    return ACCESS_ORDER.index(v) if v in ACCESS_ORDER else 0


def crit_rank(v) -> int:
    return CRIT_ORDER.index(v) if v in CRIT_ORDER else 1


__all__ = ["consolidate", "norm_name", "norm_country", "parse_date", "PEOPLE"]
