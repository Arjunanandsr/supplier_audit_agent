"""LLM layer for Azure OpenAI or the OpenAI API. Every function has a deterministic fallback, so the app works with no key.

Provider: AI_PROVIDER=azure | openai | none, or auto-detected from which keys are set (Azure first).

Guardrails:
- Only masked data is sent: golden IDs and coded values, never names or tax IDs.
- The LLM never sets a score, tier or eligibility decision.
- Responses are JSON, temperature 0, cached, and every call is logged.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

from . import db

PROMPT_VERSION = "2026-10-29.1"


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
PROVIDER_LABEL = {"azure": "Azure OpenAI", "openai": "OpenAI API", "none": "none"}


def _azure_ready() -> bool:
    return all(_env(k) for k in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT"))


def provider() -> str:
    """'azure', 'openai' or 'none'. AI_PROVIDER wins; otherwise Azure if configured, then OpenAI."""
    if _env("AI_ENABLED").lower() in ("0", "false", "no", "off"):
        return "none"
    choice = _env("AI_PROVIDER").lower()
    if choice in ("none", "off", "rules"):
        return "none"
    if choice == "azure":
        return "azure" if _azure_ready() else "none"
    if choice == "openai":
        return "openai" if _env("OPENAI_API_KEY") else "none"
    if _azure_ready():
        return "azure"
    if _env("OPENAI_API_KEY"):
        return "openai"
    return "none"


def enabled() -> bool:
    return provider() != "none"


def model() -> str:
    """Deployment name (Azure) or model name (OpenAI)."""
    p = provider()
    if p == "azure":
        return _env("AZURE_OPENAI_DEPLOYMENT")
    if p == "openai":
        return _env("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL
    return "fallback"


def provider_label() -> str:
    return PROVIDER_LABEL[provider()]


def status() -> str:
    if not enabled():
        return "AI fallback mode (no OpenAI or Azure key)"
    if time.time() < _paused_until:
        return f"{provider_label()} unreachable: using fallbacks for a few minutes"
    return f"{provider_label()}: {model()}"


_client = None
_fails = 0
_paused_until = 0.0
BREAKER_FAILS = 3          # after this many consecutive failures...
BREAKER_PAUSE_S = 300      # ...use fallbacks for 5 minutes instead of waiting on a dead endpoint


_client_for = None


def _get_client():
    """Build (and cache) the client for the active provider; rebuilt if the provider changes."""
    global _client, _client_for
    p = provider()
    if _client is None or _client_for != p:
        if p == "azure":
            from openai import AzureOpenAI
            _client = AzureOpenAI(azure_endpoint=_env("AZURE_OPENAI_ENDPOINT"), api_key=_env("AZURE_OPENAI_API_KEY"),
                                  api_version=_env("AZURE_OPENAI_API_VERSION") or "2024-10-21", timeout=30,
                                  max_retries=1)
        elif p == "openai":
            from openai import OpenAI
            _client = OpenAI(api_key=_env("OPENAI_API_KEY"), base_url=_env("OPENAI_BASE_URL") or None,
                             organization=_env("OPENAI_ORG_ID") or None, project=_env("OPENAI_PROJECT") or None,
                             timeout=30, max_retries=1)
        else:
            raise RuntimeError("No AI provider configured")
        _client_for = p
    return _client


def chat(**kwargs):
    """chat.completions.create with the active model. Retries once without temperature for models that
    only accept their default temperature."""
    kwargs.setdefault("model", model())
    try:
        return _get_client().chat.completions.create(**kwargs)
    except Exception as exc:
        if "temperature" in kwargs and "temperature" in str(exc).lower():
            kwargs.pop("temperature")
            return _get_client().chat.completions.create(**kwargs)
        raise


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:24]


def _cached(key: str):
    df = db.query("SELECT output FROM ai_cache WHERE key=?", (key,))
    return json.loads(df.iloc[0, 0]) if len(df) else None


def _store(key: str, kind: str, output) -> None:
    db.execute("INSERT OR REPLACE INTO ai_cache(key, kind, output, ts) VALUES (?,?,?,?)",
               (key, kind, json.dumps(output), db.now()))


def _log(kind: str, input_hash: str, output, status_: str, ms: int) -> None:
    db.execute("INSERT INTO ai_calls(ts, kind, prompt_version, deployment, input_hash, output, status, latency_ms)"
               " VALUES (?,?,?,?,?,?,?,?)",
               (db.now(), kind, PROMPT_VERSION, f"{provider()}:{model()}", input_hash,
                json.dumps(output)[:4000], status_, ms))


def _chat_json(kind: str, system: str, user_payload: dict, use_cache: bool = True):
    """Call the configured LLM and return parsed JSON, or None on any failure."""
    key = f"{kind}:{PROMPT_VERSION}:{_hash(user_payload)}"
    if use_cache:
        hit = _cached(key)
        if hit is not None:
            return hit
    global _fails, _paused_until
    if not enabled() or time.time() < _paused_until:
        return None
    t0 = time.time()
    try:
        resp = chat(
            temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": json.dumps(user_payload, default=str)}])
        out = json.loads(resp.choices[0].message.content)
        _log(kind, key, out, "ok", int((time.time() - t0) * 1000))
        _store(key, kind, out)
        _fails = 0
        return out
    except Exception as exc:  # network, quota, parse: always fall back
        _log(kind, key, {"error": str(exc)[:300]}, "error", int((time.time() - t0) * 1000))
        _fails += 1
        if _fails >= BREAKER_FAILS:
            _paused_until, _fails = time.time() + BREAKER_PAUSE_S, 0
        return None


# ---------------- 1. Assessment red-flag extraction ----------------
FLAG_RULES = {
    "no_mfa": r"\bmfa\b.*(planned|not|pending)|no mfa",
    "offshore_storage": r"offshore data cent",
    "no_incident_plan": r"no formal incident",
    "pentest_overdue": r"penetration test overdue",
    "shared_admin_accounts": r"shared admin",
    "backups_untested": r"backups not tested",
    "no_disk_encryption": r"not fully disk-encrypted",
}
FLAG_LABEL = {
    "no_mfa": "MFA not in place", "offshore_storage": "Offshore data storage without approval",
    "no_incident_plan": "No incident response plan", "pentest_overdue": "Penetration test overdue",
    "shared_admin_accounts": "Shared admin accounts", "backups_untested": "Backups not tested",
    "no_disk_encryption": "Laptops not disk-encrypted",
}


def keyword_flags(text: str) -> list[str]:
    t = (text or "").lower()
    return [k for k, pat in FLAG_RULES.items() if re.search(pat, t)]


def extract_flags(golden_id: str, text: str) -> tuple[list[str], str]:
    """Returns (flags, source). Comments contain no names, so they can be sent as-is."""
    system = ("You review supplier security questionnaire comments. Return JSON {\"flags\": [...]} using ONLY "
              f"these codes: {sorted(FLAG_RULES)}. Include a code only when the comment clearly states that "
              "weakness. Return an empty list if none apply.")
    out = _chat_json("flags", system, {"supplier": golden_id, "comments": text})
    if out and isinstance(out.get("flags"), list):
        return [f for f in out["flags"] if f in FLAG_RULES], "ai"
    return keyword_flags(text), "keyword rules"


# ---------------- 2. Conflict resolution assistant ----------------
def explain_conflict(conflict: dict) -> dict:
    payload = {k: conflict.get(k) for k in ("golden_id", "field", "ariba_value", "other_source",
                                            "other_value", "rule_value", "rule")}
    system = ("You help an audit reviewer resolve a data conflict between supplier systems. Using ONLY the JSON, "
              "return {\"recommended_value\": <one of the two values>, \"reason\": <one sentence>}. "
              "Prefer the more conservative value for data access unless the data says otherwise. "
              "Do not invent facts.")
    out = _chat_json("conflict", system, payload)
    if out and out.get("recommended_value") in (conflict.get("ariba_value"), conflict.get("other_value")):
        return {"recommended_value": out["recommended_value"], "reason": out.get("reason", ""), "source": "ai"}
    return {"recommended_value": conflict["rule_value"],
            "reason": f"Rule '{conflict['rule']}': {conflict['other_source']} records "
                      f"'{conflict['other_value']}' while Ariba self-declares '{conflict['ariba_value']}'. "
                      f"Until confirmed, use '{conflict['rule_value']}'.",
            "source": "rule"}


# ---------------- 3. Risk narrative ----------------
def risk_narrative(golden_id: str, score: float, tier: str, factors: dict, overrides: list, dq: float,
                   use_cache: bool = True) -> tuple[str, str]:
    payload = {"supplier": golden_id, "score": score, "tier": tier, "overrides": overrides, "data_quality": dq,
               "factors": {k: {"score": v["score"], "weight": v["weight"], "contribution": v["contribution"],
                               "detail": v["detail"]} for k, v in factors.items()}}
    system = ("You are an audit risk analyst. Explain the supplier's risk score using ONLY the JSON provided. "
              "Do not invent facts, numbers or findings. Name the top 3 contributing factors and any override "
              "applied. Reply as JSON {\"narrative\": \"3-4 sentences, plain English\"}.")
    out = _chat_json("narrative", system, payload, use_cache)
    if out and out.get("narrative"):
        return out["narrative"], "ai"
    top = sorted(factors.values(), key=lambda v: v["contribution"], reverse=True)[:3]
    parts = [f"{v['label'].lower()} ({v['detail']}; +{v['contribution']:.1f})" for v in top]
    text = (f"{golden_id} scores {score:.1f}, which places it in the {tier} tier. "
            f"The largest contributors are {parts[0]}, {parts[1]} and {parts[2]}.")
    if overrides:
        text += " Override applied: " + "; ".join(overrides) + "."
    if dq < 0.6:
        text += f" Data quality is {dq:.0%}, so treat this score as low confidence."
    return text, "template"


# ---------------- 4. Audit checklist drafting ----------------
STATIC_CHECKLIST = {
    "governance": ["Contract terms and SLAs reviewed", "Right-to-audit clause confirmed", "Compliance attestations on file"],
    "infosec": ["Access control and MFA evidence", "Vulnerability and patch management records", "Security incident log review"],
    "privacy": ["Data processing agreement in place", "Data inventory and retention schedule", "Cross-border transfer approvals"],
    "bcp": ["Business continuity plan tested in last 12 months", "Disaster recovery RTO/RPO evidence"],
    "physical": ["Offshore delivery centre physical controls", "Visitor and badge access logs"],
    "subprocessor": ["Sub-processor list and approvals", "Flow-down of contractual controls"],
    "findings": ["Evidence of remediation for each open finding", "Root-cause analysis for repeat findings"],
    "certification": ["Current ISO 27001 or SOC 2 report", "Bridge letter or remediation plan if expired"],
}


def draft_checklist(golden_id: str, modules: list[dict], profile: dict) -> tuple[dict, str]:
    payload = {"supplier": golden_id, "modules": [m["id"] for m in modules], "profile": profile}
    system = ("You draft supplier audit checklists. For each module id in the JSON, return 3-5 short, specific "
              "checklist items. Use only the profile facts given. Return JSON {\"checklist\": {module_id: [items]}}.")
    out = _chat_json("checklist", system, payload)
    if out and isinstance(out.get("checklist"), dict):
        return {m["id"]: out["checklist"].get(m["id"], STATIC_CHECKLIST.get(m["id"], [])) for m in modules}, "ai"
    return {m["id"]: STATIC_CHECKLIST.get(m["id"], []) for m in modules}, "static"


# ---------------- 5. Notification wording ----------------
TEMPLATES = {
    "audit_plan_created": ("Audit plan created: {supplier}",
                           "An audit of {supplier} is planned for {audit_date} ({audit_type}). "
                           "Your first task is due {first_due}. Scope: {scope}."),
    "risk_tier_up": ("Risk tier increased: {supplier}",
                     "{supplier} moved from {old_tier} to {new_tier} (score {score}). Please review the audit plan."),
    "new_eligible": ("New supplier needs audit: {supplier}",
                     "{supplier} is now audit-eligible ({tier}). Next audit due {next_due}."),
    "audit_due": ("Audit due in {days} days: {supplier}", "The audit of {supplier} is scheduled for {audit_date}."),
    "task_overdue": ("Task overdue: {task}", "'{task}' for {supplier} was due {due_date} and is now overdue."),
    "conflicts_waiting": ("{count} data conflicts waiting for your review",
                          "{count} supplier data conflicts need a decision on the Data consolidation page."),
}


def notification_text(event: str, ctx: dict) -> tuple[str, str, str]:
    """Returns (subject, body, source). The model sees {supplier} as a placeholder, never the real name."""
    subj_t, body_t = TEMPLATES[event]
    masked = {k: v for k, v in ctx.items() if k != "supplier"}
    system = ("You write short, professional notification messages for an audit team. Use the literal "
              "placeholder {supplier} where the supplier name goes. Use only facts in the JSON. Return JSON "
              "{\"subject\": \"...\", \"body\": \"2 sentences\"}.")
    out = _chat_json("notification", system, {"event": event, **masked})
    try:
        if out and out.get("subject") and out.get("body") and "{supplier}" in out["body"] + out["subject"]:
            return (out["subject"].replace("{supplier}", str(ctx.get("supplier", ""))),
                    out["body"].replace("{supplier}", str(ctx.get("supplier", ""))), "ai")
    except Exception:
        pass
    return subj_t.format(**ctx), body_t.format(**ctx), "template"


# ---------------- 6. Natural-language copilot ----------------
FILTER_TOOL = {
    "type": "function",
    "function": {
        "name": "filter_suppliers",
        "description": "Filter the golden supplier table.",
        "parameters": {
            "type": "object",
            "properties": {
                "tier": {"type": "array", "items": {"type": "string", "enum": ["Low", "Medium", "High", "Critical"]}},
                "data_access": {"type": "array", "items": {"type": "string",
                                                           "enum": ["none", "internal", "confidential", "pii"]}},
                "overdue": {"type": "boolean"},
                "eligible": {"type": "boolean"},
                "country": {"type": "array", "items": {"type": "string", "description": "ISO alpha-2"}},
                "category": {"type": "string", "description": "Category name contains this text"},
                "min_score": {"type": "number"},
                "has_open_findings": {"type": "boolean"},
                "certification": {"type": "array", "items": {"type": "string", "enum": ["valid", "expired", "none"]}},
                "pending_conflicts": {"type": "boolean"},
            },
        },
    },
}


def parse_question_rules(q: str) -> dict:
    """Fallback parser for common questions."""
    s = q.lower()
    args: dict = {}
    tiers = [t for t in ["Low", "Medium", "High", "Critical"] if re.search(rf"\b{t.lower()}\b", s)]
    if re.search(r"high[- ]risk", s) and not tiers:
        tiers = ["High", "Critical"]
    if tiers:
        args["tier"] = tiers
    acc = []
    if "pii" in s or "personal data" in s or "client data" in s:
        acc.append("pii")
    if "confidential" in s:
        acc.append("confidential")
    if acc:
        args["data_access"] = acc
    if "overdue" in s:
        args["overdue"] = True
    if "not eligible" in s or "monitor only" in s:
        args["eligible"] = False
    elif "eligible" in s:
        args["eligible"] = True
    if "open finding" in s or "findings" in s:
        args["has_open_findings"] = True
    if "expired" in s:
        args["certification"] = ["expired"]
    elif "no cert" in s or "without cert" in s or "uncertified" in s:
        args["certification"] = ["none"]
    if "conflict" in s:
        args["pending_conflicts"] = True
    from .generate_data import COUNTRIES
    ctry = [iso for iso, (name, *_r) in COUNTRIES.items() if re.search(rf"\b{name.lower()}\b", s)]
    if re.search(r"\b(uk|britain)\b", s):
        ctry.append("GB")
    if re.search(r"\b(usa|us|america)\b", s):
        ctry.append("US")
    ctry = sorted(set(ctry))
    if ctry:
        args["country"] = ctry
    m = re.search(r"(score|risk)\s*(above|over|>=?|at least)\s*(\d+)", s)
    if m:
        args["min_score"] = float(m.group(3))
    for cat in ["it services", "software", "logistics", "facility", "consulting", "printing", "staffing",
                "security services", "payroll"]:
        if cat in s:
            args["category"] = cat
    return args


def copilot(question: str) -> tuple[dict, str]:
    """Return (filter arguments, source). The app runs the filter itself; the LLM never runs code."""
    if enabled():
        t0 = time.time()
        key = f"copilot:{PROMPT_VERSION}:{_hash(question)}"
        hit = _cached(key)
        if hit is not None:
            return hit, "ai (cached)"
        try:
            resp = chat(
                temperature=0, tools=[FILTER_TOOL],
                tool_choice={"type": "function", "function": {"name": "filter_suppliers"}},
                messages=[{"role": "system", "content": "Translate the audit team's question into filter_suppliers "
                                                        "arguments. Only set filters the question asks for."},
                          {"role": "user", "content": question}])
            args = json.loads(resp.choices[0].message.tool_calls[0].function.arguments or "{}")
            _log("copilot", key, args, "ok", int((time.time() - t0) * 1000))
            _store(key, "copilot", args)
            return args, "ai"
        except Exception as exc:
            _log("copilot", key, {"error": str(exc)[:300]}, "error", int((time.time() - t0) * 1000))
    return parse_question_rules(question), "rule parser"


def filter_suppliers(df, args: dict):
    out = df.copy()
    if args.get("tier"):
        out = out[out.risk_tier.isin(args["tier"])]
    if args.get("data_access"):
        out = out[out.data_access_level.isin(args["data_access"])]
    if "overdue" in args:
        out = out[out.overdue.astype(bool) == bool(args["overdue"])]
    if "eligible" in args:
        out = out[out.eligible.astype(bool) == bool(args["eligible"])]
    if args.get("country"):
        out = out[out.country.isin(args["country"])]
    if args.get("category"):
        out = out[out.category_name.str.lower().str.contains(args["category"].lower(), na=False)
                  | out.service_type.fillna("").str.lower().str.contains(args["category"].lower())]
    if args.get("min_score") is not None:
        out = out[out.risk_score >= float(args["min_score"])]
    if args.get("has_open_findings"):
        out = out[(out.open_critical + out.open_high + out.open_medium) > 0]
    if args.get("certification"):
        out = out[out.certification_status.isin(args["certification"])]
    if args.get("pending_conflicts"):
        out = out[out.pending_conflicts > 0]
    return out
