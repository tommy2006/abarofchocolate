<p align="center"><img src="graphics/norrin-favicon-512.png" alt="Norrin Trustworthy Process Monitor" width="96"></p>

# Norrin Trustworthy Process Monitor (TPM)

**What it does.** You give it a table of sensor readings from a plant - any CSV, TSV, Excel, Parquet or `.dat`
file, with or without column names, gigabytes if needed - and it tells you, in plain language:

1. **What the columns are** - which ones are sensors, what each sensor probably measures (pressure, flow,
   temperature, ...), and how the sensors move together. All of this is inferred from the data itself.
2. **Whether the data can be trusted** - frozen sensors, missing values, unit slips, duplicated or out-of-order
   rows, single odd readings. Bad data is set aside so it is not mistaken for a real fault.
3. **When the process behaved unusually** - drift, sudden changes, single glitches, with the sensors responsible.
4. **Why** - a ranked, evidence-backed diagnosis for every event (a real process change, a sensor problem, a data
   problem, or unclear), challenged by a built-in critique step, and **what to do about it**.
5. **Whether more data would help** - the assessor tells you where the model is weak.
6. **A report** (HTML, PDF, PowerPoint, e-mail) and a **live monitor** that watches a running feed and warns when
   readings drift towards a known failure type.

Every page starts with a short plain summary and the next steps; the charts, tables and evidence sit behind
"Show technical analyses". Three modes - **Basic** (only the essentials), **Operator**, **Engineer** (everything,
including the decision log) - each with its own colour. A person can accept, question or override every
conclusion, rename a sensor, type operating rules in plain language, and ask "why?" in a chat; every decision is
recorded in a tamper-evident log.

**Data sovereignty.** Raw readings never leave the computer. The AI helper is a local model (Ollama); the optional
*hybrid* profile sends only aggregated, rounded and anonymised results to Claude, through a guard that checks every
payload first. Details in [docs/DATAFLOW.md](docs/DATAFLOW.md).

Built by team *abarofchocolate* for the Norrin "Trustworthy process monitor" challenge (September 2026).
What we changed after an expert reviewed our first output: [What changed after the expert review](#what-changed-after-the-expert-review-round-6).

---

## Install on Windows (no Python needed)

1. Download **`dist/NorrinTPM-Setup.exe`** from this repository (about 190 MB; stored with git LFS - use the
   "Download" button on GitHub or `git lfs pull` after cloning).
2. Run it. Windows may show "Windows protected your PC" because the file is not code-signed: choose
   *More info > Run anyway*. Keep the defaults and press **Install** (per user, no administrator rights).
3. The app starts by itself and appears in the Start menu and on the desktop as
   **Norrin Trustworthy Process Monitor**. A small control window shows it is running and opens the app in its
   own window; closing the control window stops the app.
4. First analysis: on page **0 Runs**, drop your file (or press "Create a demo run" to try synthetic data), then
   follow pages 1 to 6 in order. Page **7 Live monitor** watches a live feed.
5. Optional local AI: click **Local model** in the top bar. The panel installs Ollama, downloads a model and
   lets you pick any installed one. Without it the app still works; explanations then come from templates.

Your analyses and settings are kept in `%LOCALAPPDATA%\NorrinTPM`; the program is in
`%LOCALAPPDATA%\Programs\NorrinTPM`. Uninstall from *Settings > Apps > Installed apps*. More in
[docs/WINDOWS_APP.md](docs/WINDOWS_APP.md) (also how to rebuild the setup file).

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

The left bar is the path through an analysis, in order, **0 to 7**; everything that is not a step lives under
**Settings** (the gear at the bottom). Every page opens with a short plain summary and what to do next; the charts,
tables and evidence follow, and in Operator mode the full detail sits behind **Show technical analyses**.
Wherever something is wrong, the page shows it as **Problem -> Reason -> What to do**: the faulty data first, then
why it is faulty in plain words, then concrete steps to fix it.

| # | Page | What it shows | Challenge output |
|---|---|---|---|
| 0 | **Runs** | drop a file (any size, with a progress screen), watch the stages run, the last analyses | - |
| 1 | **Understanding** | what each sensor probably measures (pressure, flow, temperature ...), how sure the app is and why, as cards you can accept or correct; a **network diagram of how the sensors move together** (who leads, who follows); rename a sensor ("S44" -> "possibly broken") | 1 |
| 2 | **Data quality** | a **map of where the faulty data is** (batches x kinds of check), the worst pieces as Problem -> Reason -> What to do, **which checks ran on each batch** (pass / warn / fail), and what the **% score means in plain words**; operating rules typed in plain language become extra checks | 2 |
| 3 | **Monitor** | a timeline diagram of unusual behaviour, the sensors involved most often, the strongest events with their explanation and next steps, the list of suspicious single readings ("a glitch or a manipulation; the data alone can't tell") | 3 |
| 4 | **Diagnoses** | findings by likely cause (diagram), each as Problem -> Reason -> What to do, with confidence, the step-by-step reasoning and the critique that challenged it; accept / question / override on every finding | 4, 5 |
| 5 | **Assessor** | ask in plain words whether more or less data would help; learning curve and recommended actions | bonus |
| 6 | **Report** | HTML, PDF and PowerPoint in English, Finnish or Swedish; send by e-mail; export the run | all |
| 7 | **Live monitor** | watches a file that keeps growing; warns when sensors drift **towards a known failure type** (imminent or occurring) and shows **Alarm -> Problem -> Cause -> Suggestion**; see [Live monitor](#live-monitor) | bonus |
| - | **Settings** | person and mode; display (colours, text size, language); AI models (choose, download, install Ollama); **Data flow & privacy** (what may leave the machine, the egress ledger); **Decision log** (Engineer mode: every decision, hash-chain verification, export) | 6, 8 |

**Three modes**, each with its own colour scheme across the whole app (pick it with your name in the top bar):

- **Basic** (blue): by far the least on screen - the summary, the next steps, one diagram and the top problems.
  No lists, no tables.
- **Operator** (green): the everyday view, with the diagrams, and all detail behind "Show technical analyses".
- **Engineer** (violet): everything, including the decision log, hash-chain verification, the egress ledger and the
  overrides audit.

**Text size**: the **A-** and **A+** buttons in the top bar make everything smaller or larger; boxes and charts
rearrange themselves. **Chat** (the *Ask* button, or *Ask why* on any item): several chats per analysis, each with
its own context; picking something new to ask about replaces the old context; clear the context, clear a chat's
history, start a new chat, and **Stop** an answer that is still being written.

The HTML report (`workspace/<run_id>/report_<lang>.html`) contains all eight expected outputs in one printable
page: sensor understanding, data-quality checks, drift monitoring with score sparklines, root-cause diagnoses with
critique, human decisions (before / after), the decision log with chain verification, an adaptability section and
the data-flow record with an inline diagram and the egress ledger. It is template-generated; if a language model is
available, a clearly labelled "model-written summary" is added on top.

A five-minute judging walkthrough is in [docs/JUDGES_GUIDE.md](docs/JUDGES_GUIDE.md).

---

## Live monitor

The rail's last page, **7 Live monitor**, watches a data file that keeps getting new lines (a plant writing about one row per
second) and re-analyses it every cycle. It is independent of the run pipeline: no run is needed and it never touches one.
It has two tabs.

**Monitor** shows the verdict banner, counts, a table of every sensor and a history graph. Each sensor is judged on four
checks, not one number: *level* (how far the cycle average is from what it learned as normal), *trend* (steadily moving away,
inside the cycle or over the last three), *noise* (more jumpy or unusually quiet) and *range* (readings outside the min-max
seen while learning), plus *dead* (value never changes) and *missing*. A broken sensor is reported as a sensor problem,
separate from a process drift, and a stream that stops or has gaps is reported as "data not trusted".

**Settings** has three parts:

1. **Sensitivity**: how far a sensor may move from normal before it is flagged (watch / alarm, as % of its normal level or
   in normal spreads, plus noise and range limits). Type the numbers, or press **Let the AI decide**: the local model picks
   them from a summary of how each sensor behaved while learning, inside safe limits, and explains its choice.
2. **Simulate with a file**: replay any CSV, even a multi-gigabyte one, at a chosen speed and from a chosen start row. Drop
   the file on the page or type its full path (no copy is made then). A built-in demo plant needs no file.
3. **Live data**: paste a link (http / https) to a CSV that grows, or the path of a CSV another program keeps writing. Only
   the new lines are read.

**Known failure types.** Besides generic drift, the monitor compares how the sensors move with a catalogue of
known failure types (`config/failure_signatures.yaml`, built from the Tennessee Eastman fault list in `message.txt`:
which sensors are affected and how - mean shift, larger swings, fast collapse, slow drift, a sticking valve, a bump
that fades). Every cycle it scores each type from the sensors' behaviour and says whether a failure is **imminent**
(moving towards it and getting closer) or **occurring**. Types the list calls "essentially invisible" are shown as such
and never alarmed; types whose sensors are not in your file are "not applicable". When an alarm goes off, one card at
the top reads left to right: **Alarm** (what tripped, since when) -> **Problem** (which sensors behave how, with small
charts) -> **Cause** (the matching failure type and how sure, or "no known type matches: generic drift") ->
**Suggestion** (what to check or do). Add your own failure types by dropping a YAML file of the same shape into
`workspace/_live/signatures/`. The failure list never goes to a language model.

To see it: on page 7 press **Demo: a known failure type** (sensors named like the Tennessee Eastman plant; after the
learning cycles they drift like Fault 1) or replay any file with **Inject a known failure type** in Simulate.

Any CSV works: every numeric column that is not a time, id or label column is a sensor. The learned baseline needs the first
cycles to be normal operation. Data control: raw rows stay in `workspace/_live`; the model (local only, through the egress
ledger path) sees only per-sensor summaries; the only network traffic is downloading from a link you paste, nothing is sent
out. Settings, the source and a decision log (`log.jsonl`) live in `workspace/_live/`. API: `/api/live/*`; code: `tpm/live/`;
page: `tpm/api/static/js/views/live.js`.

## Command line reference

`python -m tpm <command>` (use the `.venv` interpreter, or activate the venv first).

| Command | What it does |
|---|---|
| `run <path> [--profile no-egress\|hybrid\|eu-hosted] [--stages ingest,profile,…] [--opt k=v …] [--rules FILE] [--lang en\|fi\|sv] [--run-id ID] [--no-llm] [--continue-on-error] [--quiet]` | Runs the pipeline with a live progress display (stage, %, message, elapsed vs. time budget), then prints a stage summary, the workspace path and the report path. Exit code 1 if a stage failed. |
| `serve [--host 127.0.0.1] [--port 8000] [--open]` | Starts the web UI (uvicorn). |
| `demo [--no-llm] [--lang …]` | Generates `samples/` if missing, runs the full pipeline on `samples/demo_process.csv` with `config/rules.example.md` as rules, prints how to open the UI. |
| `replay <run_id> [--speed S] [--max-batches N]` | Replays a run's dataset as a stream of batches through the batch path (checks → trust → scoring → diagnoses). `--speed` is data-seconds per wall-clock second; 0 = as fast as possible. |
| `report <run_id> [--format html\|pdf\|pptx\|summary\|all] [--lang en\|fi\|sv\|all] [--out FILE\|DIR] [--no-llm] [--pdf-engine native\|browser]` | (Re)generates the report: HTML (default), a typeset PDF, a PowerPoint deck, a one-page summary PDF, or all of them. `--out` is a file for one language and one format, otherwise a directory. `latest` works as a run id. |
| `email <run_id> --to a@b.c[,d@e.f] [--lang …] [--subject …] [--pdf] [--pptx]` | E-mails the HTML report, optionally with the PDF / the deck attached; needs `TPM_SMTP_*` in `.env` (clear error otherwise). |
| `export <run_id> [--out DIR] [--lang …]` | Writes `<run_id>_export.zip`: reports (HTML, PDF, PowerPoint), `decision_log.jsonl`, `egress_ledger.jsonl`, `verify.json`, derived JSON/JSONL artifacts. Never the raw data. |
| `verify-log <run_id>` | Recomputes the SHA-256 hash chain of the decision log (exit code 1 if broken) and lists, per kind of object, what has an entry of its own and why anything does not. |
| `showcase --run <run_id> [--rules FILE] [--no-chat]` | On a finished run: compiles rules into checks and runs them, accepts / questions / overrides three diagnoses and shows the effect on a later event, asks the why-chat one question, regenerates the report. |
| `guard-demo --run <run_id> [--profile hybrid\|eu-hosted] [--send]` | Shows the egress guard on the run's own data: a real message before and after, and a deliberately unsafe message of raw rows that is blocked. Nothing is sent unless `--send` (then only the safe message, once). |
| `models` | Which local models are in use and why, everything installed, external availability. `--pull NAME` downloads a model, `--use NAME [--embedding]` chooses one (`auto` = automatic). |
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
(`TPM_PROFILE`, `TPM_LOCAL_MODEL`, `OLLAMA_HOST`, `TPM_EXTERNAL_MODEL`, `TPM_EXTERNAL_BASE_URL`, `ANTHROPIC_API_KEY`, `ANTHROPIC_WORKSPACE_ID`,
`TPM_EU_API_KEY` and optionally `TPM_EU_BASE_URL` / `TPM_EU_MODEL` / `TPM_EU_PROVIDER` for eu-hosted, `TPM_WORKSPACE`, `TPM_TIME_BUDGET_S`, `TPM_SMTP_*`).

| Profile | External calls | What may leave the machine |
|---|---|---|
| `no-egress` (default) | none | nothing; every model task runs on the local Ollama model or on code templates |
| `hybrid` | the model tasks that work on derived results (sensor hypotheses, rule compilation, diagnosis narrative, critique, report summary) **and the chat** go to Claude through the **egress guard**; everything that reads raw data stays local | aggregated, rounded and anonymised results only (see below) - never rows, never exact readings |
| `eu-hosted` | same routing as hybrid, but the external model runs in the EU: **Mistral Large 3 on the hackathon's Verda GPU containers in Finland** (OpenAI-compatible endpoint in `profiles.eu-hosted.external_llm`, key in `TPM_EU_API_KEY`). Only services on `profiles.eu-hosted.eu_hosts` are accepted (Verda, Mistral's EU endpoint, Claude on Bedrock Stockholm / Ireland); the first-party Anthropic API (no EU processing), US regions and worldwide inference profiles are refused. Guard in strict mode; every call's ledger record names the host it went to | same, plus operator notes are dropped |

**What the hybrid profile sends, and what it never sends.** Every outgoing payload is sanitised and then checked
against an invariant just before sending; a payload that fails the check is not sent:

- column names become aliases (`S01`...), also inside sentences and in chat questions;
- every decimal number is rounded to 3 significant digits, also inside sentences; single raw readings
  (`min`, `max`, `first`, `value`, points, series, rows) are removed; aggregates over fewer than 30 samples are removed;
- dates, times and epoch time stamps become `[time]`; values of text / category / label columns become `[value]`;
  file names become `[file]`; label-based evaluation never leaves;
- the chat model gets no SQL tool; its statistics tool returns aggregates without min/max, its series tool at most
  20 bucket means over at least 30 rows each. Full-detail series go to your screen only.

**Limited calls.** Models: `claude-sonnet-5` (default, fastest) or `claude-opus-5`, chosen in the Data-flow page.
Fable / Mythos models are refused in code (30-day data retention). Per run at most 200 external calls and
120,000 output tokens (`external_llm.max_calls_per_run`, `max_output_tokens_per_run`), at most 6 calls per chat
answer; when a limit is reached, or the guard or the API refuses, the task runs on the local model instead.
Every attempt - sent, refused, over budget, failed - is in the egress ledger with the sanitised preview and token
counts, shown on the Data-flow page. An organisation-level API key also needs `ANTHROPIC_WORKSPACE_ID` in `.env`.

**What it speeds up - measured, not assumed.** On the 6 GB practice file (this laptop, 1355 s in total) 89 % of
the time is number crunching on raw data (ingest, quality checks, detection). That must stay local and cannot be
outsourced. The model calls were 149 s (5 calls, one after the other).

Real benchmark on 2026-09-19 (`python -m tpm bench-llm`, RTX 4060 laptop, local `gemma4:e4b-it-qat` vs
`claude-sonnet-5`, median of 2 calls, same sanitised payloads):

| Task | Local | Claude Sonnet 5 | |
|---|---|---|---|
| Sensor hypotheses (largest payload) | 35.8 s | 19.6 s | 1.8x faster |
| Diagnosis narrative | 10.1 s | 14.6 s | slower |
| Critique | 12.6 s | 17.8 s | slower |
| Report summary | 16.2 s | 15.7 s | same |
| One chat answer (tool agent) | 31.7 s | 17.0 s | 1.9x faster |
| **4 calls at once** | one after the other (about 65 s) | **19.5 s wall clock** | 3.3x faster |

So a single short call is *not* faster over the network than on a local GPU. The gain is that external calls run
side by side: the diagnose stage writes narratives and critiques for up to 12 findings concurrently instead of 2
sequentially, and chat answers arrive in about half the time. For a full 6 GB analysis this saves roughly two of
about twenty-two minutes; the rest is local computation by design. The benchmark used 16 calls
(88 k input and 22 k output tokens). Repeat it on your machine:
`python -m tpm bench-llm --run <run_id> --profile hybrid` (writes `llm_benchmark.json`, shown on the Data-flow page).

Switch with `TPM_PROFILE=hybrid`, `--profile hybrid`, or on the Data-flow page. Details: [docs/DATAFLOW.md](docs/DATAFLOW.md),
contract of the feature: [docs/HYBRID_SPEC.md](docs/HYBRID_SPEC.md).

---|---|---|
| `no-egress` (default) | none | nothing; every model task runs on the local Ollama model or on code templates |
| `hybrid` | derived-artifact tasks (sensor hypotheses, rule compilation, diagnosis narrative, critique, report narrative) go to Anthropic through the **egress guard**; raw-data tasks stay local | signal-catalog aggregates, relation summaries, check / flag / diagnosis statements, rule text — never rows |
| `eu-hosted` | same routing as hybrid against Mistral Large 3 on Verda in Finland (`profiles.eu-hosted.external_llm`, key `TPM_EU_API_KEY`), guard in strict mode | same, with column names replaced by aliases |

Switch with `TPM_PROFILE=hybrid`, `--profile hybrid`, or in the UI settings. Every external call is written to the
egress ledger (what was sent, to which model, why, guard result). Details: [docs/DATAFLOW.md](docs/DATAFLOW.md).

---

## Local model (optional)

The pipeline is fully functional without any language model: every narrative has a code-generated template
version. A local model adds hypotheses, rule compilation from free text, narratives, the critique and the "why"
chat, all without network egress.

The app does not expect one particular model. It uses, in this order: the model you chose, the configured default
(`gemma4:e4b-it-qat`, 6 GB, fits an 8 GB GPU), a configured fallback, or otherwise **the best chat model that is
installed and fits this computer's memory**. The same goes for the search (embedding) model
(default `nomic-embed-text`; without one, search uses word matching).

In the app: click **Local model** in the top bar. The panel shows what is installed and which model is in use and
why, lets you choose another one, **download any model from the Ollama library with a progress bar**, start Ollama,
and on a Windows computer without Ollama **download and open the official Ollama installer** (its digital
signature is checked first). On a fresh computer the panel opens by itself and offers "Download the two standard
models". These downloads fetch software only; none of your data is sent.

From the command line:
```
python -m tpm models                      # models in use and why, everything installed
python -m tpm models --pull qwen3:4b      # download through the local Ollama, with progress
python -m tpm models --use qwen3:4b       # use it from now on   (--use auto = back to automatic)
python -m tpm models --use all-minilm --embedding
```
A choice made in the app or with `--use` is stored in `config/settings.yaml` (`local_llm.model_selected_by: user`)
and then wins over `TPM_LOCAL_MODEL` in `.env`. `run.ps1 -PullModels` / `run.sh --pull-models` pull the configured
default for you. `python -m tpm bakeoff` compares the pulled models on JSON validity, schema compliance, tool calls
and latency.

## Windows app (installer)

`dist/NorrinTPM-Setup.exe` installs the monitor like any Windows program (Start menu, desktop shortcut,
"Installed apps" entry with uninstaller; per user, no administrator rights, no Python needed). Build it with
`powershell -ExecutionPolicy Bypass -File packaging/windows/build.ps1`. Details: [docs/WINDOWS_APP.md](docs/WINDOWS_APP.md).

---

## Report, export, e-mail

- `python -m tpm report <run_id> --lang all` writes `report_en.html`, `report_fi.html`, `report_sv.html` into the run
  folder.
- **PDF and PowerPoint.** `python -m tpm report <run_id> --format pdf` (or `pptx`, or `all`; with `--lang` and
  `--out`) writes `report_<lang>.pdf` / `report_<lang>.pptx`. In the app, the Report view has **Download PDF** and
  **Download PowerPoint** next to Open / Download HTML; the API routes are
  `GET /api/runs/<run_id>/report.pdf?lang=fi` and `GET /api/runs/<run_id>/report.pptx?lang=fi` (file name
  `tpm_<run_id>_<lang>.pdf|pptx`). Both are generated on demand from the same findings as the HTML report, cached per
  language, rebuilt when an artifact or a human decision changes, and never wait for the language model (a
  model-written summary is included only when the HTML report already has one). A few seconds even for a run with
  thousands of flags; large lists are capped to the most severe rows and the totals are stated.
  - The **PDF** is a typeset A4 document (ReportLab, pure Python, works offline): title page with the headline numbers,
    contents page, the eight report sections plus suspicious rows, evaluation, assessor and the labelled model summary,
    running header / footer with the run id and "page x of y", tables with repeating header rows, vector charts
    (score timelines with threshold and flagged spans, pass / warn / fail bars, trust by batch, contribution bars,
    learning curve, data-flow diagram). Bitstream Vera is embedded, so ä / ö / å print correctly on any machine.
    `--pdf-engine browser` prints the HTML report with a headless Edge / Chrome instead, when one is installed.
  - The **deck** is a 16:9 presentation of 10 to 16 slides (python-pptx): summary, what was analysed, sensor
    understanding, data trust, monitoring, suspicious rows, the top diagnoses, fault patterns, human decisions and log
    integrity, data-flow record, assessor verdict, evaluation, open points and next steps. Charts and tables are native
    PowerPoint objects (editable, with their data), text is fitted to every box (cut at a sentence boundary with "…"
    and a note pointing to the report), and the speaker notes list the evidence ids behind each slide.
  - Neither file contains raw rows: both are drawn from the derived artifacts and bucketed aggregates of the report.
- **One-page summary.** `python -m tpm report <run_id> --format summary` (or the **One-page summary (PDF)** button on
  the Report page) writes `summary_<lang>.pdf`: one A4 page to share, with what was found, how sure the findings are
  and what to do next.
- `python -m tpm export <run_id>` bundles the reports (HTML, PDF, PowerPoint), the decision log (JSONL), the egress
  ledger, the chain verification result and all derived artifacts into `exports/<run_id>_export.zip`.
- `python -m tpm email <run_id> --to someone@example.org --lang fi [--pdf] [--pptx]` sends the HTML report (`--pdf` /
  `--pptx` attach the PDF / the deck as well). In the UI: Report view → *Send by email*, with *Attach PDF* and *Attach
  PowerPoint* ticked by default. Set in `.env`:
  `TPM_SMTP_HOST`, `TPM_SMTP_PORT` (465 / 2465 SSL, 587 / 2587 STARTTLS), `TPM_SMTP_USER`, `TPM_SMTP_PASSWORD`,
  `TPM_SMTP_FROM`. Resend works as is: host `smtp.resend.com`, user `resend`, password = the Resend API key, sender
  `onboarding@resend.dev` (which delivers only to the address of your Resend account until you verify a domain).
  `python -m tpm doctor` checks the settings and that the server is reachable.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `run.ps1` will not start ("running scripts is disabled") | use `run.bat`, or `powershell -ExecutionPolicy Bypass -File run.ps1` |
| Python not found | install 3.10+ from python.org (tick "Add to PATH"), re-run the launcher |
| `pip install` fails | check the network / proxy; `python -m pip install -r requirements.txt` shows the full error |
| Port 8000 in use | `.\run.ps1 -Port 8080` / `./run.sh --port 8080` |
| "Ollama not reachable" | optional; in the app click **Local model** in the top bar (install / start Ollama, download a model); the app works in template mode meanwhile |
| Not enough RAM | close other applications; a local 6 GB model plus the pipeline wants ~8 GB free; detection subsamples automatically |
| A stage shows `failed` | the error is in `workspace/<run_id>/status.json` and the decision log; `python -m tpm run … --continue-on-error` keeps going |
| Wrong delimiter / header / grouping | `--opt delimiter=; --opt has_header=false --opt group_columns=col1`, or set them on the Runs page |
| E-mail fails | `TPM_SMTP_HOST` etc. must be in `.env`; `python -m tpm doctor` lists what is missing and tests the connection. Resend's "550 You can only send testing emails to your own email address": with `onboarding@resend.dev` send to your Resend account's address, or verify a domain |
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
  live/                       live sensor monitor: engine (four checks), monitor (sources, loop, AI-chosen sensitivity), /api/live routes
  log/                        hash-chained decision log, exports
  report/                     HTML report (Jinja2, inline SVG), PDF (pdf.py), PowerPoint (pptx_export.py), i18n EN/FI/SV, e-mail
config/settings.yaml          all tunables and profiles;  config/rules.example.md  example rules
samples/                      small synthetic demo files (scripts/make_samples.py)
docs/                         DECISIONS, ARCHITECTURE, DATAFLOW, ADAPTABILITY, EVALUATION, JUDGES_GUIDE, worklog/
tests/                        pytest (tests/fixtures/synth.py is the shared synthetic generator)
workspace/<run_id>/           every artifact of a run (git-ignored)
```

Team decisions and their config keys: [docs/DECISIONS.md](docs/DECISIONS.md). Module boundaries and artifact
contracts: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Evaluation method: [docs/EVALUATION.md](docs/EVALUATION.md).

## What changed after the expert review (round 6)

An expert reviewed our output on the 6 GB practice file and listed 30 fixes. What the app does differently now,
measured on the same file:

| Review point | Before | Now |
|---|---|---|
| Data problems and saturated valves called "sensor faults" | 139 "sensor fault on S44" | 0 sensor faults. S44 is recognised as a valve (controller output) from its 0-100 % range and how it reacts to other signals, and the same events read "actuator saturation: S44 pinned at its upper limit", a process symptom with what to check upstream. Several signals frozen in the same rows, or duplicated rows, are a data problem and no sensor is blamed |
| False alarms | 14.9 % of normal runs flagged, because a single reading above the threshold counted | 0 of 1,000 normal runs: an event needs a lasting rise. 65 % of faulty runs detected; precision and recall are printed in the report |
| A critique that always agrees | supported 3,856 of 3,857 | argues four alternatives from the evidence (data problem, process change, saturated actuator, single broken sensor); weakened 912, every disagreement logged |
| The same "88 %" everywhere | one number | labelled as a heuristic score, explained, and checked against labels when they exist (calibration table); data-quality checks carry their own certainty |
| Flat attribution | 9 %, 9 %, 4 % ... as a precise list | "no single signal dominates: about 11 signals of cluster C02", plus one lag reference per event and upstream / downstream wording |
| Unnamed patterns | "PATTERN-A (unnamed)" | named from the plant's list of known failure types when enough of its sensors lead, e.g. "possibly Fault 6: A feed loss", otherwise "cannot name" with the closest candidate |
| Data quality | 20 frozen signals = 20 findings; trust verdicts that never failed | one grouped finding for signals frozen together, duplicates and frozen blocks now make a batch untrusted, plausible ranges per signal, and timeliness says "not testable" instead of a fake pass |
| Rules, human in the loop, why-chat | described | `python -m tpm showcase --run <id>` does them for real: rules compiled into checks with pass / fail on every batch; one diagnosis accepted, one questioned, one overridden, and a later event of the same kind takes the person's label; one question answered by the local model |
| Privacy guard | promised | shown on the run's own data (Data flow page or `python -m tpm guard-demo --run <id>`): a real message before and after the guard, and a deliberately unsafe message of raw rows that is blocked; original column names never leave, also on narrow tables |
| Decision log | stage summaries | every flag, check, verdict, diagnosis, critique, inference and model call has an entry of its own; the Log page and `verify-log` show any gap and why |
| Other domains | argued | business records (`samples/demo_records.csv`) and a web-service log (`samples/demo_log.csv`) run end to end; results in [docs/ADAPTABILITY.md](docs/ADAPTABILITY.md) |
| Report | the top 100 diagnoses of one kind | a varied sample of diagnoses, a 3-sentence headline with the steps folded, drift trends with the normal band, a correlation heat map with the lead / lag summary, why this baseline, and a **one-page summary PDF** |
| Sensor understanding | "shared unit operation of cluster C05" | clusters named by the local model with their evidence and what would disprove it ("feed system", "stripper"); valves tested against the signals they drive |

Details: [docs/worklog/round6_D.md](docs/worklog/round6_D.md) (data quality) and
[docs/worklog/round6_E.md](docs/worklog/round6_E.md) (logging, guard, data flow).

## Tests

```
.venv\Scripts\python.exe -m pytest tests -q
```

## Validation on a large file

The pipeline was run end to end on the 6 GB practice file (15.3 million rows, 57 columns, 21,000 runs) on a 16 GB
laptop with an 8 GB GPU, without the language model: ingest 2.7 min, profile 1.1, quality 2.6, detect 11.5,
diagnose 0.4, assessor 1.8, report 0.2, about 20 minutes in total. The file's label columns were detected and kept
out of detection. Used only afterwards, to evaluate, they gave:

| Measure | Result |
|---|---|
| Normal runs with a false alarm | 0 of 1,000 |
| Faulty runs detected | 65 % (13,084 of 20,000) |
| Precision of the flagged rows | 1.00 |
| Recall of the labelled-faulty rows | 0.42 |
| Median delay to the first flag | 167 rows |
| Ranking quality of the anomaly score (AUROC) | 0.85 |
| Recurring patterns vs the hidden fault numbers (adjusted mutual information) | 0.66 |

An event needs a lasting rise: the median score of 20 consecutive rows must reach a threshold calibrated on
held-out normal stretches (0.55 on this file); one or two high readings alone are point findings. Every number comes
from `workspace/<run>/evaluation.json`; the detection itself never reads the labels.

