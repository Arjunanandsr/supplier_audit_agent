"""End-to-end run: generate -> consolidate -> score -> decide -> schedule -> notify.

Each step writes to SQLite and the audit log.
"""
from __future__ import annotations

import time

import pandas as pd

from . import audit_log, db
from .ai import extract_flags
from .config import RAW_DIR, TIER_ORDER, rules as load_rules, weights as load_weights
from .consolidate import consolidate
from .eligibility import evaluate_all
from .generate_data import generate
from .notifier import notify
from .planner import schedule
from .risk import score_all

RAW_FILES = ["ariba_suppliers.csv", "vms_vendors.csv", "gbams_associates.csv", "pcm_contracts.csv",
             "audit_history.csv"]


def raw_present() -> bool:
    return all((RAW_DIR / f).exists() for f in RAW_FILES)


def run_all(actor: str = "system", regenerate: bool = False, notify_changes: bool = True) -> dict:
    t0 = time.time()
    if regenerate or not raw_present():
        generate()
        audit_log.log(actor, "synthetic_data_generated", "dataset", "raw", None, {"files": RAW_FILES})

    res = consolidate()
    golden = res["golden"]
    overlays = _apply_report_overlays(golden, res["lineage"])

    flags, flag_src = {}, {"ai": 0, "keyword rules": 0}
    for r in golden.itertuples():
        f, s = extract_flags(r.golden_id, r.assessment_comments)
        flags[r.golden_id] = sorted(set(f) | set(overlays.get(r.golden_id, {}).get("red_flags", [])))
        flag_src[s] += 1

    w, rules = load_weights(), load_rules()
    golden = score_all(golden, flags, w)
    golden = evaluate_all(golden, rules)

    prev = db.read_table("golden")
    changes = {"tier_up": 0, "new_eligible": 0}
    if len(prev) and notify_changes:
        p = prev.set_index("golden_id")
        for r in golden.itertuples():
            if r.golden_id not in p.index:
                continue
            old = p.loc[r.golden_id]
            if TIER_ORDER.index(r.risk_tier) > TIER_ORDER.index(old["risk_tier"]):
                changes["tier_up"] += 1
                notify("risk_tier_up", [r.audit_owner, r.ism], r.golden_id,
                       {"supplier": r.legal_name, "old_tier": old["risk_tier"], "new_tier": r.risk_tier,
                        "score": f"{r.risk_score:.1f}"},
                       dedupe_key=f"tierup:{r.golden_id}:{old['risk_tier']}>{r.risk_tier}:{db.now()[:13]}")
                audit_log.log(actor, "risk_tier_changed", "supplier", r.golden_id, old["risk_tier"], r.risk_tier,
                              w["version"])
            if bool(r.eligible) and not bool(old["eligible"]):
                changes["new_eligible"] += 1
                notify("new_eligible", [r.audit_owner, r.supplier_owner], r.golden_id,
                       {"supplier": r.legal_name, "tier": r.risk_tier, "next_due": r.next_due},
                       dedupe_key=f"elig:{r.golden_id}:{r.next_due}")

    db.write_table(golden, "golden")
    db.write_table(res["lineage"], "lineage")
    db.write_table(res["conflicts"], "conflicts")
    db.write_table(res["matches"], "matches")
    sched = schedule(golden, rules)
    db.write_table(sched, "schedule")

    conf = res["conflicts"]
    if len(conf) and notify_changes:
        pend = conf[conf.status == "pending"].merge(golden[["golden_id", "audit_owner"]], on="golden_id")
        for owner, n in pend.groupby("audit_owner").size().items():
            notify("conflicts_waiting", [owner], None, {"count": int(n)},
                   dedupe_key=f"conflicts:{owner}:{int(n)}:{db.now()[:10]}")

    stats = res["stats"] | {
        "eligible": int(golden.eligible.sum()), "overdue": int(golden.overdue.sum()),
        "tiers": golden.risk_tier.value_counts().to_dict(), "flag_sources": flag_src, "changes": changes,
        "scheduled": int((sched.status == "Scheduled").sum()) if len(sched) else 0,
        "capacity_breach": int((sched.status == "Capacity breach").sum()) if len(sched) else 0,
        "weights_version": w["version"], "rules_version": rules["version"], "run_at": db.now(),
        "seconds": round(time.time() - t0, 1),
    }
    db.set_meta("last_run", stats)
    audit_log.log(actor, "pipeline_run", "system", "", None,
                  {k: stats[k] for k in ("golden_records", "conflicts_pending", "eligible", "scheduled", "changes")},
                  f"weights {w['version']} / rules {rules['version']}")
    return stats


def _apply_report_overlays(golden: pd.DataFrame, lineage: pd.DataFrame) -> dict:
    """Accepted AI analysis reports are newer audit evidence: their open findings replace history counts."""
    from .analyst import report_overlays
    ov = report_overlays()
    for gid, o in ov.items():
        idx = golden.index[golden.golden_id == gid]
        if not len(idx):
            continue
        i = idx[0]
        for k in ("open_critical", "open_high", "open_medium"):
            golden.at[i, k] = o[k]
        if o["rating"]:
            golden.at[i, "last_audit_rating"] = o["rating"]
        for k, ch in o["facts"].items():
            golden.at[i, k] = ch["to"]
        lineage.loc[len(lineage)] = dict(golden_id=gid, field="open_findings",
                                         value=f"{o['open_critical']}/{o['open_high']}/{o['open_medium']}",
                                         source="AI analysis (accepted)", rule=f"Report {o['report_id']}")
    return ov


def resolve_conflict(golden_id: str, field: str, value: str, decision: str, reviewer: str) -> dict:
    c = db.query("SELECT * FROM conflicts WHERE golden_id=? AND field=?", (golden_id, field))
    if not len(c):
        return {"ok": False}
    row = c.iloc[0]
    before = db.query("SELECT risk_tier, risk_score FROM golden WHERE golden_id=?", (golden_id,)).iloc[0].to_dict()
    db.execute("INSERT OR REPLACE INTO conflict_decisions(golden_id, field, values_sig, resolved_value, decision,"
               " reviewer, ts) VALUES (?,?,?,?,?,?,?)",
               (golden_id, field, row["values_sig"], value, decision, reviewer, db.now()))
    audit_log.log(reviewer, "conflict_resolved", "supplier", golden_id,
                  {"field": field, "ariba": row["ariba_value"], row["other_source"]: row["other_value"]},
                  {"field": field, "value": value, "decision": decision})
    run_all(actor=reviewer)
    after = db.query("SELECT risk_tier, risk_score FROM golden WHERE golden_id=?", (golden_id,)).iloc[0].to_dict()
    return {"ok": True, "before": before, "after": after}


def decide_match(source: str, source_id: str, decision: str, reviewer: str) -> None:
    db.execute("INSERT OR REPLACE INTO match_decisions(source, source_id, decision, reviewer, ts) VALUES (?,?,?,?,?)",
               (source, source_id, decision, reviewer, db.now()))
    audit_log.log(reviewer, f"match_{decision}", "source_record", f"{source}:{source_id}")
    run_all(actor=reviewer)


def reset_demo(actor: str = "system") -> dict:
    """Wipe decisions, plans, tasks and notifications, regenerate data and rerun."""
    for t in ["conflict_decisions", "match_decisions", "audit_plans", "tasks", "notifications", "analysis_reports",
              "golden"]:
        if db.table_exists(t):
            db.execute(f'DELETE FROM "{t}"')
    audit_log.log(actor, "demo_reset", "system")
    return run_all(actor=actor, regenerate=True, notify_changes=False)


if __name__ == "__main__":
    s = run_all(regenerate=True)
    print(pd.Series(s).to_string())
