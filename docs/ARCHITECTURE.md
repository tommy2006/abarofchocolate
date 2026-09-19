# Trustworthy Process Monitor (TPM) — architecture

Read this before touching code. It defines module ownership, the artifacts that stages exchange, and the
conventions that make the system trustworthy (evidence-first, template-first, data never leaves).

## 1. One-paragraph summary

A file or a stream of batches enters the **operator environment**. Plain code (no LLM) turns rows into
**derived artifacts**: a schema, per-signal fingerprints, correlation/lag structure, data-quality checks,
anomaly scores with per-signal contributions, change points, fault patterns and a diagnosis. LLMs only
write hypotheses and explanations **on top of** those artifacts and must cite evidence IDs. Local models
(Ollama) may see raw rows; external models (Anthropic) may only receive artifacts that pass the
**egress guard**, and every external call is written to the **egress ledger**. Every inference, flag,
diagnosis and human decision is appended to a hash-chained **decision log**.

## 2. Package layout and ownership

```
tpm/
  contracts.py        pydantic models shared by everyone            (foundation, do not change shape without a worklog note)
  config.py           settings loader, profiles                     (foundation)
  workspace.py        run directory, artifact I/O, registries       (foundation)
  memory.py           RAM budget helpers                            (foundation)
  pipeline.py         orchestrator: stages, batch processing        (foundation; platform agent may extend)
  ingest/             readers, schema/roles/grouping, streaming     AGENT A (ingest+profile)
  profile/            fingerprints, relations, structural roles     AGENT A
  quality/            baseline DQ checks, trust verdict, rules      AGENT B (quality+assessor)
  assessor/           data-quality assessor                          AGENT B
  detect/             baseline regime, detectors, OOF ensemble,
                      change points, attribution, patterns, cascade AGENT C (detect+diagnose)
  diagnose/           diagnosis assembly, critique                   AGENT C
  llm/                providers, router, guard, ledger, prompts,
                      local tool agent, embeddings, narratives      AGENT D (llm)
  api/                FastAPI server + static frontend               AGENT E (api+ui)
  log/                decision log (foundation) + exports            AGENT F (platform)
  report/             HTML report, i18n, email                       AGENT F
  cli.py              command line                                   AGENT F
tests/                pytest; fixtures/synth.py is the shared synthetic generator
config/settings.yaml  all tunables and profiles
docs/                 DECISIONS, ARCHITECTURE, DATAFLOW, ADAPTABILITY, worklog/
```

Agents edit only their own directories plus `docs/worklog/<agent>.md` and `tests/test_<agent>_*.py`.
If you need a change in a foundation file, make it minimal, backward compatible, and note it in your worklog.

## 3. Stages and artifacts

All artifacts live in `workspace/<run_id>/`. JSON for small things, JSONL for append-only lists,
Parquet for anything row-shaped. Stages communicate only through the workspace, never through globals.

| Stage | Public function | Reads | Writes |
|---|---|---|---|
| ingest | `tpm.ingest.run_ingest(ws, settings, ctx)` | `ctx["source_path"]` | `dataset.parquet`, `schema.json` (DatasetSchema), evidence, inferences |
| profile | `tpm.profile.run_profile(ws, settings, ctx)` | dataset, schema | `signals.json` (list[SignalDescriptor] — the **signal catalog**), `relations.json`, evidence, inferences, `domain.json` |
| quality | `tpm.quality.run_quality(ws, settings, ctx)` | dataset, schema, signals, `rules.json`, `domain.json` (wording) | `checks.jsonl` (CheckResult; grouped common-mode checks, `not_testable` status, `values.confidence`), `trust.jsonl` (TrustVerdict with record-level `untrusted_rows`), `batches.json`, `quality_summary.json` (run-level verdict in plain words), plausible ranges in `quality_stats.json` |
| detect | `tpm.detect.run_detect(ws, settings, ctx)` | dataset, schema, signals, relations, trust | `scores.parquet` (per-row scores + contributions), `group_scores.json` (one summary per group: max/mean score, flagged share, first crossing, leading signal; complete even when the flag list is capped), `flags.jsonl` (Flag; rich attribution for the strongest groups, summary flags for the rest, cap 4000), `patterns.json` (FaultPattern), `baseline.json`, `detect_meta.json`, `evaluation.json` (only if label columns exist) |
| diagnose | `tpm.diagnose.run_diagnose(ws, settings, ctx)` | flags, patterns, relations, signals, trust | `diagnoses.jsonl` (Diagnosis, with critique filled) |
| assess | `tpm.assessor.run_assess(ws, settings, ctx)` | everything above | `assessor.json` |
| report | `tpm.report.run_report(ws, settings, ctx)` | everything | `report_<lang>.html` |

Batch/stream path (used by replay, watch folder and HTTP push):

```
tpm.pipeline.process_batch(ws, settings, batch_df, batch_id)
  -> tpm.quality.check_batch(ws, settings, batch_df, batch_id)    -> list[CheckResult], TrustVerdict
  -> tpm.detect.score_batch(ws, settings, batch_df, batch_id, trust) -> list[Flag] (appended to flags.jsonl)
  -> tpm.diagnose.diagnose_flags(ws, settings, flags)             -> list[Diagnosis]
```

Human feedback path (from API): `tpm.pipeline.apply_decision(ws, settings, decision)` records the
decision in the log and calls the owning module's `apply_override(ws, settings, decision)` when it exists
(e.g. a role override in `profile`, a diagnosis override in `diagnose`, a rule approval in `quality`).

`ctx` is a plain dict: `source_path`, `options` (dataset options such as `has_header`, `delimiter`,
`group_columns`, `reference_period`, `domain_hint`), `progress(fraction, message)` callable, `run_id`.

## 4. Conventions that make this trustworthy

1. **Evidence first.** Any claim shown to a human is backed by `Evidence` objects with IDs
   (`EV-000123`). Create them with `ws.evidence.add(...)`. Inferences (`INF-...`) cite evidence IDs and
   carry `status` = inferred | assumed | uncertain and a confidence in [0, 1].
2. **Template first.** Every narrative (sensor report, diagnosis steps, critique, rule explanation) has a
   deterministic code-generated version. An LLM version is an optional enhancement layered on top and
   labelled with its `source` (`llm-local:<model>` / `llm-external:<model>` / `template`). The app must
   be fully functional with no LLM available.
3. **Labels never reach detection.** `schema.label_columns` and `schema.meta_columns` are excluded from
   profiling and detection. They are only read by `tpm/detect/evaluate.py` to compute metrics.
4. **Blind mode.** Signals are addressed by alias (`S01..Snn`, from `schema.signal_alias`). Original
   names are weak evidence only; they may appear in a `name_hint` evidence item with low weight.
5. **Egress.** Only `tpm.llm.router` may talk to a network model. External payloads must be built from
   contract objects (fingerprints, relations, checks, flags, diagnoses, rule text) and pass
   `tpm.llm.guard`. Local calls may include raw rows. Every external call gets an `EgressRecord`.
6. **Log everything.** Use `ws.log.record(actor, action, object_type, object_id, payload, evidence_ids)`.
   Actors: `system:<module>`, `human:<name>(<role>)`, `llm:local:<model>`, `llm:external:<model>`.
7. **Memory.** Never load the whole file into pandas. Use DuckDB (`ws.duckdb()`) over
   `dataset.parquet`, chunked iteration (`tpm.memory.chunk_rows()`), float32 downcast, and subsampling
   with a recorded sampling description. Respect `settings.time_budget_s`.
8. **Uncertainty everywhere.** Confidence on every inference, flag and diagnosis; "unknown" is a valid
   answer and is better than a confident guess.
9. **Worklog.** Append to `docs/worklog/<agent>.md` at every milestone: done / pending / how to continue.

## 5. The signal catalog

`signals.json` is a list of `SignalDescriptor`. It is the only description of the data that later stages,
the rule compiler and external LLM calls see. It contains aliases, dtype, structural role, hypotheses
with confidence, cluster membership and a fingerprint of **aggregates only** (count, mean, std, quantiles,
noise level, autocorrelation, stuck fraction, quantization step, dominant period, etc.). No raw values.

## 6. LLM routing (profile-driven; see `config/settings.yaml`)

| task key | payload | no-egress | hybrid |
|---|---|---|---|
| `column_roles` | sample rows + stats | local | local |
| `sensor_hypotheses` | signal catalog + relations summary | local | external |
| `rule_compile` | rule text + signal catalog | local | external |
| `diagnosis_narrative` | diagnosis + evidence statements | local | external |
| `critique` | diagnosis + evidence statements | local | external |
| `why_chat` | may query raw data through tools | local | local |
| `assessor_chat` | experiments on raw data | local | local |
| `report_narrative` | report sections | local | external |

Route `local` uses Ollama; `external` uses Anthropic through the guard; if the guard blocks or the
provider fails, the router falls back to local, then to template, and records the fallback.

## 7. Detection design (unsupervised, whole dataset is the subject)

1. **Baseline regime**: no normal data is provided. Candidate strategies are computed and scored for
   self-consistency: per-signal density modes (consensus-of-modes core), pre-change-point segments of each
   group, densest cluster of windows, robust covariance trimming. The winning baseline is an inference
   with evidence and an explicit assumption list. Operator may set a reference period instead.
2. **Detectors** (all produce per-signal contributions): PCA T²/SPE, robust z / EWMA / CUSUM per signal,
   correlation-structure break, Isolation Forest, optional windowed autoencoder (sklearn MLP).
3. **Out-of-fold scoring**: GroupKFold over detected groups (leakage guard keeps near-duplicate segments in
   the same fold). Every row is scored by models fitted without its group. Internal train/validation
   splits select detector hyperparameters and calibrate thresholds; the ensemble picks detectors by
   stability across folds and agreement.
4. **Change points**: onset per group, abrupt vs gradual, first- and second-order changes.
5. **Patterns**: flagged events clustered by attribution signature into unnamed fault patterns
   (`PATTERN-A`...), which the operator can name. A pseudo-label classifier (LightGBM, cross-validated)
   types new batches and reports its reliability.
6. **Cascade**: ordered propagation chains between signal clusters using lags.
7. **Sensor vs process**: single signal breaking its correlation structure → sensor/data; several
   correlated signals moving together → process.
8. **Evaluation** (only when label-like columns exist): AUROC, per-group detection rate/false alarms,
   detection delay, pattern-vs-label purity. Reported as "evaluation", never used by detection.
