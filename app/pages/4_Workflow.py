import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # app/ on the path for "import ui"
import pandas as pd
import streamlit as st

import ui
from core import db

ui.setup("Audit planning workflow")
st.caption("Live counts per stage. A stage with suppliers stuck waiting (e.g. a conflict to review) turns amber.")

g = ui.visible(ui.golden())
ids = set(g.golden_id)
plans = db.read_table("audit_plans")
tasks = db.read_table("tasks")
notifs = db.read_table("notifications")
matches = db.read_table("matches")
plans = plans[plans.golden_id.isin(ids)] if len(plans) else plans
tasks = tasks[tasks.golden_id.isin(ids)] if len(tasks) else tasks

notified = set(notifs[notifs.event == "audit_plan_created"].golden_id) if len(notifs) else set()
completed = set(plans[plans.status == "Completed"].golden_id) if len(plans) else set()
in_progress = set(plans[plans.status == "In progress"].golden_id) if len(plans) else set()

stages = [
    ("onboarded", "Onboarded\n(Ariba)", set(g.golden_id), set()),
    ("consolidated", "Consolidated", set(g.golden_id), set(g[g.pending_conflicts > 0].golden_id)),
    ("scored", "Scored", set(g[g.risk_score.notna()].golden_id), set(g[g.low_confidence.astype(bool)].golden_id)),
    ("eligible", "Eligible", set(g[g.eligible.astype(bool)].golden_id), set()),
    ("planned", "Planned", set(plans.golden_id) if len(plans) else set(), set()),
    ("tasks", "Tasks generated", set(tasks.golden_id) if len(tasks) else set(),
     set(tasks[tasks.status == "Overdue"].golden_id) if len(tasks) else set()),
    ("notified", "Notified", notified & ids, set()),
    ("completed", "Audit completed", completed | in_progress, set()),
]
eligible_unplanned = set(g[g.eligible.astype(bool) & g.overdue.astype(bool)].golden_id) - (
    set(plans.golden_id) if len(plans) else set())
stages[3] = (*stages[3][:3], eligible_unplanned)

dot = ["digraph G {", "rankdir=LR; bgcolor=transparent;",
       'node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=12, margin="0.2,0.12"];',
       'edge [color="#9b9a95"];']
for key, label, members, stuck in stages:
    color = "#fbe7c6" if stuck else "#e6efff"
    extra = f"\\n⚠ {len(stuck)} waiting" if stuck else ""
    dot.append(f'{key} [label="{label}\\n{len(members)}{extra}", fillcolor="{color}"];')
for a, b in zip(stages, stages[1:]):
    dot.append(f"{a[0]} -> {b[0]};")
unmatched = int((matches.status == "unmatched").sum()) if len(matches) else 0
dot.append(f'unmatched [label="Unmatched source records\\n{unmatched}", fillcolor="#f6dcd9"];')
dot.append("unmatched -> consolidated [style=dashed];")
dot.append("}")
st.graphviz_chart("\n".join(dot), width="stretch")

st.markdown("**Why amber?** Consolidated = conflicts waiting for review · Scored = low-confidence data · "
            "Eligible = overdue with no plan yet · Tasks = overdue tasks.")

labels = {k: lbl.replace("\n", " ") for k, lbl, *_ in stages}
pick = st.selectbox("List suppliers at stage", [k for k, *_ in stages], format_func=labels.get, index=3)
st_members, st_stuck = next((m, s) for k, _l, m, s in stages if k == pick)
only_stuck = st.toggle("Only those waiting", value=bool(st_stuck))
sel = st_stuck if only_stuck else st_members
view = g[g.golden_id.isin(sel)][["golden_id", "legal_name", "risk_tier", "risk_score", "eligibility_status",
                                 "next_due", "overdue", "pending_conflicts", "data_quality"]]
st.dataframe(view.sort_values("risk_score", ascending=False), hide_index=True, width="stretch")
