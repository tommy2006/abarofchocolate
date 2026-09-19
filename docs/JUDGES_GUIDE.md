# Judges' guide — a five-minute walkthrough

Everything below runs on your machine, without an account or an API key. Total time: about five minutes after the
one-time dependency install.

**How to move through the app.** The left bar is the order of an analysis: 0 Runs, 1 Understanding, 2 Data quality,
3 Monitor, 4 Diagnoses, 5 Assessor, 6 Report, 7 Live monitor; Settings holds the rest. Pick **Basic** mode (your name
in the top bar) to see the app as a busy operator would - only the essentials - and **Engineer** to see everything.
Every problem is shown as *Problem -> Reason -> What to do*; **A-/A+** changes the text size.

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
nothing needs to be labelled. No Python? Install the Windows app from `dist/NorrinTPM-Setup.exe` (see the README).

Then, on the finished run, let the app demonstrate the parts that need a person or a rule:

```
python -m tpm showcase --run latest                rules -> checks, accept / question / override and the downstream effect, why-chat, report
python -m tpm guard-demo --run latest              the privacy guard on this run's own data: before / after, raw rows blocked
python -m tpm report latest --format summary       one A4 page to share
```

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
- The network *How the sensors interact*: drag a sensor to untangle the picture (its lines follow), drag the
  background or scroll with two fingers to pan, pinch to zoom, and click a line or its `+2` to read in plain words
  what links the two sensors: which moves first and how many readings later the other follows, how closely, whether
  one looks like a valve acting on the other, and why the link matters.
- Try: change a role with *override*, add a note. The change is logged (section 5 / 6) and used by later stages.

## 2. Automated data quality checks — *Data quality* view, report section 2

- Baseline checks per batch (5-minute windows when a timestamp exists, else 10 % of rows): completeness (missing
  blocks), validity (out-of-range spikes, unit shifts), consistency (frozen / stuck sensors, duplicates),
  timeliness (timestamp gaps). Each check has pass / warn / fail, a severity, the responsible signals, rows, and
  evidence IDs.
- The **trust verdict** per batch: when too many signals fail, or whole records are bad (duplicated rows, a block
  frozen across many signals), the batch is marked "data cannot be trusted" and every downstream flag for that
  batch carries lower confidence (a red banner in the UI). Signals frozen or missing in the same rows are one grouped
  finding, not twenty; every signal has a plausible range (with its source); a check that cannot run on this data
  (e.g. timeliness without a time column) says **not testable** in grey instead of passing.
- **Rules**: type a plain-language rule in the rule box ("S03 must stay between 100 and 140", "S07 must not change
  by more than 5 per sample", "S02 acceleration must not exceed 3 units per sample squared", cross-signal, rolling
  statistics, missing / stale …). It is compiled into a closed JSON check spec (never arbitrary code), shown for
  approval, and every resulting check carries the rule ID for traceability. `--rules FILE` does the same from the CLI.

## 3. Drift and anomaly detection — *Monitor* view, report section 3

- No "normal" data is given: the baseline regime is chosen automatically (consensus of per-signal density modes,
  pre-change-point segments, densest windows) and its assumptions are listed. An operator may set a reference
  period instead.
- Every row is scored **out-of-fold** (GroupKFold over the detected groups; a row is never scored by a model that
  saw its own group). An **event needs a lasting rise**: the median score of one window must reach a threshold
  calibrated on held-out normal stretches; one or two high readings are point findings. When labels exist, the report
  prints precision, recall and the false-alarm rate of normal runs (0 of 1,000 on the 6 GB practice file).
- Each flag lists the responsible signals with their share, direction and lag; when no signal dominates it says so
  ("about 11 signals of cluster C02") and names one lag reference instead of a precise-looking list.
- Valves and controller outputs are recognised (0-100 % range, pinned at a limit, other signals follow them):
  a valve stuck at its limit is **actuator saturation**, a process symptom, not a sensor fault.
- Change points give the onset (abrupt vs. gradual, first- and second-order), patterns cluster similar events and
  are **named from the plant's list of known failure types** when enough of its sensors lead
  (`PATTERN-E: possibly Fault 6`), otherwise "cannot name" with the closest candidate; cascade detection orders
  propagation between signal clusters, with upstream / downstream wording.
- Sensor vs. process: one signal breaking its correlation structure is reported as a sensor / data problem, several
  correlated signals moving together as a process fault.

## 4. Root-cause diagnosis — *Diagnoses* view, report section 4

- Fault type (pattern name or "unnamed pattern"), cause class (process / sensor / data / mixed / unknown), ranked
  signals with a plain-language reason for each, the propagation chain, a numbered step-by-step explanation for a
  non-expert, confidence, and explicit uncertainty and assumptions.
- The **critique** panel (bonus): code cross-checks (lead/lag consistency with the relations, batch trust, detector
  agreement) and four alternatives argued from the evidence (a data problem, a process change, a saturated actuator,
  a single broken sensor); the verdict (supported / weakened / rejected) and the adjusted confidence are shown before
  the diagnosis is presented, and every disagreement is logged. The confidence is labelled as a heuristic score.
- Bonus "why" interface: press *Ask why* on any flag or diagnosis to ask a question in natural language. The local tool
  agent answers from the evidence and can query the raw data on this machine; the answer cites evidence IDs.

## 5. Human-in-the-loop — every card, report section 5

Accept / question / override / dismiss on any inference, flag, diagnosis, rule or pattern, with a name, a mode
(Basic / Operator / Engineer) and a note. The report lists each decision with the state **before and after**.
Overrides feed back (e.g. a corrected role changes the next batch's checks, and after an override a later event of
the same kind is typed with the person's label: `python -m tpm showcase --run latest` shows it).

## 6. Decision log — *Settings > Decision log* (Engineer mode), report section 6 and appendix

Every inference, check, flag, diagnosis, critique, egress event and human decision is an entry of its own in a
SQLite log whose entries are SHA-256 hash-chained. *Verify chain* (UI) or `python -m tpm verify-log <run_id>`
recomputes every hash and lists, per kind of object, whether each one has its own entry (and why not, if not); the
Log page shows the same table. Raw readings are never logged. Export as JSONL from the UI or with
`python -m tpm export <run_id>`.

## 7. Adaptability — report section 7, [ADAPTABILITY.md](ADAPTABILITY.md)

The pipeline never learns what a reactor is. `samples/demo_records.csv` (an order table with manual-entry errors)
goes through the same stages: run `python -m tpm run samples/demo_records.csv` and look at the same views. A third
domain, a web-service log with free-text messages (`samples/demo_log.csv`), runs too: an adapter turns how often each
kind of entry occurs and the length of the messages into signals. The
document explains which stages are generic, which adapters change, and walks a "drifting sensor" and a "corrupted
record batch" through the identical pipeline.

## 8. Data-flow record — *Settings > Data flow & privacy*, report section 8, [DATAFLOW.md](DATAFLOW.md)

- The default profile is **no-egress**: nothing leaves the machine and no network model is called. The report
  states this explicitly and the egress ledger shows only local calls.
- Switch to **hybrid** (UI settings, `--profile hybrid`, or `TPM_PROFILE=hybrid` with an `ANTHROPIC_API_KEY`) and
  the ledger records every external call: task, purpose, model, artifact types, payload size and hash, a preview of
  what was sent, and the guard result. Payloads that fail the guard (raw-looking series, too many numbers,
  categorical values, record-like structures) are blocked and served locally instead — the ledger shows `blocked`.
- **See the guard work** instead of trusting a promise: *Show the guard on this run* on the Data flow page (or
  `python -m tpm guard-demo --run latest`) puts a real message of the run through the guard (names replaced,
  numbers rounded) and a deliberately unsafe message made of raw rows, which is blocked. Nothing is sent.
- *Who wrote the explanations* (Data flow and Diagnoses pages, report section 8) says how many explanations a
  model wrote and why the rest use the evidence template.
- The model layer is swapped by configuration only: `local_llm.model` (Ollama), `external_llm.model` (Claude in
  hybrid), the **eu-hosted** profile (Mistral Large 3 on the hackathon's Verda GPU containers in Finland, key in
  `TPM_EU_API_KEY`; any other EU service through `TPM_EU_BASE_URL` / `TPM_EU_MODEL`, if its host is on
  `profiles.eu-hosted.eu_hosts`), profile routing per task. Switching the profile on the Data flow page is the
  whole toggle: no-egress (nothing leaves) → hybrid (Claude) → eu-hosted (Mistral in Finland).

## 9. Live monitor - *7 Live monitor*, no run needed

In **Engineer** mode, open page **7 Live monitor** and press **Demo: a known failure type**. The monitor learns what
normal looks like for a few cycles, then the sensors start drifting like Tennessee Eastman Fault 1:

1. *Early warning: drifting towards Fault 1* - the failure type it is moving towards, and how sure it is;
2. a *Fault 1* alarm when it is occurring, as one card read left to right: **Alarm** (what tripped, since when) ->
   **Problem** (which sensors behave how, with small charts) -> **Cause** (the matching failure type, or "no known
   type matches: generic drift") -> **Suggestion** (what to check or do);
3. on another page you still get it: a message in the corner and a dot on **7 Live monitor** in the left bar.

The catalogue of known failure types is `config/failure_signatures.yaml`; drop your own YAML of the same shape into
`workspace/_live/signatures/`. It never goes to a language model. Point the monitor at a growing file of your own with
**Watch a file**.

## Bonus items and where to find them

| Bonus | Where |
|---|---|
| No-egress mode: full pipeline on a local open-weight model | default profile; `python -m tpm models` shows the pulled model; ledger shows route = local |
| Confidence / uncertainty throughout | every inference, check, flag, pattern, diagnosis card and report row |
| Natural-language "why" interface | *Ask why* on any flag / diagnosis; the *Ask* button in the top bar |
| Visual dashboards of drift over time | Monitor view (score timelines, contributions), report sparklines |
| Critique / review step | Diagnoses view, report section 4 |
| Exportable diagnosis and decision log | Report view → export; `python -m tpm export` |
| Third domain (log / free-text records) | `python -m tpm run samples/demo_log.csv`; results and what was missed in ADAPTABILITY.md |
| One-page summary to share | Report page, *One-page summary (PDF)*; `python -m tpm report latest --format summary` |

## If something does not work

`python -m tpm doctor` checks Python, packages, RAM, disk, workspace, Ollama and `.env` and prints the fix for
each problem. The README has a troubleshooting table.
