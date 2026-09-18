# Worklog: agent_d_llm

## 2026-09-18T22:40+03:00 — kickoff, design fixed
Done:
- Read DECISIONS, ARCHITECTURE (4-6), contracts, config, workspace, pipeline, llm/__init__, settings.yaml, synth fixture.
- Environment check: Ollama 0.34.1 is running; pulled = qwen3-coder (too big), deepseek-r1:14b (too big), gemma3:4b,
  llama3.2-vision, llama3:8b. Not pulled: gemma4:e4b-it-qat (configured), qwen3:8b, granite4.1:8b, nomic-embed-text.
  => local route resolves to a fallback (gemma3:4b / llama3:8b); embeddings fall back to TF-IDF.
Pending:
- everything below.
How to continue:
- Module plan (all under tpm/llm/): providers.py, guard.py, ledger.py, prompts/ (package: __init__.py loader + *.j2),
  router.py, embeddings.py, agent.py, sandbox.py (demo workspace builder shared by tests + bakeoff).
- scripts/check_models.py, scripts/bakeoff.py, docs/MODEL_CHOICE.md, tests/test_d_{guard,router,agent}.py
- Test command: .venv\Scripts\python.exe -m pytest tests/test_d_* -q
Decisions / deviations from ARCHITECTURE.md:
- Prompt loader lives in tpm/llm/prompts/__init__.py (package) instead of a sibling prompts.py, so that a
  directory `prompts/` and a module `prompts.py` never shadow each other. Import path is unchanged: tpm.llm.prompts.
- All task JSON schemas are objects at top level (e.g. sensor_hypotheses -> {"hypotheses": [...]}) because both
  Ollama `format` and the Anthropic forced-tool trick want an object schema.

## 2026-09-18T23:25+03:00 — all modules written, tests green, real model exercised
Done:
- tpm/llm/providers.py: OllamaProvider (tags/pick_model/missing_models/pull_commands/chat with JSON-schema `format`,
  think=false for reasoning models, retry once on transient errors, embed via /api/embed with /api/embeddings fallback),
  AnthropicProvider (forced single tool `emit_result` with the schema, falls back to json extraction; base_url override;
  key from settings.external_llm.api_key_env), TemplateProvider, extract_json(), validate_schema() (mini JSON-schema).
- tpm/llm/guard.py: check()/explain()/GuardResult. Whitelist by top-level key, series > max_series_points, numeric count,
  n_samples < min_aggregate_n (block strict / drop non-strict), row-like lists (>=5 shared numeric keys and mostly numeric),
  free-text under non-text keys or inside `values`/`fingerprint` dicts, payload bytes, strict aliasing (signal_alias +
  source_column -> aliases in all strings, name fields stripped, name_hint evidence and human_note dropped).
- tpm/llm/ledger.py: record/read/summary/data_flow_statement, in-memory ledger when ws is None, EGR ids per workspace.
- tpm/llm/prompts/: __init__.py (render, schema_for, SCHEMAS for 8 tasks + AGENT_STEP_SCHEMA), 19 .j2 templates
  (_trust_rules, _language en/fi/sv, <task>.system/.user, generic, agent.system).
- tpm/llm/router.py: complete() (external->guard->Anthropic, fallback local, fallback template; one JSON repair round;
  every attempt in the ledger + decision log), local_chat() for the agent, available(), ensure_models(), explain_guard().
- tpm/llm/embeddings.py: LocalIndex (Ollama embeddings if nomic-embed-text pulled, else sklearn TF-IDF), persisted in
  <ws>/index/, search(query, k, types).
- tpm/llm/agent.py: Toolbox (sql read-only + LIMIT, describe_signal, stats, series, get_flag/diagnosis/evidence,
  list_checks, search, assessor_evaluate), load_context, deterministic_answer (en/fi/sv), run_agent JSON-action loop
  (max_tool_steps, duplicate-call guard, forced final), chat() persisting chat.jsonl + log entries + series for charts.
- tpm/llm/sandbox.py: make_demo_workspace() (dataset/schema/signals/relations/evidence/checks/trust/flag/diagnosis/rules),
  catalog_payload(), diagnosis_payload(). tpm/llm/__init__.py: added chat() and ensure_models() wrappers.
- scripts/check_models.py, scripts/bakeoff.py. tests/test_d_{guard,router,agent}.py: 27 passed (no Ollama needed).
- Real model exercised: gemma3:4b via Ollama (rule_compile JSON ok 14 s, critique ok 12.5 s, agent 2 tool calls 24 s).
  gemma3:4b ignores the Finnish-language instruction (answered in English) and got min/max swapped in one rule: model
  quality, not plumbing. bakeoff running for gemma3:4b + llama3:8b -> docs/bakeoff_results.md.
Pending:
- docs/MODEL_CHOICE.md, final worklog entry.
How to continue:
- Run: .venv\Scripts\python.exe -m pytest tests/test_d_* -q ; scripts\check_models.py ; scripts\bakeoff.py
- Public API: tpm.llm.complete(task, payload, purpose=, ws=, settings=, schema=, language=), tpm.llm.chat(ws, settings,
  message, context=, history=, actor=, task="why_chat"|"assessor_chat", language=), tpm.llm.available(), tpm.llm.ensure_models(),
  tpm.llm.ledger.summary(ws) / data_flow_statement(ws, settings), tpm.llm.guard.explain(settings), tpm.llm.embeddings.LocalIndex.
Decisions / deviations:
- Guard counts EVERY numeric scalar toward max_numeric_values_per_payload (400). A 52-signal catalog carries ~1300 numbers,
  so in the hybrid profile sensor_hypotheses on a TE-sized dataset will be guard-blocked and answered locally unless
  guard.max_numeric_values_per_payload is raised (~3000) or the caller trims fingerprints. Flagged to the lead.
- Ledger also records ATTEMPTED calls (provider unavailable, guard block) with ok=False so the trail is complete.
- Template fallbacks are logged in the decision log as action "llm_template_fallback" (not in the egress ledger: no model call).

## 2026-09-18T23:40+03:00 — DONE: bake-off run, MODEL_CHOICE written, 28 tests green
Done:
- docs/MODEL_CHOICE.md (default justification, alternatives, switching, plain-words "signal catalog" and "guard block").
- scripts/bakeoff.py run on the two pulled candidates -> docs/bakeoff_results.md:
  gemma3:4b 6/6 JSON valid, 6/6 schema ok, tools 3/3, median 20.3 s, total 100 s;
  llama3:8b 6/6, 6/6, tools 2/3, median 27.0 s, total 114 s. (gemma4:e4b-it-qat / qwen3:8b / granite4.1:8b not pulled.)
- Guard now normalizes payloads (pydantic models, numpy scalars/arrays, NaN) before scanning, so nested contract
  objects cannot hide a series (test added). Router applies the same normalization before rendering.
- Full test run: .venv\Scripts\python.exe -m pytest tests -q -> 28 passed (only agent D tests exist so far).
Pending (for integration, not blocking):
- Agents A/B/C: build payloads with the whitelisted top-level keys (signals/relations/evidence/checks/trust/flags/
  diagnosis/rule_text/patterns/assessor/schema_summary/report). Unknown keys are dropped in non-strict, blocked in strict.
- Agent B: expose tpm.assessor.ask(ws, settings, action_text) or evaluate_action(...) -> the agent tool picks it up automatically.
- Agent E: call tpm.llm.chat(ws, settings, message, context={"flag_id":...}, history=[...], actor="human:<name>(<role>)",
  task="why_chat"|"assessor_chat", language=...); use out["series"] ({"points": [[row, mean, min, max], ...]}) for the chart;
  tpm.llm.available() for the status bar; tpm.llm.ensure_models()["message"] for the setup panel;
  tpm.llm.guard.explain(settings) for the "what may leave" panel; tpm.llm.ledger.summary(ws) for the ledger view.
- Agent F: report Data-flow record = tpm.llm.ledger.data_flow_statement(ws, settings).
- Lead: consider guard.max_numeric_values_per_payload ~3000 if hybrid should send a 50-signal catalog externally.
How to continue:
- Tests: .venv\Scripts\python.exe -m pytest tests/test_d_* -q ; models: scripts\check_models.py ; bake-off: scripts\bakeoff.py
- Anthropic path is exercised only with a monkeypatched provider (no key available here). First real call to verify:
  set ANTHROPIC_API_KEY, TPM_PROFILE=hybrid, then tpm.llm.complete("critique", diagnosis_payload(ws), purpose="x", ws=ws).
