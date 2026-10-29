"""Explainable 7-factor risk score (0-100), tiers and override floors.

Deterministic: the LLM never sets a score. Weights come from config/weights.yaml.
"""
from __future__ import annotations

import json

import pandas as pd

from .config import ACCESS_LABEL, weights as load_weights

FACTOR_LABEL = {
    "data_sensitivity": "Data sensitivity",
    "access_exposure": "Access exposure",
    "business_criticality": "Business criticality",
    "financial_exposure": "Financial exposure",
    "assessment_result": "Assessment result",
    "audit_history": "Audit history",
    "certification_gap": "Certification gap",
}


def _isnull(v) -> bool:
    return v is None or (isinstance(v, float) and pd.isna(v))


def tier_for(score: float, w: dict) -> str:
    t = w["tiers"]
    if score >= t["critical"]:
        return "Critical"
    if score >= t["high"]:
        return "High"
    if score >= t["medium"]:
        return "Medium"
    return "Low"


def score_supplier(row: dict, percentile: float | None, flags: list[str], w: dict | None = None) -> dict:
    """Score one golden record. `percentile` is the contract value percentile (0-100)."""
    w = w or load_weights()
    f: dict[str, dict] = {}

    acc = row.get("data_access_level") or "none"
    f["data_sensitivity"] = (w["data_sensitivity"].get(acc, 0), f"Data access: {ACCESS_LABEL.get(acc, acc)}")

    users = row.get("users_with_system_access")
    users = 0 if _isnull(users) else int(users)
    s = 0
    for lo, hi, val in w["access_bands"]:
        if lo <= users <= hi:
            s = val
    f["access_exposure"] = (s, f"{users} users with system access")

    crit = row.get("criticality_tier") or "medium"
    f["business_criticality"] = (w["business_criticality"].get(crit, 50), f"Criticality: {crit}")

    pct = w["financial"]["missing_percentile"] if percentile is None or pd.isna(percentile) else percentile
    f["financial_exposure"] = (round(float(pct), 1), f"Contract value at {round(float(pct))}th percentile")

    a = row.get("assessment_score")
    base = w["assessment"]["missing_score"] if _isnull(a) else 100 - float(a)
    s = min(100.0, base + w["assessment"]["red_flag_penalty"] * len(flags))
    f["assessment_result"] = (round(s, 1), f"Assessment {'n/a' if _isnull(a) else int(a)}; "
                                            f"{len(flags)} red flag(s)")

    h = w["audit_history"]
    oc, oh, om = int(row.get("open_critical") or 0), int(row.get("open_high") or 0), int(row.get("open_medium") or 0)
    s = oc * h["critical_open"] + oh * h["high_open"] + om * h["medium_open"]
    if row.get("last_audit_rating") == "Unsatisfactory":
        s += h["unsatisfactory_bonus"]
    f["audit_history"] = (min(100, s), f"Open findings: {oc} critical, {oh} high, {om} medium"
                          + ("; last rating unsatisfactory" if row.get("last_audit_rating") == "Unsatisfactory" else ""))

    cert = row.get("certification_status") or "none"
    f["certification_gap"] = (w["certification_gap"].get(cert, 100), f"Certification: {cert}")

    factors = {}
    raw = 0.0
    for k, (sc, detail) in f.items():
        contrib = round(sc * w["weights"][k], 2)
        raw += contrib
        factors[k] = {"label": FACTOR_LABEL[k], "score": sc, "weight": w["weights"][k],
                      "contribution": contrib, "detail": detail}
    raw = round(raw, 1)

    final, overrides = raw, []
    o = w["overrides"]
    if oc > 0 and final < o["critical_finding_floor"]:
        final = float(o["critical_finding_floor"])
        overrides.append("Open critical finding: floor at High")
    if acc == "pii" and cert != "valid" and final < o["pii_no_cert_floor"]:
        final = float(o["pii_no_cert_floor"])
        overrides.append("PII access without valid certification: floor at High")

    dq = row.get("data_quality")
    low_conf = dq is not None and not pd.isna(dq) and dq < o["low_confidence_below"]
    return {"raw_score": raw, "risk_score": final, "risk_tier": tier_for(final, w),
            "overrides": overrides, "low_confidence": bool(low_conf), "factors": factors,
            "weights_version": w["version"]}


def score_all(golden: pd.DataFrame, flags: dict[str, list[str]], w: dict | None = None) -> pd.DataFrame:
    w = w or load_weights()
    pct = golden["contract_value_inr"].astype(float).rank(pct=True) * 100
    out = []
    for i, row in golden.iterrows():
        res = score_supplier(row.to_dict(), pct.iloc[i] if not pd.isna(pct.iloc[i]) else None,
                             flags.get(row["golden_id"], []), w)
        out.append({"risk_score": res["risk_score"], "raw_score": res["raw_score"], "risk_tier": res["risk_tier"],
                    "overrides": json.dumps(res["overrides"]), "low_confidence": res["low_confidence"],
                    "factors": json.dumps(res["factors"]), "weights_version": res["weights_version"],
                    "red_flags": json.dumps(flags.get(row["golden_id"], []))})
    return pd.concat([golden.reset_index(drop=True), pd.DataFrame(out)], axis=1)


def what_if(golden: pd.DataFrame, golden_id: str, field: str, value) -> dict:
    """Score a supplier as if one field had a different value (used in the conflict queue)."""
    w = load_weights()
    pct = golden["contract_value_inr"].astype(float).rank(pct=True) * 100
    i = golden.index[golden.golden_id == golden_id][0]
    row = golden.loc[i].to_dict()
    row[field] = value
    flags = json.loads(row.get("red_flags") or "[]")
    return score_supplier(row, pct.loc[i] if not pd.isna(pct.loc[i]) else None, flags, w)
