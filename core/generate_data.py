"""Synthetic source data for Ariba, VMS, GBAMS, PCM and audit history.

Everything here is fake. Seeded, so every run on the same business date
produces identical files. Data problems are planted on purpose so the
consolidation step has real work to do.
"""
from __future__ import annotations

import random
from datetime import date, timedelta

import numpy as np
import pandas as pd
from rapidfuzz import fuzz, process

from .config import RAW_DIR, today

SEED = 42
LABEL = "SYNTHETIC"

FIRST = ["Apex", "Vertex", "Zenith", "Summit", "Crest", "Helix", "Quantum", "Sterling", "Cobalt",
         "Indigo", "Saffron", "Banyan", "Meridian", "Pinnacle", "Aurora", "Falcon", "Harbor",
         "Evergreen", "Silverline", "Bluepeak", "Redwood", "Keystone", "Monsoon", "Granite",
         "Tidal", "Lumen", "Northstar", "Peregrine"]
THIRD = ["Services", "Solutions", "Technologies", "Systems", "Partners", "Group"]

# word -> (category_code, service_type, access weights [none, internal, confidential, pii])
SECOND = {
    "Data": ("43230000", "Data processing", [.05, .15, .30, .50]),
    "Tech": ("81110000", "IT services", [.10, .30, .35, .25]),
    "Cloud": ("81110000", "Cloud hosting", [.05, .15, .35, .45]),
    "Logistics": ("78100000", "Logistics", [.40, .40, .15, .05]),
    "Facility": ("72100000", "Facility management", [.50, .35, .10, .05]),
    "Infra": ("81110000", "Infrastructure support", [.10, .35, .35, .20]),
    "Digital": ("80100000", "Digital consulting", [.15, .35, .30, .20]),
    "Consulting": ("80100000", "Management consulting", [.20, .40, .30, .10]),
    "Analytics": ("43230000", "Analytics", [.05, .15, .35, .45]),
    "Print": ("82120000", "Printing", [.50, .30, .15, .05]),
    "Staffing": ("80110000", "Contract staffing", [.10, .30, .30, .30]),
    "Security": ("92120000", "Physical security", [.40, .40, .15, .05]),
    "Networks": ("81110000", "Network services", [.10, .30, .40, .20]),
    "Software": ("43230000", "Software development", [.10, .30, .35, .25]),
    "Payroll": ("84110000", "Payroll processing", [.00, .10, .30, .60]),
}
CATEGORY_NAMES = {
    "43230000": "Software & data", "81110000": "IT services", "78100000": "Logistics",
    "72100000": "Facility management", "80100000": "Consulting", "82120000": "Printing",
    "80110000": "HR & staffing", "92120000": "Security services", "84110000": "Finance & payroll",
}
# iso -> (country name, legal suffix, alternative suffixes, spellings seen in sources)
COUNTRIES = {
    "IN": ("India", "Pvt Ltd", ["Private Limited", "Pvt. Ltd."], ["India", "IN", "IND"]),
    "US": ("United States", "Inc", ["Incorporated", "Inc."], ["United States", "US", "USA"]),
    "GB": ("United Kingdom", "Ltd", ["Limited", "Ltd."], ["United Kingdom", "UK", "GBR"]),
    "DE": ("Germany", "GmbH", ["GMBH"], ["Germany", "DE", "DEU"]),
    "SG": ("Singapore", "Pte Ltd", ["Pte. Ltd.", "Private Limited"], ["Singapore", "SG", "SGP"]),
    "PH": ("Philippines", "Inc", ["Inc.", "Incorporated"], ["Philippines", "PH", "PHL"]),
}
COUNTRY_WEIGHTS = [0.45, 0.20, 0.12, 0.08, 0.08, 0.07]
ACCESS = ["none", "internal", "confidential", "pii"]
CRIT = ["low", "medium", "high", "critical"]
ABBREV = {"Services": "Svcs", "Technologies": "Tech", "Solutions": "Solns", "Systems": "Sys",
          "Partners": "Ptnrs"}

BENIGN = ["All controls in place per last review.", "ISO certified ISMS with annual review.",
          "Annual security awareness training completed.", "Access reviews run quarterly.",
          "Encryption at rest and in transit for client data.", "Documented change management."]
RED_FLAGS = ["MFA rollout planned next year.", "Data stored in offshore data centre without client approval.",
             "No formal incident response plan yet.", "Penetration test overdue by six months.",
             "Shared admin accounts still in use.", "Backups not tested this year.",
             "Laptops not fully disk-encrypted."]

HEROES = {
    "nimbus": dict(base="Nimbus Data Services", cc="IN", second="Data", access="pii", declared="internal",
                   users=30, crit="high", value=250_000_000, score=70, cert="valid",
                   comments=["Encryption at rest and in transit for client data."], offshore=True, subproc=False),
    "orion": dict(base="Orion Critical Systems", cc="US", second="Cloud", access="pii", declared="pii",
                  users=80, crit="critical", value=420_000_000, score=55, cert="none",
                  comments=["MFA rollout planned next year.", "Shared admin accounts still in use.",
                            "Backups not tested this year."], offshore=True, subproc=True),
    "lotus": dict(base="Lotus Stationery Supplies", cc="IN", second="Print", access="none", declared="none",
                  users=0, crit="low", value=900_000, score=90, cert="none",
                  comments=["Documented change management."], offshore=False, subproc=False),
}


def _fmt_date(d: date, r: random.Random) -> str:
    return d.strftime("%d/%m/%Y") if r.random() < 0.5 else d.isoformat()


def _country_spelling(cc: str, r: random.Random) -> str:
    return r.choice(COUNTRIES[cc][3])


def _variant(base: str, cc: str, r: random.Random) -> str:
    legal = f"{base} {COUNTRIES[cc][1]}"
    choice = r.randrange(4)
    if choice == 0:
        return legal.upper()
    if choice == 1:
        return f"{base} {r.choice(COUNTRIES[cc][2])}"
    if choice == 2:
        return base
    return legal.lower()


def _norm(s: str) -> str:
    # Local copy of the consolidation normalizer, used only to keep abbreviations matchable.
    from .consolidate import norm_name
    return norm_name(s)


def generate(out_dir=RAW_DIR, seed: int = SEED) -> dict:
    """Write the five source CSVs. Returns the true source -> Ariba mapping (for tests)."""
    rng = np.random.default_rng(seed)
    r = random.Random(seed)
    t = today()
    out_dir.mkdir(parents=True, exist_ok=True)

    pairs = [(f, s) for f in FIRST for s in SECOND]
    r.shuffle(pairs)
    sups = []
    for f, s in pairs[:197]:
        cc = r.choices(list(COUNTRIES), weights=COUNTRY_WEIGHTS)[0]
        code, stype, aw = SECOND[s]
        access = ACCESS[rng.choice(4, p=aw)]
        if access == "none":
            users = 0 if r.random() < 0.9 else r.randint(1, 5)
        elif access == "internal" and r.random() < 0.2:
            users = 0
        else:
            band = rng.choice(3, p=[0.45, 0.35, 0.20])
            users = [r.randint(1, 10), r.randint(11, 50), r.randint(51, 200)][band]
        cert_kind = r.choices(["valid", "expired", "none"], weights=[0.45, 0.15, 0.40])[0]
        score = int(np.clip(rng.normal(78, 12), 35, 99))
        comments = [r.choice(BENIGN)]
        n_flags = r.randint(1, 2) if score < 70 else (1 if r.random() < 0.15 else 0)
        comments += r.sample(RED_FLAGS, n_flags)
        sups.append(dict(base=f"{f} {s} {r.choice(THIRD)}", cc=cc, second=s, code=code, stype=stype,
                         access=access, declared=access, users=users,
                         crit=CRIT[rng.choice(4, p=[0.30, 0.40, 0.22, 0.08])],
                         value=float(np.clip(round(rng.lognormal(np.log(2e7), 1.2), -3), 5e5, 6e8)),
                         score=score, cert=cert_kind, comments=comments,
                         offshore=r.random() < 0.35, subproc=r.random() < 0.30, hero=None))
    for key, h in HEROES.items():
        code, stype, _ = SECOND[h["second"]]
        sups.append(dict(h, code=code, stype=stype, hero=key))
    order = list(range(len(sups)))
    r.shuffle(order)
    sups = [sups[i] for i in order]

    for i, s in enumerate(sups, start=1):
        s["ariba_id"] = f"ARB-{i:04d}"
        s["legal"] = f"{s['base']} {COUNTRIES[s['cc']][1]}"
        s["tax_id"] = f"TX{s['cc']}{rng.integers(10**7, 10**8)}"
        start = t - timedelta(days=r.randint(180, 1500))
        end = start + timedelta(days=365 * r.randint(1, 5))
        if s["hero"] is None and r.random() < 0.08:
            end = t - timedelta(days=r.randint(10, 200))
        elif end < t + timedelta(days=60):
            end = t + timedelta(days=r.randint(200, 900))
        s["start"], s["end"] = start, end
        s["onboard"] = start - timedelta(days=r.randint(10, 90))
        if s["cert"] == "valid":
            certs = [f"ISO27001:{(t + timedelta(days=r.randint(60, 900))).isoformat()}"]
            if r.random() < 0.4:
                certs.append(f"SOC2:{(t + timedelta(days=r.randint(60, 500))).isoformat()}")
        elif s["cert"] == "expired":
            certs = [f"ISO27001:{(t - timedelta(days=r.randint(10, 400))).isoformat()}"]
        else:
            certs = []
        s["certs"] = "|".join(certs)

    heroes = {s["hero"]: s for s in sups if s["hero"]}
    others = [s for s in sups if not s["hero"]]

    # Source membership
    def pick(pool, n, must):
        chosen = [s for s in pool if s not in must]
        r.shuffle(chosen)
        return must + chosen[: n - len(must)]

    vms_set = pick(others, 170, [heroes["nimbus"], heroes["orion"], heroes["lotus"]])
    access_pool = [s for s in others if s["users"] > 0 or s["access"] != "none"]
    gb_set = pick(access_pool, 150, [heroes["nimbus"], heroes["orion"]])
    if len(gb_set) < 150:
        rest = [s for s in others if s not in gb_set]
        gb_set += rest[: 150 - len(gb_set)]
    pcm_set = pick(others, 180, [heroes["nimbus"], heroes["orion"], heroes["lotus"]])
    gb_ids = {s["ariba_id"] for s in gb_set}
    pcm_ids = {s["ariba_id"] for s in pcm_set}

    # Planted conflicts: Ariba self-declares lower data access than GBAMS records.
    for s in others:
        if s["ariba_id"] in gb_ids and s["access"] != "none" and r.random() < 0.17:
            s["declared"] = ACCESS[ACCESS.index(s["access"]) - 1]
        s["declared_crit"] = s["crit"]
        if s["ariba_id"] in pcm_ids and s["crit"] != "low" and r.random() < 0.06:
            s["declared_crit"] = CRIT[CRIT.index(s["crit"]) - 1]
    for h in heroes.values():
        h["declared_crit"] = h["crit"]

    # Ariba (master)
    ariba = pd.DataFrame([{
        "data_label": LABEL, "supplier_id": s["ariba_id"], "supplier_name": s["legal"], "tax_id": s["tax_id"],
        "country": _country_spelling(s["cc"], r), "category_code": s["code"],
        "onboarding_date": _fmt_date(s["onboard"], r), "assessment_score": s["score"],
        "assessment_comments": " ".join(s["comments"]), "certifications": s["certs"],
        "data_access_declared": s["declared"], "criticality_declared": s["declared_crit"],
    } for s in sups])
    dupes = ariba.sample(5, random_state=seed)
    ariba = pd.concat([ariba, dupes], ignore_index=True)

    truth: dict[tuple[str, str], str] = {}
    abbrev_budget = [8]

    def source_name(s, src, sid):
        name = s["legal"]
        if r.random() < 0.30:
            name = _variant(s["base"], s["cc"], r)
        if abbrev_budget[0] > 0 and s["hero"] is None and r.random() < 0.05:
            words = s["base"].split()
            if words[-1] in ABBREV:
                cand = " ".join(words[:-1] + [ABBREV[words[-1]]])
                # Only keep an abbreviation the right supplier still wins on.
                same_cc = [x for x in sups if x["cc"] == s["cc"]]
                best = process.extractOne(_norm(cand), [_norm(x["legal"]) for x in same_cc],
                                          scorer=fuzz.token_sort_ratio)
                if best and same_cc[best[2]] is s and 80 <= best[1] < 90:
                    name = cand
                    abbrev_budget[0] -= 1
        truth[(src, sid)] = s["ariba_id"]
        return name

    vms_rows = []
    for i, s in enumerate(vms_set, start=1):
        sid = f"VMS-{i:04d}"
        vms_rows.append({"data_label": LABEL, "vendor_id": sid, "vendor_name": source_name(s, "VMS", sid),
                         "country": _country_spelling(s["cc"], r), "service_type": s["stype"],
                         "contractor_headcount": 0 if s["users"] == 0 else r.randint(1, 120),
                         "offshore_delivery": "Y" if s["offshore"] else "N"})
    for j, nm in enumerate(["Quillon Courier Services", "Brightwave Event Solutions", "Tarang Catering Group"]):
        sid = f"VMS-{len(vms_rows) + 1:04d}"
        vms_rows.append({"data_label": LABEL, "vendor_id": sid, "vendor_name": f"{nm} Pvt Ltd",
                         "country": "India", "service_type": "Other", "contractor_headcount": r.randint(1, 10),
                         "offshore_delivery": "N"})
        truth[("VMS", sid)] = None
    vms = pd.DataFrame(vms_rows)

    gbams = pd.DataFrame([{
        "data_label": LABEL, "associate_id": f"GB-{i:04d}",
        "associate_org_name": source_name(s, "GBAMS", f"GB-{i:04d}"),
        "country": _country_spelling(s["cc"], r), "users_with_system_access": s["users"],
        "data_access_level": s["access"],
        "access_locations": "Offshore" if s["offshore"] else "Onshore",
    } for i, s in enumerate(gb_set, start=1)])

    pcm = pd.DataFrame([{
        "data_label": LABEL, "contract_id": f"PCM-{i:04d}",
        "contract_party": source_name(s, "PCM", f"PCM-{i:04d}"),
        "tax_id": s["tax_id"] if r.random() < 0.6 else "",
        "country": _country_spelling(s["cc"], r), "contract_value_inr": int(s["value"]),
        "contract_start": _fmt_date(s["start"], r), "contract_end": _fmt_date(s["end"], r),
        "criticality_tier": s["crit"], "uses_subprocessors": "Y" if s["subproc"] else "N",
    } for i, s in enumerate(pcm_set, start=1)])

    # Audit history
    hist = []
    auditors = ["Priya Raman", "Arjun Mehta", "Kavya Iyer"]
    for s in sups:
        if s["hero"] == "nimbus":
            hist.append(dict(supplier_ref=s["ariba_id"], audit_date=t - timedelta(days=300), rating="Satisfactory",
                             fc=0, fh=2, fm=1, oc=0, oh=1, om=0))
            continue
        if s["hero"] or r.random() > 0.70:
            continue
        n = r.randint(1, 3)
        last = t - timedelta(days=r.randint(30, 560))
        for k in range(n):
            d = last - timedelta(days=k * r.randint(330, 450))
            fc, fh, fm = int(rng.poisson(0.2)), int(rng.poisson(0.8)), int(rng.poisson(1.5))
            latest = k == 0
            hist.append(dict(supplier_ref=s["ariba_id"], audit_date=d,
                             rating=r.choices(["Satisfactory", "Needs improvement", "Unsatisfactory"],
                                              weights=[0.6, 0.3, 0.1])[0],
                             fc=fc, fh=fh, fm=fm,
                             oc=int(rng.binomial(fc, 0.5)) if latest else 0,
                             oh=int(rng.binomial(fh, 0.5)) if latest else 0,
                             om=int(rng.binomial(fm, 0.5)) if latest else 0))
    history = pd.DataFrame([{
        "data_label": LABEL, "audit_id": f"AUD-{i:04d}", "supplier_ref": h["supplier_ref"],
        "audit_date": h["audit_date"].isoformat(), "auditor": r.choice(auditors), "rating": h["rating"],
        "findings_critical": h["fc"], "findings_high": h["fh"], "findings_medium": h["fm"],
        "open_critical": h["oc"], "open_high": h["oh"], "open_medium": h["om"],
    } for i, h in enumerate(hist, start=1)])

    ariba.to_csv(out_dir / "ariba_suppliers.csv", index=False)
    vms.to_csv(out_dir / "vms_vendors.csv", index=False)
    gbams.to_csv(out_dir / "gbams_associates.csv", index=False)
    pcm.to_csv(out_dir / "pcm_contracts.csv", index=False)
    history.to_csv(out_dir / "audit_history.csv", index=False)
    return truth


if __name__ == "__main__":
    generate()
    print(f"Synthetic data written to {RAW_DIR}")
