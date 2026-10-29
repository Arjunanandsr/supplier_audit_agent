"""Notifications behind one interface: in-app (always), email (SMTP) and Microsoft Teams (webhook).

Plus a TaskSink that exports tasks to CSV today, with a Jira / Azure DevOps stub for later.
"""
from __future__ import annotations

import os
import smtplib
import uuid
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage

import pandas as pd
import requests

from . import db
from .ai import notification_text
from .config import EXPORT_DIR, PEOPLE


def channels() -> list[str]:
    ch = ["in_app"]
    if os.getenv("SMTP_HOST"):
        ch.append("email")
    if os.getenv("TEAMS_WEBHOOK_URL"):
        ch.append("teams")
    return ch


def make_ics(summary: str, day: date, description: str = "") -> str:
    start = day.strftime("%Y%m%d")
    end = (day + timedelta(days=1)).strftime("%Y%m%d")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//AuditPilot//EN", "BEGIN:VEVENT",
        f"UID:{uuid.uuid4()}@auditpilot", f"DTSTAMP:{stamp}", f"DTSTART;VALUE=DATE:{start}",
        f"DTEND;VALUE=DATE:{end}", f"SUMMARY:{summary}", f"DESCRIPTION:{description}",
        "END:VEVENT", "END:VCALENDAR", ""])


def _send_email(to: str, subject: str, body: str, ics: str | None) -> str:
    try:
        msg = EmailMessage()
        msg["From"] = os.getenv("SMTP_FROM", "auditpilot@example.com")
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        if ics:
            msg.add_attachment(ics.encode(), maintype="text", subtype="calendar", filename="audit.ics")
        port = int(os.getenv("SMTP_PORT", "587"))
        with smtplib.SMTP(os.getenv("SMTP_HOST"), port, timeout=10) as s:
            if os.getenv("SMTP_STARTTLS", "true").lower() == "true":
                s.starttls()
            if os.getenv("SMTP_USER"):
                s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD", ""))
            s.send_message(msg)
        return "sent"
    except Exception as exc:
        return f"failed: {str(exc)[:120]}"


def _send_teams(subject: str, body: str) -> str:
    try:
        payload = {"type": "message", "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {"type": "AdaptiveCard", "version": "1.4", "body": [
                {"type": "TextBlock", "text": subject, "weight": "Bolder", "wrap": True},
                {"type": "TextBlock", "text": body, "wrap": True}]}}]}
        r = requests.post(os.getenv("TEAMS_WEBHOOK_URL"), json=payload, timeout=10)
        return "sent" if r.status_code < 300 else f"failed: HTTP {r.status_code}"
    except Exception as exc:
        return f"failed: {str(exc)[:120]}"


def notify(event: str, recipients: list[str], golden_id: str | None, ctx: dict,
           dedupe_key: str | None = None, ics: str | None = None) -> int:
    """Send one event to each recipient on every configured channel. Returns notifications written."""
    if dedupe_key:
        seen = db.query("SELECT 1 FROM notifications WHERE dedupe_key=? LIMIT 1", (dedupe_key,))
        if len(seen):
            return 0
    subject, body, _src = notification_text(event, ctx)
    rows, chans = [], channels()
    for person in dict.fromkeys(recipients):  # keep order, drop duplicates
        for ch in chans:
            if ch == "in_app":
                st = "delivered"
            elif ch == "email":
                st = _send_email(PEOPLE.get(person, {}).get("email", person), subject, body, ics)
            else:
                continue
            rows.append((db.now(), event, golden_id, person, ch, subject, body, st, dedupe_key))
    if "teams" in chans:
        rows.append((db.now(), event, golden_id, "Teams channel", "teams", subject, body,
                     _send_teams(subject, body), dedupe_key))
    db.executemany("INSERT INTO notifications(ts, event, golden_id, recipient, channel, subject, body, status,"
                   " dedupe_key) VALUES (?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


# ---------------- Task sinks ----------------
class TaskSink:
    name = "base"

    def push(self, tasks: pd.DataFrame) -> str:
        raise NotImplementedError


class CsvTaskSink(TaskSink):
    name = "CSV export"

    def push(self, tasks: pd.DataFrame) -> str:
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = EXPORT_DIR / f"audit_tasks_{datetime.now():%Y%m%d_%H%M%S}.csv"
        tasks.to_csv(path, index=False)
        return str(path)


class JiraTaskSink(TaskSink):
    """Stub: map each task to a Jira issue via POST /rest/api/3/issue. Not called in the prototype."""
    name = "Jira (stub)"

    def push(self, tasks: pd.DataFrame) -> str:
        return f"Stub only: would create {len(tasks)} Jira issues (set JIRA_URL and JIRA_TOKEN to implement)."


class AzureDevOpsTaskSink(TaskSink):
    """Stub: map each task to an ADO work item via PATCH .../_apis/wit/workitems/$Task."""
    name = "Azure DevOps (stub)"

    def push(self, tasks: pd.DataFrame) -> str:
        return f"Stub only: would create {len(tasks)} Azure DevOps work items."


SINKS = {s.name: s for s in (CsvTaskSink(), JiraTaskSink(), AzureDevOpsTaskSink())}
