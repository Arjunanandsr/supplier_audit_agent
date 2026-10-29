# AuditPilot — AI-Powered Supplier Audit Onboarding & Risk Assessment Assistant

Prototype for **AI Friday Season 3**. Four fragmented supplier sources (Ariba, VMS, GBAMS, PCM) plus audit
history become one explainable golden record per supplier. AuditPilot then scores risk, decides audit eligibility,
frequency and scope, schedules audits against auditor capacity, generates tasks and notifies audit owners,
supplier owners and ISMs.

**All data is synthetic.** Every source row is labelled `SYNTHETIC`.

**Core design rule:** deterministic rules compute every score and decision. The LLM only explains, extracts and
drafts. The LLM can be the **OpenAI API** (just an `OPENAI_API_KEY`) or **Azure OpenAI**. Every AI feature has a
fallback, so the app also runs fully with no key at all.

## Contents

1. [How to run](#how-to-run) — fastest path, step by step, configuration, verification, troubleshooting
2. [Sample data](#sample-data) — what ships in the zip
3. [Pages](#pages) · [AI Audit Analyst](#ai-audit-analyst-agent) · [Demo script](#demo-script-about-7-minutes)
4. [Architecture](#architecture) · [Risk model](#risk-model) · [AI provider](#ai-provider-openai-api-or-azure-openai) ·
   [Notifications](#notifications-and-task-tools) · [Assumptions](#assumptions-to-confirm)

## How to run

### What you need

| Requirement | Detail |
| --- | --- |
| Python | **3.10 or newer** (tested on 3.13). Check with `python --version` (Windows) or `python3 --version` (macOS/Linux). |
| Disk / RAM | About 400 MB for dependencies; 2 GB RAM is plenty |
| Network | Only to install packages, and optionally to reach OpenAI / Azure OpenAI, SMTP or Teams |
| Browser | Any modern browser (Chrome, Edge, Firefox, Safari) |
| AI key | **Optional.** An OpenAI API key or an Azure OpenAI deployment. Without one, every AI feature uses its rule-based fallback |

No database server is needed: state lives in a local SQLite file.

### Option A — one-step launcher (recommended)

1. Unzip `auditpilot.zip` and open a terminal in the `auditpilot` folder.
2. Run the launcher:
   - **Windows:** double-click `run.bat`, or run `run.bat` in Command Prompt / PowerShell.
   - **macOS / Linux:** `chmod +x run.sh` once, then `./run.sh`.
3. The first run creates a `.venv` virtual environment, installs `requirements.txt` (2–5 minutes), copies
   `.env.example` to `.env`, and starts the app. Later runs start in a few seconds.
4. Your browser opens **http://localhost:8501**. If it does not, open that address yourself.

Stop the app with **Ctrl+C** in the terminal. Use another port with `PORT=8600 ./run.sh` (macOS/Linux).

### Option B — manual steps

**Windows (PowerShell)**

```powershell
cd path\to\auditpilot
python -m venv .venv
.venv\Scripts\Activate.ps1          # Command Prompt: .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
copy .env.example .env               # optional settings, see Configuration
streamlit run app\Home.py
```

If PowerShell blocks the activate script, run once:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

**macOS / Linux**

```bash
cd path/to/auditpilot
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env                 # optional settings, see Configuration
streamlit run app/Home.py
```

Always start Streamlit **from the `auditpilot` folder** so it finds `config/`, `data/` and `.streamlit/`.

### What happens on first launch

1. The app reads the synthetic source files already in `data/raw/` (shipped in the zip).
2. It consolidates them into 200 golden records, scores risk, decides eligibility and builds the audit schedule.
3. It writes everything to `data/auditpilot.db` (created automatically, about 1 MB). This takes 1–3 seconds
   with no AI key. With an OpenAI or Azure key, the first run sends about 200 short red-flag extraction calls;
   answers are cached, so later runs are fast.
4. The **Overview** page opens. The sidebar shows the role switch, notification count, AI mode and the
   **↻ Rerun** and **⟲ Reset demo** buttons.

### Configuration (`.env`)

Everything is optional. Edit `.env` in the project folder, then restart the app.

| Setting | Purpose |
| --- | --- |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | **Option 1 — OpenAI API.** Turns on the AI features and the AI agent with a standard OpenAI key. `OPENAI_MODEL` defaults to `gpt-4o-mini`. Optional: `OPENAI_BASE_URL` (OpenAI-compatible endpoint or proxy), `OPENAI_ORG_ID`, `OPENAI_PROJECT` |
| `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_API_VERSION`, `AZURE_OPENAI_DEPLOYMENT` | **Option 2 — Azure OpenAI.** Same features through your Azure deployment |
| `AI_PROVIDER=openai \| azure \| none` | Pick the provider when both are configured. If unset: Azure when fully configured, otherwise OpenAI when `OPENAI_API_KEY` is set. The sidebar shows 🟢 and the provider + model when active |
| `AI_ENABLED=false` | Force rule-based mode even when a key is set (useful for a guaranteed offline demo) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_STARTTLS` | Send notification emails with an `.ics` invite |
| `TEAMS_WEBHOOK_URL` | Post notifications to a Teams channel (Teams Workflows webhook) |
| `AUDITPILOT_TODAY=YYYY-MM-DD` | Freeze the business date, e.g. `2027-01-20` to show overdue tasks |
| `AUDITPILOT_DB` | Use a different database file |

Policy settings live in `config/weights.yaml` (risk model) and `config/rules.yaml` (eligibility, frequency,
scope, auditor capacity, task templates). After editing them, click **↻ Rerun** in the sidebar.

### Check that it works

1. **Overview** shows 200 suppliers and about 32 open data conflicts.
2. **Supplier 360 → Nimbus Data Services** shows score **63.3 (High)**; **Orion Critical Systems** shows **Critical**.
3. **AI Audit Analyst → Sample reports → Nimbus_InfoSec_Audit_Report_2026.md → Run analysis** shows 6 findings
   with 100% evidence verified.
4. Optional automated tests, with the virtual environment active:

   ```bash
   python -m pytest -q        # expect: 13 passed
   ```

   Tests use their own temporary database, so they never touch your demo data.

### Reset, regenerate and clean up

| Goal | How |
| --- | --- |
| Start the demo over | Sidebar → **⟲ Reset demo**: clears decisions, plans, tasks, notifications and analyses, regenerates the synthetic data for today's date and rebuilds |
| Recompute after editing config | Sidebar → **↻ Rerun** |
| Rebuild from scratch | Stop the app, delete `data/auditpilot.db`, start again |
| Regenerate source files only | `python -m core.generate_data` (overwrites `data/raw/`) |
| Remove everything | Delete the `auditpilot` folder (the virtual environment is inside it) |

### Troubleshooting

| Symptom | Fix |
| --- | --- |
| `python` / `python3` not found | Install Python 3.10+ from python.org; on Windows tick **Add python.exe to PATH** |
| `ModuleNotFoundError` (streamlit, core, ui…) | Activate `.venv` and run from the `auditpilot` folder; rerun `pip install -r requirements.txt` |
| `Port 8501 is already in use` | `streamlit run app/Home.py --server.port 8600` |
| Browser does not open | Open http://localhost:8501 manually |
| Sidebar says "AI fallback mode" with a key set | OpenAI: check `OPENAI_API_KEY` (and `AI_PROVIDER` is not `azure`). Azure: check all four `AZURE_OPENAI_*` values. Restart after editing `.env` |
| OpenAI error `model_not_found` / 404 | Set `OPENAI_MODEL` to a chat model your key can use |
| OpenAI error 429 (quota / rate limit) | Add billing credit or wait; the app keeps working on fallbacks meanwhile |
| Sidebar says "… unreachable" | 3 calls failed in a row; the app uses fallbacks for 5 minutes. Check key, model/deployment name and network; see **Audit log → AI call log** for the error |
| Uploaded PDF gives no text | Scanned PDFs have no text layer; OCR is not included. Use a text PDF, Word or TXT file |
| Email or Teams not sent | See the status column in **Tasks & notifications → Notifications** for the error |
| `pip` fails behind a proxy | `pip install --proxy http://user:pass@proxy:port -r requirements.txt` |

## Sample data

Everything below is **synthetic** and ships in the zip.

| File | Rows | Use |
| --- | --- | --- |
| `data/raw/ariba_suppliers.csv` | 205 | Supplier master: onboarding, questionnaire score and comments, certifications, self-declared data access and criticality (5 duplicates planted) |
| `data/raw/vms_vendors.csv` | 173 | Vendor management: service type, contractor headcount, offshore delivery (3 vendors not in Ariba) |
| `data/raw/gbams_associates.csv` | 150 | Access management: users with system access, data access level |
| `data/raw/pcm_contracts.csv` | 180 | Procurement contracts: value (INR), dates, criticality tier, sub-processors |
| `data/raw/audit_history.csv` | 295 | Past audits: rating, findings by severity, open findings |
| `samples/AuditPilot_Sample_Data.xlsx` | 7 tabs | All five sources plus the external risk feed in one workbook, with a README tab |
| `samples/External_Supplier_Risk_Feed.csv` | 60 | Third-party risk feed: sanctions, financial health, adverse notes |
| `samples/Nimbus_InfoSec_Audit_Report_2026.md` | — | Audit report with 6 structured findings |
| `samples/Orion_SOC2_Type2_Summary_2026.txt` | — | SOC 2 Type II summary with exceptions in free text |
| `samples/Lotus_Self_Assessment_2026.txt` | — | Low-risk self-assessment questionnaire |

Planted problems: name variants (e.g. "ACME PRIVATE LIMITED" vs "Acme Pvt Ltd"), conflicting data access
between Ariba and GBAMS, missing records, mixed date and country formats, duplicates and red-flag comments.
Dates in the files are relative to 29 October 2026, the day they were generated. **Reset demo** regenerates
them relative to the current date. You can browse them on **Data consolidation → Raw sources**, or feed any of
them to the AI Audit Analyst; for the workbook, pick a tab in the **Sheet** box.

## Pages

| Page | What it shows |
| --- | --- |
| Overview | KPIs, tier distribution, criticality × data-sensitivity heatmap, highest-risk suppliers |
| Data consolidation | Source coverage, conflict queue with what-if scores and Approve/Override, match review, field lineage, raw sources |
| Supplier 360 | Golden record, risk gauge, factor breakdown, AI narrative, eligibility reasons, scope modules, audit plan, checklist, `.ics` invite |
| Audit planner | Monthly load vs capacity, audit timeline, schedule, bulk plan generation |
| Workflow | Pipeline stages with live counts; amber stages have suppliers waiting |
| Tasks & notifications | Task board, daily check (overdue + 30/7-day reminders), task sinks, notification log |
| Copilot | Plain-English questions answered as tables (function calling, with a rule-based fallback) |
| Audit log | Governance controls, append-only audit trail, AI call log |
| AI Audit Analyst | Agent that analyses a report or dataset from any source and writes a detailed analysis report |

## AI Audit Analyst (agent)

Give it any input: an uploaded **PDF, Word (.docx), TXT/Markdown, CSV, Excel or JSON** file, one of the bundled
synthetic samples in `samples/`, or one of AuditPilot's own source extracts.

**How the agent works**

1. The input is read into text (with sections) or a table.
2. Supplier names, short references, emails, phone numbers and tax IDs are masked as tokens (e.g. `[GS-0091]`).
3. With an AI key configured (OpenAI API or Azure OpenAI), the model runs a tool-calling loop over 13 tools: `document_overview`,
   `read_section`, `search_document`, `detect_suppliers`, `lookup_supplier`, `get_audit_policy`,
   `extract_findings`, `compare_with_golden`, `simulate_risk`, `profile_table`, `sample_rows`, `query_table`,
   `cross_check_table`, then `submit_report`. With no key, or if the model fails, a rule-based analyst runs
   the same tools in a fixed plan.
4. The engine then verifies: every evidence quote is matched against the source (✅ / ⚠️), the risk impact is
   recomputed by the deterministic risk model, and tokens are turned back into names for display.

**The report in the UI:** rating, risk level and confidence; headline metrics; executive summary; current vs
projected risk score with the inputs that changed; findings by severity and control area with evidence,
recommendation, owner and due days; report-vs-golden-record discrepancies; affected suppliers (for datasets);
data quality issues; prioritised actions; limitations, masking summary and the full agent trace.

**Actions:** *Accept findings into risk model* (human approval; the report's open findings replace the
audit-history counts and the pipeline reruns, so tiers, eligibility, schedules and notifications update),
*Create remediation tasks* (one per open Medium+ finding, assigned by control area, owners notified), and
**download the report as PDF, Excel, Markdown or JSON**. The PDF is a formatted landscape report with a
severity chart, colour-coded findings and every section. The Excel workbook has one tab per section (Summary,
Findings, By control area, Score impact, Discrepancies, Affected suppliers, Data quality, Recommendations,
Limitations, Agent trace) with filters and severity colours. Past analyses are in the sidebar and on Supplier 360.

| Sample | What the agent finds |
| --- | --- |
| `Nimbus_InfoSec_Audit_Report_2026.md` | 6 findings (1 critical open), expired ISO 27001 and an unapproved sub-processor that contradict the golden record; score 63.3 → 78.8 |
| `Orion_SOC2_Type2_Summary_2026.txt` | 5 SOC 2 exceptions from free text, qualified opinion |
| `Lotus_Self_Assessment_2026.txt` | No findings; low risk confirmed |
| `External_Supplier_Risk_Feed.csv` | Sanctions hits, weak financials, adverse notes, under-recorded data access, 3 suppliers not in the master, duplicates and mixed date formats |

Use the sidebar to switch role (**Audit team · Supplier owner · ISM**) and user. Supplier owners see their
suppliers; ISMs see their security-scoped suppliers; only the audit team generates plans.

## Demo script (about 7 minutes)

1. **Overview** — 200 suppliers from 4 systems; open conflicts and overdue audits.
2. **Data consolidation → Conflict queue** — pick *Nimbus Data Services*: Ariba self-declares `internal`, GBAMS
   records `pii`. The what-if shows **Medium (45.8)** vs **High (63.3)**. Most restrictive wins; approve it.
3. **Supplier 360 → Nimbus** — factor bars, AI narrative, eligibility reasons, Data privacy module in scope.
4. **Generate audit plan** — 8 dated tasks with owners; audit owner, supplier owner and ISM are notified.
5. **Supplier 360 → Orion Critical Systems** — Critical, never audited, first audit due within 30 days.
   **Lotus Stationery Supplies** — correctly excluded as *Monitor only*.
6. **Audit planner** and **Workflow** — risk-ordered schedule against capacity; live stage counts.
7. **AI Audit Analyst** — run the Nimbus audit report: the agent finds 6 findings, flags the expired certificate
   and the unapproved sub-processor, and projects 63.3 → 78.8. Accept the findings, then create remediation tasks.
   Run the external risk feed CSV to show multi-supplier analysis.
8. **Audit log** — every decision, plan, analysis and score with who, when and config version.

Use **Reset demo** in the sidebar to start clean. To show overdue tasks and reminders, set
`AUDITPILOT_TODAY=2027-01-20` in `.env`, restart, and click **Run daily check**.

## Architecture

```
Ariba ─┐
VMS   ─┤                                       ┌─ Risk engine (7 factors, 0–100, tiers, floors)
GBAMS ─┼─► Consolidation ─► Golden record ─────┼─ Eligibility engine (eligible? how often? scope)
PCM   ─┤   normalize·match   (SQLite, lineage) └─ Audit planner (capacity, tasks) ─► Notifier
History┘   survive·conflicts                                                         in-app · email · Teams
                      ▲               LLM assists (masked IDs only)          + task sinks
                      └── human review (conflicts, matches, plans) ─► audit log
```

```
auditpilot/
  app/Home.py, app/pages/*.py   Streamlit UI
  app/ui.py                     sidebar, role filter, helpers
  core/generate_data.py         synthetic Ariba, VMS, GBAMS, PCM, audit history (seed 42)
  core/consolidate.py           normalize, match, survive, lineage, conflicts, data quality
  core/risk.py                  7-factor score, tiers, override floors, what-if
  core/eligibility.py           eligibility, frequency, next due, scope modules
  core/planner.py               capacity scheduler, plans, tasks, daily check
  core/notifier.py              in-app / email / Teams notifier, .ics, task sinks
  core/ai.py                    OpenAI / Azure OpenAI calls, prompts, cache, circuit breaker, fallbacks
  core/pipeline.py              end-to-end run, conflict and match decisions, reset
  core/analyst.py               AI Audit Analyst: masking, tools, agent loop, rule-based analyst, verification
  core/doc_io.py                readers for PDF, Word, text, CSV, Excel, JSON
  core/report_export.py         analysis report export to PDF and Excel
  samples/                      synthetic reports and an external risk feed for the analyst
  core/audit_log.py, core/db.py SQLite state and audit trail
  config/weights.yaml           risk weights and thresholds
  config/rules.yaml             eligibility, frequency, scope, capacity, task templates
  tests/test_core.py            matching, worked example, heroes, plan flow, analyst
  data/raw/                     synthetic source extracts (shipped)
  run.sh, run.bat               one-step launchers
```

## Risk model

`R = Σ wᵢ · sᵢ` over seven factors, each scored 0–100.

| Factor | Weight | Score |
| --- | --- | --- |
| Data sensitivity | 25% | none 0 · internal 30 · confidential 70 · PII 100 |
| Access exposure | 15% | 0 users 0 · 1–10 40 · 11–50 70 · 51+ 100 |
| Business criticality | 15% | low 25 · medium 50 · high 75 · critical 100 |
| Financial exposure | 10% | percentile of contract value |
| Assessment result | 15% | 100 − score, +10 per red flag |
| Audit history | 15% | open critical ×25, high ×15, medium ×5; +20 if last rating unsatisfactory |
| Certification gap | 5% | valid 0 · expired 70 · none 100 |

Tiers: Critical ≥ 80 · High 60–79 · Medium 40–59 · Low < 40. Floors: an open critical finding, or PII access
without a valid certification, lifts the score to at least 60. Data quality below 60% flags *low confidence*.
All values live in `config/weights.yaml`.

## AI provider: OpenAI API or Azure OpenAI

**Option 1 — OpenAI API** (simplest). Set in `.env`:

```
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini          # or any chat model your key can use
```

**Option 2 — Azure OpenAI.** Set in `.env`:

```
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com
AZURE_OPENAI_API_KEY=<key>
AZURE_OPENAI_API_VERSION=2024-10-21
AZURE_OPENAI_DEPLOYMENT=<your GPT deployment name>
```

| Use | Fallback |
| --- | --- |
| Red-flag extraction from assessment comments | Keyword rules |
| Conflict resolution recommendation | Survivorship rule |
| Risk narrative (grounded on stored factors only) | Template from top 3 factors |
| Audit checklist per scope module | Standard checklist |
| Notification wording (`{supplier}` placeholder, name filled locally) | Fixed template |
| Copilot (function calling into `filter_suppliers`) | Rule-based question parser |

Guardrails: only golden IDs and coded values are sent; temperature 0; JSON output; responses cached in SQLite;
every call logged (prompt version, deployment, input hash, latency). After 3 consecutive failures the app uses
fallbacks for 5 minutes instead of waiting on a dead endpoint. Confirm your subscription's data-privacy and
retention terms with your cloud or security team.
With the OpenAI API, data goes to OpenAI's public service rather than your Azure tenant; masking still applies,
but check this is acceptable under your data policy before using real (non-synthetic) documents. Models that
only accept their default temperature are handled automatically (the call is retried without it).

## Notifications and task tools

In-app notifications are always on. Set `SMTP_HOST` (and optionally `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`,
`SMTP_FROM`) to send email with an `.ics` invite, and `TEAMS_WEBHOOK_URL` (a Teams Workflows webhook) to post to
a channel. Tasks export to CSV under `data/exports/`; Jira and Azure DevOps sinks are stubs ready to implement.

## Assumptions to confirm

- GBAMS is treated as business-associate / access management and PCM as procurement contract management.
- ISM is treated as the security owner who reviews security evidence and remediation.
- Eligibility thresholds, audit intervals and capacity (3 auditors × 8 audits a month) are proposed defaults.
