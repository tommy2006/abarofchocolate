# Round 6 (2026-09-19): the expert review, item by item

An expert reviewed our output on the 6 GB practice file and listed 30 fixes in six tiers. Two subagents took small
parts (D: data quality, E: logging, guard and data flow; their worklogs are round6_D.md and round6_E.md); the lead
did the rest and the integration. Measured on the same file with the final merged code unless stated otherwise
(`workspace/te_full_r7`, 15.33 M rows, run without the language model).

| # | Review item | Status | Where |
|---|---|---|---|
| 1 | Data problems and saturated valves called sensor faults | done: 0 sensor faults (was 139 on S44); 80 events read "actuator saturation: S44 pinned at its upper limit"; common freezes and duplicated rows are data problems | `tpm/detect/attribution.py` (rule order), `tpm/diagnose/diagnosis.py` |
| 2 | Manipulated vs measured | done: 9 actuators found (the xmv valves) from 0-100 % range, pinned readings, steps and lead / lag | `tpm/profile/roles.py` `manipulated_evidence` |
| 3 | Rule to check, for real | done: rules compiled into checks with rule ids and pass / fail per batch | `python -m tpm showcase`, `tpm/showcase.py` |
| 4 | Human in the loop, for real | done: accept, question, override; a later event of the same kind takes the person's label | `tpm/showcase.py` `_hitl_step` |
| 5 | Second domain | done: business records end to end | `samples/demo_records.csv`, `docs/ADAPTABILITY.md` |
| 6 | False alarms, persistence, precision / recall | done: an event needs the median score of one window above a threshold calibrated on held-out normal stretches; 0 of 1,000 normal runs, 65 % of faulty runs, precision 1.00, recall 0.42 | `tpm/detect/events.py`, `tpm/detect/evaluate.py` |
| 7 | Calibrate or label the confidence | done: labelled heuristic, calibration table when labels exist, certainty on every check | `tpm/detect/evaluate.py`, D |
| 8 | A critique that argues alternatives | done: four alternatives argued from the evidence; weakened 2,692 of 3,866 (most events lie in batches that failed the trust check), every disagreement logged | `tpm/diagnose/critique.py` |
| 9 | Name faults or say "cannot name" | done: patterns A, C and E named Fault 4, 2 and 1 from the known-failure catalogue; B and D "cannot name" with the closest candidate | `tpm/diagnose/fault_names.py` |
| 10 | A trust verdict that can fail | done (D): record-level trust; 10 of 10 batches untrusted on the reviewed run | `tpm/quality/trust.py` |
| 11 | Attribution spread and a lag reference | done: "no single signal dominates: about N signals of cluster C" when no signal carries 20 % (109 of 4,038 flags on this file), and a lag reference when the onset order can be measured | `tpm/detect/attribution.py` `_spread` |
| 12 | A diverse sample in the report | done: round-robin over cause and pattern | `tpm/report/report.py` `_diverse` |
| 13 | Unit operations with evidence and falsifier | done: the local model named 7 of 11 clusters, each with its evidence and what would disprove it (on a run with the model) | `tpm/profile/roles.py` `llm_unit_operations` |
| 14 | Lag direction as upstream / downstream | done | `tpm/detect/cascade.py` |
| 15 | Test the hypotheses | done: actuator hypotheses tested against the signals they drive or follow | `tpm/profile/roles.py` `check_hypotheses` |
| 16 | Drift trends first-class | done: per-signal trend plots with the normal band and a Kendall trend test | `tpm/report/report.py` `_drift_trends` |
| 17 | Justify the baseline | done: why this baseline and what breaks if it is wrong | `tpm/report/report.py` `_baseline_ctx` |
| 18 | Group duplicated findings | done (D): frozen / missing / quantisation blocks as one finding | `tpm/quality/grouping.py` |
| 19 | Plausibility checks | done (D): a plausible range per signal with its source | `tpm/quality/plausibility.py` |
| 20 | Timeliness "not testable" | done (D, UI by the lead): grey state, never a pass | `tpm/quality/checks.py`, `js/views/quality.js` |
| 21 | Log completeness | done (E, UI by the lead): every object has an entry of its own; gaps listed with the reason | `tpm/log/completeness.py`, Log page, `verify-log` |
| 22 | Hybrid / guard demonstration | done (E, UI by the lead): real payload before / after, unsafe raw rows blocked | `tpm/llm/guard_demo.py`, Data flow page, `guard-demo` |
| 23 | Guard the local calls or justify | done (E): local calls audited, not stripped, with the reason | `tpm/llm/guard.py` audit mode, `docs/DATAFLOW.md` |
| 24 | Headers never leave in strict mode | done (E), tested for every external task | `tests/test_d_guard_demo.py` |
| 25 | Document the template narratives | done (E, UI by the lead): "who wrote the explanations" in the report, Data flow and Diagnoses pages | `tpm/llm/ledger.py` `narrative_coverage` |
| 26 | Why-chat demonstrated | done: one question answered by the local model on a specific flag (98.6 s, showcase on the practice run) | `tpm/showcase.py` `_chat_step` |
| 27 | Third domain | done: web-service log with free text; database incident and logging bug found, the memory leak missed (stated) | `tpm/ingest/events_adapter.py`, `samples/demo_log.csv` |
| 28 | Shorter explanations | done: 3-sentence headline, steps folded | report template |
| 29 | Heat map and lag summary | done | `tpm/report/charts.py` |
| 30 | One-page summary | done: one A4 page, also a button on the Report page | `tpm/report/summary_pdf.py`, `report --format summary` |

Found while integrating: the guard let three raw records of a narrow table (5 numeric columns next to 6 text
columns) through, aliased and rounded but still rows. Records are now rows as soon as 3 numeric fields are the data's
own columns, and a sentence with 3 value pairs is a raw row when it also names a row number, a time stamp or a file
(`tpm/llm/guard.py`, `tests/test_d_guard.py::test_narrow_table_rows_never_leave`). Also fixed: `run --rules` wrote
uncompiled drafts that the quality stage then duplicated; the diagnose stage writes its log records in bulk.
