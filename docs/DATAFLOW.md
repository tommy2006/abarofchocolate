# Data-flow record

What stays inside the operator environment, what may leave it, to which model, why, and how that is proven.

## 1. The picture

```
 OPERATOR ENVIRONMENT (this machine)                                  OUTSIDE
 ┌──────────────────────────────────────────────────────────────┐
 │  input file / stream ──► ingest ──► dataset.parquet           │
 │        (never leaves)      │                                   │
 │                            ▼                                   │
 │   profile / quality / detect / diagnose / assess               │
 │   (plain code: fingerprints, relations, checks, scores,       │
 │    flags, patterns, diagnoses)  ──► workspace/<run>/*.json     │
 │                            │                                   │
 │        raw rows allowed    │   derived artifacts only          │
 │             ▼              │            ▼                      │
 │   local model (Ollama)     │      EGRESS GUARD  ─ blocked ─┐   │
 │   gemma4:e4b-it-qat        │            │ allowed         │   │
 │   tool agent may query     │            ▼                 │   │        ┌─────────────────────┐
 │   dataset.parquet          │      egress ledger ──────────┼───┼──────► │ external model      │
 │                            │      (EGR-…, hash, preview)  │   │  only  │ Anthropic / EU host │
 │                            └──────────── fallback ◄───────┘   │  under │ (hybrid, eu-hosted) │
 │   decision_log.sqlite (hash chain)   report_<lang>.html       │ hybrid └─────────────────────┘
 └──────────────────────────────────────────────────────────────┘
```

The same diagram is drawn (inline SVG) in section 8 of every report, with the live call counts of that run.

## 2. Profiles (config/settings.yaml → `profiles.*`)

| | `no-egress` (default) | `hybrid` | `eu-hosted` |
|---|---|---|---|
| network model calls | **none** | Anthropic (`external_llm.model`) | Mistral Large 3 on Verda in Finland (`profiles.eu-hosted.external_llm`, see section 5) |
| guard | strict | non-strict | strict |
| `column_roles` (sample rows + stats) | local | local | local |
| `sensor_hypotheses` (signal catalog + relation summary) | local | external | external |
| `rule_compile` (rule text + signal catalog) | local | external | external |
| `diagnosis_narrative`, `critique` (diagnosis + evidence statements) | local | external | external |
| `why_chat`, `assessor_chat` (tool agent) | local, full tools (may read exact rows) | external, aggregate-only tools, every tool result through the guard | same as hybrid |
| `report_narrative` (report section summaries) | local | external | external |

`column_roles` (its payload holds sample rows) is **always local**, in every profile. The chat agent on the external
route gets no `sql` tool, `stats` without min / max, `series` as at most 20 bucket means, and every tool result passes
the guard; on the local route it keeps its full tools (section 7 says why).
If the external route is selected but no API key is present, the guard blocks the payload, or the provider fails,
the router falls back to the local model, then to the code template, and the ledger records the fallback.

Switch profiles without touching code: `TPM_PROFILE=hybrid` in `.env`, `python -m tpm run … --profile hybrid`, or
the UI settings. The active profile is printed by the CLI, shown in the UI header and stated in the report.

## 3. What may leave, what never leaves

Only `tpm.llm.router` can talk to a network model, and only with payloads built from contract objects
(`SignalDescriptor`, relation summaries, `CheckResult`, `Flag`, `Diagnosis`, rule text). Every external payload
passes `tpm.llm.guard`, whose limits are configuration (`guard.*`):

| Rule | Default | Effect |
|---|---|---|
| `min_aggregate_n` | 30 | an aggregate (mean, std, quantile) computed over fewer samples is dropped |
| `max_series_points` | 20 | no numeric series longer than this leaves (a 20-point sketch is not a signal) |
| `max_numeric_values_per_payload` | 4 000 | total numbers per payload (a 50-signal catalog carries ~1 300 rounded aggregates) |
| `max_payload_bytes` | 200 000 | size cap |
| `forbid_categorical_values` | true | no categorical / free-text record values (names, comments) |
| `forbid_row_like_structures` | true | no lists of records sharing numeric fields, and no raw row written as text: a sentence with 5 or more different `signal = number` pairs (`S01=0.251, S02=3660, ...`) is dropped, because rounding alone would let it through. Narrow tables (a few numeric columns next to many text columns, e.g. business records or logs): records are rows as soon as 3 of their numeric fields are the data's own columns, and a sentence with 3 pairs is a raw row when it also names a row number, a time stamp or a file |
| `external_sig_digits` | 3 | every float that leaves is rounded to this many significant digits, also inside sentences |
| `alias_names_external` | true | original column names never leave in any external profile (hybrid and eu-hosted): they become `S01…` aliases, also inside sentences, chat questions and rule text; very generic header words (`sample`, `time`, `source`, ...) are removed wherever a field names columns and left alone inside sentences, where they are ordinary words |
| `drop_keys_external` | min, max, value, rows, timestamp, source_path, label, ... | fields that carry single readings, data time stamps, file names or label-based evaluation are removed at any depth |

A field that looks like raw data is dropped on its own and the rest of the payload still leaves. A payload is blocked
when nothing useful is left (also when only empty skeletons such as `{"signals": [{}]}` would remain), when a limit
is exceeded, or when the last check on exactly what would be sent (the invariant) finds anything raw.

May leave (hybrid / eu-hosted only, after the guard): signal-catalog aggregates, relation summaries (r, lag),
check / flag / diagnosis statements with aliases, evidence statements, plain-language rule text, chat questions.

Never leaves: raw rows, `dataset.parquet`, `scores.parquet`, prompts to the local model, the decision log, reports,
original column headers (in any external profile), human notes in strict mode.

`tpm.llm.guard.explain(settings)` renders this list for the current settings (shown in the Data flow view).

## 4. The egress ledger

Every model call, local or external, is an `EgressRecord` in `workspace/<run_id>/egress_ledger.jsonl` and an entry
in the decision log:

```
id, ts, task, purpose, route (local|external), provider, model, artifact_types,
payload_bytes, payload_hash, payload_preview (first 500 chars of the SANITISED payload, external only),
guard_result (allowed|blocked|budget|unavailable|fallback|n/a|demo_allowed|demo_blocked), guard_reason,
response_hash, latency_ms, ok, error, input_tokens, output_tokens,
sanitizer (what the guard changed; for local calls: what it WOULD have changed, "mode": "audit")
```

`demo_allowed` / `demo_blocked` are the records of `python -m tpm guard-demo` (section 8): payloads that were shown,
never sent. `ledger.summary`, `ledger.usage`, the run budget and the data-flow statement keep them out of every count
of real model calls.

Where to see it: UI → *Data flow*; report section 8 (summary counts + full table); `python -m tpm export` (the
JSONL file); `tpm.llm.ledger.summary(ws)` and `data_flow_statement(ws, settings, language)` for programmatic use.

## 5. Swapping the model layer

| Want | Change |
|---|---|
| another local model | `local_llm.model` in settings.yaml or `TPM_LOCAL_MODEL=qwen3:8b`; `python -m tpm models` shows what is pulled; `python -m tpm bakeoff` compares candidates |
| Ollama on another host | `OLLAMA_HOST=http://host:11434` - only a host inside the operator environment: the local route sends full payloads and is not guarded |
| external model | `external_llm.model` or `TPM_EXTERNAL_MODEL`; key in `ANTHROPIC_API_KEY` |
| EU-hosted model | `TPM_PROFILE=eu-hosted` (or the Data-flow page) and `TPM_EU_API_KEY=…` in `.env`; endpoint and model are already in `profiles.eu-hosted.external_llm` |
| another EU service | `TPM_EU_BASE_URL`, `TPM_EU_MODEL`, `TPM_EU_PROVIDER` (`openai-compatible` or `anthropic`); its host must be on `profiles.eu-hosted.eu_hosts` |

**The EU-hosted model.** `eu-hosted` sends the guarded payloads to **Mistral Large 3**
(`mistralai/Mistral-Large-3-675B-Instruct-2512-NVFP4`, a European open-weight model), which the hackathon organisers run
on **Verda** serverless GPU containers (Verda, formerly DataCrunch, is a Finnish GPU cloud with its data centres in
Finland) at `https://containers.datacrunch.io/data-sovereignty-mistral-large-3/v1`, an OpenAI-compatible endpoint.
Only the API key goes with each request, as a Bearer token; no workspace or organisation header. The profile refuses
any host that is not on `profiles.eu-hosted.eu_hosts` (Verda, Mistral's EU endpoint `api.eu.mistral.ai`, Claude on
Amazon Bedrock in Stockholm or Ireland), the first-party Anthropic API (it has no EU-only processing), and Bedrock
cross-region model ids that may run outside the EU (`global.`, `us.`, ...). Every ledger record of an eu-hosted call
names the host (`provider = "openai-compatible @ containers.datacrunch.io"`), and the data-flow statement names the
operator and the location. The guard and its invariant are the same as in hybrid, in strict mode.
| stricter guard | `guard.*` values, or `profiles.<name>.guard_strict: true` |
| no model at all | nothing to do: every narrative has a template version; `--no-llm` skips model calls entirely |

## 6. Evidence that it works

- `python -m tpm models` — routes and pulled models for the active profile.
- Run the demo under the default profile: the report's data-flow statement reads "no data of any kind left the
  operator environment" and the ledger contains only `route = local` entries.
- Run with `--profile hybrid` and a key: the ledger lists each external call with its payload preview and hash;
  blocked payloads are visible with their reason.
- `tests/test_d_*` exercise the guard on raw-looking payloads; `tests/test_f_report.py` checks that the report
  renders the ledger and the statement in all three languages.
- `python -m tpm guard-demo --run <run>` proves the guard on the run itself (section 8).
- `tests/test_d_guard_demo.py` runs every external task, the chat and rule compilation on a dataset with
  Tennessee-Eastman headers (`xmeas_1 .. xmeas_41`, `xmv_1 .. xmv_11`, `faultNumber`, `simulationRun`, `sample`) in the
  eu-hosted profile (strict guard, fake EU endpoint) and in hybrid, with questions, operator hints and rule text that
  name those headers: none of them reaches the (stubbed) provider, nor the ledger previews.
- `python -m tpm verify-log <run>` checks the hash chain and says, per object type, what is logged (section 10).

## 7. Local calls may see more, on purpose (audit mode)

The local model runs on this machine: nothing it reads leaves it. So the guard removes nothing from a local prompt, and
the chat's local tools may read exact rows (`sql`, `stats` with min / max, the full series): that is how the local chat
answers "why this row?". The external route never gets any of that.

The guard still looks at every local call, in **audit mode** (`guard.audit_local`, `guard.audit_messages`): the payload
is sent to the local model unchanged, and the ledger record says what the guard would have done had the call gone out:

```
guard_result: n/a
guard_reason: local route: the model runs on this machine and nothing leaves it, so the guard removes nothing (a local
              model may see exact readings, original column names and rows). Audit only, nothing was removed: had it
              gone out, the guard would have replaced 3 original column name(s) by aliases, rounded 2 number(s) to 3
              significant digits, removed 1 date / time stamp(s); it could have left.
sanitizer:    {"mode": "audit", "applied": false, "would_be": "allowed", "names_aliased": 3, "floats_rounded": 2, ...}
```

For the chat agent the record also counts the tool results that carried exact rows (`sql_results`). The audit costs
8 to 17 ms per call and one scan of the text columns per run (0.1 s on the 2-million-row practice file, cached), next
to 15 to 40 s for the local model call itself. A local call that is a fallback after a blocked or failed external
attempt keeps `guard_result = fallback` and the reason of the external attempt.

## 8. Proof on a real run: `python -m tpm guard-demo`

```
python -m tpm guard-demo --run <run_id> [--profile hybrid|eu-hosted] [--send] [--json]
```

1. **A real payload of the run** (the narrative payload the diagnose stage builds for the strongest diagnosis; the
   signal catalog when there are no diagnoses) goes through `guard.check` as the router would send it. The output says
   in plain words what was aliased (`xmeas_1 -> S01`), rounded (`0.1222 -> 0.122`), replaced (`[time]`, `[value]`,
   `[file]`) and removed, the bytes before and after, and the verdict.
2. **An operator question** naming original headers, a label column and the source file:
   `Why did xmeas_1 rise before xmv_11 in DIAG-000001 while faultNumber was set? The data is from te_head2m.csv.` becomes
   `Why did S01 rise before S52 in DIAG-000001 while [column] was set? The data is from [file].`
3. **A deliberately unsafe payload**, built from the run's real rows: raw records under all original headers,
   full-precision readings, a 50-reading series per signal, data time stamps (built from the row numbers when the data
   has no time column), label values, the source file name, as records and written out as text. The guard blocks it
   ("nothing useful left after sanitising") and lists each layer that stopped a part of it; the output also shows what
   the last check alone (the invariant) would have found. It is **never sent**, with or without `--send`.

All three attempts are written to the run's egress ledger (`guard_result` `demo_allowed` / `demo_blocked`, purpose
"guard demonstration: ...") and to the decision log; `guard_demo.json` in the run directory holds the result, and
report section 8 renders it in the report's language (`tpm.llm.guard_demo.report_context`). With `--send` (off by
default) the real payload is afterwards sent once through the normal router (guard, budget, ledger), so that one real
external call can be shown next to the demonstration. The command exits with 2 if the unsafe payload was not blocked
or an original header was found in anything that would be sent.

On the 2-million-row practice run (copy of `workspace/te_2m`, eu-hosted, strict): the real payload of DIAG-000001
(7 821 bytes) left with 2 numbers rounded and 1 time stamp replaced (the stages already use aliases); the question lost
its three headers and the file name; the unsafe payload (3 rows under 57 headers, 162 full-precision readings, a
150-reading series, time stamps, 3 label columns, the file name) was blocked; none of the 57 headers appeared in what
would be sent; 0.36 s.

## 9. Who writes the explanations: model or template

Every diagnosis, critique and report summary has an evidence-based template version; a model adds prose on top when
one is available and there is time. `tpm.llm.ledger.narrative_coverage(ws)` counts, per run, how many diagnoses /
critiques / report summaries a model wrote (local or external, which model) and why the rest are templates:

| reason | when | sentence (end) |
|---|---|---|
| `local_budget` | local model | a local model needs about 46 s per explanation, so the diagnose stage spends at most half of its time budget (90 s here) on the strongest diagnoses first |
| `external_cap` | external model | the external model explains at most `external_llm.max_narratives_per_run` (12) diagnoses per run, the strongest first |
| `no_llm` | run made with `--no-llm` | language models were switched off |
| `no_model` | nothing reachable | no language model was reachable when the run was made |

The sentence is printed by `python -m tpm models [--run <id>]`, by `guard-demo`, at the end of the data-flow statement
(UI) and in report section 8 (en / fi / sv), e.g. for the 15.3-million-row run: "3 of 3,857 diagnoses have a
model-written explanation (local model gemma4:e4b-it-qat); the other 3,854 use the evidence-based template, by design:
... the template states the same findings with their evidence IDs, and the chat explains any diagnosis on request."

## 10. Decision-log completeness

Every object a stage creates has an entry of its own in the hash-chained decision log:

| object | entry | written by |
|---|---|---|
| flag | `flag` (id, kind, group, rows, score, severity, cause, pattern, top signals, evidence ids) | detect stage, all flags (the old 2 000-flag cap is gone), in bulk |
| data-quality check | `check` (id, type, category, status, severity, signals, batch, rows, rule id, evidence ids) | quality stage, baseline checks and rule checks, one entry per check, in bulk |
| trust verdict | `trust` per batch, again when rule checks change it | quality stage |
| diagnosis, critique, narrative | `diagnosis`, `critique`, `narrative` | diagnose stage |
| inference | `inference` | the stage that makes the claim; claims a stage registered without an entry are logged at the end of the stage |
| model call | `egress` (the ledger record) | router |

Not logged one by one, on purpose, and said so by the audit: **raw readings** (entries carry ids, status, aliases,
rows, scores and evidence ids; a check statement such as "S01 is frozen at 0.0" and a check's `values` stay in
checks.jsonl), **evidence items** (they are the facts entries cite by id), and the profile stage's per-signal
instrument / unit-operation guesses (counted in its one `hypotheses` entry).

`DecisionLog.record_many(entries)` writes thousands of entries in one transaction per 5 000: write lock first
(`BEGIN IMMEDIATE`), then the last hash, then the entries chained one after the other, so the chain stays linear with
concurrent writers. On this laptop: 4 064 flags in 0.13 s and 653 checks in 0.02 s, against 0.9 ms per entry (4.3 s for
the same 4 717 entries) with one transaction each.

`python -m tpm verify-log <run> [--json]` prints the chain check and then, per object type, how many objects the
artifacts hold, how many are logged, and for every gap which objects and why. On the 15.3-million-row run made
before these changes it reports the 2 064 flags above the old cap, the 653 checks that had only a per-batch summary,
and the 91 profile hypotheses; on a run made now, flags and checks are complete (2-million-row practice file: 3 124
flags logged in 0.09 s, 768 checks in 0.05 s). The export bundle (`python -m tpm export`) carries the audit in
`verify.json` and the guard demonstration in `guard_demo.json`.

## Hybrid profile since 2026-09-19: sanitise, verify, then send (or do not send)

The egress guard no longer only decides "allowed / blocked"; it rewrites what would leave and then proves an
invariant on the result (`tpm/llm/guard.py`: `check`, `verify_invariant`, `sanitize_text`, `verify_texts`):

1. whitelist of artifact types; unknown or raw-looking fields are dropped one by one (field-level fail-closed);
2. original column names -> aliases `S01..`, also inside sentences, chat questions and rule text;
3. single raw readings removed at any depth (`min`, `max`, `first`, `last`, `value`, `points`, `series`, `rows`,
   time stamps, file names, label-based evaluation); limits an operator typed into a rule stay;
4. every float rounded to 3 significant digits (also decimals inside strings; epoch-sized integers too);
5. dates, clock times, epoch stamps -> `[time]`; values of the dataset's text / category / label columns ->
   `[value]` (vocabulary scanned locally, cached in `egress_vocab.json`); file names -> `[file]`;
6. invariant on the final payload and on the final chat message texts: no float with more than 3 significant
   digits, no date/time, no original column name, no vocabulary value, no long numeric list, no raw row written as
   text, no dropped key. A violation blocks the call and is written to the ledger; the task then runs on the local
   model.

Tests that keep this true: `tests/test_d_guard.py`, `tests/test_d_egress_no_raw.py` (a real pipeline run with a
capturing stub provider: nothing captured may contain a raw cell value, an original column name, a label value, a
data time stamp or the file name), `tests/test_d_hybrid_chat.py` (the same for a multi-step chat turn whose question
and history are seeded with raw values). "Rounded" means rounded: no random noise is added.
