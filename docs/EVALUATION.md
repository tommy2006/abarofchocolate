# Evaluation

How we measure whether the unsupervised pipeline works, what ground truth exists, and how label-like columns are
kept out of detection.

## 1. What is measured

| Stage | Measured against | Metric |
|---|---|---|
| ingest | synthetic generator settings | header / delimiter / transposition detection; group count and grouping method; sample period; label / meta columns excluded |
| profile | `truth["signal_roles"]` and `truth["relations"]` of the synthetic generator | structural-role accuracy (per role), recovery of the lagged pairs (correct partner and lag ± 1) |
| quality | `truth["dq"]` (injected defects) | detection of each injected defect type (missing block, spike, frozen block, unit shift, duplicates, timestamp gap) with the right signal and row range; false checks on clean batches; trust verdict on the defective batches |
| detect | `truth["groups"]` (fault type + onset per group) | **AUROC** of the out-of-fold score vs. the post-onset mask; per-group detection rate; false-alarm rate on fault-free groups; median detection delay (samples after onset); pattern-vs-fault purity |
| diagnose | fault type + the signals the generator perturbed | responsible signals in the top-3 of the ranking; cause class (sensor faults such as `stuck_sensor` must be diagnosed as sensor/data, not process); critique consistency |
| whole pipeline | time budget | full pass within `time_budget_s` (20 min for a 6 GB file on a 16 GB laptop); peak RAM below the budget in `tpm/memory.py` |

The report renders `evaluation.json` (when present) under "Evaluation (labels used for evaluation only)".

## 2. Synthetic ground truth

`tests/fixtures/synth.py` generates a generic multi-run process that is **not modelled on any real dataset**:
three latent AR(1) drivers, six lagged measured signals, two actuator-like step signals, a sample-and-hold analyzer,
a constant, a derived sum, a power-like signal. Per group it injects one of six faults (`step`, `ramp`,
`stuck_sensor`, `corr_break`, `oscillation`, `noise_burst`) at a random onset in 35–60 % of the group, and globally
six data-quality defects. It returns the data and a `truth` dictionary (fault per group with onset, defect rows,
signal roles, lagged relations). `samples/demo_process.csv` and `samples/demo_process_labeled.csv` are produced by it.

Because the generator is random-seeded, tests run the pipeline on fresh draws; the evaluation is therefore a
property of the method, not of a memorised file.

## 3. Labels are for evaluation only

The assessment data has no labels. When a dataset *does* contain label-like columns (few distinct values, block
constant per group, names such as `fault`, `label`, `class`), the ingest stage lists them in `schema.label_columns`,
the profile and detect stages skip them, and only `tpm/detect/evaluate.py` reads them — after scoring — to compute
the metrics above. The report says so explicitly ("evaluation only; labels never used for detection") and the
Understanding view shows which columns were excluded. Simulation metadata (run ids, sample counters) is treated the
same way (`schema.meta_columns`).

`samples/demo_process_labeled.csv` demonstrates this: the `fault_label` column is auto-detected, excluded, and the
run's report gains an evaluation section.

## 4. Whole dataset as the object: out-of-fold scoring

No normal data is provided and the operator wants faults found in the **entire** file, so the dataset is both the
training material and the test set. To keep that honest:

1. **GroupKFold over detected groups** (`detect.n_folds`, default 5). For each fold, detectors are fitted on the
   baseline-regime rows of the other folds and score every row of the held-out groups. Every row therefore gets a
   score from a model that never saw its group.
2. **Internal train / validation split by group** inside each training fold selects detector hyperparameters and
   calibrates thresholds (quantile of validation scores, `contamination_prior` as a prior). Nothing from the
   held-out fold touches the threshold.
3. **Leakage guard** (`detect.leakage_guard`): near-duplicate segments (high cross-correlation of fingerprints
   between groups) are forced into the same fold so a replayed run cannot score itself.
4. **Ensemble selection** by unsupervised reliability: stability of each detector's ranking across folds and
   agreement between detectors; detectors that are unstable or too slow for the time budget are dropped. The
   chosen set, fold stability and threshold method are written to `detect_meta.json` and shown in the report.
5. **Baseline regime** candidates are scored for self-consistency (consensus of per-signal modes, pre-change-point
   segments, densest windows, robust covariance trimming); the winner is an inference with evidence and listed
   assumptions, and an operator may override it with a reference period.

With a single group, the fold is over contiguous time blocks instead.

## 5. Running the evaluation

```
.venv\Scripts\python.exe -m pytest tests -q                  # unit + property tests of every stage
python -m tpm run samples/demo_process_labeled.csv          # evaluation.json + report section
python scripts/bakeoff.py                                   # local-model comparison (JSON validity, tool calls, latency)
```

Per-agent tests (`tests/test_a_*` … `tests/test_f_*`) each build a workspace from the synthetic generator so
they are independent of the other stages; the integration test runs the whole pipeline on a fresh draw.

## 6. Limits we state rather than hide

- Metrics on synthetic data are an upper bound: real faults are messier and the generator's faults are the ones we
  know how to inject.
- AUROC over post-onset masks penalises early detection of slow ramps (a ramp is barely different right after
  onset); detection delay is reported separately for that reason.
- Pattern purity depends on the number of events; with few events the classifier reliability is shown as low and
  patterns stay unnamed.
- Everything a language model writes is labelled with its source and is never part of the metrics.
