# Worklog: UI round 4 — plain summary first, "Show technical analyses" second

Requirement (user, marked IMPORTANT): "Make every explanation show a concise, jargon-free summary, easily
understandable and immediately actionable first on the UI. Only then reveal the rest of the app's current output in
an expandable menu 'Show technical analyses'."

## 2026-09-19 — kickoff
Plan (four tasks):
- T1 backend: `tpm/api/brief.py` + `GET /api/runs/{id}/brief` and `/brief/item` + `tests/test_e_brief.py`.
- T2 page layout: `js/brief.js` (`summaryCard`, `techDetails`, `itemBrief`), every view = title, summary card, ONE
  `<details class="tech-details">` with everything the view rendered before.
- T3 item level: diagnosis cards, flag detail, suspicious rows, quality checks / batch reasons, ref popovers, assessor
  answers, chat answers: plain summary first, old content in a nested `<details>` with the same label.
- T4 i18n (`brief.*` keys only), CSS appended to styles.css, `node --check` on .mjs copies, tests.

Constraints kept: no edits to `tpm/api/plain.py`, `tpm/llm/*`, `tpm/report/email.py`, `tests/conftest.py`, `rep.*` keys,
the e-mail form in report.js (only wrapped). Shared files (server.py, styles.css, i18n, dataflow.js) get targeted edits.

## 2026-09-19 — T1 done: backend brief summaries
What:
- `tpm/api/brief.py` (new): `brief_for(ws, settings, view, lang)` for views overview / understanding / quality / monitor /
  diagnoses / assessor / dataflow / log / report and `brief_item(ws, settings, id, lang)` for DIAG- / FLAG- / CHK- / batch
  ids (own templates) and EV- / INF- / RULE- / PATTERN- / EGR- ids (reuses `evidence_plain.resolve_ref`, text passed through
  `soften()`; these generic ones are English in every language). Templates in en / fi / sv in the dict `T`; `tr()` picks a
  `<key>.1` singular text when n / k / b == 1. Facts are language independent and cached per run on file mtime (`_memo`).
  Limits enforced in `_finish` (22 / 28 words, 3 points, 3 actions). Sensors are named "header of the file (S01)".
  "pending" comes from status.json stages (`stage_state`): pending / failed / skipped each have their own sentence.
  Verdict rules: quality = untrusted batches -> problem, trusted-with-issues -> attention; monitor = sustained events
  (overlapping flags merged into "places") with severity >= 0.7 -> problem, only single readings -> attention;
  diagnoses = explained process/sensor/mixed finding with confidence >= 0.65 -> problem. The "most important finding"
  prefers explained findings over unexplained ones of similar strength and never a rejected one.
- `tpm/api/server.py`: two plain `def` routes next to `/plain`: `GET /api/runs/{id}/brief?view=&lang=` (400 unknown view)
  and `GET /api/runs/{id}/brief/item?id=&lang=` (404 unknown id). run_id validation = `state.ws(run_id)` like the neighbours.
- `tests/test_e_brief.py` (new, 40 tests): every view x language shape + limits + forbidden words, dictionary parity,
  pending / failed runs, item briefs (diagnosis, flag, check, batch, evidence, pattern, rule), the team's wording for point
  findings in all three languages, actions point to existing views and well-formed refs.
Action refs the UI must understand: object ids, `B00003`, `S07`, `rows:120-180[:S01,S02]`, `section:<name>` with names
catalog, checks, suspicious, flags, timeline, recommendations, ledger, profile, verify, preview, email.
Read critically on swat_points, extra_modes_rowlabels, demo_cli, run_demo_synth, te_full_v2 (en / fi / sv).

## 2026-09-19 — T2 / T3 / T4 code landed (browser verification follows)
What:
- `js/brief.js` (new): `summaryCard(view)`, `techDetails(view, ...children)` (open state per view in sessionStorage
  `tpm.tech.<view>`, only a person's own click is remembered; opens by itself when the hash has any parameter),
  `techNested()`, `itemBrief(id)` (GET /brief/item, cached per run + language, cleared on a decision), `itemBriefLocal()`,
  `answerBlock()` (> 60 words: first two sentences, rest + technical nodes in the expander), `focusSection()`,
  `actionTarget()` (ref -> hash params), `resizeCharts()` (Plotly.Plots.resize on the details `toggle` event).
- Every view: `const page = el('div', {class:'view'})` holds title + summary card + ONE `<details class="tech-details">`;
  the old `view` variable now IS the body of that expander, so all old code (and code other people add with
  `view.append(...)`) lands inside the technical part unchanged. `addPlainBox(view, '<name>')` kept (tests look for it).
- Deep links: `core.flash()` calls the new `core.revealAncestors()`; `app.js` calls `focusSection(main, params.section)`
  after a render; blocks are tagged `data-brief-section` (catalog, checks, rules, suspicious, timeline, flags,
  recommendations, ask, ledger, profile, verify, preview, email).
- Item level: diagnoses.js (itemBrief + decision bar first, all old detail nested; "most important findings" block with
  accept / question / override above the expander, max 3, never a rejected one), monitor.js (flag detail the same way;
  suspicious row = local brief with the team's wording + nested statement / plot / ids), quality.js (batch row = plain
  verdict lines + nested reasons; check rows = plain line + inline nested statement; clicking a check row shows its
  itemBrief + nested `checkCard` instead of opening the chat, the "Ask about this" button opens the chat),
  understanding.js (signal detail: plain sentence + naming box first, measurements nested), core.js popovers (itemBrief
  first, old card nested; open for engineers), assessor.js + chat.js answers via `answerBlock` (tool trace + citations in
  the expander; `fallback.normalize_chat_result` and the chat route now pass a compact `tool_trace` through).
- runs.js: overview card ("Run X in short") on top once the selected run is done or failed; hidden during an upload.
- i18n: 28 `brief.*` keys appended textually to en / fi / sv (scratchpad script, no reordering); English fallbacks for
  all of them inside brief.js. CSS appended to styles.css (section "plain summary first").
Tests: tests/test_e_brief.py + test_e_ui_assets.py + test_e_ui_views.py + test_e_evidence_plain.py = 128 passed.

## 2026-09-19 — verified in a browser, fixes, state at hand-over
Verified on a server of this tree (port 8077, TPM_NO_DOTENV=1; stopped afterwards; port 8000 never touched), runs
extra_modes_rowlabels / swat_points, operator role, en + fi:
- 81 briefs (9 views x 3 languages x 3 runs) fetched over HTTP: limits hold, no forbidden word; static JS served.
- Every page: title, summary card, ONE expander, closed by default, label "Show technical analyses" /
  "Hide technical analyses" (fi "Näytä / Piilota tekniset analyysit"). No console errors.
- Deep link `#/monitor?flag=FLAG-000003` with the expander remembered as closed: opens by itself, the remembered
  preference stays closed; flag detail shows brief + decision bar + nested expander; chart widths equal their container
  after opening (page-level and nested). Chromium lays out closed <details> content, so widths were right even before
  the resize; the toggle resize is for engines that do not.
- Summary-card actions: object refs navigate and unfold; `section:suspicious` unfolds + scrolls; the question button
  opens the chat with the question prefilled. Popover of a cited id: brief first, old card nested.
- 360 px wide: no horizontal page scroll on any page, open or closed (the 278-button trust bar is clipped by #main as before).
Fixes found by looking: native `append()` got an array in the top-findings block (spread it); double space in
`firstSentences`; batch rows now use the server's batch brief (names the problem kinds) with the local brief as a
fallback; item background toned down; headline of a data diagnosis names the sensor; "process behaved unusually" ->
"unusual behaviour was found" where the cause may be a sensor or the data; section scroll repeats once after layout settles.
Tests: tests/test_e_brief.py 44 passed (adds: page wiring, item wiring, brief.js helpers under node, section names,
i18n + CSS); with test_e_ui_assets + test_e_ui_views + test_e_evidence_plain + test_e_ui_upload: 141 passed before the
last additions, 75 passed (brief + assets + views) after them; test_e_api -k "chat or missing or every_get or index": 25 passed.
Full suite NOT run (reviewer).
Open points / uncertain:
- EV- / INF- / RULE- / PATTERN- / EGR- item briefs reuse evidence_plain text: English in fi / sv, softened for method words.
- Problem counts in "Most common problems" are ordered by weight (severity), not by count, so the numbers can look unordered.
- The report page's Open / Download buttons are inside the expander (as instructed: everything the view rendered);
  the summary's "Read the report" / "Send it by e-mail" buttons unfold and scroll there.
- Chat: a real model round trip was not exercised in the browser (answerBlock checked with long / short texts in the page
  and under node); `tool_trace` reaches the UI only for answers produced after this change.
- Browser incident: one verification batch ran without a tabId after my tab had disappeared and landed in another
  session's tab (127.0.0.1:8791): it set localStorage `tpm.run` = extra_modes_rowlabels and `tpm.lang` = en for that
  origin, cleared its `tpm.tech.*` session keys, reloaded it and stepped through the views. Nothing on disk was touched.
  Previous values unknown, so not restored. All later calls used an explicit tabId with a port guard.
