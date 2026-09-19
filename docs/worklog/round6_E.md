# Round 6, agent E (2026-09-19): log completeness, guard proven on a run, local calls audited, headers, who writes

Review items 21-25 (expert review of the 15.3 M-row run `workspace/run_20260919_100100_3fe3ed`). Branch
`worktree-agent-a73df0489d5b24cea`. Tests: `tests/test_a_log_completeness.py` (9), `tests/test_d_guard_demo.py` (8).

## 21. Log completeness
Done:
- `DecisionLog.record_many(entries, chunk_size=5000)` (`tpm/log/decision_log.py`): one transaction per chunk; write lock
  first (`BEGIN IMMEDIATE`), then the last hash, then the entries chained one after the other and inserted with
  `executemany`; a failed chunk is rolled back whole. One hash definition (`_entry_hash`) shared by `record`,
  `record_many` and `verify_chain` (existing logs verify unchanged). Also `logged_ids()` and `scan()` (cheap reads for
  the audit); `record` now stores the four text columns as text (an int object_id used to break the chain).
- `tpm/log/stage_log.py`: `flag_entry` / `check_entry` (ids, kind / status, signals, rows, score, severity, evidence
  ids; never a check statement or `values`, which may quote readings), `log_flags`, `log_checks`,
  `log_stage_inferences(ws, stage)` (claims a stage registered without an entry are logged at the end of the stage).
- Logging lines only: `tpm/detect/__init__.py` logs EVERY flag (the `flags[:2000]` cap is gone) in bulk, the stream
  path (`score_batch`) too; the detect `stage_summary` entry carries `n_flags_logged`, `log_seconds`,
  `n_inferences_logged_at_end`. `tpm/quality/__init__.py` logs every check (baseline + rule checks, pipeline and stream
  path) one entry each, logs the trust verdicts it recomputes after rule checks (they were rewritten in trust.jsonl
  without an entry), and its `done` entry carries `n_checks_logged`, `log_seconds`.
- Completeness audit `tpm/log/completeness.py` (`audit`, `format_audit`, `completeness_statement`);
  `python -m tpm verify-log <run> [--json] [--no-audit]` prints it under the chain check (exit code still = chain).
  The export bundle (`tpm/log/exports.py`) carries it in verify.json (`completeness`) and includes guard_demo.json.
Numbers (this laptop):
- record() in a loop: 0.91 ms per entry (4 717 entries = 4.3 s); record_many: 4 064 flags 0.13 s, 653 checks 0.02 s
  (copy of the 15.3 M-row run's log, 14 927 entries after, chain verified in 0.36 s).
- Quality stage on a copy of `te_2m` (2 M rows): 768 checks logged one by one in 0.05 s in total.
- Detect stage on the same copy: 3 124 flags, every one logged, in 0.09 s (the stage itself: 503 s); 2 detect
  inferences that had no entry were logged at the stage end. `verify-log` on that copy afterwards: flags 3 124/3 124,
  checks 768/768, trust 10/10, diagnoses and critiques 2 280/2 280, egress 31/31, patterns 6/6; left: the 87 profile
  hypotheses (summarised); audit 0.45 s over 10 705 entries.
- Audit of the 15.3 M-row run (made before these changes): 0.35-0.48 s; it names 2 064 flags above the old cap, 653 checks
  with only a per-batch summary, 91 profile hypotheses (summarised in the stage's `hypotheses` entry, n=91) and 1
  detect inference; evidence 5 186 (4 672 cited by a log entry, 408 only by artifacts, 106 by neither, by kind).
- Fresh run of samples/demo_process.csv: flags 12/12, checks 796/796, trust 66/66, diagnoses 6/6, critiques 6/6
  logged; left: 18 profile hypotheses (summarised) and 10 rule drafts (see follow-ups).

## 22. Guard proven on a run: `python -m tpm guard-demo --run <run> [--profile hybrid|eu-hosted] [--send] [--json]`
Done (`tpm/llm/guard_demo.py`, command in `tpm/cli.py`):
- real payload (narrative payload of the strongest diagnosis, as the diagnose stage builds it; catalog fallback) ->
  `guard.check` -> plain before / after: names aliased (with counts), numbers rounded (examples), time / value / file
  replacements, fields removed, bytes, verdict;
- an operator question naming original headers, a label column and the file -> before / after;
- a deliberately unsafe payload from the run's REAL rows (records under all original headers, full-precision readings,
  50-reading series, time stamps (from row numbers when there is no time column), label values, file name; as records
  and written out as text) -> blocked, reason layer by layer, plus what the invariant alone finds on the raw payload.
  Never sent; its readings are never copied into guard_demo.json, the ledger preview or the report;
- ledger records `demo_allowed` / `demo_blocked` (purpose "guard demonstration: ..."), also in the decision log; kept
  out of `ledger.summary` / `usage` / budget / statement counts; `guard_demo.json` in the run dir;
- `--send` (off by default): the SAFE payload goes once through `router.complete` (guard, budget, ledger); refused with
  a reason when the external route is not usable; exit code 2 if the unsafe payload was not blocked or a header leaked;
- report hook: `guard_demo.report_context(ws, lang)` (en / fi / sv); wired into report section 8 by a delimited block in
  `tpm/report/report.py` (`_dataflow_round6`) and `report.html.j2`; the ledger table colours demo_blocked red.
Guard fixes found while doing it (`tpm/llm/guard.py`):
- a raw row written as text ("row 5: S01=0.25038, S02=3674.1, ...") passed after rounding: now a string with >= 5
  different `signal = number` pairs (aliases, [column] or original names) is dropped, and the invariant checks it too;
- a payload stripped to empty skeletons (`{"signals": [{}]}`) counted as "useful": now blocked as nothing useful left;
- the TE meta column `source` made the guard rewrite the app's own keys `narrative_source` / `source` to `[column]` and
  drop them: `source` joined the generic header words (still removed wherever a field names columns);
- note "aliased N original column names" was the alias-map size, not replacements: reworded.
Numbers: te_2m copy, eu-hosted strict: real payload 7 821 -> 7 773 bytes, allowed, 2 numbers rounded, 1 time stamp;
question "Why did xmeas_1 rise before xmv_11 in DIAG-000001 while faultNumber was set? The data is from te_head2m.csv."
-> "Why did S01 rise before S52 in DIAG-000001 while [column] was set? The data is from [file]."; unsafe payload (3 rows
x 57 headers, 162 readings, 150-reading series, 3 labels, file name) blocked; 0 of 57 headers in what would be sent;
0.36 s. demo_cli copy (hybrid): same shape, 15 headers, blocked.

## 23. Local calls: audit mode
Done: `guard.audit_local(payload, settings, ws)` / `guard.audit_messages(messages, settings, ws)`; `router.complete`
(local route) and `router.local_chat` call them: nothing is removed, `guard_result` stays `n/a`, `guard_reason` says
plainly why local calls may see more (the model runs on this machine, nothing leaves it; the chat's local tools may
read exact rows to answer "why this row?") and what the guard WOULD have removed; `sanitizer` = `{"mode": "audit",
"applied": false, "would_be": ..., counts, notes}` (+ `sql_results` for the agent). Fallback-after-external records keep
`fallback` + the external reason. `ledger.local_why()` in the data-flow statement (says so when a run predates audit
mode). Cost: `audit_local` 8-17 ms, `audit_messages` < 1 ms, text-vocabulary scan once per run 0.10 s on te_2m.

## 24. Original headers never leave
`tests/test_d_guard_demo.py::test_te_headers_never_reach_the_provider[eu-hosted|hybrid]`: a TE-style dataset
(xmeas_1..41, xmv_1..11, faultNumber, simulationRun, sample; file te_style_secret_plant.csv) through the pipeline,
then every external task (sensor_hypotheses with a hint naming headers, rule_compile with rule texts naming them,
diagnosis_narrative, critique, report_narrative, plain views, why_chat with a question + history naming them,
assessor_chat) with a recording stub provider: none of the header strings (nor the file name) is in any message or
ledger preview; every external task was really attempted and allowed. The guard-demo tests check the same on its output.

## 25. Who writes the explanations
`ledger.narrative_coverage(ws)` (cached per run): diagnoses / critiques / report summaries written by a model (route +
model) vs template, reason `none | all | no_llm | no_model | external_cap | local_budget | generic`, from
diagnoses.jsonl, report_llm_<lang>.json, the ledger and the diagnose stage's note; `coverage_sentence(cov, lang)` and
`coverage_details` in en / fi / sv. Printed by `python -m tpm models [--run <id>]` (latest run by default), guard-demo,
the data-flow statement (UI), report section 8. 15.3 M-row run: "3 of 3,857 diagnoses have a model-written explanation
(local model gemma4:e4b-it-qat); the other 3,854 use the evidence-based template, by design: a local model needs about
46 s per explanation, so the diagnose stage spends at most half of its time budget (90 s here) on the strongest
diagnoses first; the template states the same findings with their evidence IDs, and the chat explains any diagnosis on
request."

## Docs
`docs/DATAFLOW.md`: routing table fixed (chat is external in hybrid), guard table (4 000 numbers, raw rows as text,
names never leave), ledger fields (demo_*, audit), new sections 7 (local calls, audit mode), 8 (guard-demo), 9 (who
writes the explanations), 10 (decision-log completeness).

## Tests run
- new: test_a_log_completeness.py 9 passed; test_d_guard_demo.py 8 passed.
- all of tests/test_d_*.py, tests/test_a_*.py, tests/test_f_*.py (with the two new files): 204 passed, 5 skipped
  (286 s). Baseline before the changes (all D, A, f_cli, f_report, f_report_fixes): 150 passed, 2 skipped.
- tests/test_b_*, test_c_*, test_e_*, test_g_live (stages whose logging lines changed, API reading the ledger):
  324 passed, 2 skipped, 1 xfailed (660 s). So the whole suite of this branch passes.
- by hand: `guard-demo` on copies of workspace/demo_cli (hybrid) and workspace/te_2m (eu-hosted), reports en / fi / sv
  on the te_2m copy, `verify-log` on a copy of the 15.3 M-row run, on the te_2m copy after re-running quality + detect,
  and on a fresh run of samples/demo_process.csv.

## Follow-ups for the lead (files I must not edit)
- UI Data-flow page (`tpm/api/static/js/views/dataflow.js`, `tpm/api/server.py`):
  - a "Guard demonstration" card from `guard_demo.json` (`tpm.llm.guard_demo.report_context(ws, lang)` gives
    localised lines; `load(ws)` the raw result) and a button that runs `guard_demo.run_demo(ws, settings)` (never with
    send=True from the UI without a confirmation);
  - chip colours: `demo_blocked` -> fail, `demo_allowed` -> neutral; `tpm/api/fallback.py::ledger_summary` counts every
    external record that is not allowed as "blocked": skip `guard_result in ('demo_allowed', 'demo_blocked')`
    (`tpm.llm.ledger.is_demo`);
  - "Who wrote the explanations": `ledger.coverage_sentence(ledger.narrative_coverage(ws), lang)` on the Data-flow
    and Diagnoses pages. Until then the English data-flow statement (`ledger.data_flow_statement`, extras on) already
    ends with that sentence and with the demonstration summary.
- UI Decision-log page (`views/log.js`): `tpm.log.completeness.audit(ws)` (rows: object, n_artifacts, n_logged,
  excluded[{n, reason, examples}]) and `completeness_statement(res)`; report section 6 could show the sentence too.
- PDF / PowerPoint exports (`tpm/report/pdf.py`, `pptx_export.py`) have their own data-flow part: add the coverage
  sentence and `report_context` lines.
- Profile stage: one line at the end of `run_profile`, `log_stage_inferences(ws, "profile")`, closes the last inference
  gap (per-signal instrument / unit-operation guesses).
- Diagnose stage: 2 single-transaction records per diagnosis (7 714 for the 15.3 M-row run, about 7 s at 0.9 ms each);
  `record_many` would make it well under a second.
- `tpm run --rules FILE` (cmd_run) pre-writes uncompiled rule drafts RULE-001..N into rules.json, including prose lines
  of `config/rules.example.md` ("Each non-empty line that does not start with `#` is one rule. ..."), and the quality
  stage then compiles the same file again into new ids: duplicates. The audit names them.
