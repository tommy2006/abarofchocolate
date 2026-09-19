# Round 6 D - data-quality module after the expert review (items 10, 18, 19, 20, 7 part 2, 5)

Owner: agent D of round 6 (quality module `tpm/quality/*`, plus the status consumers in assessor / report / api / detect).
Design points kept on purpose: exposure-weighted severity for single-signal problems, row-scoped `local_untrusted`,
`BATCH_MIN_EXPOSURE = 0.25`. Nothing is tuned to one dataset: every threshold below is a share of rows or signals.

## What changed and why

### #18 Grouped (common-mode) findings - `tpm/quality/grouping.py` (new), `checks.py`
- A *block* = rows in which >= k signals carry the same finding at the same time for >= `stuck_min_run / 2` rows,
  k = `common_mode_k(n_signals)` = 3, or 10 % of the signals when there are fewer than 30 (never < 2). Sweep line
  over the per-signal intervals, membership = the signal covers >= half of the block or >= half of its own finding
  lies inside it; blocks with similar signal sets (Jaccard >= 0.5) are one family = ONE check.
- `frozen_block` (consistency): runs >= each signal's own stuck / saturation threshold (hold periods and actuator
  thresholds respected). Statement: "Rows 1200-1399 of batch B0001 are frozen in 8 signals at once (...): a logging or
  data problem, not 8 broken sensors; these rows are not real measurements of any of them." Per-signal stuck /
  saturation / stale findings inside the block are NOT reported again; they stay traceable in
  `values.members[signal] = {n_runs, rows, longest_run, value, as}`; `values.blocks` lists every block (capped 100).
  A signal's own freeze outside the block is still its own `stuck` check (with `values.grouped_in`).
- `missing_block` (completeness): rows where >= k signals are missing at once (k raised above what independent gaps
  explain: expected missing per row from the fingerprints + 4 sd). Per-signal `missing` / `dropout` count only the
  signal's own gaps. All-missing rows stay `empty_rows`.
- `quantization_block` (validity): the same windows re-quantized in >= k signals.
- Also fixed on the way: runs were capped at 48 per signal and batch (the stuck share was underestimated on long
  batches - the reason 30 signals x 48 runs showed "0.87 %"); a run crossing a read chunk was counted twice;
  exact duplicates were only found inside one 500k-row read chunk (now 64-bit row hashes over the whole batch).

### #10 Trust verdict - `trust.py`
- Record-level problems (hit every signal of a row): `duplicate_rows`, `duplicate_key`, `frozen_block`,
  `missing_block`, `empty_rows`. `record_share` = rows they cover (rows counted by two findings once:
  `values.record_fraction`).
- Row scope: common-mode blocks and empty rows always; duplicates / repeated keys from `DUP_ROW_SCOPE_MIN = 2 %`
  (below that a retransmission does not make the rows around it unreliable; from 2 % the logger/export copies
  records systematically). New `TrustVerdict.untrusted_rows` (row ranges, all signals) + `untrusted_row_share`;
  block members also get per-signal `local_untrusted` entries so detection attributes anomalies there to data.
- Whole batch: `RECORD_BATCH_SHARE = 10 %` -> untrusted (one row in ten is not an independent measurement; lower
  than the 25 % for one signal because a record-level problem hits all signals at once). Score:
  `record_penalty = (1 - trust_fail_threshold) * min(1, record_share / 0.10)`: record-level problems alone put the
  score exactly on the threshold at 10 % (and the verdict is forced to untrusted from there, score capped below the
  threshold); below 10 % they lower the score, and together with other problems can still tip it. Statement: "This
  data cannot be trusted as a whole: in batch B00003, 5.7% of its rows are frozen in 28 signals at once (...); 4.8% of
  its rows are exact copies of earlier rows. 10% of the rows are not real, independent measurements, ...", or, when
  only the combination tips it, "... Together these problems bring its trust score to 0.47, below the threshold of
  0.50." Every untrusted batch statement starts with "This data cannot be trusted as a whole".
- Trusted batches say "usable only in part" when rows are untrusted, "passed the baseline checks with N warning(s)"
  when only warnings exist (never "passed all baseline checks" while something was found).
- Run level: `quality_summary.json` (`trust.run_summary`): verdict untrusted (>= 50 % of the rows in untrusted
  batches) | partly_untrusted | usable_with_problems (any fail or untrusted rows) | clean, a plain statement, the
  dominant problem (severity x share of rows), not-testable categories. Read by the report (overview + section 2),
  `plain.py`, and returned by `GET /api/runs/{id}/trust` as `summary`. Stage message ends with the statement.

### #19 Plausibility - `tpm/quality/plausibility.py` (new), `checks.py`
- Plausible range per signal, per bound (hints may only tighten a data bound, operator rules may only widen it):
  data (p1..p99 widened by 3 spans), physical hints
  (non-negative when every normal reading >= 0; percentage-like when the normal part lies in 0..100, uses >= 10 points
  of it and >= 1 % of readings sit at a bound, or the signal is actuator-like), with a tolerance of 1 % of the span
  (or of 0..100) / 2 recording steps; operator `range` rules (active/approved, compiled limits reused, compiler
  untouched) always lie inside the range (an allowed reading is plausible by definition; the rule check reports
  operating violations itself).
- Roles: `plausibility` = outside the plausible range (cannot be real: sensor/conversion/logging error);
  `out_of_range` = inside it but > `range_sigma` robust sigma (unusual but possible). A reading is reported by one
  of them only; readings inside a detected unit shift go to `unit_shift` only. `sign_violation` is no longer emitted
  (the non-negative hint covers it). Ranges + derivation are stored in `quality_stats.json["plausible_ranges"]`
  and one `plausible_range` evidence item; the statement says where each bound comes from.
- `detect/suspicious.py` and `detect/events.py` read `plausibility` points too (one token each) so implausible
  readings stay in the suspicious-rows list.

### #20 Timeliness - `checks.py`
- No time column (or no usable timestamps in the batch): one check per batch, `check_type
  "timeliness_not_testable"`, status `"not_testable"` (new `CheckStatus`), statement "Timeliness cannot be tested:
  the file has no time column. ...". No `timeliness_ok` then. `is_problem(status)` in `_common.py`.
- Consumers made neutral: trust (`counts_for_trust`), assessor scores (category score `None` when only not-testable,
  `not_testable` list, overall over tested categories, what-if deltas tolerate None), assessor actions,
  report (status label, counts "not testable n", grey chart segment, verdict sentence en/fi/sv), PDF, brief.py
  (item headline "This check could not be run", problem words), evidence_plain (explainers, status word),
  plain.py, suspicious/events (skip), server `/checks` summary already tolerant.

### #7 part 2 Confidence on every check - `_common.check_confidence`
- `values.confidence` (0..1) and `values.confidence_basis` (plain words) on every check incl. rule checks:
  sample size (n / (n + 30)) x distance from the pass/fail boundary (ratio 1 -> 0.5, 2x -> 0.89, 3x -> ~1); exact
  tests (duplicates, infinite values, dropouts) 0.97 x size; not_testable 0.0. Not shown in statements.

### #5 Domain-aware wording - `_common.WORDING`, `data_wording(ws)`
- `domain.json` sensor_stream >= 0.5 (or no domain.json) = sensor words; else column / entries / "a copied or default
  value" / "entry or export problem". Applies to stuck, saturation, stale, missing, dropout, unit shift,
  quantization, relation break, duplicates, plausibility (and its hints: "a count, amount or price"), all grouped
  checks and the trust reasons of grouped checks.

## Before / after (quality stage on COPIES of finished runs; before = HEAD e70f160 code, after = this branch)
Originals untouched (only dataset.parquet + json inputs copied to the scratchpad). Stride 1 everywhere.

| run copy | code | checks | fail | warn | pass | not testable | grouped | untrusted batches | seconds |
|---|---|---|---|---|---|---|---|---|---|
| reviewed run (15.3 M rows, 52 signals, no time column) | production output the reviewer saw | 653 | 293 | 340 | 20 | 0 | 0 | 0 / 10 | 210 |
| same | HEAD | 839 | 429 | 390 | 20 | 0 | 0 | 0 / 10 | 204 |
| same | this branch | 401 | 176 | 205 | 10 | 10 | 17 | 10 / 10 | 207-228 (shared machine) |
| te_2m (2 M rows) | HEAD | 768 | 342 | 404 | 22 | 0 | 0 | 0 / 10 | 37 |
| te_2m | this branch | 318 | 111 | 185 | 12 | 10 | 12 | 8 / 10 | 24-36 |
| demo_cli (timestamps) | HEAD | 268 | 26 | 1 | 241 | 0 | 0 | 0 / 66 | 5.7 |
| demo_cli | this branch | 266 | 24 | 1 | 241 | 0 | 0 | 0 / 66 | 4.8 |
| extra_uneven_headerless (no header, no time) | HEAD | 51 | 5 | 4 | 42 | 0 | 0 | 0 / 12 | 1.2 |
| extra_uneven_headerless | this branch | 51 | 5 | 4 | 30 | 12 | 0 | 0 / 12 | 1.2 |

Reviewed run by type (HEAD -> branch): stuck fail 262 -> 0, stuck warn 34 -> 2, saturation 12 -> 0,
quantization_change 168 -> 22, new frozen_block 10 (one per batch: 241-286 blocks, 3.9-6.6 % of the rows, median 27
signals at once, 31 in total) and quantization_block 7, out_of_range 127 -> 124 (readings inside unit shifts are
reported by unit_shift only), timeliness_ok 10 -> timeliness_not_testable 10; duplicates / unit_shift / local_spike
unchanged in count (duplicate share now batch-wide: B00001 4.08 % -> 4.5 %). The old per-signal stuck checks said
"0.87 % of the batch" because only the first 48 runs were counted; the real common-mode share is ~5 %.
- B00001 before: "Data in batch B00001 has structural problems: 62618 exact duplicate rows ... (4.08%) ..." (trusted,
  0.63). After: "This data cannot be trusted as a whole: in batch B00001, 4.5% of its rows are frozen in 27 signals at
  once (a logging or data problem); 4.5% of its rows are exact copies of earlier rows. Together these problems bring
  its trust score to 0.49, below the threshold of 0.50."
- B00005-B00010 after: 6.1-6.6 % frozen + 16.4-16.5 % copies -> "23% of the rows are not real, independent
  measurements, so statistics and alarms computed on this batch are unreliable."
- Run statement: "This data cannot be trusted as a whole: 10 of 10 batches (100% of the rows) failed the trust check,
  mainly because of duplicated rows. 176 checks failed and 205 warned. Timeliness could not be tested."
- Plausible ranges: 52 signals, 19 non-negative, 7 percentage-like, 26 data-only; no reading outside them (the
  valves' -0.7 / 100.6 extremes are within the 1-point tolerance) - item 19 on this data is transparency.
- demo_cli: the 1e5 spike is `plausibility` ("outside its plausible range 2242 to 3177 ... The range comes from the
  data ...") instead of out_of_range; the x1000 unit shift is reported once (unit_shift) instead of unit_shift +
  out_of_range; 5 duplicates in a 185-row batch (2.7 %) are now row-scoped.
- extra_uneven_headerless: 12 trivial timeliness passes -> 12 "Timeliness cannot be tested: the file has no time
  column"; 3 out_of_range -> 3 plausibility (a signal with normal range -0.29..0.35 reading 577.7 / 1e5).
- samples/demo_records.csv (ingest+profile+quality in a scratch workspace, domain business_records): "S04 is frozen at
  7.5 for 61 entries ...: the same value was repeated, e.g. a copied or default value, not a real change; the column
  is treated as unreliable in those rows"; negative quantities -> plausibility "(a non-negative quantity such as a
  count, amount or price)".

## Tests
- New `tests/test_b_quality_round6.py` (19 tests): grouping unit tests, one frozen_block instead of per-signal stuck
  (members traceable, independent freeze still its own check), small block row-scoped, duplicates at 1 / 5 / 16 %
  (not scoped / scoped / untrusted as a whole + run statement), duplicates across read chunks, plausible-range
  derivation (percentage, temperature near 100 is not a percentage, outlier-inflated max is not a percentage,
  non-negative, data-only, rule widening), plausibility vs out_of_range disjoint, operator rule limits, timeliness not
  testable / tested, missing_block, confidence on every check, domain wording, >48 frozen runs counted, compute_trust
  == verdict and the drop-duplicates what-if.
- Changed on purpose: `tests/test_b_quality.py` (the injected 99999 spike is now `plausibility`, not out_of_range);
  `tests/test_b_assessor.py::test_dq_scores` asserted `timeliness == 1.0` with no timestamps - exactly review item
  20 - now `None` / `not_testable == ["timeliness"]`.
- Full suite on this branch: 530 passed, 7 skipped, 1 xfailed (9 min). `test_b_*` 95, `test_c_*` 26 + 1 xfail.
- The decision-log calls in `tpm/quality/__init__.py` are untouched (another agent owns logging); the stage summary
  dict they log now also carries `n_not_testable`, `verdict` and `statement`.

## UI follow-ups (not edited: tpm/api/static is owned by others)
- `js/views/quality.js`: `isOk` (l.56) and every "problems" filter (kinds chips l.64-79, heat map l.343-350, pieces
  l.363-375, per-category l.409-411) treat anything that is not `pass` as a problem, so `status: "not_testable"`
  shows as a warning chip today. Treat it as its own grey state ("not testable", never counted as pass or problem).
  `/checks` `summary` (l.258) now has a `not_testable` count per category; the status column / `rowClass`
  (l.262, 273) need a label and a grey `st-not_testable` row class.
- Show `values.confidence` (+ `confidence_basis` as tooltip) in the check table / detail (item 7).
- Show the run verdict `summary.statement` (new field of `GET /api/runs/{id}/trust`, from `quality_summary.json`)
  in the trust banner instead of deriving "fine" from the untrusted-batch count.
- Per batch: `TrustVerdict.untrusted_rows` / `untrusted_row_share` = rows untrusted for EVERY signal (duplicated,
  frozen or missing records); today only `local_untrusted` (per signal) is drawn.
- Grouped checks (`frozen_block`, `missing_block`, `quantization_block`): the detail should list
  `values.members` (per-signal traceability: n_runs, rows, longest_run, as) and `values.blocks` ([start, end, n]).
- i18n `static/i18n/{en,fi,sv}.json`: `dq.not_testable`, `dq.why.frozen_block`, `dq.why.missing_block`,
  `dq.why.quantization_block`, `dq.why.plausibility`, `dq.why.timeliness_not_testable` (whyType/typeWord fall back
  to the raw type today).
- `core.js` `st()` + `styles.css` (`.st.*`, `.tbl tr.st-*`) and `styles-views.css` (`.dq-chip.*`): a grey
  not-testable variant; `charts.js` pass/warn/fail colours: grey for not_testable.
- `js/views/assessor.js`: category scores are `null` when not testable (already filtered out); could say
  "timeliness: not testable" from `dq_scores.not_testable`.
- `runs.js` / `dataflow.js`: the quality stage message now ends with the run statement (longer text).
- Round-5 `tpm/api/advice.py` (uncommitted in main when this was written): add library entries for `frozen_block`,
  `missing_block`, `quantization_block`, `plausibility`, `timeliness_not_testable`, and treat status `not_testable`
  like a pass (no fix advice).
- `tpm/llm/agent.py` l.673 (`status != "pass"` decides whether check lines go into the chat context): harmless,
  but `is_problem()` would be exact; not edited because that file is being changed by another agent.

## How to continue
- Tunables are module constants: `trust.DUP_ROW_SCOPE_MIN / RECORD_BATCH_SHARE / RECORD_PENALTY`,
  `plausibility.SPAN_MARGIN / TOL_* / PCT_*`, `grouping.MEMBER_SHARE / CLUSTER_JACCARD`, `_common.common_mode_k`.
- Duplicates are detected per batch (not across batches); common-mode spikes (a corrupted record in many signals)
  are not grouped yet (a real process disturbance also moves many signals).
- detect could read `TrustVerdict.untrusted_rows` (record-level ranges, all signals) in `trust_context`; today it
  sees block members through `local_untrusted` (largest 12 blocks per batch).
