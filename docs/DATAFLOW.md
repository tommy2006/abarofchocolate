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
| network model calls | **none** | Anthropic (`external_llm.model`) | same routing, endpoint = `external_llm.base_url` |
| guard | strict | non-strict | strict |
| `column_roles` (sample rows + stats) | local | local | local |
| `sensor_hypotheses` (signal catalog + relation summary) | local | external | external |
| `rule_compile` (rule text + signal catalog) | local | external | external |
| `diagnosis_narrative`, `critique` (diagnosis + evidence statements) | local | external | external |
| `why_chat`, `assessor_chat` (may query raw data through tools) | local | local | local |
| `report_narrative` (report section summaries) | local | external | external |

Tasks whose payload can contain raw rows (`column_roles`, the chat tools) are **always local**, in every profile.
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
| `max_numeric_values_per_payload` | 400 | total numbers per payload |
| `max_payload_bytes` | 200 000 | size cap |
| `forbid_categorical_values` | true | no categorical / free-text record values (names, comments) |
| `forbid_row_like_structures` | true | no lists of records sharing numeric fields |
| `allow_column_names` / `alias_column_names_in_strict` | true / true | original headers may leave in non-strict mode; in strict mode they are replaced by `S01…` aliases |

May leave (hybrid / eu-hosted only, after the guard): signal-catalog aggregates, relation summaries (r, lag),
check / flag / diagnosis statements with aliases, evidence statements, plain-language rule text, chat questions.

Never leaves: raw rows, `dataset.parquet`, `scores.parquet`, prompts to the local model, the decision log, reports,
human notes in strict mode.

`tpm.llm.guard.explain(settings)` renders this list for the current settings (shown in the Data flow view).

## 4. The egress ledger

Every model call, local or external, is an `EgressRecord` in `workspace/<run_id>/egress_ledger.jsonl` and an entry
in the decision log:

```
id, ts, task, purpose, route (local|external), provider, model, artifact_types,
payload_bytes, payload_hash, payload_preview (first 500 chars, external only),
guard_result (allowed|blocked|fallback|n/a), guard_reason, response_hash, latency_ms, ok, error
```

Where to see it: UI → *Data flow*; report section 8 (summary counts + full table); `python -m tpm export` (the
JSONL file); `tpm.llm.ledger.summary(ws)` and `data_flow_statement(ws, settings, language)` for programmatic use.

## 5. Swapping the model layer

| Want | Change |
|---|---|
| another local model | `local_llm.model` in settings.yaml or `TPM_LOCAL_MODEL=qwen3:8b`; `python -m tpm models` shows what is pulled; `python -m tpm bakeoff` compares candidates |
| Ollama on another host | `OLLAMA_HOST=http://host:11434` |
| external model | `external_llm.model` or `TPM_EXTERNAL_MODEL`; key in `ANTHROPIC_API_KEY` |
| EU-hosted endpoint | `TPM_PROFILE=eu-hosted`, `TPM_EXTERNAL_BASE_URL=https://…` |
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
