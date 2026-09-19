# Merged team decisions (2026-09-18)

Two pairs answered the 50 design questions separately. Merge rules applied, in order:
1. If one pair gave the default and the other did not, the non-default answer wins.
2. If one pair's answer is more rigorous, the more rigorous one wins.
3. If the pairs disagree, the answer that yields the highest unsupervised-learning quality wins.

The team said: **be ready to change the app if the answers change later.** Every decision below maps to a
config key or a clearly bounded module so it can be flipped without a rewrite.

| # | Decision | Where it lives |
|---|---|---|
| 1 | Submission 2026-09-20 11:00 Finland time (EEST). Judges run the app themselves; a pitch may follow. | README, launcher |
| 2 | Two pairs; little parallel coding expected but possible. Clean module boundaries, single `main` branch. | `docs/ARCHITECTURE.md` |
| 3 | Judges run on hidden files: one-command install, CLI + UI, docs. | `run.ps1`, `run.sh`, `tpm/cli.py` |
| 4 | Assessment data: another **undocumented industrial sensor dataset** (pair 2, specific) but the pipeline stays **fully schema-agnostic** (pair 1). | `tpm/ingest/schema.py` |
| 5 | **No labels** in assessment data. Unsupervised is the primary path. Label-like columns, if present, are auto-detected, excluded from detection, used only for evaluation. | `tpm/ingest/schema.py`, `tpm/detect/evaluate.py` |
| 6 | Formats: CSV/TSV, whitespace `.dat` (with/without header, transposed detection), Parquet, Excel, JSON/JSONL. Streaming; provision for 15+ GB. | `tpm/ingest/readers.py` |
| 7 | Input modes: file upload, replay-as-stream, continuous incoming batches (watch folder + HTTP push). | `tpm/ingest/stream.py`, API |
| 8 | Column names may be absent. When present they are **very weak evidence**. Inference runs on aliases `S01..Snn` (blind mode on by default); names only shown as tooltips. | `ingest.blind_mode` |
| 9 | Time and sample period inferred from data (timestamps, monotone counters). Never ask. Unknown period → sample units, recorded as an assumption. | `tpm/ingest/schema.py` |
| 10 | Grouping: evaluate several strategies (block-constant key columns, counter resets, change-point segmentation, none), score, pick best; operator can override. | `tpm/ingest/schema.py` |
| 11 | Label/meta columns auto-detected and excluded; operator confirms in the UI. | schema + UI |
| 12 | Onset detection **within** groups is core: find when a fault actually starts even without labels. | `tpm/detect/changepoints.py` |
| 13 | Baseline ("normal"): (c) auto-selected dominant/stable regime is primary; (b) operator may set a reference period; (a) labels only for evaluation. | `tpm/detect/baseline.py` |
| 14 | Role taxonomy: domain-agnostic **structural** roles first (constant, counter, timestamp, categorical, text, continuous-measured, actuator-like, held/sampled, derived/redundant). Instrument / unit-operation roles only as **hypotheses** with confidence, gated by a data-driven domain likelihood (sensor-like vs record-like). | `tpm/profile/roles.py` |
| 15 | No dataset-specific domain primer. LLM general knowledge only as hypothesis. Optional operator-provided domain hint. | `tpm/llm/prompts` |
| 16 | External LLM: Anthropic Claude. **Default profile: no-egress.** | `config/settings.yaml` |
| 17 | Routing table accepted (see ARCHITECTURE). | `profiles.*.routing` |
| 18 | Egress guard thresholds as proposed, configurable, strict mode available. | `guard.*` |
| 19 | Guard-blocked payload → fall back to local model, ledger entry `fallback`. | `tpm/llm/guard.py` |
| 20 | Retrieval: local tool-agent over raw data **and** embedding retrieval over logs/reports/rules, local embedding model. | `tpm/llm/agent.py`, `tpm/llm/embeddings.py` |
| 21 | Local model default `gemma4:e4b-it-qat`; alternatives configurable; bake-off script. | `local_llm.*`, `scripts/bakeoff.py` |
| 22 | Target machine class: 16 GB RAM / 8 GB VRAM laptop. Creative RAM optimisation is a requirement. | `tpm/memory.py`, DuckDB out-of-core |
| 23 | Full pass on a 6 GB file in ≤ 20 min. | `time_budget_s` |
| 24 | Rules via file **and** a chat-like UI. Every flag is clickable and opens a follow-up chat. | UI + `tpm/quality/rules.py` |
| 25 | Rule types: threshold/range, rate of change, **second-order change (acceleration)**, duration, cross-signal, rolling stats, missing/stale. Never arbitrary code. | `tpm/quality/rules.py` |
| 26 | Rules refer to signal IDs by default; role-name matching behind `rules.allow_role_names`. | config |
| 27 | Batch = 5-minute window when timestamps exist, else 10 % of rows; configurable. k-fold used for out-of-fold scoring and, time permitting, augmentation. | `batch.*` |
| 28 | Untrusted data: continue but flag, lower confidence, highlight in plain language; banner when critical. | `tpm/quality/trust.py` |
| 29 | Detectors: classical ensemble + lightweight autoencoder; choose by unsupervised reliability unless too slow for the budget. | `tpm/detect/ensemble.py` |
| 30 | Sensor-vs-process discrimination via correlation-structure break; plus **cascade / sequential failure** detection. | `tpm/detect/cascade.py` |
| 31 | Critique: code checks + detector cross-checks + LLM devil's advocate. | `tpm/diagnose/critique.py` |
| 32–35 | No normal data is given; the **whole dataset is the object of analysis**. GroupKFold out-of-fold scoring: every row is scored by a model not fitted on it. Internal train/validation splits by group for model selection and threshold calibration. Leakage guard for near-duplicate segments. | `tpm/detect/ensemble.py`, `tpm/detect/splits.py` |
| 36 | Iterate over several detectors/classifiers; pick the most reliable (stability across folds + agreement). | ensemble |
| 37 | Show all metrics, operator-first; localize faults, show propagation impact; clean UI. | UI, report |
| 38 | Assessor: combined score, leading with ML fitness and coverage. | `tpm/assessor` |
| 39 | "More data" = held-back pool **and** uploaded files; learning curves are the estimator. | assessor |
| 40 | Assessor evaluates any natural-language action but recommends only when evidence supports it; nothing applied without approval. | assessor + agent |
| 41 | Precompute at training + on-demand bounded experiments. | assessor |
| 42 | Advise + apply with approval, logged. | assessor + decision log |
| 43 | Custom UI: FastAPI + hand-built modern frontend (no Streamlit / no build step), following `docs/skills/frontend-design.md`. | `tpm/api/static` |
| 44 | Roles: operator / engineer / reviewer with role-dependent views. | UI |
| 45 | Overrides feed back into later stages and are logged. | decision log + stages |
| 46 | SQLite log with hash chain + JSONL export; HTML report; EN/FI/SV; email. | `tpm/log`, `tpm/report` |
| 47–48 | No extra-domain runs. Adaptability shown by an architectural walkthrough plus the schema-agnostic pipeline (a synthetic-records fixture exists for tests only). | `docs/ADAPTABILITY.md` |
| 49 | Push code to `main` of github.com/tommy2006/abarofchocolate. | git |
| 50 | Least setup for judges: launcher scripts, no Docker required; app works without Ollama (template mode) and without an API key. | launcher |

## 2026-09-19 (team, round 4): hybrid profile, local models, summary-first UI, Windows app

- **Hybrid = limited, anonymised calls to Claude.** Raw input data never goes to the API; aggregated, rounded
  (3 significant digits) or anonymised (aliased) derived data may. Models: `claude-sonnet-5` or `claude-opus-5`.
  **Never Fable / Mythos** (30-day data retention): refused in code, whatever the configuration says. Calls are
  capped per run and per chat answer; every attempt is in the egress ledger. Contract: `docs/HYBRID_SPEC.md`.
- **Chat may use the external model in hybrid**, because the chat model only ever sees sanitised aggregates (no SQL
  tool, no min/max, bucket means over >= 30 rows).
- **eu-hosted refuses the first-party Anthropic endpoint** (it has no EU processing).
- **No fixed local model.** The app uses the chosen model, else the configured default, else the best installed
  model that fits the machine; models and Ollama itself can be downloaded from the app.
- **Summary first.** Every page and every explanation starts with a short plain summary and next steps; all
  previous output sits under "Show technical analyses".
- **Signals can be renamed by a person** ("S44" -> "possibly broken"): a logged decision, shown everywhere as
  "possibly broken (S44)".
- **Windows installer** (per user, no admin rights): `docs/WINDOWS_APP.md`.

