"""Audit eligibility, frequency, next due date and scope modules, with a reason for each decision."""
from __future__ import annotations

import json
from datetime import date, timedelta

import pandas as pd

from .config import ACCESS_LABEL, rules as load_rules, today


def _isnull(v) -> bool:
    return v is None or (isinstance(v, float) and pd.isna(v))


def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    day = min(d.day, [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0) else 28,
                      31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
    return date(y, m, day)


def scope_for(row: dict, rules: dict) -> list[dict]:
    acc = row.get("data_access_level") or "none"
    users = 0 if _isnull(row.get("users_with_system_access")) else int(row["users_with_system_access"])
    crit = row.get("criticality_tier") or "medium"
    open_total = int(row.get("open_critical") or 0) + int(row.get("open_high") or 0) + int(row.get("open_medium") or 0)
    checks = {
        "governance": (True, "always included"),
        "infosec": (users > 0 or acc in ("confidential", "pii"),
                    f"{users} users with access; data access = {ACCESS_LABEL.get(acc, acc)}"),
        "privacy": (acc == "pii", "data access = PII / client data"),
        "bcp": (crit in ("high", "critical"), f"criticality = {crit}"),
        "physical": (row.get("offshore_delivery") is True or row.get("offshore_delivery") == 1, "offshore delivery = yes"),
        "subprocessor": (row.get("uses_subprocessors") is True or row.get("uses_subprocessors") == 1,
                         "uses sub-processors = yes"),
        "findings": (open_total > 0, f"{open_total} open finding(s)"),
        "certification": (row.get("certification_status") in ("expired", "none"),
                          f"certification {row.get('certification_status')}"),
    }
    out = []
    for m in rules["scope_modules"]:
        hit, why = checks[m["id"]]
        if hit:
            out.append({"id": m["id"], "name": m["name"], "reason": why})
    return out


def evaluate(row: dict, rules: dict | None = None, ref: date | None = None) -> dict:
    rules = rules or load_rules()
    ref = ref or today()
    e = rules["eligibility"]
    acc = row.get("data_access_level") or "none"
    users = 0 if _isnull(row.get("users_with_system_access")) else int(row["users_with_system_access"])
    crit = row.get("criticality_tier") or "medium"
    score = float(row.get("risk_score") or 0)
    tier = row.get("risk_tier") or "Low"

    end = row.get("contract_end")
    end_d = date.fromisoformat(end) if isinstance(end, str) and end else None
    reasons = []
    if end_d and end_d < ref:
        return {"eligible": False, "eligibility_status": "Not eligible",
                "eligibility_reasons": json.dumps([f"Contract ended on {end_d.isoformat()}"]),
                "audit_type": None, "interval_months": None, "next_due": None, "overdue": False,
                "scope_modules": json.dumps([])}

    if acc in e["data_access_levels"]:
        reasons.append(f"{ACCESS_LABEL[acc]} data access")
    if users >= e["min_users_with_access"]:
        reasons.append(f"{users} users with system access")
    if crit in e["criticality_tiers"]:
        reasons.append(f"criticality is {crit}")
    if score >= e["min_risk_score"]:
        reasons.append(f"risk score {score:.1f} is at or above {e['min_risk_score']}")

    if not reasons:
        return {"eligible": False, "eligibility_status": "Monitor only",
                "eligibility_reasons": json.dumps(["No eligibility trigger: no sensitive data, no system access, "
                                                   f"criticality {crit}, risk score {score:.1f} below "
                                                   f"{e['min_risk_score']}. Rescored when source data changes."]),
                "audit_type": None, "interval_months": None, "next_due": None, "overdue": False,
                "scope_modules": json.dumps([])}

    fq = rules["frequency"][tier]
    last = row.get("last_audit_date")
    if isinstance(last, str) and last:
        next_due = add_months(date.fromisoformat(last), fq["interval_months"])
        due_reason = f"last audit {last} + {fq['interval_months']} months"
    else:
        next_due = ref + timedelta(days=fq["first_within_days"])
        due_reason = f"never audited: first audit within {fq['first_within_days']} days"
    scope = scope_for(row, rules)
    return {"eligible": True, "eligibility_status": "Eligible",
            "eligibility_reasons": json.dumps(reasons + [f"Next due: {due_reason}"]),
            "audit_type": fq["audit_type"], "interval_months": fq["interval_months"],
            "next_due": next_due.isoformat(), "overdue": next_due < ref,
            "scope_modules": json.dumps(scope)}


def evaluate_all(golden: pd.DataFrame, rules: dict | None = None) -> pd.DataFrame:
    rules = rules or load_rules()
    ref = today()
    res = pd.DataFrame([evaluate(r, rules, ref) for r in golden.to_dict("records")])
    res["rules_version"] = rules["version"]
    return pd.concat([golden.reset_index(drop=True), res], axis=1)
