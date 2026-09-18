# Model choice (LLM layer, agent D)

The Trustworthy Process Monitor works with **no model at all** (template mode), with a **local model** through
Ollama, and optionally with an **external model** (Anthropic) that only ever receives derived artifacts that
passed the egress guard. This note explains which local model is the default, why, how to switch, and two
terms the team asked about: the *signal catalog* and *the guard blocking a payload*.

## 1. Default local model: `gemma4:e4b-it-qat`

| criterion | why it matters here | gemma4:e4b-it-qat |
|---|---|---|
| Size on disk / VRAM | Target machine is a 16 GB RAM / 8 GB VRAM laptop that also runs DuckDB and the detectors | 6.1 GB quantization-aware-trained (QAT) build; fits 8 GB VRAM together with an 8k context (`local_llm.num_ctx: 8192`) |
| Licence | Judges and operators must be able to run it without legal friction | Apache-2.0 |
| Structured output | Every task in this app expects strict JSON validated against a schema (see `tpm/llm/prompts/__init__.py`) | native JSON / function-calling training; Ollama's `format: <json schema>` constrains decoding on top of that |
| Context | Signal catalogs and evidence lists for 50+ signals are 10-20k characters | 128k context window; we cap at 8k for VRAM |
| Quality vs speed | Hypotheses, rule compilation and critiques must be produced in seconds, not minutes | "effective 4B" architecture: per-token cost of a ~4B model, quality closer to 8B class |

The application does **not** depend on native tool calling: the local agent uses a JSON-action loop
(`{"thought", "action", "args"}` / `{"action": "final", "answer", "citations"}`) validated with a schema, so any
model that can emit JSON works. That is also why the fallback list can contain models without tool support.

## 2. Alternatives and fallbacks

`config/settings.yaml`:

```yaml
local_llm:
  model: gemma4:e4b-it-qat
  fallback_models: [qwen3:8b, granite4.1:8b, llama3:8b, gemma3:4b]
  embedding_model: nomic-embed-text
```

`OllamaProvider.pick_model()` uses the configured model if it is pulled, otherwise the first pulled fallback in
this order, otherwise nothing (template mode). The bake-off (`scripts/bakeoff.py`) writes `docs/bakeoff_results.md`
with JSON validity, schema compliance, tool-call success and latency per pulled model.

| model | size | notes |
|---|---|---|
| qwen3:8b | ~5 GB | strong JSON and reasoning; a "thinking" model, the provider sends `think: false` to keep outputs clean |
| granite4.1:8b | ~5 GB | enterprise-oriented, good instruction following, Apache-2.0 |
| llama3:8b | 4.7 GB | widely available, no native JSON mode, relies on Ollama's constrained decoding |
| gemma3:4b | 3.3 GB | smallest; fine for JSON tasks, weaker at following the language instruction (answers in English when asked for Finnish) |
| nomic-embed-text | 0.27 GB | local embeddings for the search index; without it the index falls back to TF-IDF (still local) |

Machines with more VRAM can set a larger model (e.g. `qwen3:14b`); machines with less can drop to `gemma3:4b`.

## 3. How to switch

- Environment: `TPM_LOCAL_MODEL=qwen3:8b` (and `OLLAMA_HOST=http://host:11434` for a remote Ollama inside the operator network).
- Config: edit `local_llm.model` / `local_llm.fallback_models` in `config/settings.yaml`.
- Check what is pulled and get the exact pull commands: `.venv\Scripts\python.exe scripts\check_models.py`
  (the same report is available in the UI through `tpm.llm.ensure_models()`).
- External model (only for the `hybrid` / `eu-hosted` profiles): `external_llm.model`, `TPM_EXTERNAL_MODEL`,
  `TPM_EXTERNAL_BASE_URL` (EU-hosted endpoint), key in the env named by `external_llm.api_key_env`
  (`ANTHROPIC_API_KEY`). The default profile `no-egress` never reads the key.

Nothing is pulled automatically. The app starts in template mode and upgrades itself to the local model as soon
as one of the listed models is available.

## 4. What the "signal catalog" is (plain words)

When a file arrives, the pipeline does not know what the columns are. The **signal catalog** (`signals.json`) is
the description the system builds for every column it decided to treat as a signal:

- an **alias** (`S07`) used everywhere instead of the original header (headers may be missing or misleading);
- what kind of column it is structurally (continuously varying measurement, step-like actuator, sample-and-hold
  analyzer, constant, counter, timestamp, ...), with a confidence;
- **hypotheses** about what it might measure (flow, pressure, temperature, ...) and which part of the process it
  belongs to, each with a confidence and a status (inferred / assumed / uncertain);
- which other signals it moves with (correlation and lag), and which cluster it belongs to;
- a **fingerprint of aggregates only**: count, mean, std, quantiles, noise level, autocorrelation, stuck fraction,
  quantization step, ... never the values themselves;
- the evidence IDs (`EV-...`) behind every statement, so a human can check.

It is the only description of the data that the rule compiler, the report and any external model ever see.
If you can read the catalog, you know everything the language model knows about the data.

## 5. What "the guard blocking a payload" means (plain words)

Before anything is sent to an external model, the payload (the JSON the model would receive) goes through the
**egress guard** (`tpm/llm/guard.py`). The guard is a set of code checks, not a model. It answers one question:
*does this look like derived, aggregated information, or does it look like data?* It blocks the payload when:

- it contains a list of numbers longer than 20 (a time series in disguise);
- it contains more than 400 numbers in total, or is larger than 200 kB;
- an aggregate was computed over fewer than 30 samples (small aggregates can reveal individual values);
- it contains record-like structures (a list of objects that share five or more numeric fields, i.e. rows);
- it contains free-text or categorical values (names, comments, labels: the PII risk);
- it contains anything that is not on the whitelist of artifact types (catalog, relations, checks, trust, flags,
  diagnoses, evidence statements, rule text, patterns, assessor results, chat question, schema summary, report sections).

In strict mode (profiles `no-egress` and `eu-hosted`) the guard also replaces every original column name with its
alias and drops human-written notes.

**Blocked** does not mean the task fails. The router answers the same task with the **local model** instead, and if
no local model is available, with a **code template**. Every attempt is written to the egress ledger
(`egress_ledger.jsonl`) and the hash-chained decision log: task, route, model, artifact types, payload size and
SHA-256, a 500-character preview of what was sent (external only), the guard's verdict and reason, latency, and
whether it succeeded. The "Data-flow record" in the report is generated from that ledger
(`tpm.llm.ledger.data_flow_statement`), and the UI can show the plain-language version of the current rules with
`tpm.llm.guard.explain(settings)`.

In the default `no-egress` profile the guard is never even reached: the routing table sends every task to the local
model, and `Profile.allow_external` is false, so no network model is called even if an API key is present.
