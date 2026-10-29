"""Shared page setup, sidebar, role filter and small display helpers."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from core import ai, db  # noqa: E402
from core.config import ACCESS_LABEL, ROLE_LABEL, TIER_COLOR, people_with_role, today  # noqa: E402
from core.pipeline import reset_demo, run_all  # noqa: E402

ROLES = ["Audit team", "Supplier owner", "ISM"]
ROLE_KEY = {"Audit team": "audit_owner", "Supplier owner": "supplier_owner", "ISM": "ism"}


def setup(title: str, icon: str = "🛡️") -> None:
    st.set_page_config(page_title=f"{title} · AuditPilot", page_icon=icon, layout="wide")
    if not db.table_exists("golden") or not len(db.query("SELECT 1 FROM golden LIMIT 1")):
        with st.spinner("First run: generating synthetic data and building golden records…"):
            run_all(actor="system", notify_changes=False)
    _sidebar()
    st.title(title)


def _persist(name: str, default):
    if name not in st.session_state:
        st.session_state[name] = default
    return st.session_state[name]


def _sidebar() -> None:
    sb = st.sidebar
    sb.markdown("### 🛡️ AuditPilot")
    sb.caption("Supplier audit onboarding & risk assistant · **SYNTHETIC DATA**")
    role = _persist("role", "Audit team")
    new_role = sb.selectbox("Viewing as", ROLES, index=ROLES.index(role), key="role_w")
    st.session_state["role"] = new_role
    pool = people_with_role(ROLE_KEY[new_role])
    person = _persist(f"person_{new_role}", pool[0])
    new_person = sb.selectbox("User", pool, index=pool.index(person) if person in pool else 0,
                              key=f"person_w_{new_role}")
    st.session_state[f"person_{new_role}"] = new_person
    st.session_state["user"] = new_person

    unread = db.query("SELECT COUNT(*) n FROM notifications WHERE recipient=? AND channel='in_app'",
                      (new_person,)).iloc[0, 0]
    sb.markdown(f"🔔 **{unread}** notifications for {new_person.split()[0]}")
    sb.divider()
    sb.caption(("🟢 " if ai.enabled() else "⚪ ") + ai.status())
    last = db.get_meta("last_run") or {}
    if last:
        sb.caption(f"Last run {last.get('run_at', '')[:16].replace('T', ' ')} · weights v{last.get('weights_version')}"
                   f" · rules v{last.get('rules_version')} · business date {today().isoformat()}")
    c1, c2 = sb.columns(2)
    if c1.button("↻ Rerun", help="Re-ingest sources and recompute everything", width="stretch"):
        with st.spinner("Running pipeline…"):
            run_all(actor=new_person)
        st.rerun()
    if c2.button("⟲ Reset demo", help="Clear decisions, plans, tasks and notifications", width="stretch"):
        with st.spinner("Resetting…"):
            reset_demo(actor=new_person)
        st.session_state.pop("selected", None)
        st.rerun()


def role() -> str:
    return st.session_state.get("role", "Audit team")


def user() -> str:
    return st.session_state.get("user", people_with_role("audit_owner")[0])


def golden() -> pd.DataFrame:
    return db.read_table("golden")


def visible(df: pd.DataFrame) -> pd.DataFrame:
    """Role-based view: audit team sees all; supplier owners their suppliers; ISMs their security-scoped suppliers."""
    r, u = role(), user()
    if r == "Audit team":
        return df
    if r == "Supplier owner":
        return df[df.supplier_owner == u]
    ism = df[df.ism == u]
    return ism[ism.scope_modules.fillna("[]").str.contains('"infosec"')]


def role_note(df_all: pd.DataFrame, df_vis: pd.DataFrame) -> None:
    if role() != "Audit team":
        st.info(f"Showing {len(df_vis)} of {len(df_all)} suppliers visible to {user()} ({role()}).")


def tier_badge(tier: str) -> str:
    c = TIER_COLOR.get(tier, "#888")
    return (f"<span style='background:{c};color:white;padding:2px 10px;border-radius:12px;"
            f"font-weight:600;font-size:0.85em'>{tier}</span>")


def access_label(v) -> str:
    return ACCESS_LABEL.get(v, "Unknown") if isinstance(v, str) else "Unknown"


def inr(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    v = float(v)
    if v >= 1e7:
        return f"₹{v / 1e7:.2f} Cr"
    if v >= 1e5:
        return f"₹{v / 1e5:.1f} L"
    return f"₹{v:,.0f}"


def jl(v, default=None):
    if v is None or (isinstance(v, float) and pd.isna(v)) or v == "":
        return default if default is not None else []
    return json.loads(v)


def supplier_picker(df: pd.DataFrame, label: str = "Supplier", key: str = "picker") -> str | None:
    if not len(df):
        st.warning("No suppliers visible for this user.")
        return None
    df = df.sort_values("risk_score", ascending=False)
    opts = df.golden_id.tolist()
    names = dict(zip(df.golden_id, df.legal_name))
    tiers = dict(zip(df.golden_id, df.risk_tier))
    cur = st.session_state.get("selected")
    idx = opts.index(cur) if cur in opts else 0
    gid = st.selectbox(label, opts, index=idx, key=key,
                       format_func=lambda g: f"{names[g]} · {g} · {tiers[g]}")
    st.session_state["selected"] = gid
    return gid


ROLE_LABELS = ROLE_LABEL
