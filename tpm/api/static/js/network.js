/* "How the sensors interact" (Understanding page): the sensor network as an SVG a person can work with.
   - drag a sensor: it moves and its lines, arrows and numbers follow; positions are kept per run (browser storage)
     until "Reset positions";
   - drag the empty background, or scroll (two fingers on a trackpad, the mouse wheel; Shift + wheel sideways): the
     view moves; pinch, Ctrl + scroll or the + / - buttons zoom around the pointer; "Fit" shows every sensor again;
   - click a sensor: onClick(id); click a line or its number: onEdgeClick(edge); keyboard: Tab to a sensor or a line,
     Enter opens it; arrow keys move the view, + / - zoom, 0 fits.
   Pointer positions are mapped through the element's size on screen, so the page's text-size zoom (CSS zoom on the
   body) never shifts what is under the pointer. Dots and text keep their size while zooming; only distances change. */
import { el, store } from './core.js';
import { networkLayout, sensorKindColor, SENSOR_KINDS, htmlLegend } from './charts.js';

const NS = 'http://www.w3.org/2000/svg';
const R = 9;               // dot radius (px)
const CLICK_PX = 4;        // a press that moves less than this is a click, not a drag
const MIN_K = 0.25, MAX_K = 8;
let uid = 0;

function s(tag, attrs = {}, ...kids) {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null && v !== false) n.setAttribute(k, String(v));
  for (const c of kids.flat()) if (c !== null && c !== undefined && c !== false) n.append(c);
  return n;
}
/** Push apart sensors that sit on top of each other (dense groups), then scale back into [-1, 1]. Deterministic. */
export function declutter(pos, { dmin = 0.21, rounds = 120 } = {}) {
  const ids = Object.keys(pos);
  const p = Object.fromEntries(ids.map((k) => [k, { x: pos[k].x, y: pos[k].y }]));
  for (let r = 0; r < rounds; r++) {
    let moved = false;
    for (let i = 0; i < ids.length; i++) for (let j = i + 1; j < ids.length; j++) {
      const a = p[ids[i]], b = p[ids[j]];
      let dx = b.x - a.x, dy = b.y - a.y; let d = Math.hypot(dx, dy);
      if (d >= dmin) continue;
      if (d < 1e-6) { dx = Math.cos(i + j); dy = Math.sin(i + j); d = 1; }   // same spot: a fixed direction per pair
      const push = (dmin - Math.min(d, dmin)) / 2 * 0.9;
      a.x -= dx / d * push; a.y -= dy / d * push; b.x += dx / d * push; b.y += dy / d * push; moved = true;
    }
    if (!moved) break;
  }
  const m = Math.max(1e-9, ...ids.map((k) => Math.max(Math.abs(p[k].x), Math.abs(p[k].y))));
  if (m > 1) for (const k of ids) { p[k].x /= m; p[k].y /= m; }
  return p;
}
const plainText = (html) => String(html || '').replace(/<br\s*\/?>/gi, '\n').replace(/<[^>]+>/g, '');
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

/** nodes: [{id, label, kind, cluster, hover}]; edges from strongestEdges() ({a, b, r, lag, ...}; lag > 0: a moves
    first, b follows). Options: height, onClick(id), onEdgeClick(edge), edgeTitle(edge) -> text, kindLabel(kind),
    labels {zoomIn, zoomOut, fit, reset, chart}, storageKey (keeps dragged positions). Returns {shown, select, fit}. */
export function drawNetwork(node, nodes, edges, { height = 520, onClick, onEdgeClick, edgeTitle, kindLabel = (kd) => kd, labels = {}, storageKey } = {}) {
  const id = ++uid;
  const linked = new Set(edges.flatMap((e) => [e.a, e.b]));
  const shown = nodes.filter((nd) => linked.has(nd.id));
  const ids = new Set(shown.map((nd) => nd.id));
  edges = edges.filter((e) => ids.has(e.a) && ids.has(e.b));
  const base = declutter(networkLayout(shown, edges.map((e) => ({ a: e.a, b: e.b, w: Math.abs(e.r) }))));  // layout units, y up
  let saved = {};
  try { saved = (storageKey && store.get(storageKey, null)) || {}; } catch { saved = {}; }
  const pos = {};
  const place0 = (nd) => { const v = saved[nd.id]; return Array.isArray(v) && Number.isFinite(+v[0]) && Number.isFinite(+v[1]) ? { x: +v[0], y: +v[1] } : { x: base[nd.id].x, y: base[nd.id].y }; };
  for (const nd of shown) pos[nd.id] = place0(nd);

  // ---- DOM: a box with the SVG and a small toolbar
  const btn = (text, title, onClick_) => el('button', { class: 'btn btn-sm net-btn', type: 'button', title, 'aria-label': title, onClick: onClick_ }, text);
  const tools = el('div', { class: 'net-tools' });
  const svg = s('svg', { class: 'net-svg', tabindex: 0, role: 'group', 'aria-label': labels.chart || 'sensor network', focusable: 'true' });
  const wrap = el('div', { class: 'net-wrap', style: { height: `${height}px` } }, svg, tools);
  node.replaceChildren(wrap);
  const mk = (name, fill) => s('marker', { id: `net${id}-${name}`, viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 9, markerHeight: 9, markerUnits: 'userSpaceOnUse', orient: 'auto-start-reverse' },
    s('path', { d: 'M0,0 L10,5 L0,10 z', style: `fill:${fill}` }));
  svg.append(s('defs', {}, mk('arrow', 'var(--ink-2)'), mk('arrow-neg', 'var(--fail)'), mk('arrow-sel', 'var(--accent)')));
  const gEdges = s('g', { class: 'net-edges' });
  const gNodes = s('g', { class: 'net-nodes' });
  const gLabels = s('g', { class: 'net-labels' });   // the numbers on top of the sensors: always visible and clickable
  svg.append(gEdges, gNodes, gLabels);

  // ---- edges: an invisible wide line takes the clicks; the drawn line; a number (+k readings) on lagged ones
  const E = edges.map((e, i) => {
    const first = e.lag < 0 ? e.b : e.a, follow = e.lag < 0 ? e.a : e.b;
    const neg = Number(e.r) < 0;
    const w = 1 + 5 * Math.max(0, Math.abs(Number(e.r)) - 0.4);
    const hit = s('line', { class: 'net-hit' });
    const line = s('line', { class: 'net-line' + (neg ? ' neg' : ''), style: `stroke-width:${w.toFixed(2)}px`, 'marker-end': e.lag ? `url(#net${id}-${neg ? 'arrow-neg' : 'arrow'})` : null });
    const tip = edgeTitle ? edgeTitle(e) : `${e.a} - ${e.b}: r ${Number(e.r).toFixed(2)}`;
    let lbl = null, lblText = null, lblBox = null;
    if (e.lag) {
      lblText = s('text', { class: 'net-num', 'text-anchor': 'middle', 'dominant-baseline': 'central' }, `+${Math.abs(e.lag)}`);
      lblBox = s('rect', { class: 'net-num-box', rx: 7, ry: 7, height: 15 });
      lbl = s('g', { class: 'net-lbl', 'data-i': i }, s('title', {}, ''), lblBox, lblText);
    }
    const g = s('g', { class: 'net-edge', tabindex: 0, role: 'button', 'aria-label': tip, 'data-i': i }, s('title', {}, tip), hit, line);
    gEdges.append(g);
    if (lbl) { lbl.querySelector('title').textContent = tip; gLabels.append(lbl); }
    return { e, i, g, hit, line, lbl, lblText, lblBox, first, follow, neg };
  });

  // ---- nodes: a coloured dot with the sensor's name; the hover text as a tooltip
  const N = {};
  for (const nd of shown) {
    const c = s('circle', { r: R, class: 'net-dot', style: `fill:${sensorKindColor(nd.kind)}` });
    const tx = s('text', { class: 'net-name', 'text-anchor': 'middle', y: -(R + 5) }, nd.label || nd.id);
    const g = s('g', { class: 'net-node', tabindex: 0, role: 'button', 'aria-label': plainText(nd.hover || nd.label || nd.id).replace(/\n/g, ', '), 'data-id': nd.id }, s('title', {}, plainText(nd.hover || nd.id)), c, tx);
    gNodes.append(g);
    N[nd.id] = g;
  }

  // ---- view: layout units -> SVG px (the viewBox is the element's own size, so 1 unit = 1 CSS px of the layout)
  let W = 600, H = height, k = 1, px = 0, py = 0;
  const unit = () => Math.min(W, H) / 2 * 0.86;
  const toPx = (p) => ({ x: W / 2 + p.x * unit() * k + px, y: H / 2 - p.y * unit() * k + py });
  const fromDelta = (dx, dy) => ({ x: dx / (unit() * k), y: -dy / (unit() * k) });
  /** client (pointer) coordinates -> SVG px: through the size on screen, whatever the page zoom is */
  const clientToSvg = (cx, cy) => { const r = svg.getBoundingClientRect(); return { x: (cx - r.left) * (W / (r.width || W)), y: (cy - r.top) * (H / (r.height || H)) }; };
  const ratio = () => { const r = svg.getBoundingClientRect(); return W / (r.width || W); };
  let selected = -1;

  function placeEdge(o) {
    const p = toPx(pos[o.first]), q = toPx(pos[o.follow]);
    const dx = q.x - p.x, dy = q.y - p.y, d = Math.hypot(dx, dy) || 1;
    const ux = dx / d, uy = dy / d;
    const x1 = p.x + ux * (R + 2), y1 = p.y + uy * (R + 2);
    const x2 = q.x - ux * (R + (o.e.lag ? 3 : 2)), y2 = q.y - uy * (R + (o.e.lag ? 3 : 2));
    for (const ln of [o.hit, o.line]) { ln.setAttribute('x1', x1.toFixed(1)); ln.setAttribute('y1', y1.toFixed(1)); ln.setAttribute('x2', x2.toFixed(1)); ln.setAttribute('y2', y2.toFixed(1)); }
    if (o.lbl) {
      const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
      const tw = 9 + 6.5 * String(o.lblText.textContent).length;
      o.lblBox.setAttribute('x', (mx - tw / 2).toFixed(1)); o.lblBox.setAttribute('y', (my - 7.5).toFixed(1)); o.lblBox.setAttribute('width', tw.toFixed(1));
      o.lblText.setAttribute('x', mx.toFixed(1)); o.lblText.setAttribute('y', my.toFixed(1));
    }
  }
  function placeNode(nid) { const p = toPx(pos[nid]); N[nid].setAttribute('transform', `translate(${p.x.toFixed(1)},${p.y.toFixed(1)})`); }
  const byStrength = E.filter((o) => o.lbl).sort((x, y) => Math.abs(Number(y.e.r)) - Math.abs(Number(x.e.r)));
  function declutterNumbers() {
    const kept = [];
    for (const o of byStrength) {
      const x = +o.lblBox.getAttribute('x'), y = +o.lblBox.getAttribute('y'), w = +o.lblBox.getAttribute('width'), h = 15;
      const hit = o.g.classList.contains('sel') ? false : kept.some((b) => x < b.x + b.w + 2 && x + w + 2 > b.x && y < b.y + h + 1 && y + h + 1 > b.y);
      o.lbl.style.display = hit ? 'none' : '';
      if (!hit) kept.push({ x, y, w });
    }
  }
  function place() { for (const nid of Object.keys(N)) placeNode(nid); for (const o of E) placeEdge(o); declutterNumbers(); }
  function fit() {
    const pts = Object.values(pos);
    if (!pts.length) return;
    const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
    const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
    const spanX = Math.max(0.05, maxX - minX), spanY = Math.max(0.05, maxY - minY);
    // room for the names above the dots and the toolbar
    k = clamp(Math.min((W - 70) / (spanX * unit()), (H - 70) / (spanY * unit())), MIN_K, MAX_K);
    const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
    px = -cx * unit() * k; py = cy * unit() * k + 6;
    place();
  }
  function zoomAt(f, sx, sy) {
    const k2 = clamp(k * f, MIN_K, MAX_K); if (k2 === k) return;
    // keep the point under (sx, sy) where it is
    const lx = (sx - W / 2 - px) / (unit() * k), ly = (sy - H / 2 - py) / (unit() * k);
    k = k2; px = sx - W / 2 - lx * unit() * k; py = sy - H / 2 - ly * unit() * k;
    place();
  }
  const pan = (dx, dy) => { px += dx; py += dy; place(); };
  function resize() {
    const w = wrap.clientWidth || 600, h = wrap.clientHeight || height;
    if (w === W && h === H) return false;
    // keep the centre of the view where it was
    const cxL = (-px) / (unit() * k), cyL = py / (unit() * k);
    W = w; H = h; svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    px = -cxL * unit() * k; py = cyL * unit() * k;
    place();
    return true;
  }
  const save = () => {
    if (!storageKey) return;
    const out = {};
    for (const nd of shown) if (Math.abs(pos[nd.id].x - base[nd.id].x) > 1e-6 || Math.abs(pos[nd.id].y - base[nd.id].y) > 1e-6) out[nd.id] = [+pos[nd.id].x.toFixed(4), +pos[nd.id].y.toFixed(4)];
    try { store.set(storageKey, Object.keys(out).length ? out : null); } catch { /* private mode */ }
    resetBtn.disabled = !Object.keys(out).length;
  };
  function select(i) {
    try { return select_(i); } finally { declutterNumbers(); }
  }
  function select_(i) {
    if (selected >= 0 && E[selected]) { E[selected].g.classList.remove('sel'); if (E[selected].lbl) E[selected].lbl.classList.remove('sel'); if (E[selected].e.lag) E[selected].line.setAttribute('marker-end', `url(#net${id}-${E[selected].neg ? 'arrow-neg' : 'arrow'})`); }
    selected = typeof i === 'number' ? i : -1;
    if (selected >= 0 && E[selected]) { E[selected].g.classList.add('sel'); if (E[selected].lbl) { E[selected].lbl.classList.add('sel'); gLabels.append(E[selected].lbl); } if (E[selected].e.lag) E[selected].line.setAttribute('marker-end', `url(#net${id}-arrow-sel)`); gEdges.append(E[selected].g); }
  }

  // ---- toolbar
  const resetBtn = btn('↺', labels.reset || 'Reset positions', () => { for (const nd of shown) pos[nd.id] = { x: base[nd.id].x, y: base[nd.id].y }; saved = {}; save(); fit(); });
  resetBtn.disabled = !Object.keys(saved).length;
  tools.append(btn('+', labels.zoomIn || 'Zoom in', () => zoomAt(1.3, W / 2, H / 2)), btn('−', labels.zoomOut || 'Zoom out', () => zoomAt(1 / 1.3, W / 2, H / 2)), btn('⤢', labels.fit || 'Fit', fit), resetBtn);

  // ---- pointer: drag a sensor, pan on the background, click a sensor or a line, pinch with two fingers
  const pointers = new Map();
  let drag = null;   // {mode: 'node' | 'edge' | 'pan' | 'pinch', id, i, x0, y0, moved, last, dist}
  svg.addEventListener('pointerdown', (ev) => {
    if (ev.button !== 0 && ev.pointerType === 'mouse') return;
    pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
    try { svg.setPointerCapture(ev.pointerId); } catch { /* ignore */ }
    if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      drag = { mode: 'pinch', dist: Math.hypot(a.x - b.x, a.y - b.y), moved: true };
      return;
    }
    const nodeEl = ev.target.closest && ev.target.closest('.net-node');
    const edgeEl = ev.target.closest && (ev.target.closest('.net-lbl') || (!nodeEl && ev.target.closest('.net-edge')));
    drag = { mode: edgeEl && edgeEl.classList.contains('net-lbl') ? 'edge' : nodeEl ? 'node' : edgeEl ? 'edge' : 'pan', id: nodeEl ? nodeEl.dataset.id : null, i: edgeEl ? Number(edgeEl.dataset.i) : -1, x0: ev.clientX, y0: ev.clientY, last: { x: ev.clientX, y: ev.clientY }, moved: false };
    svg.classList.add(drag.mode === 'node' ? 'dragging-node' : 'dragging');
    ev.preventDefault();
  });
  svg.addEventListener('pointermove', (ev) => {
    if (!pointers.has(ev.pointerId)) return;
    pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
    if (!drag) return;
    if (drag.mode === 'pinch' && pointers.size >= 2) {
      const [a, b] = [...pointers.values()];
      const d = Math.hypot(a.x - b.x, a.y - b.y);
      const m = clientToSvg((a.x + b.x) / 2, (a.y + b.y) / 2);
      if (drag.dist > 0) zoomAt(d / drag.dist, m.x, m.y);
      drag.dist = d;
      return;
    }
    if (!drag.moved && Math.hypot(ev.clientX - drag.x0, ev.clientY - drag.y0) < CLICK_PX) return;
    drag.moved = true;
    const rr = ratio();
    const dx = (ev.clientX - drag.last.x) * rr, dy = (ev.clientY - drag.last.y) * rr;
    drag.last = { x: ev.clientX, y: ev.clientY };
    if (drag.mode === 'node') {
      const d = fromDelta(dx, dy);
      pos[drag.id] = { x: pos[drag.id].x + d.x, y: pos[drag.id].y + d.y };
      placeNode(drag.id);
      for (const o of E) if (o.first === drag.id || o.follow === drag.id) placeEdge(o);
      declutterNumbers();
    } else pan(dx, dy);   // the background, or a line dragged instead of clicked
  });
  const end = (ev) => {
    pointers.delete(ev.pointerId);
    if (!drag) return;
    const d = drag; drag = null;
    svg.classList.remove('dragging', 'dragging-node');
    if (d.mode === 'pinch') return;
    if (!d.moved && ev.type === 'pointerup') {
      if (d.mode === 'node' && onClick) onClick(d.id);
      else if (d.mode === 'edge' && E[d.i]) { select(d.i); if (onEdgeClick) onEdgeClick(E[d.i].e); }
    } else if (d.mode === 'node') save();
  };
  svg.addEventListener('pointerup', end);
  svg.addEventListener('pointercancel', end);
  // wheel: two-finger scroll / mouse wheel moves the view; pinch (Ctrl + wheel in the browser) zooms at the pointer
  svg.addEventListener('wheel', (ev) => {
    ev.preventDefault();
    const unitPx = ev.deltaMode === 1 ? 16 : ev.deltaMode === 2 ? H : 1;
    const rr = ratio();
    if (ev.ctrlKey || ev.metaKey) {
      const p = clientToSvg(ev.clientX, ev.clientY);
      zoomAt(Math.exp(-ev.deltaY * unitPx * 0.0025), p.x, p.y);
    } else if (ev.shiftKey && !ev.deltaX) pan(-ev.deltaY * unitPx * rr, 0);
    else pan(-ev.deltaX * unitPx * rr, -ev.deltaY * unitPx * rr);
  }, { passive: false });
  // keyboard
  svg.addEventListener('keydown', (ev) => {
    const t = ev.target;
    if ((ev.key === 'Enter' || ev.key === ' ') && t && t.classList) {
      if (t.classList.contains('net-node') && onClick) { ev.preventDefault(); onClick(t.dataset.id); return; }
      if (t.classList.contains('net-edge')) { ev.preventDefault(); const i = Number(t.dataset.i); select(i); if (onEdgeClick) onEdgeClick(E[i].e); return; }
    }
    const step = 40;
    const keys = { ArrowLeft: [step, 0], ArrowRight: [-step, 0], ArrowUp: [0, step], ArrowDown: [0, -step] };
    if (keys[ev.key]) { ev.preventDefault(); pan(...keys[ev.key]); return; }
    if (ev.key === '+' || ev.key === '=') { ev.preventDefault(); zoomAt(1.3, W / 2, H / 2); }
    else if (ev.key === '-' || ev.key === '_') { ev.preventDefault(); zoomAt(1 / 1.3, W / 2, H / 2); }
    else if (ev.key === '0') { ev.preventDefault(); fit(); }
  });

  // ---- size: follow the box (window size, text size); fit once when first shown
  let fitted = false;
  const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(() => {
    if (!wrap.isConnected) { ro.disconnect(); return; }
    const changed = resize();
    if (!fitted && W > 0) { fitted = true; fit(); } else if (changed) place();
  }) : null;
  if (ro) ro.observe(wrap);
  W = wrap.clientWidth || 600; H = wrap.clientHeight || height;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  fit(); fitted = W > 0;

  // colours only: the counts of each kind are on the kind filter of the sensor cards
  htmlLegend(node, SENSOR_KINDS.filter((x) => shown.some((nd) => nd.kind === x)).map((kd) => ({ label: kindLabel(kd), color: sensorKindColor(kd), round: true })));
  return { shown: shown.length, fit, select: (edge) => select(edge ? E.findIndex((o) => o.e === edge) : -1) };
}
