"""Paths, configuration loading and shared constants."""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
EXPORT_DIR = DATA_DIR / "exports"
DB_PATH = Path(os.getenv("AUDITPILOT_DB", DATA_DIR / "auditpilot.db"))
CONFIG_DIR = ROOT / "config"

ACCESS_ORDER = ["none", "internal", "confidential", "pii"]
ACCESS_LABEL = {
    "none": "None",
    "internal": "Internal",
    "confidential": "Confidential",
    "pii": "PII / client data",
}
CRIT_ORDER = ["low", "medium", "high", "critical"]
TIER_ORDER = ["Low", "Medium", "High", "Critical"]
TIER_COLOR = {"Low": "#4c9a6a", "Medium": "#d4a72c", "High": "#e07b39", "Critical": "#c0392b"}

# Fake people used as owners. All synthetic.
PEOPLE = {
    "Priya Raman": {"role": "audit_owner", "email": "priya.raman@example.com"},
    "Arjun Mehta": {"role": "audit_owner", "email": "arjun.mehta@example.com"},
    "Kavya Iyer": {"role": "audit_owner", "email": "kavya.iyer@example.com"},
    "Rahul Nair": {"role": "supplier_owner", "email": "rahul.nair@example.com"},
    "Sneha Kapoor": {"role": "supplier_owner", "email": "sneha.kapoor@example.com"},
    "Vikram Rao": {"role": "supplier_owner", "email": "vikram.rao@example.com"},
    "Ananya Das": {"role": "supplier_owner", "email": "ananya.das@example.com"},
    "Rohan Gupta": {"role": "supplier_owner", "email": "rohan.gupta@example.com"},
    "Deepa Krishnan": {"role": "ism", "email": "deepa.krishnan@example.com"},
    "Sanjay Pillai": {"role": "ism", "email": "sanjay.pillai@example.com"},
    "Meera Joshi": {"role": "ism", "email": "meera.joshi@example.com"},
}
ROLE_LABEL = {"audit_owner": "Audit team", "supplier_owner": "Supplier owner", "ism": "ISM"}


def people_with_role(role: str) -> list[str]:
    return [n for n, p in PEOPLE.items() if p["role"] == role]


def load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


def weights() -> dict:
    return load_yaml("weights.yaml")


def rules() -> dict:
    return load_yaml("rules.yaml")


def today() -> date:
    """Business date. Set AUDITPILOT_TODAY=YYYY-MM-DD to freeze it for a demo."""
    v = os.getenv("AUDITPILOT_TODAY")
    return date.fromisoformat(v) if v else date.today()
