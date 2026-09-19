# Worklog: agent_a_ingest_profile

## 2026-09-18T22:40+03:00 — Plan and design experiments
Done:
- Read DECISIONS, ARCHITECTURE, contracts, config, workspace, memory, pipeline, synth fixture, settings, llm entry point.
- Created `workspace/samples/te_head.csv` (30 001 lines, `head -n 30001 te_process.csv`) for smoke tests. Never analyse the full file here.
- DuckDB experiments (1.5.5): `COPY (SELECT *, row_number() OVER () - 1 AS __row__ FROM read_csv(...))` preserves file order
  (0 mismatches on a 3M-row counter file, parallel reader). `ASOF JOIN` does NOT preserve order -> never use joins to add
  `__group__`; use a scalar projection (key columns) or a chunked pyarrow rewrite (row ranges). Whitespace `.dat` files are
  read with `read_csv(delim='\x01', columns={'line':'VARCHAR'})` + `regexp_split_to_array(trim(line),'\s+')` (streaming).
  `nullstr=[...]`, `decimal_separator=','`, `encoding='latin-1'` work. `os.replace` of dataset.parquet works on Windows
  while the shared DuckDB connection is open. `LAG() OVER (ORDER BY __row__)` on 3M rows = 0.09 s.
Pending:
- Everything in the deliverable list (readers, schema, stream, profile: fingerprints/relations/roles, tests).
How to continue:
- Design (module by module) is described in the entries below as they land. Tests: `.venv\Scripts\python.exe -m pytest tests/test_a_* -q`.
Decisions / deviations from ARCHITECTURE.md:
- Batches in `batches.json` use half-open row ranges: `row_start` inclusive, `row_end` exclusive, plus `n_rows`.
- `signals.json` contains ONLY signal columns (schema.signal_columns); label/meta/time/key columns are described in
  `schema.json` and `understanding.json` only, so a stage that iterates the catalog can never touch a label.
- Constant numeric columns stay in the catalog as role `constant` with `excluded=True` (structural role is still reported).

## 2026-09-18T23:20+03:00 — Ingest stage complete (readers, schema, stream, run_ingest)
Done:
- `tpm/ingest/readers.py`: `detect_format` (delimiter/decimal/header/comment-skip/encoding sniff, transposed detection
  via lag-1 autocorrelation down columns vs along rows), `convert_to_parquet` (DuckDB COPY with `row_number() OVER ()-1`
  as `__row__`, float64->float32 downcast except integer-valued/epoch-like/large-magnitude columns, strict->promote
  ints->ignore_errors->all_varchar retry ladder, whitespace .dat via regexp_split, JSON/JSONL via read_json, Excel via
  openpyxl streaming + pyarrow writer, transposed matrices via disk memmap), `dataframe_to_parquet`, `read_small`.
- `tpm/ingest/sample.py`: contiguous-chunk sampling (`chunk_plan`, `read_chunks`, `assign_blocks`).
- `tpm/ingest/schema.py`: `infer_schema` (typing incl. numeric strings/epoch/datetime strings with try_strptime, time
  column + period, counters, grouping strategies key_columns/combos/counter_reset/time_gaps/changepoint/none with scores,
  label/meta detection, aliases, domain likelihood), `materialize_groups` (projection or chunked pyarrow rewrite,
  verifies `__row__` order), `apply_override` (group_columns / time_column / order_column / move column).
- `tpm/ingest/stream.py`: `build_batches` (5-min windows per group when a timestamp exists, else 10 % of rows cut at
  group boundaries), `iter_batches`, `read_rows`, `replay`, `align_incoming`, `watch_folder`.
- `tpm/ingest/__init__.py`: `run_ingest`, `ingest_dataframe`, re-exports `apply_override`.
- Verified on all 7 synth variants (same shape, 12/12 groups, timestamp 180 s, label detected) and on te_head.csv
  (60 groups via simulationRun, counter aligned, 53 signals, 1.4 s).
Pending:
- profile stage (fingerprints, relations, roles, understanding.json), tests, final smoke timing.
How to continue:
- `tpm/profile/fingerprints.py` next; run `python -c "from tpm.ingest import run_ingest"` smoke via the scratch script.
Decisions / deviations from ARCHITECTURE.md:
- `groups.json` (extra artifact): list of {group_id,row_start,row_end,n_rows} blocks in file order.
- `stream_state.json` (extra artifact): next __row__ and seen files for push/watch batches; stream batch ids are `SB00001..`.
- Time column strings are cast to TIMESTAMP (and numeric strings to DOUBLE) during the `__group__` rewrite, so
  dataset.parquet always has typed columns after ingest.

## 2026-09-19T00:15+03:00 — Profile stage complete, tests green, benchmarks
Done:
- `tpm/profile/fingerprints.py`: `compute_global_stats` (DuckDB aggregates in memory-sized column batches; exact
  quantiles <= 2M rows, approx_quantile above; NaN-aware; share_at_min/max second pass), `load_dynamics_sample`
  (whole groups or contiguous chunks, bounded by RAM budget, shared with relations), `compute_dynamics` (lag-1/5
  autocorrelation, noise level, stuck fraction, hold period + regularity, level autocorrelation, quantization step,
  jump ratio, dominant FFT period with prominence, trend strength), `compute_fingerprints` (+ distribution shape via
  bimodality coefficient, boundedness 0-100 / 0-1 / nonnegative).
- `tpm/profile/relations.py`: `compute_relations` (winsorized Pearson + Spearman, lagged xcorr for the top-80 pairs:
  pre-whitened first differences for continuous pairs, update-instant masked xcorr (FFT lag-sums) when a held signal
  is involved, plateau rule, `a` always leads `b`; average-linkage clusters on 1-|r| at 0.5 -> C01..; redundancy by
  stepwise OLS with per-group R2 (q70 >= 0.9995, median >= 0.95), minimal regressor set by backward elimination,
  derived member = same-sign coefficients then largest variance; leaders graph). Evidence for pairs/lags/clusters/redundancy.
- `tpm/profile/roles.py`: `structural_role` (constant, derived_redundant, counter, categorical, held_sampled
  (strictly regular short hold OR loosely regular + autocorrelated levels), actuator_like, continuous_measured; each
  with confidence + alternatives), `heuristic_hypotheses` (<= 0.5, gated by sensor_stream >= 0.5), `llm_hypotheses`
  (tpm.llm.complete("sensor_hypotheses") with JSON schema, robust parsing, confidence capped 0.6, inference per accepted
  item with source from LLMResult, log entry under llm:local/external actor; never raises), `apply_override`
  (signal/inference: role override, hypotheses, excluded; marks inferences overridden; rewrites catalog).
- `tpm/profile/__init__.py`: `run_profile` (signals.json, relations.json, domain.json, understanding.json), `write_catalog`,
  `build_understanding` (template narrative per signal + dataset assumptions + unknowns), `signal_narrative`.
- Tests: `tests/test_a_ingest.py` (15) + `tests/test_a_profile.py` (11): `.venv\Scripts\python.exe -m pytest tests/test_a_* -q`
  -> 26 passed in ~21 s. Robustness sweep over 6 synth seeds/sizes: 0 role mismatches, 36/36 lags within +-1 (all exact).
- Timing: te_head.csv (30k x 57) ingest 1.3 s, profile 7.9 s (LLM skipped). 664 MB synthetic CSV (2M x 40, 4000 groups):
  ingest 10.6 s (DuckDB conversion ~78 MB/s), profile 16.3 s -> extrapolated ~4 min for the 6 GB / 15M x 57 target.
  Full-file run is left to integration (never run here).
- End-to-end through `tpm.pipeline.run_pipeline(..., stages=["ingest","profile"])` works (36 progress events).
Pending / known gaps:
- Local LLM call (llama3:8b reachable) takes 30-100 s and was unparseable on 57 signals once; it is gated on
  remaining budget (> 2*timeout+60 s) and `options.skip_llm`. Router/prompt tuning is agent D's.
- Nested key detection tries pairs of the top-4 key candidates only (no triples); non-contiguous key groupings use the
  window-based block extraction (slower on 15M rows, still one sort).
- Transposed matrices are read through a float32 memmap: text columns in a transposed file are not supported.
- Excel: first sheet unless `options.sheet`; whole file streamed through openpyxl (Excel limit ~1M rows).
- Instrument hypotheses are heuristic labels ("temperature-like" etc.); no units inference.
How to continue:
- Run `.venv\Scripts\python.exe -m pytest tests/test_a_* -q`; smoke: see `tests/test_a_ingest.py::test_te_head_smoke`.
- To tune grouping scores: `tpm/ingest/schema.py::_evaluate_key_candidate` / `_evaluate_counter_candidate`.
- To add a role rule: `tpm/profile/roles.py::structural_role` (fingerprint keys documented in fingerprints.py).
- Downstream contract: iterate `ws.signals()` (only signal columns; check `excluded`), `relations.json["pairs"]`
  (a leads b by `lag`), `batches.json` (row_end exclusive), `groups.json`, `understanding.json`.
Decisions / deviations from ARCHITECTURE.md:
- `understanding.json` (extra artifact) is the code-generated sensor understanding report; `domain.json` also carries
  `hypotheses_enabled` (sensor_stream >= 0.5) so the UI can explain why hypotheses are absent on record-like data.
- Foundation files untouched. `materialize_groups` closes/reopens the shared DuckDB connection only if Windows refuses
  the file replace (private attrs `ws._duck`/`ws._lock`, guarded).

## 2026-09-19T11:09+03:00 — Missing-value tokens and non-finite values are NULL on every ingest path
Done:
- Bug: `python -m tpm run samples\extra_uneven_headerless.dat --no-llm` failed in assess with `Out of Range Error: STDDEV_SAMP is out of range!`.
  Root cause: the whitespace path relied on TRY_CAST to null the NAN_TOKENS, but DuckDB parses 'NaN' / 'nan' / 'inf' / 'Infinity' / '1e999'
  as IEEE NaN / inf, treats them as ordinary values, and stddev_samp / var_samp raise on them. Same run now: all 7 stages done (35 s),
  the affected column holds 44 NULLs and 0 NaN.
- tpm/ingest/readers.py: invariant "a float column of dataset.parquet holds finite values or NULL" (module docstring). `finite_sql()`;
  whitespace path nulls NAN_TOKENS before the cast in every column (matches nullstr on the delimited path) and an integer column with
  NaN tokens no longer fails the conversion (was a hard ingest error); `_projection` (delimited / parquet / JSON) and
  `_keep_double_columns` guard FLOAT/DOUBLE; `_write_chunks_parquet` (transposed, Excel, DataFrame) masks +/-inf (Arrow already nulls NaN).
- Defensive guard on the mean/std aggregates: tpm/quality/_common.py `finite_sql()` used by global_stats, tpm/assessor/coverage.py
  unit_fingerprints, tpm/assessor/actions.py assess_new_file; tpm/profile/fingerprints.py (was isnan only, inf still raised); tpm/llm/agent.py tool_stats.
- Tests: tests/test_a_ingest.py (whitespace float + integer columns, delimited, transposed, DataFrame), tests/test_b_assessor.py
  (ingest -> profile -> quality -> assess on a headerless whitespace file with NaN tokens; legacy parquet holding NaN/inf; new file with nan/inf).
  All 5 fail on the old code. Full suite before the rebase onto 58a1506: 227 passed, 2 skipped, 1 xfailed; touched modules after it: 73 passed.
Pending:
- assess_new_file / the add_file action read the new file with read_csv_auto, not through tpm/ingest: the appended rows can still carry
  NaN / inf into dataset_curated.parquet (aggregates are guarded, the parquet invariant is not enforced there).
- fingerprint `n_inf` is always 0 for freshly ingested runs now (inf is stored as missing and counted in missing_rate).
How to continue:
- `.venv\Scripts\python.exe -m pytest tests/test_a_ingest.py tests/test_b_assessor.py -q -k "nan or finite"`
