"""Capacity-aware audit scheduler, audit plan + task generation, and the daily check."""
from __future__ import annotations

import json
from datetime import date, timedelta

import pandas as pd

from . import audit_log, db
from .ai import draft_checklist
from .config import TIER_ORDER, rules as load_rules, today
from .eligibility import add_months
from .notifier import make_ics, notify


def _month_key(d: date) -> str:
    return f"{d.year}-{d.month:02d}"


def schedule(golden: pd.DataFrame, rules: dict | None = None) -> pd.DataFrame:
    """Place each eligible audit in its due month if capacity allows, else the latest earlier month
    with room (never before this month), else flag a capacity breach."""
    rules = rules or load_rules()
    p = rules["planner"]
    cap = p["auditors"] * p["audits_per_auditor_per_month"]
    ref = today()
    horizon_end = add_months(ref.replace(day=1), p["horizon_months"])
    months = [add_months(ref.replace(day=1), i) for i in range(p["horizon_months"])]
    load = {_month_key(m): 0 for m in months}

    elig = golden[golden.eligible.astype(bool)].copy()
    elig["tier_rank"] = elig.risk_tier.map(lambda t: -TIER_ORDER.index(t))
    elig = elig.sort_values(["tier_rank", "next_due", "risk_score"], ascending=[True, True, False])
    rows = []
    for r in elig.itertuples():
        due = date.fromisoformat(r.next_due)
        if due >= horizon_end:
            rows.append(dict(golden_id=r.golden_id, legal_name=r.legal_name, risk_tier=r.risk_tier,
                             risk_score=r.risk_score, next_due=r.next_due, scheduled_month=None,
                             audit_date=None, status="Beyond horizon", overdue=bool(r.overdue)))
            continue
        due_month = max(due.replace(day=1), months[0])
        placed = None
        for m in sorted([m for m in months if m <= due_month], reverse=True):
            if load[_month_key(m)] < cap:
                placed = m
                break
        status = "Scheduled"
        if placed is None:
            later = [m for m in months if m > due_month and load[_month_key(m)] < cap]
            placed = later[0] if later else None
            status = "Capacity breach"
        if placed is None:
            rows.append(dict(golden_id=r.golden_id, legal_name=r.legal_name, risk_tier=r.risk_tier,
                             risk_score=r.risk_score, next_due=r.next_due, scheduled_month=None,
                             audit_date=None, status="Capacity breach", overdue=bool(r.overdue)))
            continue
        load[_month_key(placed)] += 1
        audit_day = max(placed.replace(day=15), ref + timedelta(days=p["min_lead_days"]))
        if status == "Scheduled" and audit_day > due and not r.overdue:
            audit_day = max(due, ref + timedelta(days=p["min_lead_days"]))
        rows.append(dict(golden_id=r.golden_id, legal_name=r.legal_name, risk_tier=r.risk_tier,
                         risk_score=r.risk_score, next_due=r.next_due, scheduled_month=_month_key(placed),
                         audit_date=audit_day.isoformat(), status=status, overdue=bool(r.overdue)))
    sched = pd.DataFrame(rows)
    capacity = pd.DataFrame([{"month": k, "planned": v, "capacity": cap} for k, v in load.items()])
    db.write_table(capacity, "capacity")
    return sched


def active_plan(golden_id: str) -> pd.DataFrame:
    return db.query("SELECT * FROM audit_plans WHERE golden_id=? AND status IN ('Planned','In progress')",
                    (golden_id,))


def generate_plan(golden_id: str, actor: str, use_ai_checklist: bool = True) -> dict:
    """Create an audit plan with dated, owned tasks and notify the owners. Idempotent per supplier."""
    existing = active_plan(golden_id)
    if len(existing):
        return {"created": False, "plan_id": existing.iloc[0]["plan_id"], "message": "An active plan already exists."}
    g = db.query("SELECT * FROM golden WHERE golden_id=?", (golden_id,))
    if not len(g):
        return {"created": False, "plan_id": None, "message": "Supplier not found."}
    row = g.iloc[0].to_dict()
    if not row["eligible"]:
        return {"created": False, "plan_id": None, "message": f"Supplier is {row['eligibility_status']}."}
    sched = db.query("SELECT * FROM schedule WHERE golden_id=?", (golden_id,))
    rules = load_rules()
    ref = today()
    if len(sched) and sched.iloc[0]["audit_date"]:
        audit_day = date.fromisoformat(sched.iloc[0]["audit_date"])
    else:
        audit_day = max(date.fromisoformat(row["next_due"]), ref + timedelta(days=rules["planner"]["min_lead_days"]))
    modules = json.loads(row["scope_modules"])
    module_ids = {m["id"] for m in modules}
    profile = {k: row[k] for k in ("data_access_level", "criticality_tier", "risk_tier", "certification_status",
                                   "offshore_delivery", "uses_subprocessors", "service_type")}
    checklist, src = draft_checklist(golden_id, modules, profile) if use_ai_checklist else ({}, "none")

    plan_id = f"PLN-{golden_id.split('-')[1]}-{audit_day:%Y%m%d}"
    db.execute("INSERT INTO audit_plans(plan_id, golden_id, audit_date, audit_type, scope, checklist, status,"
               " created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
               (plan_id, golden_id, audit_day.isoformat(), row["audit_type"], json.dumps(modules),
                json.dumps({"items": checklist, "source": src}), "Planned", actor, db.now()))
    task_rows = []
    for t in rules["tasks"]:
        if t.get("requires_module") and t["requires_module"] not in module_ids:
            continue
        due = audit_day + timedelta(days=t["offset"])
        owner = row[t["owner_role"]]
        task_rows.append((f"{plan_id}-T{t['seq']}", plan_id, golden_id, t["seq"], t["title"], t["owner_role"],
                          owner, due.isoformat(), "Not started", db.now()))
    db.executemany("INSERT INTO tasks(task_id, plan_id, golden_id, seq, title, owner_role, owner, due_date, status,"
                   " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", task_rows)
    audit_log.log(actor, "audit_plan_created", "audit_plan", plan_id, None,
                  {"golden_id": golden_id, "audit_date": audit_day.isoformat(), "tasks": len(task_rows),
                   "scope": [m["name"] for m in modules]}, rules["version"])
    ics = make_ics(f"Audit: {row['legal_name']}", audit_day, row["audit_type"] or "")
    notify("audit_plan_created", [row["audit_owner"], row["supplier_owner"], row["ism"]], golden_id,
           {"supplier": row["legal_name"], "audit_date": audit_day.isoformat(), "audit_type": row["audit_type"],
            "first_due": task_rows[0][7], "scope": ", ".join(m["name"] for m in modules)},
           dedupe_key=f"plan:{plan_id}", ics=ics)
    return {"created": True, "plan_id": plan_id, "tasks": len(task_rows), "checklist_source": src,
            "message": f"Plan {plan_id} created with {len(task_rows)} tasks."}


def update_task(task_id: str, status: str, actor: str) -> None:
    old = db.query("SELECT status, plan_id FROM tasks WHERE task_id=?", (task_id,))
    if not len(old) or old.iloc[0]["status"] == status:
        return
    db.execute("UPDATE tasks SET status=?, updated_at=? WHERE task_id=?", (status, db.now(), task_id))
    audit_log.log(actor, "task_status_changed", "task", task_id, old.iloc[0]["status"], status)
    plan_id = old.iloc[0]["plan_id"]
    st = db.query("SELECT seq, status FROM tasks WHERE plan_id=?", (plan_id,))
    conduct = st[st.seq == 5]
    if len(st) and (st.status == "Done").all():
        db.execute("UPDATE audit_plans SET status='Completed' WHERE plan_id=?", (plan_id,))
    elif len(conduct) and conduct.iloc[0]["status"] == "Done":
        db.execute("UPDATE audit_plans SET status='In progress' WHERE plan_id=?", (plan_id,))


def daily_check(actor: str = "scheduler") -> dict:
    """Mark overdue tasks and send time-based reminders. Safe to run repeatedly (deduplicated)."""
    ref = today()
    tasks = db.query("SELECT t.*, g.legal_name, g.audit_owner FROM tasks t JOIN golden g USING(golden_id) "
                     "WHERE t.status NOT IN ('Done')")
    overdue = 0
    for t in tasks.itertuples():
        if date.fromisoformat(t.due_date) < ref and t.status != "Overdue":
            db.execute("UPDATE tasks SET status='Overdue', updated_at=? WHERE task_id=?", (db.now(), t.task_id))
            audit_log.log(actor, "task_marked_overdue", "task", t.task_id, t.status, "Overdue")
            overdue += 1
        if date.fromisoformat(t.due_date) < ref:
            notify("task_overdue", [t.owner, t.audit_owner], t.golden_id,
                   {"task": t.title, "supplier": t.legal_name, "due_date": t.due_date},
                   dedupe_key=f"overdue:{t.task_id}")
    reminders = 0
    plans = db.query("SELECT p.*, g.legal_name, g.audit_owner, g.supplier_owner, g.ism FROM audit_plans p "
                     "JOIN golden g USING(golden_id) WHERE p.status='Planned'")
    for p in plans.itertuples():
        days = (date.fromisoformat(p.audit_date) - ref).days
        for window in (30, 7):
            if 0 <= days <= window:
                reminders += notify("audit_due", [p.audit_owner, p.supplier_owner, p.ism], p.golden_id,
                                    {"days": window, "supplier": p.legal_name, "audit_date": p.audit_date},
                                    dedupe_key=f"due{window}:{p.plan_id}") > 0
                break
    audit_log.log(actor, "daily_check", "system", "", None, {"overdue_marked": overdue, "reminders": int(reminders)})
    return {"overdue_marked": overdue, "reminders": int(reminders)}
