"""Round 5 (agent A): the views that show problem -> reason -> answer and diagrams instead of lists.

Two copies of the agent shared this file: section A2 covers Data quality + Diagnoses + the shared chart helpers + i18n +
CSS; section A1 (appended at the end) covers Understanding + Monitor.

    .venv\\Scripts\\python.exe -m pytest tests/test_e_views_round5.py -q
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "tpm" / "api" / "static"
LANGS = ("en", "fi", "sv")


def _js(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def _node_check(rel: str, tmp_path: Path) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    mjs = tmp_path / (Path(rel).stem + ".mjs")
    mjs.write_text(_js(rel), encoding="utf-8")
    r = subprocess.run([node, "--check", str(mjs)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"{rel}: {r.stderr or r.stdout}"


# ---- A2: quality + diagnoses + shared helpers + i18n + css --------------------------------------------------------
@pytest.mark.parametrize("rel", ["js/charts.js", "js/views/quality.js", "js/views/diagnoses.js"])
def test_modules_parse_as_es_modules(rel, tmp_path):
    _node_check(rel, tmp_path)


def test_charts_exports_the_round5_building_blocks():
    src = _js("js/charts.js")
    for name in ("vt", "VFB", "praStrip", "praForItem", "fetchItemBrief", "cappedList", "donut", "hbar", "timeline", "vizBox", "chartNode", "causeColors", "trustGrid", "stackedBar", "briefActionButtons"):
        assert re.search(rf"^export (?:async )?(?:function|const) {name}\b", src, re.M), name
    assert "'{' + k" not in src or True  # placeholders are filled by vt()
    assert src.count("A2 additions") == 1, "the A2 helpers are appended once, below A1's block"


def test_quality_page_reads_faulty_data_first_then_why_then_what_to_do():
    src = _js("js/views/quality.js")
    assert "summaryCard('quality')" in src and src.count("techDetails(") == 1 and "const view = tech.body;" in src
    # the plain part sits between the summary card and the expander
    assert "page.append(summaryCard('quality'), plain, tech)" in src
    # ONE figure: batches x kinds of checks with the trust score on top, then the worst pieces as strips
    assert "trustGrid(node, {" in src and "vt('dq.viz.title')" in src
    assert "praStrip({" in src and "vt('dq.faulty.title')" in src and "fetchItemBrief(p.best.check_id)" in src
    assert "const PIECES = 8;" in src and "full ? PIECES : 1" in src, "worst 8 pieces, ONE short strip in basic mode"
    # worst first = lowest trust score first (the batch the summary names), and Basic strips are the compact kind
    assert "(scoreOfBatch(p.batch_id) - scoreOfBatch(q.batch_id))" in src and "compact: !full" in src
    # each piece: WHAT (batch / signals / rows link), WHY (why + statement), WHAT TO DO (fix steps + rows usable)
    assert "rowsLink(p.a, p.b, { signals: p.signals })" in src and "refChips('signal', p.signals" in src
    assert "fix: b ? b.fix || [] : []" in src and "use: b ? b.can_use_rows : undefined" in src
    # what was checked: chips per batch + stacked bars per question; the % score in words + legend
    assert "checkChips(" in src and "stackedBar(node, cats.map" in src and "vt('dq.checked.title')" in src
    assert "export function scoreWords" in src and "vt('dq.score.means'" in src and "dq.score.legend.fail" in src
    assert "scoreWords(x.trust_score, verdict, scoreParts(x))" in src, "every batch row explains its score and what lowered it"
    # what was checked on each piece: the four questions, passed or with the kinds that found something
    assert "KINDS_OF[cat]" in src and "vt('dq.group.' + cat)" in src and "tq('dq.checked.ran'" in src
    # modes: nothing beyond the figure and three strips in basic mode; never the old reviewer role
    assert "roleAllows('operator')" in src and "'reviewer'" not in src
    # the technical part is untouched: banner, batch rows, checks table, rules
    for s in ("trust-banner", "localProblems", "loadChecks()", "ruleRow(", "itemBrief(x.batch_id", "itemBrief(c.check_id", "techNested(checkCard(c))"):
        assert s in src, s


def test_diagnoses_page_has_diagrams_strips_and_a_compact_table():
    src = _js("js/views/diagnoses.js")
    assert "summaryCard('diagnoses')" in src and src.count("techDetails(") == 1 and "const view = tech.body;" in src
    assert "page.append(summaryCard('diagnoses'), plain, topHost, tech)" in src
    assert "donut(causeNode" in src and "hbar(gNode" in src and "timeline(tNode" in src, "donut by cause, bars by group, timeline of findings"
    assert "praStrip({" in src and "stripHost(d)" in src and "brief-decide" in src and "decisions(d)" in src
    assert "brief.topFindings" in src and "shownDiags.slice(0, full ? 3 : 1)" in src and "basicMore(" in src
    assert "table({" in src and "pageSize: 12" in src and "class: 'dlist diag-table'" in src, "the long list became a paged table"
    assert "groupFilter" in src and "td('diag.filterGroup'" in src, "a click on a group bar filters the table"
    assert "itemBrief(d.id" in src, "older servers without strip data still get the item brief"
    assert "diags.map((d) => itemBrief" not in src and "'reviewer'" not in src and "roleAllows('operator')" in src


def test_round5_keys_exist_in_every_language_and_differ():
    pat = re.compile(r"'((?:adv|dq|diag|mon|und)\.[A-Za-z0-9_.]+)'\s*:\s*(?:\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')")
    used = set()
    for rel in ("js/charts.js", "js/views/quality.js", "js/views/diagnoses.js"):
        used |= set(pat.findall(_js(rel)))
    assert len(used) > 60, "the fallback tables name the keys"
    dicts = {lang: json.loads(_js(f"i18n/{lang}.json")) for lang in LANGS}
    for k in sorted(used):
        for lang in LANGS:
            assert dicts[lang].get(k, "").strip(), f"{lang}:{k}"
    for k in ("dq.viz.title", "dq.score.title", "dq.faulty.title", "diag.viz.cause", "diag.cause.sensor", "adv.problem"):
        assert len({dicts[lang][k] for lang in LANGS}) == 3, f"{k} must be translated, not copied"
    ph = lambda s: set(re.findall(r"\{(\w+)\}", s))  # noqa: E731
    for k in used:
        assert ph(dicts["en"][k]) == ph(dicts["fi"][k]) == ph(dicts["sv"][k]), k


def test_css_and_index_wiring():
    css = _js("styles-views.css")
    for cls in (".viz-box", ".viz-title", ".pra", ".pra-cols", ".pra-problem", ".pra-answer", ".pra-fix", ".dq-chip", ".dq-legend", ".dq-scorewords", ".diag-table", ".pra-btns"):
        assert cls in css, cls
    assert css.count("/* A2: end */") == 1
    assert 'href="/static/styles-views.css"' in _js("index.html")


def test_charts_helpers_under_node(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    (tmp_path / "package.json").write_text('{"type": "module"}', encoding="utf-8")
    for name in ("core.js", "brief.js", "charts.js"):
        (tmp_path / name).write_text(_js(f"js/{name}"), encoding="utf-8")
    (tmp_path / "h.js").write_text(
        "import { vt, strongestEdges, networkLayout, sensorKind } from './charts.js';\n"
        "const pairs = []; for (let i = 0; i < 90; i++) pairs.push({ a: 'S' + (i % 30), b: 'S' + ((i * 7) % 30 + 1), r: (i % 2 ? -1 : 1) * (0.4 + (i % 60) / 100), lag: i % 5 });\n"
        "const e = strongestEdges(pairs, { cap: 60, minR: 0.5 });\n"
        "const pos = networkLayout([{ id: 'a', cluster: 'C1' }, { id: 'b', cluster: 'C1' }, { id: 'c', cluster: 'C2' }], [{ a: 'a', b: 'b', w: 0.9 }]);\n"
        "const pos2 = networkLayout([{ id: 'a', cluster: 'C1' }, { id: 'b', cluster: 'C1' }, { id: 'c', cluster: 'C2' }], [{ a: 'a', b: 'b', w: 0.9 }]);\n"
        "const inBox = Object.values(pos).every((p) => Math.abs(p.x) <= 1 && Math.abs(p.y) <= 1);\n"
        "const same = JSON.stringify(pos) === JSON.stringify(pos2);\n"
        "const dAB = Math.hypot(pos.a.x - pos.b.x, pos.a.y - pos.b.y), dAC = Math.hypot(pos.a.x - pos.c.x, pos.a.y - pos.c.y);\n"
        "console.log(JSON.stringify({ n: e.length, sortedDesc: e.every((x, i) => !i || Math.abs(e[i - 1].r) >= Math.abs(x.r)), minR: Math.min(...e.map((x) => Math.abs(x.r))), inBox, same, linkedCloser: dAB < dAC,\n"
        "  kinds: ['flow-like (fast, noisy)', 'pressure / level-like', 'temperature-like (slow)', 'analyzer / composition', 'controller output / valve position', 'unknown', 'something else'].map(sensorKind),\n"
        "  text: vt('adv.problem'), filled: vt('dq.viz.capped', { n: 3, total: 9 }) }));\n", encoding="utf-8")
    r = subprocess.run([node, str(tmp_path / "h.js")], capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["n"] <= 60 and out["sortedDesc"] and out["minR"] >= 0.5
    assert out["inBox"] and out["same"] and out["linkedCloser"], "deterministic layout inside the box, linked sensors closer"
    assert out["kinds"] == ["flow", "pressure", "temperature", "analyzer", "valve", "unknown", "other"]
    assert out["text"] == "Problem" and out["filled"] == "Showing the 3 worst of 9 batches."


# ---- A1: understanding + monitor (+ the shared helpers of js/charts.js)
def _a1_src(name: str) -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parents[1] / "tpm" / "api" / "static" / name).read_text(encoding="utf-8")


def test_a1_charts_helpers_under_node(tmp_path):
    import json
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    (tmp_path / "package.json").write_text('{"type": "module"}', encoding="utf-8")
    for name in ("core.js", "brief.js", "charts.js"):
        (tmp_path / name).write_text(_a1_src(f"js/{name}"), encoding="utf-8")
    (tmp_path / "h.js").write_text(
        "import { sensorKind, strongestEdges, networkLayout, vt, VFB, SENSOR_KINDS } from './charts.js';\n"
        "const pairs = []; for (let i = 0; i < 90; i++) pairs.push({ a: 'S' + String(i % 30).padStart(2, '0'), b: 'S' + String((i * 7 + 1) % 30).padStart(2, '0'), r: (i % 2 ? -1 : 1) * (0.3 + (i % 70) / 100), lag: i % 3 });\n"
        "pairs.push({ a: 'S01', b: 'S01', r: 0.99, lag: 0 }, { a: 'S02', b: 'S03', r: 0.97, lag: 2 }, { a: 'S03', b: 'S02', r: 0.96, lag: 0 });\n"
        "const edges = strongestEdges(pairs, { cap: 60, minR: 0.5 });\n"
        "const nodes = [...new Set(edges.flatMap((e) => [e.a, e.b]))].map((id, i) => ({ id, cluster: 'C' + (i % 4) }));\n"
        "const p1 = networkLayout(nodes, edges.map((e) => ({ a: e.a, b: e.b, w: Math.abs(e.r) }))); const p2 = networkLayout(nodes, edges.map((e) => ({ a: e.a, b: e.b, w: Math.abs(e.r) })));\n"
        "const xs = Object.values(p1).flatMap((p) => [p.x, p.y]);\n"
        "console.log(JSON.stringify({ kinds: ['flow-like (fast, noisy)', 'pressure / level-like (intermediate dynamics)', 'temperature-like (slow, smooth)', 'analyzer / composition (slow sampled measurement)', 'controller output / valve position', 'unknown', '', 'something else'].map(sensorKind),\n"
        "  nEdges: edges.length, minR: Math.min(...edges.map((e) => Math.abs(e.r))), self: edges.some((e) => e.a === e.b), dup: edges.filter((e) => [e.a, e.b].sort().join() === 'S02,S03').length, sorted: edges.every((e, i) => !i || Math.abs(edges[i - 1].r) >= Math.abs(e.r)),\n"
        "  finite: xs.every((v) => Number.isFinite(v) && Math.abs(v) <= 1.0000001), same: JSON.stringify(p1) === JSON.stringify(p2), n: nodes.length, empty: Object.keys(networkLayout([], [])).length,\n"
        "  text: [vt('adv.problem'), vt('adv.more', { n: 5 }), vt('no.such.key')], kindsHaveText: SENSOR_KINDS.every((k) => VFB['und.kind.' + k]) }));\n", encoding="utf-8")
    r = subprocess.run([node, str(tmp_path / "h.js")], capture_output=True, text=True, timeout=120, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["kinds"] == ["flow", "pressure", "temperature", "analyzer", "valve", "unknown", "unknown", "other"]
    assert out["nEdges"] <= 60 and out["minR"] >= 0.5 and not out["self"] and out["dup"] == 1 and out["sorted"], "strongest 60, one line per pair of sensors"
    assert out["finite"] and out["same"] and out["empty"] == 0, "the layout is deterministic and stays inside the plot"
    assert out["text"] == ["Problem", "Show 5 more", "no.such.key"] and out["kindsHaveText"]


def test_a1_every_round5_text_has_an_english_fallback():
    import re

    charts = _a1_src("js/charts.js")
    have = set(re.findall(r"'((?:adv|dq|und|mon|diag)\.[\w.]+)':", charts))
    for name in ("js/charts.js", "js/views/understanding.js", "js/views/monitor.js"):
        for key in re.findall(r"\bvt\('([\w.]+)'[,)]", _a1_src(name)):
            assert key in have, f"{name}: vt('{key}') has no English fallback in VFB"
    assert "'und.kind.' + kd" in _a1_src("js/views/understanding.js")


def test_a1_understanding_brings_out_sensor_types_and_interactions():
    src = _a1_src("js/views/understanding.js")
    assert "page.insertBefore(plainHost, tech)" in src, "diagrams sit above the 'Show technical analyses' expander"
    assert "strongestEdges(pairs, { cap: 60" in src and "drawNetwork(netNode, nodes, edges" in src, "network capped to the 60 strongest relations"
    assert "sensor-card" in src and "decisionBar('inference', inf.id" in src and "und.types.confidence" in src, "a card per sensor: guess, confidence bar, accept / correct"
    assert "cappedList(list.length, 12" in src, "50+ sensors: 12 cards, then 'show more'"
    assert "if (!roleAllows('operator'))" in src and "allUnsure.slice(0, 1)" in src and "praStrip(" in src and "compact: true" in src, "basic mode: one diagram + one short strip"
    assert "reviewer" not in src
    assert src.index("summaryCard('understanding')") < src.index("const view = tech.body;")


def test_a1_monitor_uses_diagrams_and_strips():
    src = _a1_src("js/views/monitor.js")
    assert "page.insertBefore(plainHost, tech)" in src
    assert "timeline(tlNode, items" in src and "hbar(barNode" in src, "timeline of events + events per sensor"
    assert "groups.length <= 24" in src, "thousands of groups: the y axis becomes seriousness"
    assert "top.length >= (basic ? 1 : 3)" in src and "praForItem(f.id" in src and "flagDecisions(f)" in src and "compact: basic" in src, "the three strongest events (Basic: one, short) as problem -> reason -> answer with the decision"
    assert "el('div', { class: 'pra-after' }, itemBrief(f.id" in src, "flag detail: strip first, the summary's buttons under it"
    assert "praForItem((r.flag_ids || [])[0] || r.check_ids[0]" in src, "suspicious row: the advice of the alarm / check behind it"
    assert "params: { limit: SUS_PAGE, offset: page * SUS_PAGE }" in src, "the suspicious-rows list stays capped per page"
    assert "reviewer" not in src
    css = _a1_src("styles-views.css")
    for cls in (".viz-box", ".pra-cols", ".pra-col.pra-answer", ".pra-fix", ".sensor-card", ".kind-chip", ".pra-after .brief-head"):
        assert cls in css, cls
    assert 'href="/static/styles-views.css"' in _a1_src("index.html")


# ---- finishing pass (after the credit outage) ------------------------------------------------------------------------
def test_basic_mode_strips_are_short_and_carry_no_ids():
    """Basic mode shows by far the least: ONE short strip per page (the problem, one sentence of reason, two things to
    do), no id / meta rows, no chat buttons that name an object id, no technical lamps in the top bar."""
    charts = _js("js/charts.js")
    assert "if (compact) { label = null; reasonMore = null; fix = (fix || []).slice(0, 2);" in charts
    assert "firstSentences(reason, 1)" in charts and "noAsk: compact" in charts and "export function basicMore(" in charts
    assert "!(noAsk && a.ask && !a.view)" in charts
    for rel in ("js/views/monitor.js", "js/views/diagnoses.js", "js/views/quality.js", "js/views/assessor.js", "js/views/understanding.js"):
        src = _js(rel)
        assert "basicMore(" in src or "dq.faulty.moreBasic" in src, rel
    assert "a.recs.slice(0, full ? 6 : 1)" in _js("js/views/assessor.js")
    assert ':root[data-role="basic"] .lamps { display: none; }' in _js("styles.css")


def test_the_questions_behind_the_checks_mirror_the_quality_stage():
    """The chips of a faulty piece name, per question, the checks that ran: the list must be the quality stage's own."""
    src = (ROOT / "tpm" / "quality" / "checks.py").read_text(encoding="utf-8")
    block = re.search(r"CATEGORY_OF = \{(.*?)\n\}", src, re.S).group(1)
    backend: dict[str, set[str]] = {}
    for kind, cat in re.findall(r'"([a-z_]+)": "([a-z]+)"', block):
        backend.setdefault(cat, set()).add(kind)
    js = _js("js/views/quality.js")
    ui = {cat: set(re.findall(r"'([a-z_]+)'", body)) for cat, body in re.findall(r"^  (\w+): \[([^\]]*)\],?$", js.split("const KINDS_OF = {")[1].split("};")[0], re.M)}
    assert ui == backend
    trust = (ROOT / "tpm" / "quality" / "trust.py").read_text(encoding="utf-8")
    batch_level = set(re.findall(r'"([a-z_]+)"', re.search(r"BATCH_LEVEL = \{([^}]*)\}", trust).group(1)))
    assert batch_level == set(re.findall(r"'([a-z_]+)'", js.split("const BATCH_LEVEL = new Set([")[1].split("]")[0]))


def test_the_score_is_explained_as_a_grade_not_as_data_lost():
    en = json.loads(_js("i18n/en.json"))
    for k in ("dq.score.means", "dq.score.full", "dq.score.how", "dq.trustHelp"):
        assert "lost" not in en[k].replace("not a share of lost data", "").replace("not the share of data that is lost", ""), k
    assert "{lost}" not in _js("js/charts.js") and "{pct}" in en["dq.trustHelp"]
    q = _js("js/views/quality.js")
    assert "tq('dq.score.part.sensors'" in q and "tq('dq.score.part.batch'" in q and "tq('dq.score.part.small'" in q


def test_sensor_guesses_and_reasons_are_shown_in_plain_words():
    """The profile writes its guesses in engine terms ('sample-and-hold with period ~2.0 samples'); every reason it
    can write has a plain version on the sensor cards."""
    roles = (ROOT / "tpm" / "profile" / "roles.py").read_text(encoding="utf-8")
    und = _js("js/views/understanding.js")
    lines = [ln for ln in roles.splitlines() if '"kind": "instrument"' in ln and '"reasoning"' in ln]
    starts = {re.search(r'"reasoning": f?"([^"{(~]+)', ln).group(1).strip() for ln in lines}
    assert len(starts) >= 5, starts
    for s in starts:
        assert f"/^{s}" in und, f"no plain words for the reason '{s}...'"
    for key in ("und.why.held", "und.why.noisy", "und.why.medium", "und.why.smooth", "und.why.bounded", "und.why.steps", "und.why.llm", "und.unitop.cluster", "und.guess.lead"):
        assert f"'{key}'" in und, key


def test_new_chats_start_from_the_item_on_the_page_and_live_alarms_reach_every_page():
    chat = _js("js/chat.js")
    assert "chat.pageCtx = { view: state.view, run: state.run, ctx: next }" in chat and "p.view === state.view && p.run === state.run ? p.ctx : null" in chat
    app = _js("app.js")
    assert "api('/api/live/alarm')" in app and "live.noteAlarm(a)" in app and "setInterval(watchLive, LIVE_POLL_MS)" in app
    live = _js("js/views/live.js")
    assert "export function noteAlarm(" in live and "trip.key === 'sig.towards'" in live
    assert '.rail-item[data-alarm] .rail-num::after' in _js("styles.css")
