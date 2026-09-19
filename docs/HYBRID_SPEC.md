# Hybrid profile: limited, anonymised calls to the Anthropic API (spec, 2026-09-19)

Team decision (user, 2026-09-19): raw input data never goes to the API. **Aggregated, coarsened ("noised") or
anonymised** derived data may. External model: `claude-sonnet-5` (default, fastest) or `claude-opus-5`.
**Never Fable / Mythos models** (30-day data retention). Goal: see how much faster analysis and chat get.

Measured before this work (6 GB practice file, this laptop, `workspace/te_full_v2`): 1355 s total =
ingest 266 + profile 105 + quality 171 + detect 556 + diagnose 132 + assess 110 + report 14.
Local-model time inside that: 149 s (5 calls of 25-37 s: sensor_hypotheses 1, diagnosis_narrative 2, critique 2;
the diagnose stage could afford only 2 of N narratives). Chat: 1-2 min per message because the tool agent makes
3-8 sequential local calls. So: numeric stages are CPU/disk bound and cannot be outsourced (that would need raw
data); the wins are (a) chat, (b) the model calls of profile/diagnose/report, run concurrently, and
(c) more diagnoses getting a model narrative inside the same time.

## 1. Settings (config/settings.yaml, tpm/config.py) — additive, defaults keep no-egress behaviour

```yaml
profiles.hybrid.routing:    why_chat: external, assessor_chat: external   (others unchanged; column_roles stays local)
profiles.eu-hosted.routing: same as hybrid
external_llm:
  model: claude-sonnet-5
  model_by_task: {}                       # optional, e.g. {critique: claude-opus-5}
  allowed_model_patterns: [sonnet, opus]  # a model id must contain one of these ...
  blocked_model_patterns: [fable, mythos] # ... and none of these (checked on the lower-cased id)
  max_calls_per_run: 200                  # successful external calls per run workspace (pipeline + chat)
  max_calls_per_chat_turn: 6
  max_output_tokens_per_run: 120000
  max_parallel: 6                         # concurrent external calls
  max_narratives_per_run: 12              # diagnoses that get narrative + critique when the route is external
  timeout_s: 60
guard:
  external_sig_digits: 3                  # every float that leaves is rounded to this many significant digits
  alias_names_external: true              # original column names never leave, in any external profile
  drop_keys_external: [min, max, first, last, value, values_at, observed, reading, readings, raw, sample, samples,
                       rows, points, series, time, timestamp, start_time, end_time, t_start, t_end, source_path,
                       file, filename, path, evaluation, label, labels]
```
`Settings.external_model_for(task) -> str`. `ExternalLLMConfig.model_allowed(model) -> (bool, reason)`.
Env override `TPM_EXTERNAL_MODEL` stays; a blocked model makes the external route unavailable (falls back to
local) and says so in `available()`.

## 2. Guard v2 (tpm/llm/guard.py): sanitise, then verify an invariant

`check(payload, settings, strict=None, ws=None) -> GuardResult` keeps its signature (ws is new, optional).
Order: whitelist top-level keys -> alias names -> **sanitise** -> **verify invariant** -> size check.

Sanitise (all external profiles; `strict` additionally drops human notes as today):
1. Field-level fail-closed instead of payload-level: an unknown top-level key, free text under a non-text key,
   an aggregate with `n_samples < min_aggregate_n`, a row-like structure, a numeric series longer than
   `max_series_points` -> **that field/item is dropped** and a note is added. The payload is blocked only when
   nothing useful is left, the size cap is exceeded, or the invariant (below) fails. This fixes the false blocks
   found in the dry run: `signals[].heuristic_instrument`, `schema.$schema`, `template_parser_error`,
   `candidates`, `checks[].detail`, `instruction` / `instructions` (code-written task instructions are allowed text:
   add them to TEXT_KEYS / ARTIFACT_KEYS as `meta`).
2. Keys in `guard.drop_keys_external` are removed at any depth (single raw readings, data timestamps, file
   names, label-based evaluation). Exception: a key named `value`/`values` whose parent is a rule/threshold
   definition written by the operator (rule JSON) stays.
3. Every float is rounded to `external_sig_digits` significant digits. Integers stay (counts, row numbers, ids).
   Number tokens inside strings with more significant digits are rounded the same way (do not touch ids such
   as EV-000123, S05, B12, DIAG-3, ISO dates handled in 4, or version-like tokens).
4. ISO date/datetime tokens inside strings are replaced by `[time]` (row numbers remain the locator).
5. Data vocabulary: `guard.data_vocabulary(ws)` = distinct values of the dataset's non-signal text/categorical/
   label columns (from the schema; DuckDB `SELECT DISTINCT`, capped at 5000 values, time-boxed 5 s, cached in
   `workspace/<run>/egress_vocab.json`; values shorter than 3 characters or purely numeric are ignored).
   Any whole-word occurrence in an outgoing string is replaced by `[value]`. No ws -> skip.
6. Original column names -> aliases (S01..) everywhere, including inside strings (existing `_alias_strings`),
   in hybrid too.

Invariant, verified on the sanitised payload just before sending (`guard.verify_invariant(sanitized, amap, vocab,
cfg) -> list[str]` of violations; any violation blocks the payload and is written to the ledger):
no float with more than `external_sig_digits` significant digits; no ISO datetime; no original column name;
no vocabulary value; no list of more than `max_series_points` numbers; no dropped key present.

`GuardResult` gains `alias_map` and `sanitizer` (counts: floats_rounded, keys_dropped, strings_redacted, ...).
`guard.explain()` must describe the new rules in plain language (the Data-flow view shows it).

## 3. Providers / router

- `AnthropicProvider`: one pooled SDK client per (base_url, key) (thread-safe, reused); refuses a model that
  fails `model_allowed` with `ProviderError`; returns usage: `chat(...) -> (text, parsed, latency_ms)` stays, the
  last usage is exposed as `provider.last_usage = {"input_tokens", "output_tokens"}` (thread-local or returned
  through a new `chat_ex`). Consecutive same-role messages are merged and a leading assistant turn is dropped.
- `eu-hosted` requires `external_llm.base_url` to be set and not an `anthropic.com` host; otherwise the external
  route is unavailable with a clear reason (the first-party API has no EU processing).
- (2026-09-19, EU endpoint) A profile may carry `external_llm` keys that replace the top-level ones while it is active
  (`Settings._apply_profile_llm`, re-applied by `with_profile`; `Settings.base_external_llm` is the top-level block the
  UI's model choice writes). `eu-hosted` uses this for `provider: openai-compatible`, the organisers' Verda endpoint,
  `mistralai/Mistral-Large-3-675B-Instruct-2512-NVFP4` and `api_key_env: TPM_EU_API_KEY`; `TPM_EU_BASE_URL` /
  `TPM_EU_MODEL` / `TPM_EU_PROVIDER` override them. `profiles.eu-hosted.eu_hosts` (fnmatch patterns) lists the only
  hosts it accepts, and Bedrock cross-region ids (`global.`, `us.`, ...) are refused.
- `OpenAICompatProvider`: POST `{base_url}/chat/completions` with `Authorization: Bearer <key>` only; system message
  first, turns normalised like Anthropic's; a schema is sent as `response_format: json_schema` (non-object schemas
  wrapped in `{"result": ...}` and unwrapped), and a 4xx answer to it is retried once without it; HTTP 429 / 5xx and
  connection errors are retried once, a read timeout is not; usage from `usage.prompt_tokens / completion_tokens`.
  `providers.external_provider(settings)` picks the class from `external_llm.provider`; the router records
  `external_llm.provider_label` (`"<provider> @ <host>"` with a custom endpoint) as the ledger's `provider`.
  `AnthropicProvider` sends `anthropic-workspace-id` only to Anthropic's own API.
- Ledger (`EgressRecord`): add `input_tokens`, `output_tokens`, `sanitizer` (dict), keep hash/preview. The
  preview stores the SANITISED payload only. `ledger.usage(ws) -> {external_calls, external_ok, input_tokens,
  output_tokens, blocked, budget_left_calls, avg_latency_ms_external, avg_latency_ms_local, by_task:{...}}`.
  Appends are already under a lock: keep them thread-safe.
- Budget: before an external call `router._budget(ws, settings)`; exhausted -> ledger record with
  `guard_result="budget"` and local fallback.
- `router.complete()` unchanged for callers. It passes `ws` to the guard and uses `external_model_for(task)`.
- New `router.complete_many(jobs, *, ws, settings, max_parallel=None) -> list[LLMResult]` where a job is the
  kwargs dict of `complete`; thread pool when the job's route is external, sequential otherwise.
- New `router.agent_chat(messages, *, task, purpose, ws, settings, schema, max_tokens, payload_parts) -> LLMResult`:
  routes the tool agent. External only when the task routes external, the profile allows it, a key is present,
  the budget allows it AND every element of `payload_parts` (the structured objects the messages were built
  from) passed `guard.check`; the message text itself is additionally run through the string sanitiser and the
  invariant. Otherwise identical to today's `local_chat` (which stays).

## 4. Chat agent (tpm/llm/agent.py)

External mode of `run_agent` (decided once per turn through `router` availability + route):
- tool list without `sql`; `stats` returns n, mean, std, q05, median, q95, n_null, row_start, row_end (no min/max)
  and refuses windows with fewer than `min_aggregate_n` rows; `series` returns at most `max_series_points` bucket
  means, each over >= `min_aggregate_n` rows (else it says the window is too short to summarise). The full-detail
  series still goes to the UI (`tb.last_series`), never to the model.
- system prompt: aliases instead of dataset column names; context text built from the sanitised context.
- every tool result passes `guard.check({"tool_result": ...})` (add the artifact key); a blocked result is replaced
  by `{"withheld": true, "reason": ...}` so the model can continue.
- at most `max_calls_per_chat_turn` external calls per turn; any external failure mid-turn -> the turn is
  restarted on the local agent (today's behaviour), then the deterministic answer.
- the final answer is post-processed locally: first mention of an alias becomes `S05 (press_r)` (operator
  display name if set). `source` = `llm-external:<model>`; `chat()` reports `route`, `external_calls`.

## 5. Stages

- diagnose: when `diagnosis_narrative` routes external and is available: build ALL diagnoses template-only first,
  then narrative + critique for the strongest `max_narratives_per_run` concurrently (`max_parallel` workers, each
  worker: narrative then critique of one diagnosis), inside the stage's time budget. Local route: unchanged.
- profile `sensor_hypotheses`: payload fingerprints carry q05/q95 instead of min/max and an `n_samples`.
- rule_compile / critique / report_narrative / plain views / assessor: payload keys made guard-clean
  (`instructions`, no `$schema` in the payload; pass the schema through the `schema=` argument).
- `tpm bench-llm --run <id> [--tasks ...] [--n 3]`: replays a finished run's model tasks (payloads rebuilt from
  artifacts) on the local route and on the external route, prints a latency table and writes
  `workspace/<run>/llm_benchmark.json` (the Data-flow view shows it). External calls go through the normal
  router path (guard, budget, ledger).

## 6. API / UI (Data-flow view)

`GET /api/runs/{id}/llm/usage` (ledger.usage + budget + benchmark if present), `GET /api/llm/status` adds
`external_models_allowed`, `external_model_blocked_reason`; `PUT /api/settings` accepts
`external_llm.model` only when allowed. Data-flow view: "External model use" card: profile, model picker
(Sonnet 5 / Opus 5), calls used / cap, tokens, average seconds local vs external, what was sent (sanitised
previews, already there), sanitiser notes. Plain-language sentence first.

## 7. Rules for everyone working on this

- Never read, print or copy `.env`. Tests and ad-hoc scripts run with `TPM_NO_DOTENV=1` and a fake key with a
  stubbed provider. No real network model calls during development; the lead runs the real benchmark once.
- Another session is editing the e-mail feature right now. Do not touch: `tpm/report/email.py`,
  `tests/conftest.py`, the `report/email` route in `tpm/api/server.py`, the e-mail form in
  `tpm/api/static/js/views/report.js`, the `rep.*` i18n keys, e-mail tests in `tests/test_e_api.py` and
  `tests/test_f_export_wiring.py`, the README e-mail section, `.env.example` mail lines. Use targeted edits
  (never rewrite a whole shared file); do not run `git add`, `git commit`, `git stash` or `git checkout` on files.
- Frontend files are ES modules: check a `.mjs` copy with `node --check`; never put a raw newline inside a quoted
  JS string.
- Log your work in `docs/worklog/hybrid.md` (append a dated section: what changed, files, tests run, open
  points) so anyone can continue.
- Run only the test files that concern your change while developing (`.venv\Scripts\python.exe -m pytest
  tests/<file> -q`); the full suite takes 8 minutes and is run once by the reviewer.
