"""SQLite helpers. All app state lives here so every page reads the same data."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime

import pandas as pd

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS conflict_decisions(
  golden_id TEXT, field TEXT, values_sig TEXT, resolved_value TEXT,
  decision TEXT, reviewer TEXT, ts TEXT, PRIMARY KEY(golden_id, field));
CREATE TABLE IF NOT EXISTS match_decisions(
  source TEXT, source_id TEXT, decision TEXT, reviewer TEXT, ts TEXT,
  PRIMARY KEY(source, source_id));
CREATE TABLE IF NOT EXISTS audit_plans(
  plan_id TEXT PRIMARY KEY, golden_id TEXT, audit_date TEXT, audit_type TEXT,
  scope TEXT, checklist TEXT, status TEXT, created_by TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS tasks(
  task_id TEXT PRIMARY KEY, plan_id TEXT, golden_id TEXT, seq INTEGER, title TEXT,
  owner_role TEXT, owner TEXT, due_date TEXT, status TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS notifications(
  notif_id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, event TEXT, golden_id TEXT,
  recipient TEXT, channel TEXT, subject TEXT, body TEXT, status TEXT, dedupe_key TEXT);
CREATE TABLE IF NOT EXISTS audit_log(
  log_id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT, action TEXT,
  entity TEXT, entity_id TEXT, before TEXT, after TEXT, version TEXT);
CREATE TABLE IF NOT EXISTS ai_cache(key TEXT PRIMARY KEY, kind TEXT, output TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS ai_calls(
  call_id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, prompt_version TEXT,
  deployment TEXT, input_hash TEXT, output TEXT, status TEXT, latency_ms INTEGER);
CREATE TABLE IF NOT EXISTS run_meta(key TEXT PRIMARY KEY, value TEXT);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.executescript(SCHEMA)
    return c


def query(sql: str, params: tuple | list = ()) -> pd.DataFrame:
    with closing(connect()) as c:
        return pd.read_sql_query(sql, c, params=params)


def execute(sql: str, params: tuple | list = ()) -> None:
    with closing(connect()) as c:
        c.execute(sql, params)
        c.commit()


def executemany(sql: str, rows: list) -> None:
    if not rows:
        return
    with closing(connect()) as c:
        c.executemany(sql, rows)
        c.commit()


def table_exists(name: str) -> bool:
    with closing(connect()) as c:
        r = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        return r is not None


def read_table(name: str) -> pd.DataFrame:
    if not table_exists(name):
        return pd.DataFrame()
    return query(f'SELECT * FROM "{name}"')


def write_table(df: pd.DataFrame, name: str) -> None:
    with closing(connect()) as c:
        df.to_sql(name, c, if_exists="replace", index=False)
        c.commit()


def set_meta(key: str, value) -> None:
    execute("INSERT OR REPLACE INTO run_meta(key, value) VALUES (?, ?)", (key, json.dumps(value)))


def get_meta(key: str, default=None):
    df = query("SELECT value FROM run_meta WHERE key=?", (key,))
    return json.loads(df.iloc[0, 0]) if len(df) else default
