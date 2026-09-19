# Worklog: hybrid profile (limited, anonymised calls to the Anthropic API)

Contract: docs/HYBRID_SPEC.md. Append a dated section per work package (what changed, files, tests, open points).

## 2026-09-19T15:35+03:00 — egress core (spec sections 1, 2, 3): settings, guard v2, providers / router / ledger

What changed
- Settings (additive; no-egress stays the default). `external_llm`: `model_by_task`, `allowed_model_patterns`
  [sonnet, opus], `blocked_model_patterns` [fable, mythos], `max_calls_per_run` 200, `max_calls_per_chat_turn` 6,
  `max_output_tokens_per_run` 120000, `max_parallel` 6, `max_narratives_per_run` 12, `timeout_s` 60, plus `effort: low`
  and `max_tokens: 4096` (Sonnet 5 / Opus 5 think by default and thinking counts toward max_tokens; low effort is the
  fastest setting). `guard`: `external_sig_digits` 3, `alias_names_external` true, `drop_keys_external` [...];
  `max_numeric_values_per_payload` in the yaml raised 400 -> 4000 (= the class default; a 50-signal catalog has ~1300
  rounded aggregates). Profiles: hybrid and eu-hosted route `why_chat` / `assessor_chat` external; eu-hosted has
  `require_custom_endpoint: true`.
  `ExternalLLMConfig.model_allowed(model) -> (bool, reason)`; fable / mythos are blocked in code as well
  (`ALWAYS_BLOCKED_MODEL_PATTERNS`), so emptying the config lists does not open them. `endpoint_is_first_party()`.
  `Settings.external_model_for(task) -> str`, `Settings.external_block_reason(task=None) -> Optional[str]` (profile,
  blocked model, eu-hosted without a non-anthropic.com `base_url`). `TPM_EXTERNAL_MODEL` still overrides; a blocked model
  makes the route unavailable (local fallback), never an error.
- Guard v2 (tpm/llm/guard.py, rewritten). `check(payload, settings, strict=None, ws=None) -> GuardResult`; order:
  whitelist top-level keys -> alias names -> sanitise -> `verify_invariant` -> size. Fail-closed per FIELD: unknown
  top-level key, free text under a non-text key (also free-text dict keys), aggregate with n_samples < min, row-like
  block, series > max_series_points -> that field / item is dropped with a note. Blocked only when nothing useful
  (non-meta) is left, too many numbers / bytes, or the invariant fails. Sanitiser: drop keys at any depth (rule limits
  min/max/value(s) inside a compiled rule or under `rules` / `rule_text` / `candidates` / `template_parser_error` stay,
  and their numbers are not rounded: operator-written limits are not data); every float -> 3 significant digits
  (integral floats under locator keys such as row_start / n_samples become ints), decimal tokens inside strings and
  numeric dict keys rounded the same way (ids, versions, integers untouched); ISO dates / datetimes, dd.mm.yyyy and
  hh:mm:ss tokens -> `[time]`; dataset text values -> `[value]`; source file name / path / stem -> `[file]`; original
  column names -> alias (S01..) or `[column]` for non-signal columns, case-insensitive (exact case for names shorter
  than 3), also inside identifiers split by "_" (press_r__roll_mean). Names come from the payload AND from the run
  (schema.json, signals.json), so a chat question or rule text that names a column is aliased too.
  Deliberate exemptions, all documented in `explain()`: generic header words of NON-signal columns (`time`, `id`,
  `sample`, `run`, ... `GENERIC_COLUMN_WORDS`) are only removed where a field lists columns, not inside sentences;
  everyday category words (`normal`, `faulty`, ... `GENERIC_VALUE_WORDS`) and the code's own structural words are not
  in the vocabulary (redacting them only damages code-written sentences).
  `data_vocabulary(ws)` / `vocabulary_record(ws)`: DuckDB SELECT DISTINCT over the VARCHAR / ENUM columns of
  dataset.parquet (own connection, 5 s time box via interrupt, 5000 cap, low-cardinality columns first, `__group__`
  / `__row__` skipped because those ids are generated or built from key columns that are scanned anyway), cached in
  `workspace/<run>/egress_vocab.json` (keyed by dataset mtime+size) and in memory. 6 GB practice run: 0.12 s.
  `verify_invariant(sanitized, amap, vocab, cfg) -> list[str]` (messages never repeat the offending value), also
  `sanitize_text(text, settings, ws=None, amap=None) -> (text, counts)`, `verify_texts(texts, settings, ws, amap)`,
  `alias_map(ws, payload=None)`, `round_sig`. `GuardResult` gained `alias_map` (local only) and `sanitizer` (counts).
  New artifact keys: `tool_result(s)`, `instruction`, `candidates`, `template_parser_error`, `action`,
  `action_types`, `template_answer`, `signal_aliases`, `evidence_ids`. `explain()` rewritten in plain language.
- Providers: `AnthropicProvider` pooled SDK client per (base_url, key, timeout); refuses a model that fails
  `model_allowed` (ProviderError, before any client is built); `normalize_turns()` (leading assistant turn dropped,
  same-role turns merged, empty turns skipped); `last_usage` (thread-local per instance, settable: test stubs can
  report tokens); sends `output_config={"effort": ...}`, never sampling parameters; `thinking: disabled` only for a
  forced tool call on a non-first-party endpoint (Bedrock rejects forced tool choice with thinking on);
  `stop_reason == "refusal"` raises ProviderError (-> local fallback). SDK here is anthropic 1.7.0.
- Ledger: `EgressRecord` + `input_tokens`, `output_tokens`, `sanitizer` (counts + first 12 notes). `guard_result` now
  also `budget` and `unavailable` (route not usable: blocked model, EU endpoint missing, no key; nothing is counted as
  sent). Preview = sanitised payload only. `ledger.usage(ws, settings=None)` -> external_calls, external_ok,
  input_tokens, output_tokens, blocked, budget_refused, max_calls_per_run, budget_left_calls,
  max_output_tokens_per_run, budget_left_output_tokens, avg_latency_ms_external, avg_latency_ms_local, by_task{...}.
  Appends are serialised by the module lock (several threads / Workspace objects). `data_flow_statement` mentions
  rounding, tokens and budget refusals.
- Router: `complete()` signature unchanged: availability -> guard (with ws) -> `_budget(ws, settings, reserve=True)`
  -> send with `external_model_for(task)`; external `max_tokens = max(caller, external_llm.max_tokens)`; a `system`
  override goes through `sanitize_text`; every outcome recorded. Budget counts successful external calls in the run's
  ledger + calls on the wire (so parallel jobs cannot overshoot) and output tokens; no ws -> not tracked.
  New: `complete_many(jobs, *, ws, settings, max_parallel=None, deadline_s=None) -> list[LLMResult]` (job = kwargs of
  complete; external + usable jobs in a thread pool, others sequential, order kept, never raises);
  `agent_chat(messages, *, task, purpose, ws, settings, schema, max_tokens, payload_parts, artifact_types=None,
  temperature=None) -> LLMResult` (pre-send stop = exactly `local_chat`; a failure of the external call itself returns
  ok=False, route="external" so the agent can restart the turn locally); `external_ready(task, ws, settings) ->
  (bool, reason)` for the once-per-turn decision. Local fallbacks of parallel external jobs are serialised
  (`_LOCAL_FALLBACK_LOCK`). `available()` adds `external_models_allowed`, `external_model_blocked_reason`,
  `external_unavailable_reason`, `external_model_by_task`; `external` is now true only when the route is really usable.
  `tpm.llm` exports `complete_many`, `external_ready`, `usage`.
  Thread safety checked: DecisionLog.record holds its own RLock around BEGIN IMMEDIATE (hash chain verified after 6
  concurrent calls), ledger ids unique, jsonl appends under one lock.

Files
- tpm/config.py (appended fields / methods only), config/settings.yaml, tpm/contracts.py (EgressRecord),
  tpm/llm/guard.py, tpm/llm/router.py, tpm/llm/providers.py (AnthropicProvider + helpers; the Ollama part belongs to the
  model-selection work and was left alone), tpm/llm/ledger.py, tpm/llm/__init__.py,
  tests/test_d_guard.py (payload-level-block expectations rewritten to field-level, intent kept; new cases),
  tests/test_d_router.py (appended), tests/test_d_egress_no_raw.py (new).

Tests (TPM_NO_DOTENV=1, fake key, provider stubbed, no network)
- `.venv\Scripts\python.exe -m pytest tests -q -k "test_d_"` -> 62 passed (guard 18, router 23, egress_no_raw 2,
  agent 7, models 12 from the model-selection work).
- test_d_egress_no_raw: real pipeline run on the synthetic generator with distinctive column names, label / category
  values, timestamps and file name; hybrid + eu-hosted; all 5 external tasks (17 calls) reach the stubbed provider, no
  column name, category value, file name, date, decimal with > 3 significant digits or full-precision cell value in any
  message or ledger preview.
- Same walk on copies of real runs (scratch script, not in the repo): demo_cli, extra_wide_labeled,
  extra_modes_rowlabels, extra_uneven_headerless, te_full_v2 (6 GB run, parquet hard-linked): 17 of 17 calls allowed
  in both profiles, no raw numbers, dates, file names or distinctive names / values in the captured text. The earlier
  false blocks (heuristic_instrument, schema.$schema, template_parser_error, candidates, checks[].detail, instruction(s),
  n_samples < 30 in strict mode) are gone.

Open points
- tpm/assessor (`ask`, `parse_action`) only calls the model when `route_for("assessor_chat") == "local"`: with the new
  routing the assessor answers from its template in hybrid until that gate is changed to use `complete()` regardless
  of the route (payload keys are already whitelisted; `evaluation` is a dropped key: rename it, e.g. `assessment`).
- tpm/llm/agent.py still calls `router.local_chat`: chat stays local until section 4 switches to `agent_chat` /
  `external_ready`.
- rule_compile still puts RULE_JSON_SCHEMA into the payload: it is sent gutted (harmless, the schema also travels in
  the system prompt through `schema=`); section 5 removes it. Fingerprint `min` / `max` are dropped by the guard;
  section 5 replaces them with q05 / q95.
- Integers are never rounded (spec): an integer-valued reading inside a sentence would pass. The key drops and the
  free-text rule are the defence there.
- The budget is per process + ledger file: two processes writing the same run could overshoot by their in-flight calls.
- No real API call was made. First real check by the lead: TPM_PROFILE=hybrid, then `tpm bench-llm` (section 5) or
  `tpm.llm.complete("critique", diagnosis_payload(ws), purpose="x", ws=ws)`; watch `ledger.usage(ws)`.

## 2026-09-19T16:00+03:00 — hybrid chat (spec section 4): the tool agent on the external model

What changed
- `tpm/llm/agent.py`: the chat agent has two modes, decided once per turn in `chat()` through `_external_turn()`
  (`settings.route_for(task) == "external"` and `router.external_ready(task, ws, settings)`: profile, allowed model,
  endpoint, key, run budget). no-egress never gets past the first test, so the external provider is not even built.
  - External mode = `Toolbox(..., external=True)` (`tb.external_view()` shares the loaded schema / catalog / index);
    `run_agent` reads the mode from `tb.external`, its signature is unchanged.
  - Tools (`tool_specs(settings, external)`): no `sql`; `stats` returns n, mean, std, q05, median, q95, n_null,
    row_start, row_end (+ signal, group_id, n_samples; no min / max / column) and refuses fewer than
    `guard.min_aggregate_n` values; `series` returns at most `guard.max_series_points` bucket means, each over at
    least `min_aggregate_n` values (smaller buckets are left out; nothing left -> "window too short to summarise"), as
    `bucket_row_start`, `bucket_mean` and `bucket_mean_in_std` (the same means in standard deviations from the mean
    of the range: 3 significant digits of a level such as 2700 would hide the shape; still an aggregate over >= 30
    rows; remove the key if the team wants bucket means only). Only catalogued signals resolve for stats / series in
    external mode (label, id and time columns are refused). `tb.last_series` still gets the full-detail series for
    the UI; it is never put into a message.
  - The loaded context (`_context_compact`) and every tool result go through `guard.check({"context": ...})` /
    `guard.check({"tool_result": ...}, ws=ws)` BEFORE they are put into a message (`_guarded`). A refused result
    becomes `{"withheld": true, "reason": <guard reason>}` and the turn goes on; the trace entry gets `withheld`.
    The sanitised objects are also passed to `router.agent_chat(payload_parts=...)`, which checks them again and
    runs the message text through the string sanitiser and the invariant.
  - New prompt `tpm/llm/prompts/agent.system.external.j2` (aliases only, no dataset columns, no sql, explains
    [time] / [value] / [column] / [file] and `withheld`). `agent.system.j2` is unchanged.
  - Cap: `external_llm.max_calls_per_chat_turn` external calls per turn. Tool calls are limited to cap - 1 and the
    model is told when it has to answer. The count comes from the egress ledger (`_external_sent`), so external
    calls a tool makes on its own through the router (the assessor) count too.
  - Failure: provider error, no JSON, run budget used up mid-turn, cap reached without an answer, or a call that
    `agent_chat` did not send -> the external attempt ends ("external_error" + a trace entry marked
    `route: external`) and `chat()` restarts the whole turn on the local agent with the local prompt and tools
    (nothing of the external conversation is reused), then the deterministic answer.
  - Final answer: `expand_aliases(answer, alias_labels(tb))` on this machine: first mention of an alias becomes
    `S05 (press_r)`; the operator's `display_name` wins; headerless files get no expansion. Follow-up suggestions
    keep the plain alias (they become the next question). Earlier expanded answers in `history` are collapsed back
    to the alias before they are sent (`_collapse_aliases`).
  - `chat()` result gains `route` ("external" | "local" | "none") and `external_calls`; `source` is
    `llm-external:<model>`; both are also written to chat.jsonl and the decision log.

Files
- tpm/llm/agent.py, tpm/llm/prompts/agent.system.external.j2 (new), tests/test_d_hybrid_chat.py (new).
  The PyInstaller spec bundles every non-Python file under tpm/, so the new template ships with the next build.

Tests (TPM_NO_DOTENV=1, fake key, AnthropicProvider.chat scripted, no network, no Ollama)
- `tests/test_d_hybrid_chat.py` -> 8 passed (12 s; real pipeline run on the synthetic generator with distinctive
  column names, label values, timestamps and file name): nothing raw in any message or ledger preview of a 9-call
  turn whose question and history contain a column name, a full-precision reading, a timestamp, a label value and
  the file name; sql not offered and refused; stats / series shapes; UI series intact; withheld path; restart on
  the local agent and deterministic answer when the provider raises; per-turn cap and run budget; calls made by a
  tool count toward the cap; no-egress never touches the external provider; alias expansion with display_name.
- test_d_agent 7, test_d_router 23, test_d_guard 18, test_d_models 12, test_e_hybrid_ui 21, test_e_ui_assets 23,
  test_e_api 37, test_b_assessor 23, test_e_evidence_plain 59 -> all passed.
- tests/test_d_egress_no_raw.py -> 2 FAILED, not caused by the chat work: only its last assertion
  (`totals["keys_dropped"] > 0`) fails, because the section-5 stage payloads edited at the same time no longer carry
  drop-listed keys (q05 / q95 instead of min / max). Everything before it (all tasks sent, nothing raw) passes. The
  assertion should become `floats_rounded and names_aliased`.
- Scratch check (not in the repo) on copies of demo_cli, extra_wide_labeled, extra_uneven_headerless,
  extra_modes_rowlabels (eu-hosted) and te_full_v2 (6 GB run, parquet hard-linked), stubbed provider, 5 tool calls +
  answer: every call allowed, nothing withheld, no column name / file name / date / decimal with more than 3
  significant digits in the sent text. Local overhead of such a turn on te_full_v2: 9.3 s, of which 4.2 s is the
  first TF-IDF search index build, stats 0.9 s, series 0.5 s; guard + ledger cost about 0.3 s per call.

Open points
- `router.agent_chat` answers a call it may not send (guard, budget) on the LOCAL model with the external-mode
  messages. The agent asks `external_ready` before every call and guards every part itself, so this is rare; when
  it happens the local result is thrown away and the turn restarts locally (one wasted local call). Wanted:
  `agent_chat(..., local_fallback: bool = True)`; with False it returns `LLMResult(ok=False, route="none",
  error=<reason>)` instead of calling `local_chat`. The agent would pass False.
- `tpm/api/fallback.py normalize_chat_result` does not forward `external_calls` and strips `route` / `withheld` /
  `error` from the tool-trace entries; `tpm/llm/__init__.py chat()` error fallback lacks `route` / `external_calls`.
- Evidence statements written by the stages carry a signal's range inside the sentence ("range [2.61e+03, 1e+05]"):
  the guard rounds it to 3 significant digits but it is still a min / max. Stage wording, not chat.
- Input tokens: the whole conversation (system prompt with up to 7,000 characters of context + every tool result, up
  to 4,000 characters each) is resent on every call, about 20,000 characters on the last call of a 5-tool turn. Prompt
  caching is not used.
- No real API call was made.

## 2026-09-19T16:05+03:00 — API + Data-flow view (spec section 6): external model use

What changed
- `tpm/api/server.py` (targeted edits; the report/email route untouched; all new routes are plain `def`, so they run in
  the thread pool):
  - `GET /api/runs/{id}/llm/usage` -> `{available, profile, allow_external, external_model, external_model_by_task,
    external_models_allowed, external_model_blocked_reason, external_unavailable_reason, usage, caps, sanitizer, guard,
    benchmark}`. `usage` = `tpm.llm.ledger.usage(ws, settings)` as is. `caps` = the six limits of `external_llm`
    (`_external_caps`). `sanitizer` = `_sanitizer_totals(ledger)`: `{payloads, numbers_rounded, names_aliased,
    values_withheld, detail}` summed over external records with `guard_result == "allowed"` (sent, or failed on the wire;
    blocked / budget records never left and are not counted; `vocabulary_size` and `notes` are skipped). `guard` =
    `{external_sig_digits, alias_names_external, min_aggregate_n, max_series_points}`. `benchmark` = None, or
    `{created_at, n, local_model, external_model, rows}` from `workspace/<run>/llm_benchmark.json`; a broken file gives
    None, never an error. 404 for an unknown run like the neighbours.
  - `_benchmark_rows(data)` is tolerant about the file, because `tpm bench-llm` (section 5) does not exist yet. It reads
    `tasks` as a list of `{task, n, local, external}` or as a dict `{task: {...}}`; `local` / `external` may be a dict
    with `avg_latency_ms` (preferred), `mean_latency_ms`, `avg_ms`, `mean_ms`, `latency_ms`, `avg_s`, `mean_s` or
    `seconds`, plus optional `ok` / `n`, or a bare number of milliseconds. Row = `{task, n, local_s, external_s, local_ok,
    external_ok, speedup}`. Whoever writes bench-llm: `{"created_at", "n", "local_model", "external_model", "tasks":
    [{"task", "n", "local": {"ok", "avg_latency_ms"}, "external": {"ok", "avg_latency_ms"}}]}` shows up without changes.
  - `GET /api/llm/status` (new; the spec names it, it did not exist): `tpm.llm.available(settings)` plus `profile`,
    `allow_external`; `external_models_allowed` / `external_model_blocked_reason` / `external_unavailable_reason` are
    there even when the llm package fails (`_external_model_state(settings)`, same answers as `available()` without
    probing Ollama).
  - `GET /api/settings` gains `external_models_allowed`, `external_model_blocked_reason`, `external_unavailable_reason`,
    `external_caps`, `external_sig_digits`.
  - `PUT /api/settings` accepts `{"external_llm": {"model": id}}` (or flat `{"external_model": id}`, like `local_model`).
    400 with the reason of `ExternalLLMConfig.model_allowed` for a Fable / Mythos / foreign-family / empty id; 400 when
    `external_llm` carries anything but `model` (nobody empties the pattern lists through the API) or the id is not
    id-shaped. Saved in place by `_rewrite_external_model_line` (the `model:` line inside the `external_llm:` block, so
    the comments of settings.yaml stay; ids that would need YAML quoting go through `save_settings_overrides`). Profile
    and model can change in one request; both in-place edits run first, the rest is merged as before. A pinned
    `TPM_EXTERNAL_MODEL` is overwritten for this process (same rule as `TPM_PROFILE`), the change is written to the
    decision log of open runs (`settings / external_model {from, to}`), the answer carries `external_model`.
- `tpm/api/static/js/externaluse.js` (new): `externalUseCard({settings, profileCards, onChanged}) -> {root, reload(settings)}`
  and the pure helpers `callSeconds`, `externalMode` ('off' | 'on' | 'unusable'), `unusableKey`, `usageFacts`,
  `factsFromSettings`, `modelLabel`. Card: one plain sentence first (what leaves, what never leaves, which model, Fable-
  class models refused because they keep data for 30 days; "Nothing leaves this machine" in no-egress; when allowed but
  not usable: the reason in plain words: no key / no EU endpoint / refused model), then the profile cards, the model
  picker (Sonnet 5 / Opus 5, disabled when the server does not allow the id; repaints at once, the slow GET /api/settings
  follows), four tiles (calls used of cap + left / stopped / kept local, tokens in / out, seconds per call local,
  seconds per call external with "N× faster"), three sanitiser tiles with a one-sentence explanation, the benchmark
  table when present (else, for engineers, a per-task table from `usage.by_task`), and a pointer to the ledger.
  Without a run or with an older server the card renders from GET /api/settings; in no-egress with nothing ever sent
  the numbers are left out.
- `tpm/api/static/js/views/dataflow.js`: the card is inserted between the summary card and "Show technical analyses"
  (`page.insertBefore(ext.root, tech)`): it is where a person decides what may leave. The profile selector moved into
  it (the block keeps `data-brief-section="profile"`, so the summary card's action still lands on it; the card itself is
  `data-brief-section="external"`). Model status became `renderModels()` and is repainted after a profile / model
  change. Ledger table: new column tokens in / out, `budget` result coloured, preview column titled "What was sent
  (after cleaning)".
- `tpm/api/static/styles.css`: block appended at the end (`.sec.extuse`, `.extuse-*`): auto-fit grids with
  `minmax(min(140px, 100%), 1fr)`, `overflow-wrap: anywhere`, no fixed widths, no nowrap; tables scroll inside `.tbl-wrap`.
- i18n: 47 new `flow.ext.*` keys in en / fi / sv, inserted after `flow.noLedger` by a script that reads, inserts and writes
  in one go and checks that no existing key or its order changed. No existing key was touched.
- `tests/test_e_hybrid_ui.py` (new, 21 tests): usage route against a temp workspace with a fake ledger (counts, tokens,
  latency, budget, sanitiser totals of cleared payloads only, empty run, unknown run), benchmark in list and dict shape
  and a broken file, status / settings name the allowed models, PUT accepts Opus and keeps the yaml comments, refuses
  fable / FABLE / mythos / gpt-4o / empty with the reason and leaves the file untouched, refuses other `external_llm`
  fields and a multi-line id, profile switching alone and together with the model, a Fable model pinned through
  `TPM_EXTERNAL_MODEL` is reported and the UI choice wins, `node --check` on .mjs copies, every `flow.ext.*` key used in
  the JS exists in three languages with equal placeholders and no dead keys, view wiring, narrow-window CSS rules, and
  the pure helpers run under node.

Checked in a browser (scratch server: temp settings and workspace, `TPM_NO_DOTENV=1`, fake key, `AnthropicProvider.chat`
stubbed to raise, Ollama pointed at a closed port): hybrid / no-egress / eu-hosted, en / fi / sv, 800 px and 375 px:
`documentElement.scrollWidth == clientWidth`, no element of the card overflows, no console errors, model pick and
profile switch repaint the card. While that server was up another session's browser automation drove the same tab and
triggered one `report_narrative` external attempt: it hit the stub (ledger: allowed, ok=false, "stubbed"), nothing went
to the network.

Tests run (TPM_NO_DOTENV=1)
- `tests/test_e_hybrid_ui.py`: 21 passed.
- `tests/test_e_ui_assets.py`: 23 passed (includes the new module in the node check and the i18n parity test).
- `tests/test_e_api.py -k "not email and not mail"`: 35 passed, 2 deselected.
- `tests/test_e_ui_views.py tests/test_e_brief.py`: 52 passed.

Open points
- `llm_benchmark.json` is not written by anything yet (section 5, `tpm bench-llm`); the shape above is what the view
  expects, the other shapes listed above are tolerated.
- The sanitiser totals count every external record the guard allowed, also calls that failed afterwards, while the
  "calls" tile counts successful calls (the budget's definition in `ledger.usage`). The note under the tiles says
  "payloads that the guard cleared for sending".
- GET /api/settings probes Ollama through `available()`; with Ollama down it takes about 2 s on this laptop. The card
  does not wait for it (usage route and optimistic repaint), the rest of the Data-flow view does, as before.
- The top-bar lamp "External route: exists" still only means profile + key (`external_route_exists`); a refused model or
  a missing EU endpoint shows in the card, not in the lamp (app.js is another session's file right now).
- The per-chat-turn cap (`max_calls_per_chat_turn`) is in `caps` but not shown as a tile.

## 2026-09-19T16:20+03:00 — stage call sites + benchmark (spec section 5)

What changed
- diagnose, concurrent path (tpm/diagnose/__init__.py). `_diagnose` asks `_external_route_ready(ws, settings)` once:
  true only when `diagnosis_narrative` AND `critique` both route external and `tpm.llm.external_ready` says the route is
  usable (profile, allowed model, endpoint, key, run budget). Then `_diagnose_concurrent`: (1) every event diagnosis is
  built from the templates (`_build_event`, same ids DIAG-%06d, same time-budget warning); (2) the strongest
  `external_llm.max_narratives_per_run` go to `_model_replies`: a ThreadPoolExecutor with `external_llm.max_parallel`
  workers ("tpm-diagnose-llm-*"), one worker = `_ask_model` = narrative, then critique of ONE diagnosis, both through
  `tpm.llm.complete` (guard, budget, ledger as always); (3) the replies are applied on the main thread in diagnosis
  order with the same records as the one-by-one loop (narrative -> critique -> diagnosis). Workers only make model calls:
  payloads, code checks and evidence statements are read on the main thread before the pool starts, each worker gets a
  deep copy of its diagnosis (it merges the narrative into the copy so that the critique sees it, as in the sequential
  path), registries / DuckDB / diagnosis log records stay on the main thread. Time: no call starts after
  min(half the stage budget, 0.7 * budget - elapsed); calls on the wire may finish until the stage budget ends, later
  ones are abandoned (diagnosis stays template-only, code critique). Circuit breaker: after
  `EXTERNAL_MISSES_BEFORE_STOP` (2) replies that did not come from the external model (route down -> `complete` fell
  back to the single local model) no further call is started. The closing note in the log names the external model,
  the concurrency and the cap. Local route (and any mixed routing): the old loop, unchanged apart from the extraction of
  `_build_event`; its log texts are byte-identical.
- tpm/diagnose/diagnosis.py: `add_llm_narrative` split into `narrative_payload(ws, diag) -> dict`,
  `merge_narrative(diag, res) -> bool` (pure, thread-safe on a private copy), `apply_narrative(ws, diag, res) -> Diagnosis`
  (merge + log record). `add_llm_narrative(ws, settings, diag, language)` keeps signature and behaviour.
- tpm/diagnose/critique.py: `cited_evidence(ws, diag) -> list`, `objections_payload(diag, checks, evidence) -> dict`
  (pure), `parse_objections(diag, res) -> (objections, source)`; `llm_objections` composes them as before.
  `critique_diagnosis(..., use_llm=True, model_objections=None)`: `model_objections=(objs, source)` = answer already
  obtained from the model, no call is made.
- Payload hygiene (checked with guard.check on demo_cli and te_full_v2: every stage payload allowed, 0 keys dropped):
  * sensor_hypotheses (tpm/profile/roles.py): fingerprints carry q05 / q95 (full list also q50) instead of min / max,
    plus `n_samples` (= fingerprint count); `boundedness` "0-100" / "0-1" is sent as `range_0_100` / `range_0_1` (the
    guard dropped the hyphenated form as free text).
  * rule_compile (tpm/quality/rules.py): no `schema` / `$schema` in the payload (the closed schema still goes through
    `schema=` into the system prompt); `catalog_payload` gives q01 / median / q99 instead of min / max / median (also
    fixes the median fallback, which read a fingerprint key that does not exist).
  * critique, plain views (tpm/api/plain.py), assessor x2: code-written task text under `instructions`.
  * assessor `ask`: `evaluation` -> `assessment`. Beyond "payload keys": the gate in `ask` and `parse_action` is now
    `route_for("assessor_chat") in ("local", "external")` (it was `== "local"`), otherwise the assessor fell silent
    (template only) in hybrid / eu-hosted after the routing change of section 1. External goes through `complete()`:
    guard, budget, local fallback.
  * report narrative payload (tpm/report/report.py): checked, already clean (`instructions`, no schema): not edited.
- `tpm bench-llm` (tpm/cli.py `cmd_bench_llm` + new module tpm/llm/bench.py):
  `python -m tpm bench-llm --run <id|latest> [--profile hybrid] [--tasks a,b] [--n 3] [--routes local,external]
  [--no-chat] [--dry-run]`. Payloads are rebuilt from the run's artifacts with the stages' own builders
  (`build_llm_payload`, `narrative_payload`, `objections_payload` with the stored code checks, `report.collect(...)
  ["llm_payload"]`); narrative / critique use the n strongest event diagnoses in turn. Route forced per call with
  `bench.forced_route(settings, route, tasks)` = deep copy of the settings with the profile routing overridden;
  `allow_external` is never changed, so under no-egress the external rows are reported as skipped with the reason (use
  `--profile hybrid`). Calls go through `tpm.llm.complete` / `tpm.llm.chat` with the real run (guard, budget, ledger).
  Only calls the requested route answered itself count as ok; a task whose call fell back is not repeated. Extra
  measurements: one chat turn per route (`why_chat`, fixed question), and one batch of external calls at once through
  `complete_many` (wall clock vs summed latency). Output: table + `workspace/<run>/llm_benchmark.json`
  {generated_at, run_id, profile, dry_run, n_requested, machine, tasks:[{task, route, model, n, ok, median_s, mean_s,
  min_s, max_s, answered_by, errors?, skipped?}], chat:{task, question, turns:[{route, answered_by, source, ok, seconds,
  tool_calls, external_calls}]}, concurrent:{jobs, ok, max_parallel, wall_s, sum_latency_s}, speedup:{definition,
  per_task, all_tasks, chat, concurrency}, usage (= ledger.usage), notes}.
  `--dry-run`: `bench.stub_providers()` swaps both providers for stubs (sleep 0.01 s, smallest object the task schema
  accepts, agent step = "final") and the benchmark works on `bench.scratch_copy(ws)` (small files copied, files > 64 MB
  hard-linked, decision log left out), so the real run's ledger, chat history and decision log get no entries for calls
  that never happened. Stub timings are written to `llm_benchmark.dry_run.json`, never to `llm_benchmark.json` (the file
  the Data-flow view is meant to show). Dry run on te_full_v2: 16 s in total, all 4 external payloads pass the guard.

Files
- tpm/diagnose/__init__.py, tpm/diagnose/diagnosis.py, tpm/diagnose/critique.py, tpm/profile/roles.py,
  tpm/quality/rules.py, tpm/api/plain.py, tpm/assessor/__init__.py, tpm/assessor/actions.py, tpm/cli.py (new command +
  parser entry + usage line only), tpm/llm/bench.py (new), tests/test_d_hybrid_stages.py (new).
- Tests whose expectation was the old payload shape, one assertion each: tests/test_b_rules.py
  (`"schema" in payload` -> schema arrives through `schema=`), tests/test_d_egress_no_raw.py (the sanitiser no longer
  HAS keys to drop on a real run: `keys_dropped == 0` is now asserted; rounding and aliasing still > 0).
- Not touched: router / guard / providers / ledger / agent.py, tpm/report/*, anything of the e-mail work.

Tests (TPM_NO_DOTENV=1, fake key, AnthropicProvider.chat stubbed, network client patched to raise, Ollama "not running")
- tests/test_d_hybrid_stages.py: 7 passed. Concurrent vs sequential on the shared synthetic detect run (17 event
  diagnoses, stub sleeps 0.2 s per call, 34 calls): identical diagnoses (every field except created_at), identical
  decision-log records per diagnosis, hash chain intact, unique ledger ids, 1.65 s against 7.54 s; cap of 2 narratives
  respected (4 calls, the 2 strongest); no-egress never enters the concurrent path; route down with 3 workers -> at most
  6 calls instead of 34, everything template-only; payload hygiene (fingerprint, instructions, no schema, guard drops
  nothing); `bench-llm --dry-run` through `cli.main` writes the JSON, leaves the run and the provider classes as they
  were; no-egress benchmark skips the external route.
- `pytest tests -k "test_d_"`: 77 passed.
- `pytest tests -k "diagnos or critique or roles or rules or plain or assessor or report"`: 203 passed (the first run had 202 passed / 1 failed: the
  test_b_rules assertion above). tests/test_f_cli.py + tests/test_a_profile.py: 19 passed.

Open points
- rule_compile on the external route: `RULE_JSON_SCHEMA` is a pseudo schema (`"type": "range"`, property values such as
  "alias"), and AnthropicProvider wraps any non-object schema into a forced tool `input_schema`. The real API is likely
  to reject that with a 400 -> the call falls back to the local model (no data issue, only no speed-up for that task).
  Fix belongs to providers.py (skip the forced tool call when the schema is not a JSON Schema object) or to a real JSON
  Schema for rules. Not verifiable without a real call; bench-llm does not cover rule_compile.
- When the external route fails, up to `max_parallel` calls that already started fall back to the local model one after
  the other (router `_LOCAL_FALLBACK_LOCK`); the stage does not wait for them beyond its budget, but the local model
  stays busy for that long. A `no_local_fallback` switch on `complete()` would avoid it (router is not mine).
- Plain views and the report narrative come back from the external model with aliases (S05) where the local text had
  operator names; the chat agent expands them locally (`expand_aliases`), these two call sites do not yet.
- bench-llm on the real run writes what it does into the run: ledger records (purpose "benchmark: ..."), two chat
  turns by actor system:bench-llm, and it uses the run's external budget (n=3: 12 + 4 + up to 6 calls).
- Real numbers: `python -m tpm bench-llm --run te_full_v2 --profile hybrid` (local side alone is about 4 x 3 calls of
  25-37 s plus a 1-2 min chat turn; `--n 1` or `--routes external` shortens it).

Addendum (same work package, 16:40): benchmark file and the Data-flow view
- The section-6 reader (`tpm/api/server.py::_benchmark_rows`) had guessed one item per task with `local` / `external`
  sub-objects; tpm.llm.bench writes the shape of the task description, one row per task AND route. Targeted edit in
  `_benchmark_rows`: rows that carry `route` are folded per task first (nothing else in server.py touched; the two
  shapes of tests/test_e_hybrid_ui.py still pass). The benchmark JSON also carries the four top-level keys that view
  reads: `created_at`, `n`, `local_model` (the model that answered locally), `external_model`.
- Tests after this: tests/test_d_hybrid_stages.py + tests/test_e_hybrid_ui.py + tests/test_b_assessor.py: 51 passed.
