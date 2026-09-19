# Round 5 B (2026-09-19) - chat controls + live monitor alarm path

Owner: agent B of round 5. Two parts: (1) the chat drawer gets several chats per run, a clearable context, clear /
delete history and a Stop button that really stops the model; (2) the live monitor watches for drift towards known
failure types (config/failure_signatures.yaml, from message.txt) and shows one alarm card
ALARM -> PROBLEM -> CAUSE -> SUGGESTION.

## 2026-09-19 afternoon - Part 1: chat controls

Done:
- `tpm/llm/agent.py`: stop flags (`stop_event`, `request_stop(turn_id|chat_id)`, `is_stopped`), checked by
  `run_agent` before every model call and after every tool call (`stop=` callable); a stopped turn returns
  `{"stopped": True, "incomplete": True}`. `chat(..., chat_id=, client_turn_id=)`: the UI names the turn in advance
  so Stop can flag it while the model works; both persisted turns carry `chat_id` (old turns count as "default");
  a stopped turn is persisted as `status: "stopped"` and logged as `chat_stopped`. `chat_history(ws, limit, chat_id)`
  filters; `clear_chat(ws, chat_id)` removes one chat's turns.
- `tpm/api/fallback.py`: `normalize_chat_result` passes `turn_id`, `chat_id`, `stopped` and the `series` the agent
  looked at (drawn as a sparkline under the answer); id helpers + `clear_chat` so the routes work without the agent.
- `tpm/api/server.py` (chat routes only): POST /chat takes `chat_id` + `client_turn_id`, answers `{stopped: true}`
  for a stopped turn; GET /chat?chat_id= filters and returns `chats` (ids, first question, context, n);
  POST /chat/stop {turn_id|chat_id}; POST /chat/clear {chat_id, delete} and DELETE /chat?chat_id= remove the turns
  (logged as `chat_cleared` / `chat_deleted`). No new imports in server.py.
- `tpm/api/static/js/chat.js`: chat list (select + "+ New chat" + menu with Clear history / Delete chat), per-run
  localStorage (`tpm.chats.<run>`), server history reloaded per chat; context chip with an x; `setChatContext` /
  `openChat` on a chat with messages REPLACES the context and adds a "Context changed to ..." line; Send turns into
  Stop while busy (AbortController + POST /chat/stop), input disabled meanwhile, "Stopped." line in the thread;
  citations, tool trace, follow-ups, Ask-why entry points unchanged.
- i18n: chat.* keys (15) in en/fi/sv. CSS in `styles-live.css`.
- Tests: `tests/test_e_chat_controls.py` (7).

## 2026-09-19 evening - Part 2: live monitor, known failure types, alarm card

Done:
- `config/failure_signatures.yaml` (new): faults 1-20 from message.txt as data (id, fault, name + name_fi/sv,
  columns, pattern, confidence documented|hypothesis, visible, suggestion + fi/sv, note) plus three generic
  signatures (G-shift, G-variance, G-collapse) with no fixed columns. Faults 3, 9, 15, 16-20 are kept with
  `visible: false` (never alarmed, shown as "not visible in sensors").
- `tpm/live/signatures.py` (new): `load(extra_dirs)` (built-in file + every *.yaml in the monitor's
  `workspace/_live/signatures/` and `_live/`; a later id overrides), `match_columns` (exact, loose, alias S01..,
  display names from `_live/names.json`), per-pattern `indicator()` in [0,1], `evaluate()` with score = mean
  indicator over the signature's present sensors, direction of travel from the score history, projection over 5
  cycles, status quiet | imminent | occurring | na | invisible with hysteresis, `best_cause()`.
  A signature needs at least half of its sensors (and >= 2) in the feed, else "na".
- `tpm/live/engine.py`: `learn` adds `step_ref`, `cycle_stats` adds `steps` + `std`, new `features()` (z, var_ratio,
  slope, jump_ratio, z_hist, mono) reused by the matcher. New MSG keys `sig.imminent|occurring|cleared`.
- `tpm/live/monitor.py`: loads the catalogue, evaluates it every real cycle, writes `state["signatures"]` and
  `state["alarm"]` (the card: trip / problem sensors with sparkline data / cause with confidence and "also" /
  suggestion), signature events + `signature_alert` log lines, `signatures` in the cycle log. The advice prompt
  to the local model still gets sensor names + verdict only (catalogue never enters a prompt; test checks it).
  `start_demo(scenario="plant"|"te")`: "te" names the sensors xmeas_1, xmeas_4, xmv_3, ... and drifts them like
  Fault 1. `start_simulation(inject=<sig id>|"auto", inject_after=<rows>, inject_strength)`: adds the failure's
  pattern to its columns of any replayed file (mean shift, collapse, variance, slow drift, jumpy, bump, fading).
- `tpm/live/router.py`: GET /api/live/signatures[?reload=1], POST /api/live/signatures/reload.
- `tpm/api/static/js/views/live.js`: alarm card (4 steps, sparklines, i18n names/suggestions), "Known failure
  types" panel (score bar, status, sensors, direction; n/a + invisible collapsed), signature events, toast once per
  alarm id, "Demo: a known failure type" button, inject chooser in Simulate, basic role (`roleAllows('operator')`
  false) = verdict + alarm card + one chart, no tabs/tables. Page number follows the lead's renumbering (7).
- i18n: live.* keys (72) in en/fi/sv. CSS in `styles-live.css`.
- Tests: `tests/test_g_live_signatures.py`.

How to demo the drift alarm (judges):
1. Live monitor page -> "Demo: a known failure type" (engineer role). Learning takes 3 cycles of 10 s; from cycle 7
   the sensors xmeas_1, xmeas_4, xmv_3 drift; the panel shows "Fault 1 ... imminent" (rising), then "occurring";
   the alarm card appears with ALARM (Fault 1, since when) -> PROBLEM (the three sensors, sparklines, "N spreads
   above/below normal") -> CAUSE (documented, match %) -> SUGGESTION (check the A feed and stream 4 ...). A toast
   fires once. At cycle 10 xmeas_30 freezes (sensor problem, shown separately).
2. "Try the built-in demo" (generic names): the drift on temperature and flow matches the generic mean-shift
   signature: CAUSE says "No known failure type matches. Generic pattern: mean shift on some sensors".
3. Any file: Settings -> Simulate with a file -> choose the file, pick "Inject a known failure type" (e.g. Fault 6:
   feed loss on a TE file, or "auto" on any file) -> Start. The failure is added after learning + 2 cycles.
4. Own failure types: drop a YAML like config/failure_signatures.yaml into workspace/_live/signatures/ and restart
   the source (or GET /api/live/signatures?reload=1).

Open points:
- Signature matching uses z (normal spreads) independent of the operator's percent thresholds; a very short
  learning phase makes sigma small and the matcher keener. Thresholds live in signatures.py constants.
- brief_bump / fading_shift / slow_drift are implemented from their descriptions and tested only through the
  injector's shapes; no real fault-4/5/13 data was checked.
- Column lists of faults 8, 10, 13 are hypotheses (marked so in the YAML and the UI).

## 2026-09-19 19:55 - B's cut-off piece finished (known-failure-type matcher): 3 failing tests fixed at the root
- 19:20 Failure 1 (F11 "variance increase" also occurring on a square-wave / sticking-valve input): product bug.
  `engine.typical_moves` (median reading-to-reading change, counting only readings that changed, so analyzers are
  judged on their updates) -> baseline `move_ref`, cycle `moves`, feature `move_ratio`. `signatures.indicator`:
  jumpy needs the typical move to stay normal (sticky = 1 at <= 1.15x, 0 at >= 1.45x: more jitter everywhere is not
  sticking); variance_increase is multiplied by (1 - jumpy), so a step pattern is not "more jitter". Regression added
  the other way round too: 4x noise on xmeas_9 / xmeas_21 / xmv_10 -> F11 occurring, F14 quiet, best cause F11.
- 19:25 Failure 2 (TE alarm: newest signature event was G-shift, not F01): product bug (one alarm, most specific
  cause). `signatures._mark_covered`: an active generic signature that shares a sensor with an active fixed-sensor
  one (or a generic one with a more specific pattern) gets `covered_by`. `LiveMonitor._signature_alerts` (new, from
  `_analyse`): alerts follow the shown status (covered = quiet), no alert for a covered generic, a generic alert of
  this or the previous cycle is removed from the event list when the specific one trips, and the specific event /
  log line carries `replaces: [...]`; no "cleared" line for a replaced generic. `best_cause`: on shared sensors the
  more specific pattern wins even with a slightly lower score (collapse over mean shift). Generic signatures now need
  their pattern on 2 sensors ("some sensors"); a single off sensor gets the plain drift card (the test's own tail).
  New test: generic alert first, specific one next cycle -> only F01 left, `replaces == ["G-shift"]`.
- 19:30 Failure 3 (injected mean shift +0.1 % instead of clearly visible): product bug (ramp of 2 cycles = 1000 rows
  on a 60-row file, size from the level). Injection size = 5 normal spreads of the column x strength
  (`INJECT_SPREADS`; spread = learned sigma, else the readings played before the injection; a flat column takes
  0.1 % of its level like `engine.learn`); ramp capped to a third of the rows left (file rows counted / estimated);
  a file too short for the chosen row is refused with a message in the UI's words ('Inject after', 'Repeat').
  The test's 2 %-of-level bar was wrong for xmeas_4 (spread 0.35 % of level): now asserted in the column's own spread
  (>= 4.5 spreads, exactly the reported `plan.amplitude` from row 40). Also fixed: an invisible failure type WITHOUT
  columns (F03, F09, F16-F20) passed the "not reliably visible" check. Plan now reports ramp_rows / spreads / amplitude.
- 19:35 Injector shapes checked against the matcher (scratchpad script, every pattern): jumpy was a one-sided square
  wave with a period of ramp/8 rows (2 % jumps + a mean shift -> F11 / F04 / G-shift won) -> centred stick-slip,
  holds 8 readings then jumps (`INJECT_STICK_ROWS`); collapse falls by max(80 % of level, 2 x size). Now F01, F06,
  F08, F11, F14, F04, G-shift, G-variance, G-jumpy each come out as their own best cause.
- 19:40 New generic signature G-jumpy ("readings hold still and then jump", en/fi/sv) in the YAML, so a sticking
  valve on unknown sensor names still gets a cause now that it no longer counts as a variance increase.
- 19:50 E2E, server of this tree on port 8092 (own TPM_WORKSPACE in the scratchpad, never the port-8000 workspace):
  TE demo (interval 2 s, 100 rows/s) went quiet -> occurring in ONE cycle: the demo drift was 10-20 spreads over 3
  cycles. `DEMO_PLANTS["te"]` now drifts 0.8 normal spreads per cycle (capped at 8) from cycle 7; xmeas_30 freezes
  at cycle 12; 15 cycles (150 s at the UI's 10 s). Demo readings moved into `demo_readings()` (used by the thread and
  by a new test that checks imminent-before-occurring for 3 cycle offsets; the old drift fails it). Re-run: F01
  quiet (4-8) -> imminent (9, 45 %) -> occurring (10, 71 %); alarm card: trip sig.occurring F01 + fi/sv name,
  problem xmeas_1 / xmeas_4 / xmv_3 with sparklines and "how", cause documented 71 %, suggestion "Check the A feed..."
  + fi/sv; events only F01 (G-shift covered_by F01); plant demo: cause G-shift generic (UI: "No known failure type
  matches. Generic pattern: ..."), problem temperature + flow. No cycle errors. Server stopped.
- Tests: tests/test_g_live_signatures.py 16 (12 + 1 new + 3 parametrised), tests/test_g_live.py 17: 33 passed.
  No JS / CSS / i18n touched. Open: events carry `replaces` but live.js does not show it yet (optional line
  "replaces the generic alert"); the TE demo now takes 150 s instead of 120 s.
- 20:05 Full suite (tests/, -k 'not slow'): 558 passed, 3 deselected, 1 xfailed. Port 8092 server stopped; port 8000 untouched.

## 2026-09-19 21:15 - QA (after the credit outage): chat drawer + live monitor in a headless browser
- 19:58-21:10 Server of this tree on port 8093 (TPM_NO_DOTENV=1, own TPM_WORKSPACE in the session scratchpad with copies of
  demo_cli + swat_points; the port-8000 server and workspace untouched), local gemma4 for real answers; QA scripts and
  screenshots in the scratchpad folder r5b_qa/. Server stopped at the end.
- Chat, checked OK: "+ New chat" (empty, own context), switching restores each chat's messages + context, context chip
  with x, Ask why on a diagnosis then on a flag replaces the context with a "Context changed to ..." line, Clear history
  (client + GET /chat?chat_id= returns 0 turns, chat_cleared logged), Delete chat (chat_deleted logged), Stop during a
  real gemma4 answer ("Stop" while busy, "Stopped." after 0.3 s, input usable, chat_stop logged at once, the turn is
  persisted as stopped + chat_stopped once the model step in flight returns, 6-25 s later), a question asked right after
  Stop answered in 37 s (fi, basic), a normal question answered with citations, sources, series chart and follow-ups
  (177 s); en/fi/sv x basic/operator/engineer: no console errors, no raw keys, the drawer fits 360 px.
- Chat bugs fixed (js/chat.js, styles-live.css): the context text named the flag of a diagnosis ("FLAG-000011 ..." for
  DIAG-000001) -> the object itself first (ctxText, chip order), long statements cut at a word with the full text on
  hover; browsing items (every page sets the context of the selected item) piled up "Context changed" lines -> one
  pending line, none when back to the context of the last question, "Context changed to the whole run" for a page-level
  question, "Context cleared" only for the x; lines are stored as kind + context and re-translated on a language switch;
  an empty chat's name follows its context until the first question, Clear history renames it; a history reload put
  every context line at the end and showed a stopped turn after the next question (the stopped turn is persisted late)
  -> lines go back between the same turns, answers are paired with their question by turn_id (byTurn); every re-render
  (a page selecting an item) wiped the question being typed and moved the focus into the drawer -> draft kept (per
  chat), focus only when an answer ends; Stop after a run switch posted to the new run -> to the answer's run; the
  chat menu stays open on outside clicks -> closes; an autoAsk while busy was dropped -> put in the input; "**bold**"
  markers of model answers shown raw -> removed; follow-ups looked like plain text -> bordered buttons; "+ New chat"
  wrapped onto two lines -> nowrap.
- server.py (chat routes only): POST /chat/stop with a turn id flagged every running turn of the chat too, so a question
  asked right after Stop could be stopped as well (the two requests race) -> only the named turn; the whole chat only
  without a turn id (Clear / Delete). Test added.
- Live, checked OK: TE demo -> Fault 1 quiet (4-8) -> imminent at cycle 9 (37 %) -> occurring at cycle 10 (64 %); the
  card shows ALARM -> PROBLEM (3 sensors, sparklines, "N spreads above/below normal") -> CAUSE (documented, match %) ->
  SUGGESTION in en/fi/sv, one warn toast + one alarm toast; panel with score bars, 15 n/a + invisible types collapsed;
  basic role = verdict + card + one chart (no tabs, tables, panel); plant demo -> generic cause; no console errors.
- Live bugs fixed: (1) the TE demo raised "ALARM: the process is drifting" (card + toast) in normal running right after
  learning in most runs: xmeas_1 / xmv_3 wandered 1-1.6 % of their level against the default 1 % / 2 % thresholds and
  tripped the trend check -> DEMO_PLANTS["te"] spreads under 1 % of the level (drift in spreads unchanged, so the
  Fault 1 timing is identical; checked offline for 20 cycle offsets: 0 alarms in cycles 4-6, percent alarm at the
  occurring cycle); (2) after a demo / non-repeating replay had played all its rows the monitor reported "Data not
  trusted, only 0 rows arrived" every cycle and flooded "What changed" -> the cycles stop (source_finished in the log),
  the last verdict / card / failure types stay, the status says "finished", start buttons stay on the page (engineer);
  (3) Stop left the alarm card next to "the data source was stopped" -> cleared; (4) a generic cause read "Known failure
  type occurring: Generic: ..." and "Generic pattern: Generic: ..." -> "The process is drifting: mean shift on some
  sensors" (card, toast, events); (5) a generic row covered by Fault 1 showed a second red "occurring" -> neutral row
  "same sensors as Fault 1 ..., which explains it", no "3/2"; (6) imminent card titled "Alarm: Watch" -> "Early
  warning"; (7) basic card footer pointed at an event list basic mode does not show -> hidden; (8) server messages whose
  key is not translated showed the raw key -> the server's English text; events name the generic alert they replace.
- Tests added: test_g_live_signatures (TE demo quiet in normal running x5 offsets, finished replay keeps its analysis,
  stop clears the alarm), test_e_chat_controls (stop by turn id leaves the next question running, drawer wiring).
  Requested files: 80 passed; full suite (-k 'not slow'): 567 passed, 3 deselected, 1 xfailed.
- i18n (owned by another agent now): 9 new live.* keys requested (status.finished, alarm.titleImminent, sig.coveredBy,
  event.replaces, alarm.trip.genericOccurring / genericImminent, alarm.toast.genericImminent, event.generic.occurring /
  imminent); English fallbacks in live.js FB until then.
- Open (not in my files): the top bar is wider than a 360 px screen (run picker + lamps, page scrolls sideways);
  "PROBLEM" breaks into "PROBLE / M" in the Monitor page's flag strip when the drawer is open (monitor.js /
  styles-views.css); Stop frees the UI at once, but the local model finishes its current step (non-streaming httpx call
  in providers.py cannot be cancelled), so a question right after Stop waits for that step; follow-ups are not persisted
  by the agent (gone after a reload); the source name / detail line of the live page is server English.
