"""Agent E: view logic that can be checked without a browser.

* pages unlock with their pipeline stage (``viewAccess`` of core.js, run under node);
* the Runs list, the trust-by-batch rows, the suspicious-rows list and the ``point`` flag kind are wired.

    .venv\\Scripts\\python.exe -m pytest tests/test_e_ui_views.py -q
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "tpm" / "api" / "static"

HARNESS = r"""
import { viewAccess, lockText, VIEW_STAGES, fmt } from './core.mjs';
const mk = (states, extra = {}) => Object.assign({ run_id: 'r1', state: 'running', stages: ['ingest', 'profile', 'quality', 'detect', 'diagnose', 'assess', 'report'].map((s, i) => Object.assign({ stage: s, state: 'pending', progress: 0, message: '' }, states[s] || {})) }, extra);
const views = ['runs', 'understanding', 'quality', 'monitor', 'diagnoses', 'assessor', 'log', 'dataflow', 'report'];
const open = (st, run = 'r1') => views.filter((v) => viewAccess(v, st, run).open);
const done = { state: 'done', progress: 1 };
const out = {};
out.noRun = open(null, null);
out.noStages = open({ run_id: 'old', state: 'done', stages: [] });
out.fresh = open(mk({}));
const running = mk({ ingest: done, profile: done, quality: { state: 'running', progress: 0.42, message: 'checking batch 5 of 12' } });
out.running = open(running);
out.monitorWhileQualityRuns = viewAccess('monitor', running, 'r1');
out.qualityWhileRunning = viewAccess('quality', running, 'r1');
out.qualityText = lockText(out.qualityWhileRunning);
out.afterDiagnose = open(mk({ ingest: done, profile: done, quality: done, detect: done, diagnose: done, assess: { state: 'running', progress: 0.1 } }));
out.finished = open(mk({ ingest: done, profile: done, quality: done, detect: done, diagnose: done, assess: done, report: done }, { state: 'done' }));
const failed = mk({ ingest: done, profile: done, quality: done, detect: { state: 'failed', message: 'boom', error: 'Traceback: ValueError: boom' }, diagnose: { state: 'skipped', message: 'previous stage failed' }, assess: { state: 'skipped' }, report: { state: 'skipped' } }, { state: 'failed' });
out.failed = open(failed);
out.failedMonitor = viewAccess('monitor', failed, 'r1');
out.failedDiagnoses = viewAccess('diagnoses', failed, 'r1');
const diedOutside = mk({ ingest: done }, { state: 'failed', error: 'MemoryError: out of memory' });
out.diedOutside = viewAccess('understanding', diedOutside, 'r1');
const skipped = mk({ ingest: done, profile: done, quality: done, detect: done, diagnose: done, assess: { state: 'skipped', message: 'not implemented yet' }, report: done }, { state: 'done' });
out.skipped = open(skipped);
out.skippedAssessor = viewAccess('assessor', skipped, 'r1');
out.viewStages = VIEW_STAGES;
out.sizes = [fmt.bytes(6 * 1024 ** 3), fmt.bytes(30 * 1024 ** 2), fmt.bytes(512), fmt.dur(42), fmt.dur(185), fmt.dur(4320)];
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def access(tmp_path_factory):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    d = tmp_path_factory.mktemp("core")
    (d / "core.mjs").write_text((STATIC / "js" / "core.js").read_text(encoding="utf-8"), encoding="utf-8")
    (d / "harness.mjs").write_text(HARNESS, encoding="utf-8")
    r = subprocess.run([node, str(d / "harness.mjs")], capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_pages_unlock_with_their_stage(access):
    assert access["noRun"] == ["runs", "dataflow"], "without a run only Runs and Data flow have something to show"
    assert access["noStages"] == ["runs", "understanding", "quality", "monitor", "diagnoses", "assessor", "log", "dataflow", "report"], "a run without a stage record is never blocked"
    assert access["fresh"] == ["runs", "log", "dataflow"], "decision log and data flow are open as soon as a run is selected"
    assert access["running"] == ["runs", "understanding", "log", "dataflow"]
    assert access["afterDiagnose"] == ["runs", "understanding", "quality", "monitor", "diagnoses", "log", "dataflow", "report"], "the report page opens once diagnoses exist"
    assert len(access["finished"]) == 9
    assert access["viewStages"]["report"] == ["report", "diagnose"]


def test_locked_page_names_stage_and_progress(access):
    q = access["qualityWhileRunning"]
    assert q["open"] is False and q["reason"] == "waiting" and q["stage"] == "quality" and q["stageState"] == "running" and abs(q["progress"] - 0.42) < 1e-9
    assert "lock.waits" in access["qualityText"] or "42" in access["qualityText"]
    m = access["monitorWhileQualityRuns"]
    assert m["stage"] == "detect" and m["stageState"] == "pending" and m["running"]["stage"] == "quality" and m["running"]["message"] == "checking batch 5 of 12"
    assert 0.5 < m["overall"] < 0.7, "2 stages done + 42 % of the third, of the 4 on the way to Monitor"


def test_failed_and_skipped_runs(access):
    assert access["failed"] == ["runs", "understanding", "quality", "log", "dataflow"], "a failed run keeps what completed"
    fm = access["failedMonitor"]
    assert fm["reason"] == "failed" and fm["failedStage"] == "detect" and "ValueError: boom" in fm["error"]
    fd = access["failedDiagnoses"]
    assert fd["reason"] == "failed" and fd["failedStage"] == "detect" and "boom" in fd["error"], "pages behind the failed stage show the same failure reason"
    assert access["diedOutside"]["reason"] == "failed" and "MemoryError" in access["diedOutside"]["error"], "status.error is used when no stage carries the failure"
    assert "assessor" not in access["skipped"] and access["skippedAssessor"]["reason"] == "skipped", "a skipped stage never opens its page"
    assert access["skippedAssessor"]["message"] == "not implemented yet"


def test_sizes_and_durations_for_big_uploads(access):
    assert access["sizes"] == ["6.00 GB", "30.00 MB", "512 B", "42 s", "3 min 05 s", "1 h 12 min"]


def _src(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_rail_and_router_use_the_lock():
    app = _src("app.js")
    assert "viewAccess(def.id)" in app and "renderLocked(main, def)" in app, "a locked view must render the friendly panel, not the empty view"
    assert "updateRailLocks" in app and "bus.on('status'" in app, "the rail follows the live status stream"
    assert "lockGlyph()" in app and "aria-disabled" in app
    assert "!['done', 'failed', 'skipped'].includes(sg.state)" in app, "a late progress event must not set a finished stage back to running"
    css = _src("styles.css")
    for cls in (".rail-item.locked", ".rail-item.unlocked", ".lockpanel", ".upload-screen", ".batchrow", ".grouplist", ".sus-wording"):
        assert cls in css, cls


def test_runs_list_shows_five_and_keeps_the_selected_run():
    src = _src("js/views/runs.js")
    assert "const RECENT_RUNS = 5;" in src
    assert "runs.showMore" in src and "runs.showFewer" in src and "runs.selectedOlder" in src
    assert "older.find((r) => r.run_id === state.run)" in src, "an older selected run stays visible"


def test_trust_rows_show_reasons_and_hide_groups():
    src = _src("js/views/quality.js")
    assert "local_untrusted" in src and "rowsLink(g.row_start, g.row_end" in src, "row-scoped problems link to the Monitor at those rows"
    assert "const LOCAL_TOP = 5;" in src and "dq.moreRanges" in src
    assert "dq.showGroups" in src and "if (open && !list.firstChild)" in src, "group numbers are put in the page only on request"
    assert "b.group_ids.join(', ')" not in src, "the batch line must not dump every group number"
    core = _src("js/core.js")
    assert "export function rowsLink" in core and "case 'rows':" in core and "(?<rowa>" in core


def test_monitor_suspicious_rows_and_point_flags():
    src = _src("js/views/monitor.js")
    assert "runApi('/suspicious'" in src and "sus.data.available" in src, "404 or available:false hides the section"
    assert "point_dominated" in src and "pointDominated ? susTop : susMid" in src, "the list comes before the timeline when points dominate"
    assert "params: { limit: SUS_PAGE, offset: page * SUS_PAGE }" in src, "server-side paging with limit/offset"
    assert "plotSignalsAround(node, sigs, r.row, end" in src and "sus.askWhy" in src
    assert "const KINDS = ['anomaly', 'point'," in src and "f.kind !== 'point' && f.pattern_id" in src
    assert "point: 'fail'" in _src("js/core.js")
    en = json.loads(_src("i18n/en.json"))
    for k in ("mon.kind.point", "sus.title", "sus.spread", "sus.src.detector", "sus.src.range_check", "sus.src.local_spike", "sus.pointLead", "lock.title.waiting", "lock.title.failed", "dq.showGroups", "dq.why.stuck", "runs.showMore"):
        assert k in en, k
    assert en["sus.pointLead"].startswith("Most deviations in this data are isolated single readings")
    assert en["dq.showGroups"] == "Show all group numbers ({n})" and en["runs.showMore"] == "Show more past runs ({n})"
