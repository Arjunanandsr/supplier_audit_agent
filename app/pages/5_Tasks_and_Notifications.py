import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # app/ on the path for "import ui"
import streamlit as st

import ui
from core import db, notifier
from core.planner import daily_check, update_task

ui.setup("Tasks & notifications")
STATUSES = ["Not started", "In progress", "Done", "Overdue"]

tasks = db.query("SELECT t.*, g.legal_name FROM tasks t JOIN golden g USING(golden_id) ORDER BY due_date")
mine_only = st.toggle(f"Only tasks owned by {ui.user()}", value=ui.role() != "Audit team")
if mine_only:
    tasks = tasks[tasks.owner == ui.user()]
elif ui.role() != "Audit team":
    tasks = tasks[tasks.golden_id.isin(set(ui.visible(ui.golden()).golden_id))]

top = st.columns([1, 1, 2])
if top[0].button("▶ Run daily check", help="Mark overdue tasks and send 30/7-day reminders (deduplicated)"):
    out = daily_check(actor=ui.user())
    st.session_state["flash_t"] = f"Daily check: {out['overdue_marked']} tasks marked overdue, {out['reminders']} reminders sent."
    st.rerun()
sink_name = top[1].selectbox("Task sink", list(notifier.SINKS), label_visibility="collapsed")
if top[2].button(f"Push {len(tasks)} tasks to {sink_name}"):
    st.session_state["flash_t"] = notifier.SINKS[sink_name].push(tasks)
    st.rerun()
if st.session_state.get("flash_t"):
    st.success(st.session_state.pop("flash_t"))

tab_board, tab_list, tab_notif = st.tabs(["Board", "All tasks", "Notifications"])
with tab_board:
    if not len(tasks):
        st.info("No tasks yet. Generate an audit plan on Supplier 360 or the Audit planner.")
    cols = st.columns(4)
    for col, status in zip(cols, STATUSES):
        sub = tasks[tasks.status == status]
        col.markdown(f"#### {status} ({len(sub)})")
        for t in sub.head(25).itertuples():
            with col.container(border=True):
                st.markdown(f"**{t.title}**")
                st.caption(f"{t.legal_name} · {t.owner} · due {t.due_date}")
                new = st.selectbox("Status", STATUSES, index=STATUSES.index(t.status), key=f"s_{t.task_id}",
                                   label_visibility="collapsed")
                if new != t.status:
                    update_task(t.task_id, new, ui.user())
                    st.rerun()
        if len(sub) > 25:
            col.caption(f"+{len(sub) - 25} more in All tasks")
with tab_list:
    st.dataframe(tasks[["task_id", "legal_name", "seq", "title", "owner_role", "owner", "due_date", "status"]],
                 hide_index=True, width="stretch")

with tab_notif:
    st.caption("Channels active: " + ", ".join(notifier.channels())
               + ". Set SMTP_HOST and TEAMS_WEBHOOK_URL in .env to enable email and Teams.")
    scope = st.radio("Show", [f"For {ui.user()}", "All"], horizontal=True)
    n = db.query("SELECT ts, event, recipient, channel, subject, body, status FROM notifications ORDER BY notif_id DESC")
    if scope != "All":
        n = n[n.recipient == ui.user()]
    st.dataframe(n, hide_index=True, width="stretch")
