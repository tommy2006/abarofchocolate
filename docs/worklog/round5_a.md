# Worklog: UI round 5 (agent A) — problem → reason → answer, diagrams instead of lists

User requests (quoted in the task): (1) Data quality pages show what checks ran on each piece and what the % score
means in layman terms; (2) faulty data first, then why, then what to do; (3) a clear path problem → reason → answer
with suggestions for every problem; (4) sensor type hypotheses and sensor interactions brought out; (5) diagrams
instead of long lists, well coloured and labelled.

Files owned here: `tpm/api/advice.py` (new), `tpm/api/advice_api.py` (new), `tpm/api/brief.py` (why / fix on item
briefs), `js/views/{quality,diagnoses,monitor,understanding}.js`, `js/charts.js` (additive), `styles-views.css`
(new), i18n keys `adv.* / dq.* / und.* / mon.* / diag.*` (new keys only), `tests/test_e_advice.py`,
`tests/test_e_views_round5.py`, one line in `server.py`.

## 2026-09-19 — kickoff
Plan:
- A. backend: suggestion library (en / fi / sv) for every check type, every cause class x flag kind, batches;
  `advice_for(kind, subtype, facts, lang)`; `GET /api/runs/{id}/advice`; item briefs carry `why` + `fix`.
- B. Data quality: faulty data first (batch x check-type heatmap + trust bars in ONE figure), worst-8 pieces as
  Problem → Reason → Answer strips, "What was checked" chips + stacked bar, the % score explained in words + legend.
- C. Diagnoses: strips per finding, donut by cause + bar by group, timeline of findings.
- D. Monitor: timeline strip of events, events per sensor, strips for the strongest events.
- E. Understanding: sensor-type cards (instrument + unit operation, confidence bar, evidence, accept / correct),
  network of sensor interactions (nodes coloured by instrument type, edges by |r|, arrows by lag).
- F. Modes: `roleAllows('operator')` for anything beyond basic, `roleAllows('engineer')` for engineer-only; basic =
  summary card + ONE diagram + top 3 strips.
- 19:02 A landed on disk: tpm/api/advice.py (library en/fi/sv, advice_for, advice_for_object), tpm/api/advice_api.py (GET /advice), one line in server.py, brief_item carries why / fix / can_use_rows. Tests next.

## 2026-09-19 19:05 — NOTE: two copies of subagent A are running (see round5_lead.md: a session restart duplicates a
session). Copy "A2" (this entry) found the other copy's advice.py / advice_api.py / test_e_advice.py on disk at 19:02,
keeps them as they are (50 tests pass with brief), and continues with the frontend in this order, re-reading every
file right before writing it and keeping whatever is already on disk: js/charts.js (additive block "round 5" at the
end), styles-views.css, js/views/quality.js, js/views/diagnoses.js, js/views/monitor.js, js/views/understanding.js,
i18n keys, tests/test_e_views_round5.py. If you are the other copy and read this before touching those files: the
files already carry a "round 5" marker comment when A2 has written them - build on them, do not replace them.
- 19:03 tests/test_e_advice.py written: 6 tests pass (with test_e_brief.py: 50 passed). Next: charts.js helpers + styles-views.css, then quality.js.
- 19:06 js/charts.js: round-5 helpers appended (vt + English fallbacks VFB, praStrip / praForItem, cappedList, donut, hbar, timeline, sensorKind, networkLayout, strongestEdges, drawNetwork). Next: styles-views.css + quality.js.

## 2026-09-19 19:10 — copy "A1" (wrote advice.py / advice_api.py / test_e_advice.py / the charts.js round-5 block)
Read A2's note. To stop the two copies overwriting each other, A1 SPLITS the remaining work and takes the END of
A2's list:
- A1 (this copy): js/views/understanding.js, js/views/monitor.js, then tests/test_e_views_round5.py for those two.
- A2: styles-views.css, js/views/quality.js, js/views/diagnoses.js, i18n keys (one writer only: A2; the English texts
  of every round-5 key are in `VFB` in js/charts.js, A1 only adds keys there).
- js/charts.js: the round-5 block at the end is A1's (exports listed in the 19:06 line). Line 3 now imports
  `t as t0` (A2's edit) and `vt()` uses `t0`. Add new helpers BELOW the block; do not rewrite it.
- CSS: A1 appends only a block marked `/* A1: understanding + monitor */` to styles-views.css, after re-reading it.

## 2026-09-19 19:12 — A2 ACCEPTS the split
- A2 takes: styles-views.css (base block first, A1 appends its marked block), js/views/quality.js, js/views/diagnoses.js,
  ALL i18n keys (adv.* / dq.* / diag.* / mon.* / und.* incl. every key of `VFB` in charts.js; A1: add English texts to
  VFB only and list the key names in this log, A2 inserts them into en / fi / sv), and tests/test_e_views_round5.py:
  A2 creates the file with a section `# ---- A2: quality + diagnoses + i18n + css`; A1 appends its own tests under a
  section `# ---- A1: understanding + monitor` at the END of the file (re-read before appending; never rewrite).
- charts.js: A2's heredoc append at 19:06 FAILED (spawn error), so A1's block is the only round-5 block; A2 adds any
  extra helper it needs BELOW it, in a block marked `/* A2 additions */`, and uses A1's exports otherwise.
- 19:09 A1: js/views/understanding.js patched (network of sensor interactions above the expander, sensor-type cards with kind filter + confidence bar + why + accept/correct via decisionBar on the instrument inference, capped 12 + show more; basic mode = network + 3 'not sure what X measures' strips). node --check ok. Next: monitor.js.
- 19:10 A1: js/views/monitor.js patched (timeline of events by group/row or seriousness, coloured by kind, sized by seriousness, click opens the alarm; bar 'sensors involved in the most events'; top-3 strongest events as problem->reason->answer strips with the decision bar; flag detail and suspicious-row detail start with the strip). node --check ok. Next: CSS block, tests, server check.
- 19:10 A1: styles-views.css was still empty, so A1 appended ONE block (marked 'A1: ... / A1: end') with the shared classes of charts.js (.viz-host .viz-box .viz-title .viz-help .viz-note .viz-chart .viz-grid .pra* .pra-after) and .kind-chip* .sensor-card* .sensor-dot. A2: do not redefine these; add page-specific classes (dq-*, diag-*) below.
- 19:14 A1: advice.py wording fixes after reading real runs over HTTP (a sentence starting with a sensor's own name keeps its spelling; 'rivit (rivit 5-9)' doubled noun removed). A1's test section is ready in the scratchpad (a1_tests_section.py, 4 tests pass); it is appended to tests/test_e_views_round5.py once A2 has created that file (or A1 creates the file if it is still missing at the end).
- 19:16 A1: headless-browser check (browser-automation skill, port 8081; demo_cli / te_2m / swat_points x operator / basic x understanding / monitor): no console errors, no raw i18n keys, no horizontal scroll, charts 994 px wide, basic = 1 diagram + 3 strips and no technical part, operator = 2 diagrams + cards / strips. Looked at te_2m screenshots: network readable (60 of 80 connections); monitor timeline with ~3000 events was a blob -> smaller, translucent marks above 400 events; the per-sensor bar is one colour.
- 19:17 A1 -> A2: to halve your i18n load, A1 inserts the adv.* / und.* / mon.* keys of VFB (+ diag.viz.row, diag.viz.serious) into en / fi / sv NOW with scratchpad/a1_i18n.py (append before the closing brace, skips keys that exist, re-reads before writing). A2 inserts only dq.* and the other diag.* keys, and skips any key that already exists.
- 19:18 A1 reviewed A2's Data quality page in the headless browser (demo_cli + te_2m operator, extra_modes_rowlabels basic): 2 diagrams + 8 strips + 'What was checked' + 'What the % score means' above the expander (basic: 1 diagram + 3 strips), score sentence next to the batch, no console errors, no raw keys, no horizontal scroll. A1 now waits for A2's diagnoses.js + tests file; A1 does NOT touch them.
- 19:19 A1 reviewed A2's Diagnoses page headless (demo_cli, te_2m operator + basic): donut + groups bar + timeline + top strips above the expander (basic: donut + strips), no console errors / raw keys / horizontal scroll. A1 waits for tests/test_e_views_round5.py from A2, then appends its section.

## 2026-09-19 — A3 (after the usage-credit outage): i18n
- 19:42 A3: i18n finished. A checker in the session scratchpad (i18n_check.py + i18n_suffixes.py) collects every key literal of
  js/*.js, js/views/*.js, app.js and index.html, every key of the fallback tables (VFB / FB / FALLBACK / MODE_FB) and all
  59 dynamic prefixes ('dq.why.' + check_type, 'diag.cause.' + c, 'live.msg.' + key ...), expanding each with the
  concrete values from the code and the backend (contracts literals, quality/checks.py check types,
  assessor ACTION_TYPES, live engine MSG, signatures STATUSES / PATTERNS, pipeline STAGES ...). It found 86 keys
  missing in en / fi / sv: dq.* (viz, faulty, checked, score, group, piece), diag.* (cause, viz, table, filterGroup,
  col.what), 5 more dq.why.* (empty_rows, duplicate_key, gap, duplicate_timestamp, irregular_sampling), 5
  ass.action.* (add_file, downsample, drop_group, drop_range, drop_regime), plain.* (5), und.* naming texts (10) and
  settings.logLocked. All were appended before the closing brace of en / fi / sv (en = the JS fallback text; fi / sv
  written by hand); sv adv.problem "Problem" -> "Fel" (Fel - Orsak - Åtgärd), because the test wants it translated.
  Checker now: 0 missing in every language, 0 unresolved prefixes, no placeholder mismatch, no fi / sv value equal to
  en except abbreviations / ids and words spelled the same in Swedish (Status, Signal, Batch, data, sensor ...).
  Tests: test_e_views_round5 + test_e_ui_assets + test_e_brief + test_e_hybrid_ui + test_e_chat_controls 110 passed;
  the other i18n-reading tests 161 passed, with one flaky failure in test_g_live_signatures under load that passed
  twice when run again (13/13, and 30/30 together with test_g_live). No browser check: no port was assigned.

## 2026-09-19 — A4: QA + fix of every page, every mode (headless browser, port 8094)
- 20:40 A4 kickoff: server on 8094 (TPM_NO_DOTENV=1). Sweep script (scratchpad qa/qa.mjs): modes basic / operator / engineer x
  en / fi x runs demo_cli / te_2m (+ swat_points for quality / monitor) x pages 0-7 + every Settings tab, at 1280 px, 360 px
  and text size 160 %; console errors, failed requests, raw i18n keys, horizontal scroll, spilling boxes, counts of
  diagrams / strips / tables per mode. First pass: no console errors, no raw keys; found: top bar wider than 360 px,
  top bar height stuck after a resize (min-height fed by --topbar-h), text size 160 % ignores the narrow layouts (media
  queries do not see CSS zoom), batch labels of the trust grid drawn over the heat map, "null" printed on Data quality in
  Basic mode, Basic "What to do now" buttons leading into the hidden technical part, Report / Assessor empty in Basic,
  Verify / Export of the decision log hidden inside Settings, sensor names capitalised at a sentence start ("Xmv_3").
- 21:10 A4 fixes (all node --check ok on .mjs copies; 120 tests of the 7 UI files pass):
  - styles.css: top bar min-height fixed at 54 px (was var(--topbar-h), which is measured from the bar: after one narrow
    or zoomed moment it stayed 201 px tall); <body> is a size container ("page") and every layout breakpoint is a
    container query, so A+ / 160 % really switches to the narrow layouts (rail as a tab strip, wrapped top bar, stacked
    strips); tools wrap, run picker shrinks, on a phone the top bar scrolls away and the numbered rail stays on top;
    per-mode top-bar tint + rail pill "Basic / Operator / Engineer mode" (click = change person or mode), light + dark;
    embedded Settings pages keep their head controls (Verify / Export of the decision log were hidden); a.btn not
    underlined; fix list style for item summaries.
  - styles-views.css: a problem -> reason -> answer strip stacks its steps when the strip itself is narrow (container
    query, was a 860 px media query: cramped in detail boxes); chips / confidence words / sensor chips wrap; HTML chart
    legends; report actions box; focus card.
  - brief.js: Basic mode drops next steps that lead into the hidden technical part (section refs, except the parts that
    are visible in Basic: report preview, assessor suggestions, data-flow profile); a finding / alarm / check / batch opens
    as problem -> reason -> answer in a popup (with accept / question / override) instead of a page that hides it;
    item summaries now show "What to do about it" (the fix steps the server already sends); runAction shared with
    charts.briefActionButtons.
  - core.js: batch / alarm / finding links open that popup in Basic mode; row links keep the whole number
    ("rows 5,760–5,784" linked "rows 5" = row 5: wrong place) and also work in fi / sv ("rivit 5 760–5 784", "rader");
    the plain-words box (and its local-model translation call) is skipped in Basic mode, where it would be hidden.
  - charts.js: basicItemModal; praForItem can carry the object's next steps; trust grid: batch names under the grid (they
    were drawn over its first row), short kind names + turned batch names on a narrow box; donut: shares inside, names +
    counts as an HTML legend (outside labels were cut off); binnedTimeline (counts per stretch of the data, stacked by kind
    / cause, click = those rows) for > 400 events / > 300 findings instead of a blob of dots; HTML legends for timeline,
    network and stacked bars (Plotly's legend grew over the plot on a phone); every chart follows its own box width
    (ResizeObserver: a chart drawn before its neighbour box existed spilled out, e.g. the diagnoses ring).
  - quality.js: "null" printed in Basic mode (native append(null)); checks-run chips on every strip in every mode;
    "Open this check" only where the technical part exists; Basic "N further problems ... in Operator mode"; a click on
    the grid opens the batch popup in Basic mode.
  - monitor.js: Basic mode draws rows asked for ("Show these rows") as its one diagram with "What was found in these
    rows"; a group link shows that group's strongest events; a clicked event opens the popup in Basic mode.
  - diagnoses.js: groups chart only when findings pile up in a few groups, else "Sensors named in the most findings"
    (te_2m: 2,280 findings over 2,198 groups made the old chart one bar "other groups"); binned timeline; pattern
    filter also filters the top strips; Basic help text without "Show technical analyses".
  - understanding.js: Basic strips get "Name this sensor" + accept / question / override of the guess; a sensor link /
    network dot shows that sensor's card first in Basic mode (the detail lives in the hidden part).
  - assessor.js: "How to make the data better": every suggestion as problem -> reason -> answer with "Apply after
    approval" above the technical part (Basic: first three; the page was a summary card only in Basic mode).
  - report.js: Open / Download HTML / PDF / PowerPoint above the technical part (Basic mode could not open the report).
  - settings.js: an old #/log link in Operator / Basic mode says the log is shown in Engineer mode.
  - brief.py: a sentence starting with a sensor name keeps its spelling ("Xmv_3 (S44) is probably faulty").
  - i18n (only writer): +19 keys en / fi / sv (role.mode.*, brief.fixTitle, brief.topFindingsHelpBasic,
    dq.faulty.moreBasic, mon.viz.binnedHelp, mon.topHere, mon.topGroup, diag.viz.sensor(Help), diag.viz.binnedHelp,
    ass.plain.*, and B's new live.alarm.titleImminent / live.event.replaces / live.sig.coveredBy / live.status.finished);
    dq.viz.help reworded (grey = nothing found). A3's checker: 0 missing, 0 unresolved prefixes, 0 fi / sv = en.
- 21:35 A4 more: sensor bars (Monitor "Sensors involved in the most events", Diagnoses "Sensors named in the most
  findings") coloured by what each sensor probably measures, kinds as legend + help text; network legend without counts
  (the network leaves out sensors without a strong link, the counts are on the kind filter); a language picked in
  Settings > Display now re-labels rail / lamps / top bar too (only the page was redrawn); +6 i18n keys
  (mon.viz.perSignalHelp and 5 new live.* keys of the live.js editor: generic-drift toast / trip / events),
  diag.viz.sensorHelp reworded; checker clean again (1,083 keys in use, 0 missing, 0 unresolved prefixes).
- 21:40 A4 sweeps (qa/qa.mjs, 3 passes): pass 3 = 119 page checks en (demo_cli, te_2m, swat_points) + 78 fi, all modes,
  every page + Settings tab, at 1280 px, 360 px and 160 % (1280 and 360): 0 console errors, 0 failed requests, 0 raw
  keys, 0 "null"/"undefined" texts, 0 horizontal page scroll (also 360 px at 160 %); Basic = summary + at most 1
  diagram + at most 3 strips, 0 tables, no technical part, accept / question on Monitor / Diagnoses / Understanding
  strips, switch to Operator in the note and on the rail pill. Interaction checks (qa/interact*.mjs; every dialog
  cancelled, no decision recorded): rail 0..7 + Settings, logo 512 px + favicon 200 image/png, #/dataflow and #/log
  land in Settings (log only in Engineer, notice otherwise), Engineer: Verify ("Chain intact: 265 entries verified."),
  Export, Egress ledger, Human decisions audit; A+ x4 = 160 % with rail as tab strip and no overflow, A- back to a 75 px
  top bar; Basic next steps / batch links open the problem -> reason -> answer popup; row links draw the rows;
  network dot / sensor bar / binned bar clicks lead to the sensor card / sensor / rows; mode colours measured
  (top bar, rail, pill, primary button differ per mode in light and dark).
- 21:55 A4: the full suite (-x) stopped at test_e_views_round5::test_a1_monitor_uses_diagrams_and_strips (it looks for
  "hbar(barNode" in monitor.js; my sensorBars helper hid it): monitor / diagnoses call hbar with the kind colours again
  plus charts.sensorKindLegend; the 7 UI files: 120 passed. brief.py: the Understanding summary point "... It is listed
  in the technical part" (hidden in Basic mode) now reads "... Ask it below what it assumed." (the ask button is always
  one of that card's next steps), en / fi / sv.
- 22:10 A4 done. Last sweep after the final edits (understanding / monitor / diagnoses, all modes, te_2m + demo_cli,
  1280 / 360 / 160 %): clean. Tests: the 7 UI files 120 passed; full suite `-k "not slow"` 567 passed, 3 deselected,
  1 xfailed. Port 8094 server stopped; port 8000 untouched (it still runs the old brief.py: restart it for the two
  brief.py text fixes; the static files are served fresh). Side effect of the fi sweep: the local model wrote its
  cached Finnish rewording of the "In plain words" boxes (plain_<view>_fi_m.json) into demo_cli / te_2m / swat_points.
  Left for others: styles-live.css (B) still has two @media (max-width: 900px) rules (alarm steps, known-failure rows)
  that do not follow the text size -> `@container page (max-width: 900px)` (body is the "page" container now).
  Screenshots: scratchpad qa/shots/final_quality_operator.png, final_understanding_operator.png,
  final_diagnoses_basic.png (+ b_*.png Basic pages, zoom160_quality.png, n360_*.png, modes_grid.png).
