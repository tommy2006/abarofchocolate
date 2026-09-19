"""The interactive sensor network of the Understanding page (js/network.js): layout decluttering under Node, the wiring
of the page (drag, pan, zoom, click a line -> explanation) and the texts that explain the lines and numbers."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "tpm" / "api" / "static"


def _js(rel: str) -> str:
    return (STATIC / rel).read_text(encoding="utf-8")


def test_declutter_separates_stacked_sensors_and_stays_in_the_box(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    (tmp_path / "package.json").write_text('{"type": "module"}', encoding="utf-8")
    for name in ("core.js", "brief.js", "charts.js", "network.js"):
        (tmp_path / name).write_text(_js(f"js/{name}"), encoding="utf-8")
    (tmp_path / "h.js").write_text(
        "import { declutter } from './network.js';\n"
        "const pos = { a: { x: 0.5, y: 0.5 }, b: { x: 0.5, y: 0.5 }, c: { x: 0.52, y: 0.49 }, d: { x: -0.9, y: -0.9 }, e: { x: 0.99, y: 0.98 } };\n"
        "const p1 = declutter(pos), p2 = declutter(pos);\n"
        "const ids = Object.keys(p1); let dmin = 9;\n"
        "for (let i = 0; i < ids.length; i++) for (let j = i + 1; j < ids.length; j++) dmin = Math.min(dmin, Math.hypot(p1[ids[i]].x - p1[ids[j]].x, p1[ids[i]].y - p1[ids[j]].y));\n"
        "console.log(JSON.stringify({ dmin, same: JSON.stringify(p1) === JSON.stringify(p2), inBox: ids.every((k) => Math.abs(p1[k].x) <= 1 + 1e-9 && Math.abs(p1[k].y) <= 1 + 1e-9), untouched: pos.a.x === 0.5 && pos.b.x === 0.5 }));\n",
        encoding="utf-8")
    r = subprocess.run([node, str(tmp_path / "h.js")], capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["dmin"] >= 0.18, "stacked sensors are pushed apart"
    assert out["same"] and out["inBox"] and out["untouched"], "deterministic, inside the box, the input is not changed"


def test_the_page_wires_drag_pan_zoom_and_the_explanation():
    net = _js("js/network.js")
    for needle in ("addEventListener('pointerdown'", "setPointerCapture", "addEventListener('wheel'", "{ passive: false }", "ev.ctrlKey || ev.metaKey",
                   "pointers.size === 2", "store.set(storageKey", "getBoundingClientRect()", "ResizeObserver", "onEdgeClick(E[d.i].e)", "declutterNumbers()"):
        assert needle in net, needle
    und = _js("js/views/understanding.js")
    assert "import { drawNetwork } from '../network.js';" in und
    assert "drawNetwork(netNode, nodes, edges" in und
    for needle in ("onEdgeClick: explainRelation", "edgeTitle: edgeTip", "storageKey: `net.${state.run}`", "t('und.net.key.arrow')", "t('und.rel.use', vars)"):
        assert needle in und, needle
    assert "export function drawNetwork" not in _js("js/charts.js"), "one network implementation (js/network.js)"
    css = _js("styles-views.css")
    assert ".net-svg { display: block; width: 100%; height: 100%; touch-action: none;" in css


def test_every_explaining_text_exists_in_three_languages_with_the_same_placeholders():
    langs = {lang: json.loads(_js(f"i18n/{lang}.json")) for lang in ("en", "fi", "sv")}
    und = _js("js/views/understanding.js")
    used = set(re.findall(r"t\('(und\.(?:net|rel)\.[\w.]+)'", und)) | set(re.findall(r"'(und\.rel\.(?:word|method|m)\.)' \+", und))
    keys = sorted(k for k in langs["en"] if k.startswith(("und.net.", "und.rel.", "und.why.v")) or k == "und.why.valve")
    assert len(keys) >= 45
    for k in keys:
        ph = set(re.findall(r"\{(\w+)\}", langs["en"][k]))
        for lang in ("fi", "sv"):
            assert k in langs[lang], (lang, k)
            assert set(re.findall(r"\{(\w+)\}", langs[lang][k])) == ph, (lang, k)
    for k in used:
        if not k.endswith("."):
            assert k in langs["en"], k
