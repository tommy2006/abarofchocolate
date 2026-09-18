# Worklog: agent_b_quality_assessor

## 2026-09-18T10:00 — Kick-off, plan
Done:
- Read DECISIONS, ARCHITECTURE, contracts, config, workspace, memory, pipeline, synth fixture, settings, rules.example, llm/__init__.
- Both `tpm/quality` and `tpm/assessor` are empty stubs; nothing from A/C/D exists yet.
Plan (files I own):
- `tpm/quality/_common.py`  shared helpers: signal catalog loading (tolerant to missing signals.json), alias<->column
  resolution (dataset.parquet has original column names, streamed batches may carry alias columns), CHK id counter,
  global robust stats via DuckDB (median/MAD/min/max/q01/q99) with fingerprint reuse.
- `tpm/quality/batches.py`  define_batches (time windows via DuckDB or row fraction); reuses batches.json if present.
- `tpm/quality/checks.py`   baseline checks (completeness/validity/consistency/timeliness), chunked with carried state;
  `check_batch` for the stream path.
- `tpm/quality/trust.py`    trust_verdict.
- `tpm/quality/rules.py`    closed JSON rule schema + vectorized executor + template parser + LLM compile + lifecycle.
- `tpm/quality/__init__.py` run_quality, check_batch, compile_rule, apply_override.
- `tpm/assessor/{scores,coverage,fitness,actions,__init__}.py`.
- Tests: `tests/test_b_helpers.py` (workspace builder from synth), `tests/test_b_quality.py`, `tests/test_b_rules.py`,
  `tests/test_b_assessor.py`.
Pending: everything above.
How to continue: run `.venv\Scripts\python.exe -m pytest tests/test_b_* -q`.
Decisions / deviations:
- No `jsonschema` package installed: rule specs are validated by a hand-written closed validator in rules.py.
- pandas 3.0 (copy-on-write, string dtype by default) and numpy 2.5 are installed; code avoids chained assignment.

## 2026-09-18T12:10 — Quality stage complete (batches, checks, trust, rules), 51 tests green
Done:
- `tpm/quality/_common.py`: SignalInfo catalog loader (signals.json -> schema.json -> parquet columns), alias/column
  resolution, CHK id counter, `global_stats` (fingerprints first, gaps filled by ONE DuckDB streaming scan with
  approx_quantile; cached in `quality_stats.json`), `robust_scale` (MAD guarded by quantile spread; std only as last resort).
- `tpm/quality/batches.py`: `define_batches` (time windows via DuckDB aggregation, else row fraction; min/max rows;
  reuses an existing batches.json), `load_batch_frame` (float32 except time column, optional stride).
- `tpm/quality/checks.py`: `BatchAccumulator` (chunked, carried stuck-run and timestamp state) producing check types
  missing/dropout/empty_rows | out_of_range/impossible_value/unit_shift/quantization_change | duplicate_rows/duplicate_key/
  stuck/saturation/sign_violation/relation_break | gap/out_of_order/duplicate_timestamp/irregular_sampling/stale, plus one
  `<category>_ok` pass result per batch. `run_checks_for_batch` (dataset path) and `check_batch` (stream path).
- `tpm/quality/trust.py`: `trust_verdict` (formula in module docstring), `signal_trust_summary`.
- `tpm/quality/rules.py`: closed schema (`RULE_JSON_SCHEMA`, `validate_compiled`), vectorised `run_rule` for all 9 types
  (group-boundary aware), template grammar (`parse_rule_text[_detailed]`, role names only with `rules.allow_role_names`,
  ambiguous -> candidates), `compile_rule` (template -> LLM `rule_compile` -> draft with explanation), `apply_override`
  (approve/reject/edit/retire/activate), `load_rules_file`, `add_rules_from_file`, `run_active_rules`, `run_rules_on_frame`.
- `tpm/quality/__init__.py`: `run_quality` (rewrites checks/trust on rerun; time-budget stride guard; rules only when
  ctx.options.rules_file is set), `check_batch` (baseline + active rules + trust), re-exports.
- Tests: tests/test_b_quality.py (11), tests/test_b_rules.py (40). Command: `.venv\Scripts\python.exe -m pytest tests/test_b_* -q`
- Scale check (scratch, not a test): 300k rows x 52 signals per batch = 2.1 s (-> 15M rows ~105 s), global stats 3.2 s / 3M rows, RSS < 300 MB.
Pending:
- Assessor package (scores, coverage, fitness, actions, __init__) + tests/test_b_assessor.py.
How to continue:
- `tpm/assessor/*` per the deliverable list; `tpm.detect.fit_score_subset` is optional (fallback PCA estimator in fitness.py).
Decisions / deviations:
- The derived-formula check (`relation_break`) warns only when the residual exceeds 0.5x the signal's robust spread: the
  synthetic generator's process faults legitimately break `derived_sum` after onset, and those are faults, not DQ.
- `quantization_change` uses per-window distinct-value share relative to the batch median (vectorised) instead of decimals
  (float32 downcast makes decimal counting unreliable); windows overlapping a frozen run are excluded.
- Trust: one bad signal keeps the batch trusted (score ~0.8) but lists it in `untrusted_signals`; `trusted=False` needs a
  critical share of signals (settings.quality.critical_signal_fraction) or a severe batch-level problem.
- Stream-path `check_batch` also evaluates active rules and rewrites the batch's trust line in trust.jsonl.

## 2026-09-18T13:40 — Assessor complete; 73 agent-B tests green; batches switched to the team's half-open shape
Done:
- Batches now use the SAME shape as `tpm/ingest/stream.py`: half-open `[row_start, row_end)`, `n_rows = row_end - row_start`,
  plus `method`. `normalize_batches` accepts an inclusive-end batches.json too (detected via n_rows / adjacency).
- `tpm/quality/trust.py`: pure `compute_trust(...)` (no evidence) used by the assessor for what-if scoring.
- `tpm/assessor/scores.py`: `compute_dq_scores` (pure; exclusions for signals/batches/check types/row ranges,
  trust recomputed) + `dq_scores` (evidence per category + overall) + `worst_signals`.
- `tpm/assessor/coverage.py`: `unit_definition` (groups if >= 4 else row windows), `unit_fingerprints` (one DuckDB
  GROUP BY: mean/std per checkable signal), robust standardisation, k-means with silhouette-chosen k (subsample <= 2000),
  thin regimes, balance, near-constant / redundant signals, label balance (evaluation only), `coverage_score`, scaler +
  centroids + novelty threshold stored so a new file can be projected (`project_units`).
- `tpm/assessor/fitness.py`: `learning_curve` (stratified holdback over regimes, fractions from settings, second seed for
  uncertainty when the budget allows, slope at the end, diminishing-returns point, `would_help_more_data`),
  `run_experiment` -> `tpm.detect.fit_score_subset` (agent C; keys threshold_cv_mean/detector_agreement/flagged_fraction/
  auroc normalised) with `fallback_fit_score_subset` (PCA-SPE, two half-models) whenever C's function is missing/fails
  or when signals are excluded; `compare_with_without` for drop_signal.
- `tpm/assessor/actions.py`: `parse_action` (templates + optional local LLM, all results validated by `normalize_action`),
  `evaluate_action` per type (recommend only when evidence supports it), `assess_new_file` (DuckDB read, name/position
  mapping, missing profile, regime projection, curve-based gain), `apply_action` (dataset_curated.parquet + curation.json).
- `tpm/assessor/__init__.py`: `run_assess` (assessor.json with dq_scores, coverage, fitness, combined_score
  0.4 fitness/0.3 coverage/0.3 dq, candidate-action evaluations, recommendations REC-xxx, more/less data verdicts),
  `ask` (template answer, local-LLM rewrite with JSON `answer` extraction and hallucinated-id stripping, chat.jsonl),
  `apply_override` (object_type "assessor": new_value.action or REC id; dismiss/reject logged only).
- Tests: tests/test_b_assessor.py (21). Total agent B: 73 passed in ~36 s.
Pending:
- Integration run through the real pipeline with agent A's ingest/profile (running now; results in the next entry).
How to continue:
- `.venv\Scripts\python.exe -m pytest tests/test_b_* -q`; chat: `tpm.assessor.ask(ws, settings, "would dropping S05 improve quality?")`.
- Human approval path: `tpm.pipeline.apply_decision(ws, settings, HumanDecision(object_type="assessor", action="apply_assessor_action", new_value={"action": {...}}))`.
Decisions / deviations:
- Learning-curve units are groups when there are >= 4 groups, else row windows; agent C's `fit_score_subset` is only
  used for group units (it expects __group__ ids), otherwise the fallback estimator runs (recorded in `estimators`).
- The local LLM (agent D's router, llama3 in the current install) answers `assessor_chat` as JSON; `ask` takes only the
  `answer` field and removes any evidence id that is not in the evaluation's evidence list, then appends the real ids.
- `apply_action` never touches dataset.parquet; it writes dataset_curated.parquet (a filtered copy) + curation.json.

## 2026-09-18T14:05 — Integration through the real pipeline (A ingest/profile -> B quality/assess with C's estimator)
Done:
- `run_pipeline(synth.csv, stages=[ingest, profile, quality, assess], options={"rules_file": config/rules.example.md})`
  finishes in 67 s: quality 0.8 s (8 batches from A's batches.json, 98 checks, all 5 injected DQ issues found under
  A's aliases/roles/fingerprints), assess uses `detect.fit_score_subset` for the learning curve (metric
  detector_agreement; uncertainty honest -> "unclear"), combined score written to assessor.json.
- FIX from the integration: operating-rule violations (category "rule") no longer lower trust unless the rule type is
  data-quality-like (missing/stuck) -- `tpm/quality/trust.py::counts_for_trust`, `rule_type` now stored in the rule
  CheckResult values. Before the fix the example rules made S02/S07 "untrusted" in every batch and the assessor
  recommended dropping them; detection must SEE rule-violating signals, not ignore them.
- Every CheckResult now cites evidence, including `<category>_ok` summaries and rule pass results.
Pending: nothing blocking. Nice-to-have: LLM-enhanced rule explanations, `report` hooks for assessor.json (agent F).
How to continue: `.venv\Scripts\python.exe -m pytest tests/test_b_* -q` (74 tests). Integration script pattern: see
  the "Integration" entry above (tmp workspace, `run_pipeline(..., stages=[...], continue_on_error=True)`).
Decisions / deviations:
- Agent A's batches.json ids are 5 digits (B00001); mine are 4 (B0001) when I create the file. Both are accepted everywhere.

## 2026-09-18T14:20 — Final state
Done:
- Integration re-run after the trust fix: quality 0.64 s, 0 untrusted batches, trust only lowered by the real DQ
  injections (B00001 S04 stuck, B00003 S02 spike, B00005 S01 frozen, B00006 S12 unit shift); assessor recommends only
  drop_duplicates; data-quality score 0.93.
- Learning-curve verdict says "unclear" when the uncertainty of the slope dominates the estimated gain (agent C's
  agreement metric is noisy on 19 groups); thin regimes are still mentioned as a coverage argument.
- 74 tests: `.venv\Scripts\python.exe -m pytest tests/test_b_* -q` (about 36 s).
Known gaps (for whoever continues):
- Exact duplicates are only found within a chunk (500k rows) of a batch; far-apart duplicates in huge batches are missed
  (documented in checks.py). `apply_action(drop_duplicates)` de-duplicates the whole dataset in DuckDB.
- `quantization_change` is self-referencing within the batch (a whole batch at a new precision is not flagged).
- Rule text with role names needs `rules.allow_role_names=true`; ambiguous phrases return candidates for confirmation,
  the UI (agent E) must surface `Rule.compile_explanation` for that.
- The assessor's LLM rewrite is optional and local-only; every answer has a template version.
