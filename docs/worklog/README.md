# Worklogs

One file per agent. Append-only. Every milestone gets an entry so that anyone can continue the work
if an agent stops (usage limits, crash, hand-off).

Entry format:

```
## <ISO timestamp> — <short title>
Done:
- ...
Pending:
- ...
How to continue:
- exact files/functions to touch next, and the test command that proves it works
Decisions / deviations from ARCHITECTURE.md:
- ...
```

Files:
- `agent_a_ingest_profile.md`
- `agent_b_quality_assessor.md`
- `agent_c_detect_diagnose.md`
- `agent_d_llm.md`
- `agent_e_api_ui.md`
- `agent_f_platform.md`
- `integration.md` (lead)
