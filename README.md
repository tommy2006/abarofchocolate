# Trustworthy Process Monitor (TPM)

An autonomous data-reliability pipeline for **undocumented, high-volume tabular data**: it ingests a file or a
stream of batches, infers what every column is (from the data, not from headers), checks whether the data itself
can be trusted, detects drift and anomalies with per-signal attribution, diagnoses the root cause, and keeps a
human able to accept, question or override every conclusion. Raw rows never leave the machine; every inference,
flag, diagnosis and human decision is written to a hash-chained decision log.

Built for the Norrin "Trustworthy process monitor" challenge (September 2026). The primary testbed is an
undocumented industrial sensor dataset; the pipeline is schema-agnostic and runs unchanged on other messy tables
(see [docs/ADAPTABILITY.md](docs/ADAPTABILITY.md)).

---

## 60-second start

Requirements: **Python 3.10+** (3.12 recommended), ~2 GB free disk. No Docker, no API key, no account.
Ollama is optional (it enables model-written explanations; without it the app runs in template mode).

**Windows** — double-click `run.bat` (or in PowerShell: `.\run.ps1`).
**macOS / Linux** — `chmod +x run.sh && ./run.sh`.

The launcher finds Python, creates `.venv`, installs `requirements.txt` (a few minutes the first time), copies
`.env.example` to `.env`, checks for Ollama, starts the web UI at **http://127.0.0.1:8000** and opens your browser.

To also generate synthetic demo data and analyse it before the UI opens:

```
.\run.ps1 -Demo          # Windows
./run.sh --demo          # macOS / Linux
```

Manual equivalent (any OS):

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt      # macOS/Linux: .venv/bin/python
.venv\Scripts\python.exe -m tpm demo                            # synthetic sample -> full pipeline -> HTML report
.venv\Scripts\python.exe -m tpm serve --open                    # web UI
```

Analyse your own file: drop it on the **Runs** page of the UI, or `python -m tpm run path/to/file.csv`.
Formats: CSV / TSV, whitespace-separated `.dat` (with or without header, transposed detection), Parquet, Excel,
JSON / JSONL. Files are processed out-of-core (DuckDB + chunked Parquet), so multi-GB inputs work on a 16 GB laptop.

---

## What you will see

The UI (FastAPI + hand-built frontend, no build step) has one view per expected output. Pick a **role**
(operator / engineer / reviewer) in the header to change what is emphasised; the language switch (EN / FI / SV)
applies to the UI and to the report.

| View | What it shows | Expected output |
|---|---|---|
| **Runs** | upload / replay / watch-folder / HTTP push; stage progress; time budget | — |
| **Understanding** | the signal catalog: alias `S01..Snn`, structural role, instrument / unit-operation hypotheses with confidence, the evidence statements (EV-…) behind each inference, what is uncertain, dataset assumptions (sample period, grouping, excluded label / meta columns) | 1 |
| **Data quality** | baseline checks (completeness, validity, consistency, timeliness) per batch, the trust verdict and "data cannot be trusted" banner, plain-language rules and their compiled checks with pass / warn / fail and rule traceability | 2 |
| **Monitor** | out-of-fold anomaly score timeline per group with threshold, flags with the responsible signals, change points, fault patterns | 3 |
| **Diagnoses** | fault type, ranked signals with plain-language reasons, propagation chain, step-by-step explanation, confidence, uncertainty, the critique that challenged the diagnosis; click "why" on any flag to open a chat grounded in the evidence | 4 |
| every card | accept / question / override / dismiss with a note; overrides feed back into later stages | 5 |
| **Decision log** | every system and human decision with evidence IDs, hash-chain verification, JSONL export | 6 |
| **Data flow** | active profile, what may leave the machine and what never does, the egress ledger of every model call | 8 |
| **Report** | the self-contained HTML report (EN / FI / SV), e-mail it, export the run as a zip | 7 (adaptability section), all |
| **Assessor** | how fit the data is for unsupervised monitoring, learning curve, recommended actions (applied only with approval) | bonus |

The HTML report (`workspace/<run_id>/report_<lang>.html`) contains all eight expected outputs in one printable
page: sensor understanding, data-quality checks, drift monitoring with score sparklines, root-cause diagnoses with
critique, human decisions (before / after), the decision log with chain verification, an adaptability section and
the data-flow record with an inline diagram and the egress ledger. It is template-generated; if a language model is
available, a clearly labelled "model-written summary" is added on top.

A five-minute judging walkthrough is in [docs/JUDGES_GUIDE.md](docs/JUDGES_GUIDE.md).

---

## Command line reference

`python -m tpm <command>` (use the `.venv` interpreter, or activate the venv first).

| Command | What it does |
|---|---|
| `run <path> [--profile no-egress\|hybrid\|eu-hosted] [--stages ingest,profile,…] [--opt k=v …] [--rules FILE] [--lang en\|fi\|sv] [--run-id ID] [--no-llm] [--continue-on-error] [--quiet]` | Runs the pipeline with a live progress display (stage, %, message, elapsed vs. time budget), then prints a stage summary, the workspace path and the report path. Exit code 1 if a stage failed. |
| `serve [--host 127.0.0.1] [--port 8000] [--open]` | Starts the web UI (uvicorn). |
| `demo [--no-llm] [--lang …]` | Generates `samples/` if missing, runs the full pipeline on `samples/demo_process.csv` with `config/rules.example.md` as rules, prints how to open the UI. |
| `replay <run_id> [--speed S] [--max-batches N]` | Replays a run's dataset as a stream of batches through the batch path (checks → trust → scoring → diagnoses). `--speed` is data-seconds per wall-clock second; 0 = as fast as possible. |
| `report <run_id> [--lang en\|fi\|sv\|all] [--out FILE] [--no-llm]` | (Re)generates the HTML report. `latest` works as a run id. |
| `email <run_id> --to a@b.c[,d@e.f] [--lang …] [--subject …]` | E-mails the report; needs `TPM_SMTP_*` in `.env` (clear error otherwise). |
| `export <run_id> [--out DIR] [--lang …]` | Writes `<run_id>_export.zip`: reports, `decision_log.jsonl`, `egress_ledger.jsonl`, `verify.json`, derived JSON/JSONL artifacts. Never the raw data. |
| `verify-log <run_id>` | Recomputes the SHA-256 hash chain of the decision log. Exit code 1 if broken. |
| `models` | Which local models are pulled, the exact `ollama pull` commands for missing ones, and external availability. |
| `bakeoff` | Runs `scripts/bakeoff.py` (local-model comparison on representative tasks). |
| `doctor` | Checks Python, packages, free RAM, disk, workspace writability, Ollama, `.env`, implemented stages; prints fixes. |
| `list` | Lists runs in the workspace. |

Dataset options for `--opt` (all optional, everything is inferred otherwise): `has_header=false`, `delimiter=;`,
`decimal=,`, `encoding=latin-1`, `transposed=true`, `sheet=Sheet1`, `format=csv`, `time_column=<name>`,
`group_columns=a,b`, `domain_hint="chemical process"`, `language=fi`, `report_llm=false`.

Rules file: one plain-language rule per line (`#` comments allowed), e.g. `config/rules.example.md`.

---

## Configuration and privacy profiles

Everything tunable is in `config/settings.yaml`; secrets and machine-specific overrides go in `.env`
(`TPM_PROFILE`, `TPM_LOCAL_MODEL`, `OLLAMA_HOST`, `TPM_EXTERNAL_MODEL`, `TPM_EXTERNAL_BASE_URL`, `ANTHROPIC_API_KEY`,
`TPM_WORKSPACE`, `TPM_TIME_BUDGET_S`, `TPM_SMTP_*`).

| Profile | External calls | What may leave the machine |
|---|---|---|
| `no-egress` (default) | none | nothing; every model task runs on the local Ollama model or on code templates |
| `hybrid` | derived-artifact tasks (sensor hypotheses, rule compilation, diagnosis narrative, critique, report narrative) go to Anthropic through the **egress guard**; raw-data tasks stay local | signal-catalog aggregates, relation summaries, check / flag / diagnosis statements, rule text — never rows |
| `eu-hosted` | same routing as hybrid against an EU-hosted endpoint (`external_llm.base_url`), guard in strict mode | same, with column names replaced by aliases |

Switch with `TPM_PROFILE=hybrid`, `--profile hybrid`, or in the UI settings. Every external call is written to the
egress ledger (what was sent, to which model, why, guard result). Details: [docs/DATAFLOW.md](docs/DATAFLOW.md).

---

## Local model (optional)

The pipeline is fully functional without any language model: every narrative has a code-generated template
version. A local model adds hypotheses, rule compilation from free text, narratives, the critique and the "why"
chat, all without network egress.

1. Install Ollama: https://ollama.com/download
2. Pull the configured model (6 GB, fits an 8 GB GPU) and the embedding model:
   ```
   ollama pull gemma4:e4b-it-qat
   ollama pull nomic-embed-text
   ```
   Smaller alternative: `ollama pull gemma3:4b` then `TPM_LOCAL_MODEL=gemma3:4b` in `.env`.
   Configured fallbacks (`local_llm.fallback_models`): `qwen3:8b`, `granite4.1:8b`, `llama3:8b`, `gemma3:4b`.
3. `python -m tpm models` shows what is pulled and what is missing. `run.ps1 -PullModels` / `run.sh --pull-models`
   pull the configured model for you.
4. `python -m tpm bakeoff` compares the pulled models on JSON validity, schema compliance, tool calls and latency.

---

## Report, export, e-mail

- `python -m tpm report <run_id> --lang all` writes `report_en.html`, `report_fi.html`, `report_sv.html` into the run
  folder. Print to PDF from the browser for a shareable copy.
- `python -m tpm export <run_id>` bundles the reports, the decision log (JSONL), the egress ledger, the chain
  verification result and all derived artifacts into `exports/<run_id>_export.zip`.
- `python -m tpm email <run_id> --to someone@example.org --lang fi` sends the report. Set in `.env`:
  `TPM_SMTP_HOST`, `TPM_SMTP_PORT` (587 STARTTLS, 465 SSL), `TPM_SMTP_USER`, `TPM_SMTP_PASSWORD`, `TPM_SMTP_FROM`.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `run.ps1` will not start ("running scripts is disabled") | use `run.bat`, or `powershell -ExecutionPolicy Bypass -File run.ps1` |
| Python not found | install 3.10+ from python.org (tick "Add to PATH"), re-run the launcher |
| `pip install` fails | check the network / proxy; `python -m pip install -r requirements.txt` shows the full error |
| Port 8000 in use | `.\run.ps1 -Port 8080` / `./run.sh --port 8080` |
| "Ollama not reachable" | optional; install Ollama and `ollama pull gemma4:e4b-it-qat`; the app works in template mode meanwhile |
| Not enough RAM | close other applications; a local 6 GB model plus the pipeline wants ~8 GB free; detection subsamples automatically |
| A stage shows `failed` | the error is in `workspace/<run_id>/status.json` and the decision log; `python -m tpm run … --continue-on-error` keeps going |
| Wrong delimiter / header / grouping | `--opt delimiter=; --opt has_header=false --opt group_columns=col1`, or set them on the Runs page |
| E-mail fails | `TPM_SMTP_HOST` etc. must be in `.env`; `python -m tpm doctor` lists what is missing |
| Anything else | `python -m tpm doctor` |

---

## Project structure

```
run.ps1 / run.bat / run.sh    one-command launchers
tpm/
  cli.py, __main__.py         command line (python -m tpm ...)
  contracts.py                shared pydantic models (Evidence, Inference, SignalDescriptor, Flag, Diagnosis, ...)
  config.py, workspace.py     settings + profiles; run directory with registries and the decision log
  pipeline.py                 orchestrator (ingest -> profile -> quality -> detect -> diagnose -> assess -> report)
  ingest/  profile/           readers, schema, grouping, streaming; fingerprints, relations, structural roles
  quality/ assessor/          baseline checks, trust verdict, rule compiler; data assessor
  detect/  diagnose/          baseline regime, OOF ensemble, change points, patterns, cascade; diagnosis + critique
  llm/                        router, egress guard, ledger, providers (Ollama / Anthropic), local tool agent
  api/                        FastAPI server + static UI
  log/                        hash-chained decision log, exports
  report/                     HTML report (Jinja2, inline SVG), i18n EN/FI/SV, e-mail
config/settings.yaml          all tunables and profiles;  config/rules.example.md  example rules
samples/                      small synthetic demo files (scripts/make_samples.py)
docs/                         DECISIONS, ARCHITECTURE, DATAFLOW, ADAPTABILITY, EVALUATION, JUDGES_GUIDE, worklog/
tests/                        pytest (tests/fixtures/synth.py is the shared synthetic generator)
workspace/<run_id>/           every artifact of a run (git-ignored)
```

Team decisions and their config keys: [docs/DECISIONS.md](docs/DECISIONS.md). Module boundaries and artifact
contracts: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Evaluation method: [docs/EVALUATION.md](docs/EVALUATION.md).

## Tests

```
.venv\Scripts\python.exe -m pytest tests -q
```

## Validation on a large file

The pipeline was run end-to-end on a 6 GB, 15.3-million-row industrial simulation file (57 columns, 21,000 runs) on a
16 GB laptop with an 8 GB GPU: ingest 4.4 min, profile 1.8, quality 2.9, detect 9.3, diagnose 2.2, assessor 1.8,
report 0.2 — about 20 minutes when the machine is otherwise idle. Labels present in that file were auto-detected and
kept out of detection; used for evaluation only they gave: no false alarms on the normal runs (0 % of their rows flagged),
95–99 % of post-onset rows flagged for the strong fault classes, AUROC 0.85, and the unnamed fault patterns aligned with the
hidden fault types (adjusted mutual information 0.60). Every number above comes from `workspace/<run>/evaluation.json`
and `detect_meta.json`; the detection pipeline itself never reads the labels.
