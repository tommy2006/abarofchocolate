# Local model bake-off (2026-09-18 23:02)

Configured default: `gemma4:e4b-it-qat`; fallbacks: qwen3:8b, granite4.1:8b, llama3:8b, gemma3:4b. num_ctx=8192, temperature=0.2.

| model | tasks | JSON valid | schema ok | tool calls ok | median latency (ms) | total (s) |
|---|---|---|---|---|---|---|
| llama3:8b | 6 | 6 | 6 | 1/3 | 42795 | 203.5 |
| gemma3:4b | 6 | 6 | 6 | 3/3 | 18691 | 103.7 |

## Per task

| model | task | JSON | schema | tools ok/called | latency (ms) | note |
|---|---|---|---|---|---|---|
| llama3:8b | column_roles | yes | yes | 0/0 | 51007 |  |
| llama3:8b | rule_compile | yes | yes | 0/0 | 42795 |  |
| llama3:8b | sensor_hypotheses | yes | yes | 0/0 | 66931 |  |
| llama3:8b | diagnosis_narrative | yes | yes | 0/0 | 8015 |  |
| llama3:8b | critique | yes | yes | 0/0 | 6706 |  |
| llama3:8b | tool_agent | yes | yes | 1/3 | 28020 | According to the signal catalog, S01 contributed most to FLAG-000001. Its mean in group 1 is unknown, and its mean in th |
| gemma3:4b | column_roles | yes | yes | 0/0 | 26909 |  |
| gemma3:4b | rule_compile | yes | yes | 0/0 | 5901 |  |
| gemma3:4b | sensor_hypotheses | yes | yes | 0/0 | 18691 |  |
| gemma3:4b | diagnosis_narrative | yes | yes | 0/0 | 8874 |  |
| gemma3:4b | critique | yes | yes | 0/0 | 14318 |  |
| gemma3:4b | tool_agent | yes | yes | 3/3 | 28957 | The signal that contributed most to FLAG-000001 is S01, with a mean of 101 across the entire dataset. The evidence suppo |

Tasks: column_roles / rule_compile / sensor_hypotheses / diagnosis_narrative / critique (JSON with schema) and a 3-step tool-agent question on the demo workspace.
Selection rule: highest schema-ok count, then tool-call success, then latency. Switch with `TPM_LOCAL_MODEL=<model>` or `local_llm.model` in config/settings.yaml.
