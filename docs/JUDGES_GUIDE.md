# Judges' guide — a five-minute walkthrough

Everything below runs on your machine, without an account or an API key. Total time: about five minutes after the
one-time dependency install.

## 0. Start (1 minute of typing, a few minutes of installing)

```
Windows:      double-click run.bat            (or  .\run.ps1 -Demo)
macOS/Linux:  ./run.sh --demo
```

`-Demo` / `--demo` generates a synthetic dataset (`samples/demo_process.csv`: 20 runs x 400 rows, 12 signals, six
fault types and six data-quality defects, no labels, no documentation), runs the whole pipeline on it with
`config/rules.example.md` as operating rules, and then opens the UI at http://127.0.0.1:8000. The console shows
each stage with progress and elapsed time versus the time budget, then the workspace path and the report path.

To use your own file instead: drop it on the **Runs** page (or `python -m tpm run <file>`). Headers may be absent;
nothing needs to be labelled.

## 1. Sensor understanding report — *Understanding* view, report section 1

- Every column is addressed by an alias (`S01 … Snn`). Column headers are shown only as tooltips and count as weak
  evidence: the roles are inferred from fingerprints (distribution, noise level, autocorrelation, stuck fraction,
  quantisation) and from lagged cross-correlations.
- Each signal card shows: structural role (continuously measured / actuator-like / sample-and-hold / constant /
  derived / counter / timestamp / categorical …) with confidence, instrument and unit-operation **hypotheses** with
  their own lower confidence, the evidence statements (`EV-000123: S02 and S03 correlate r=0.81 at lag 2`), and a
  "what is uncertain" list. Click an evidence ID to see the numbers behind it.
- The dataset panel states the inferred sample period, the grouping strategy that won (several were scored), and
  which columns were auto-detected as labels / identifiers and **excluded from detection**.
- Try: change a role with *override*, add a note. The change is logged (section 5 / 6) and used by later stages.

## 2. Automated data quality checks — *Data quality* view, report section 2

- Baseline checks per batch (5-minute windows when a timestamp exists, else 10 % of rows): completeness (missing
  blocks), validity (out-of-range spikes, unit shifts), consistency (frozen / stuck sensors, duplicates),
  timeliness (timestamp gaps). Each check has pass / warn / fail, a severity, the responsible signals, rows, and
  evidence IDs.
- The **trust verdict** per batch: when too many signals fail, the batch is marked "data cannot be trusted" and
  every downstream flag for that batch carries lower confidence (a red banner in the UI).
- **Rules**: type a plain-language rule in the rule box ("S03 must stay between 100 and 140", "S07 must not change
  by more than 5 per sample", "S02 acceleration must not exceed 3 units per sample squared", cross-signal, rolling
  statistics, missing / stale …). It is compiled into a closed JSON check spec (never arbitrary code), shown for
  approval, and every resulting check carries the rule ID for traceability. `--rules FILE` does the same from the CLI.

## 3. Drift and anomaly detection — *Monitor* view, report section 3

- No "normal" data is given: the baseline regime is chosen automatically (consensus of per-signal density modes,
  pre-change-point segments, densest windows) and its assumptions are listed. An operator may set a reference
  period instead.
- Every row is scored **out-of-fold** (GroupKFold over the detected groups; a row is never scored by a model that
  saw its own group). The timeline shows the ensemble score per group with the threshold; flags mark where the
  score stays above it, and each flag lists the responsible signals with their share, direction and lag — never an
  unexplained aggregate.
- Change points give the onset (abrupt vs. gradual, first- and second-order), patterns cluster similar events
  (`PATTERN-A …`, nameable by the operator), and cascade detection orders propagation between signal clusters.
- Sensor vs. process: one signal breaking its correlation structure is reported as a sensor / data problem, several
  correlated signals moving together as a process fault.

## 4. Root-cause diagnosis — *Diagnoses* view, report section 4

- Fault type (pattern name or "unnamed pattern"), cause class (process / sensor / data / mixed / unknown), ranked
  signals with a plain-language reason for each, the propagation chain, a numbered step-by-step explanation for a
  non-expert, confidence, and explicit uncertainty and assumptions.
- The **critique** panel (bonus): code cross-checks (lead/lag consistency with the relations, batch trust, detector
  agreement) plus a devil's-advocate pass; the verdict (supported / weakened / rejected) and the adjusted confidence
  are shown before the diagnosis is presented.
- Bonus "why" interface: click *why?* on any flag or diagnosis to ask a question in natural language. The local tool
  agent answers from the evidence and can query the raw data on this machine; the answer cites evidence IDs.

## 5. Human-in-the-loop — every card, report section 5

Accept / question / override / dismiss on any inference, flag, diagnosis, rule or pattern, with a name, a role
(operator / engineer / reviewer) and a note. The report lists each decision with the state **before and after**.
Overrides feed back (e.g. a corrected role changes the next batch's checks).

## 6. Decision log — *Decision log* view, report section 6 and appendix

Every inference, check, flag, diagnosis, egress event and human decision is an entry in a SQLite log whose entries
are SHA-256 hash-chained. *Verify chain* (UI) or `python -m tpm verify-log <run_id>` recomputes every hash. Export
as JSONL from the UI or with `python -m tpm export <run_id>`.

## 7. Adaptability — report section 7, [ADAPTABILITY.md](ADAPTABILITY.md)

The pipeline never learns what a reactor is. `samples/demo_records.csv` (an order table with manual-entry errors)
goes through the same stages: run `python -m tpm run samples/demo_records.csv` and look at the same views. The
document explains which stages are generic, which adapters change, and walks a "drifting sensor" and a "corrupted
record batch" through the identical pipeline.

## 8. Data-flow record — *Data flow* view, report section 8, [DATAFLOW.md](DATAFLOW.md)

- The default profile is **no-egress**: nothing leaves the machine and no network model is called. The report
  states this explicitly and the egress ledger shows only local calls.
- Switch to **hybrid** (UI settings, `--profile hybrid`, or `TPM_PROFILE=hybrid` with an `ANTHROPIC_API_KEY`) and
  the ledger records every external call: task, purpose, model, artifact types, payload size and hash, a preview of
  what was sent, and the guard result. Payloads that fail the guard (raw-looking series, too many numbers,
  categorical values, record-like structures) are blocked and served locally instead — the ledger shows `blocked`.
- The model layer is swapped by configuration only: `local_llm.model` (Ollama), `external_llm.model` /
  `external_llm.base_url` (EU-hosted endpoint), profile routing per task.

## Bonus items and where to find them

| Bonus | Where |
|---|---|
| No-egress mode: full pipeline on a local open-weight model | default profile; `python -m tpm models` shows the pulled model; ledger shows route = local |
| Confidence / uncertainty throughout | every inference, check, flag, pattern, diagnosis card and report row |
| Natural-language "why" interface | *why?* on any flag / diagnosis; the chat box on Diagnoses and Assessor |
| Visual dashboards of drift over time | Monitor view (score timelines, contributions), report sparklines |
| Critique / review step | Diagnoses view, report section 4 |
| Exportable diagnosis and decision log | Report view → export; `python -m tpm export` |
| Third domain (log / free-text records) | architectural walkthrough in ADAPTABILITY.md (text columns become categorical / text roles with the same detectors on their derived features) |

## If something does not work

`python -m tpm doctor` checks Python, packages, RAM, disk, workspace, Ollama and `.env` and prints the fix for
each problem. The README has a troubleshooting table.
