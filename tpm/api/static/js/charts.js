/* Plotly wrapper themed from the CSS tokens, plus inline-SVG sparklines. Plotly is served from the
   Python package at /static/vendor/plotly.min.js; when it is missing, charts degrade to a notice. */
import { el, state, t as t0, runApi, fmt, navigate, bus, closeAllModals, refLink, cleanText, linkifyRefs, modal, decisionBar, refTypeOfId } from './core.js';
import { runAction, hiddenInBasic, firstSentences } from './brief.js';

export function tokens() {
  const cs = getComputedStyle(document.documentElement);
  const g = (n) => cs.getPropertyValue(n).trim();
  return { bg: g('--bg-2'), ink: g('--ink'), ink2: g('--ink-2'), ink3: g('--ink-3'), line: g('--line'), ok: g('--ok'), warn: g('--warn'), fail: g('--fail'), info: g('--info'), accent: g('--accent'), font: g('--font') };
}
// categorical palette for signals: chosen to keep distinct hue steps in both themes
export const PALETTE = ['#4cc0b2', '#e6a83c', '#7aa7e6', '#e6604f', '#a884d8', '#7fbf5a', '#d47fb0', '#5fb8d8', '#c9a15a', '#8ea0b0', '#5f8f6a', '#c26c8a'];
export const colorFor = (i) => PALETTE[i % PALETTE.length];

export function baseLayout(extra = {}) {
  const k = tokens();
  return Object.assign({
    paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
    font: { family: k.font, color: k.ink2, size: 12 },
    margin: { l: 48, r: 16, t: 28, b: 40 },
    xaxis: { gridcolor: k.line, zerolinecolor: k.line, linecolor: k.line, tickcolor: k.line, automargin: true },
    yaxis: { gridcolor: k.line, zerolinecolor: k.line, linecolor: k.line, tickcolor: k.line, automargin: true },
    legend: { orientation: 'h', y: 1.12, x: 0, font: { size: 11.5 } },
    hoverlabel: { bgcolor: k.bg, bordercolor: k.line, font: { color: k.ink, family: k.font } },
    hovermode: 'x unified',
  }, extra);
}
export const CONFIG = { displaylogo: false, responsive: true, modeBarButtonsToRemove: ['lasso2d', 'select2d', 'toImage'], displayModeBar: 'hover' };

/* Text size: a chart is drawn outside the CSS zoom of the page (.tpm-plot in styles.css), because Plotly's hover and
   click do not know about zoom. What Plotly measures in pixels (fonts, marker sizes, margins, height) is scaled by the
   text size here instead, and every chart is drawn again when the text size changes. */
const DATA_KEYS = new Set(['x', 'y', 'z', 'text', 'customdata', 'hovertext', 'labels', 'values', 'ids', 'meta', 'parents']);
function zoomed(obj, z, key = '') {
  if (Array.isArray(obj)) return obj.length && obj[0] && typeof obj[0] === 'object' && !Array.isArray(obj[0]) ? obj.map((v) => zoomed(v, z, key)) : obj;
  if (!obj || typeof obj !== 'object' || obj instanceof Date || ArrayBuffer.isView(obj)) return obj;   // typed arrays are data
  const out = {};
  for (const [k, v] of Object.entries(obj)) {
    if (DATA_KEYS.has(k)) out[k] = v;
    else if (k === 'size' && (/font$/i.test(key) || key === 'marker')) out[k] = typeof v === 'number' ? v * z : Array.isArray(v) ? v.map((s) => (typeof s === 'number' ? s * z : s)) : v;
    else out[k] = zoomed(v, z, k);
  }
  return out;
}
function zoomLayout(lay, z) {
  const out = zoomed(lay, z);
  for (const k of ['height', 'width']) if (typeof out[k] === 'number') out[k] = Math.round(out[k] * z);
  if (out.margin) for (const s of ['l', 'r', 't', 'b', 'pad']) if (typeof out.margin[s] === 'number') out.margin[s] = Math.round(out.margin[s] * z);
  if (out.uniformtext && typeof out.uniformtext.minsize === 'number') out.uniformtext.minsize *= z;
  out.hoverlabel = Object.assign({}, out.hoverlabel, { font: Object.assign({ size: 13 * z }, (out.hoverlabel || {}).font) });  // Plotly's default is 13, not the layout font
  return out;
}
const drawn = new Set();                                   // charts on screen, drawn again when the text size changes
bus.on('zoom.changed', () => { for (const node of [...drawn]) { if (!node.isConnected || !node._plotArgs) { drawn.delete(node); continue; } try { plot(node, ...node._plotArgs); } catch { /* ignore */ } } });
/** Width of a chart's box in page pixels (the size the text is laid out in), whatever the text size. */
export function boxWidth(node) { return (node.getBoundingClientRect().width || 0) / (Number(state.zoom) || 1); }

export function plot(node, traces, layout = {}, config = {}) {
  if (!window.Plotly) { node.replaceChildren(el('div', { class: 'notice warn', text: 'Plotly is not loaded (vendor/plotly.min.js missing).' })); return null; }
  node._plotArgs = [traces, layout, config];
  node.classList.add('tpm-plot');
  drawn.add(node);
  let lay = baseLayout(layout);
  // merge axis defaults with caller's axis settings
  for (const ax of ['xaxis', 'yaxis', 'yaxis2']) if (layout[ax]) lay[ax] = Object.assign({}, baseLayout()[ax] || baseLayout().yaxis, layout[ax]);
  const z = Number(state.zoom) || 1;
  if (z !== 1) { lay = zoomLayout(lay, z); traces = traces.map((tr) => zoomed(tr, z)); }
  // Plotly follows the window only: a box that changes width by itself (a second box added next to it, the chat drawer
  // opening, the text size) is watched here, so the chart is redrawn to its box instead of spilling out or staying small
  if (!node._ro && window.ResizeObserver) {
    let last = node.clientWidth;
    node._ro = new ResizeObserver(() => { const w = node.clientWidth; if (w && Math.abs(w - last) > 2 && node.isConnected) { last = w; try { window.Plotly.Plots.resize(node); } catch { /* not drawn yet */ } } });
    node._ro.observe(node);
  }
  return window.Plotly.react(node, traces, lay, Object.assign({}, CONFIG, config));
}
export function purge(node) {
  if (node) { drawn.delete(node); node._plotArgs = null; }
  if (node && node._ro) { node._ro.disconnect(); node._ro = null; }
  if (window.Plotly && node) try { window.Plotly.purge(node); } catch { /* ignore */ }
}

/** Inline SVG sparkline; values may contain nulls. */
export function sparkline(values, { w = 120, h = 28, color, fill = true, threshold } = {}) {
  const k = tokens();
  const vals = values.map((v) => (v === null || v === undefined ? NaN : Number(v)));
  const finite = vals.filter((v) => !isNaN(v));
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`); svg.setAttribute('width', w); svg.setAttribute('height', h); svg.classList.add('spark');
  if (!finite.length) return svg;
  let min = Math.min(...finite), max = Math.max(...finite);
  if (threshold !== undefined && threshold !== null) { min = Math.min(min, threshold); max = Math.max(max, threshold); }
  if (max === min) { max = min + 1; }
  const x = (i) => (i / Math.max(1, vals.length - 1)) * (w - 2) + 1;
  const y = (v) => h - 2 - ((v - min) / (max - min)) * (h - 4);
  let d = ''; let started = false;
  vals.forEach((v, i) => { if (isNaN(v)) { started = false; return; } d += (started ? ' L' : ' M') + x(i).toFixed(1) + ' ' + y(v).toFixed(1); started = true; });
  if (fill) {
    const area = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    area.setAttribute('d', d + ` L${x(vals.length - 1).toFixed(1)} ${h - 1} L1 ${h - 1} Z`);
    area.setAttribute('fill', color || k.accent); area.setAttribute('opacity', '0.15'); svg.append(area);
  }
  if (threshold !== undefined && threshold !== null) {
    const th = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    th.setAttribute('x1', 1); th.setAttribute('x2', w - 1); th.setAttribute('y1', y(threshold)); th.setAttribute('y2', y(threshold)); th.setAttribute('stroke', k.fail); th.setAttribute('stroke-dasharray', '3 2'); th.setAttribute('stroke-width', '1'); svg.append(th);
  }
  const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  p.setAttribute('d', d); p.setAttribute('fill', 'none'); p.setAttribute('stroke', color || k.accent); p.setAttribute('stroke-width', '1.4'); p.setAttribute('stroke-linejoin', 'round');
  svg.append(p);
  return svg;
}

/* ================================================================================================================
   Round 5 (additive): "Problem -> Reason -> Answer" strips, labelled diagrams (donut, bars, timeline, sensor
   network) and the texts they need. English fallbacks live here so a page never shows a raw key while the i18n
   files are edited by several people at once.
   ================================================================================================================ */
export const VFB = {
  'adv.problem': "Problem", 'adv.reason': "Reason", 'adv.answer': "Answer",
  'adv.use.yes': "These rows can be used", 'adv.use.partly': "Usable, except the rows set aside", 'adv.use.no': "Do not rely on these rows",
  'adv.loading': "Looking up the answer", 'adv.showRows': "Show these rows", 'adv.more': "Show {n} more", 'adv.none': "No advice is stored for this item.",
  'adv.top': "Start here: the most important problems", 'adv.topHelp': "Each strip reads left to right: what is wrong, why, and what to do about it.",
  'adv.moreBasic': "{n} more are listed in Operator mode.",
  'dq.viz.title': "Where the faulty data is", 'dq.viz.help': "Top: how far each batch can be trusted (green = trusted, amber = usable with care, red = not trusted). Bottom: which kind of check found a problem in which batch (grey = nothing found, amber = smaller problem, red = failed). Hover for the reason, click a batch to open it.",
  'dq.viz.score': "Trust (%)", 'dq.viz.capped': "Showing the {n} worst of {total} batches.", 'dq.viz.cell.pass': "passed", 'dq.viz.cell.warn': "smaller problem", 'dq.viz.cell.fail': "failed",
  'dq.faulty.title': "The faulty data, worst first", 'dq.faulty.none': "No faulty data was found: every check passed.", 'dq.faulty.more': "{n} further problems are listed under “Show technical analyses”.",
  'dq.faulty.batch': "Batch {id}", 'dq.faulty.wholeBatch': "the whole batch",
  'dq.checked.title': "What was checked", 'dq.checked.help': "Every batch went through the same checks. The bars show how often each check passed, found a smaller problem, or failed.",
  'dq.checked.batch': "Checks run on batch {id}", 'dq.checked.total': "{n} checks were run on {b} batches.",
  'dq.score.title': "What the % score means", 'dq.score.means': "{pct} is a trust grade, not a share of lost data: 100 % means every check passed.",
  'dq.score.full': "100 %: every check passed on this batch.",
  'dq.score.rest.trusted': "Rows marked unreliable are set aside; the rest can be used.", 'dq.score.rest.untrusted': "So much is affected that findings from this batch should not be relied on.",
  'dq.score.legend.ok': "Green: trusted, every check passed", 'dq.score.legend.warn': "Amber: usable, some rows or sensors are set aside", 'dq.score.legend.fail': "Red: below {pct}, the batch is not trusted",
  'dq.score.how': "The score is a grade, not the share of data that is lost. It starts at 100 % (every check passed) and drops most when sensors cannot be used (up to 70 points when many of them fail), less for problems of the whole batch such as gaps, duplicate rows or time going backwards (up to 30 points), and a little for smaller warnings (up to 10 points).",
  'dq.group.completeness': "Is anything missing?", 'dq.group.validity': "Are the values possible?", 'dq.group.consistency': "Do the values agree?", 'dq.group.timeliness': "Is the timing right?", 'dq.group.rule': "Your own rules",
  'diag.viz.cause': "Findings by likely cause", 'diag.viz.group': "Groups with the most findings", 'diag.viz.timeline': "When the findings happened", 'diag.viz.timelineHelp': "Each dot is one finding: further right = later in the data, higher = more serious, colour = likely cause. Click a dot to open it.",
  'diag.viz.row': "row in the data", 'diag.viz.serious': "how serious (%)", 'diag.viz.findings': "findings", 'diag.cause.process': "Process changed", 'diag.cause.sensor': "Faulty sensor", 'diag.cause.data': "Data problem", 'diag.cause.mixed': "More than one cause", 'diag.cause.unknown': "Not explained",
  'diag.table.title': "All findings", 'diag.table.what': "What", 'diag.table.where': "Where",
  'mon.viz.timeline': "When and where unusual behaviour was found", 'mon.viz.timelineHelp': "Each mark is one event: further right = later in the data, bigger = more serious, colour = kind of event. Click a mark to open it.",
  'mon.viz.perSignal': "Sensors involved in the most events", 'mon.viz.events': "events", 'mon.viz.group': "group", 'mon.top': "The strongest events",
  'und.types.title': "What each sensor probably measures", 'und.types.help': "The system guesses the kind of instrument from how the values behave, not from the names. Accept a guess or correct it; your answer is recorded.",
  'und.types.confidence': "How sure", 'und.types.unitop': "Part of the plant", 'und.types.why': "Why", 'und.types.all': "All kinds", 'und.types.noGuess': "no guess yet", 'und.types.showAll': "Show all {n} sensors", 'und.types.details': "Details",
  'und.net.title': "How the sensors interact", 'und.net.help': "Each dot is a sensor, coloured by what it probably measures. A line joins two sensors that move together: thicker = stronger, red = they move in opposite directions. An arrow points from the sensor that moves first to the one that follows; the number on it (+3) says how many readings later it follows. Click a dot for details.",
  'und.net.capped': "Showing the {n} strongest of {total} connections.", 'und.net.none': "No strong connections between sensors were found.", 'und.net.lag': "follows {n} readings later", 'und.net.together': "move together", 'und.net.opposite': "move in opposite directions",
  'und.kind.flow': "Flow", 'und.kind.pressure': "Pressure / level", 'und.kind.temperature': "Temperature", 'und.kind.analyzer': "Analyser / composition", 'und.kind.valve': "Valve / controller output", 'und.kind.power': "Power / speed", 'und.kind.other': "Other", 'und.kind.unknown': "Unknown",
  'und.unsure.problem': "The system is not sure what {name} measures.", 'und.unsure.reason': "Its best guess is “{guess}”, but it is only {pct} sure.", 'und.unsure.fix1': "If you know what it measures, name it; the name is used everywhere from then on.", 'und.unsure.fix2': "Accept or correct the guess so later explanations use the right words.", 'und.unsure.name': "Name this sensor",
};
/** Text of a round-5 key: the loaded dictionary first, the English fallback above otherwise. */
export function vt(key, vars) {
  let v = t0(key, vars);
  if (!v || v === key) { v = VFB[key] || key; for (const [a, b] of Object.entries(vars || {})) v = v.replaceAll(`{${a}}`, String(b)); }
  return v;
}

export const CAUSE_ORDER = ['process', 'sensor', 'data', 'mixed', 'unknown'];
export function causeColors() { const k = tokens(); return { process: k.warn, sensor: k.info, data: '#8e7cc3', mixed: k.fail, unknown: k.ink3 }; }
export function kindColors() { const k = tokens(); return { anomaly: k.fail, point: '#d47fb0', drift: k.warn, changepoint: k.info, dq: '#8e7cc3', rule: '#5fb8d8', cascade: '#c26c8a' }; }

/** A titled box around a diagram: heading, one plain sentence on how to read it, the chart node. */
export function vizBox(title, help, ...children) {
  return el('section', { class: 'viz-box' }, el('h2', { class: 'viz-title', text: title }), help ? el('p', { class: 'viz-help', text: help }) : null, children);
}
export function chartNode(cls = '') { return el('div', { class: 'chart viz-chart ' + cls }); }

// ---------------------------------------------------------------- Problem -> Reason -> Answer
const USE_CLS = { yes: 'ok', partly: 'warn', no: 'fail' };
/** One strip. problem / reason: string | Node | array; fix: [string]; use: yes|partly|no; extra: node(s) under the steps.
    compact (Basic mode): the problem, ONE sentence of reason and the first two things to do; no ids, no second-level
    detail (pass `reason` as text so it can be cut). */
export function praStrip({ verdict = 'attention', problem, where, reason, reasonMore, fix = [], use, extra, label, compact = false } = {}) {
  if (compact) { label = null; reasonMore = null; fix = (fix || []).slice(0, 2); if (typeof reason === 'string') reason = firstSentences(reason, 1); }
  const col = (cls, n, head, ...kids) => el('div', { class: 'pra-col ' + cls }, el('div', { class: 'pra-head' }, el('span', { class: 'pra-num', 'aria-hidden': 'true', text: n }), head), kids);
  return el('div', { class: 'pra' + (compact ? ' compact' : ''), dataset: { verdict } },
    label ? el('div', { class: 'pra-label small muted' }, label) : null,
    el('div', { class: 'pra-cols' },
      col('pra-problem', '1', vt('adv.problem'), el('p', { class: 'pra-main' }, problem), where ? el('div', { class: 'pra-where small' }, where) : null),
      el('div', { class: 'pra-arrow', 'aria-hidden': 'true', text: '→' }),
      col('pra-reason', '2', vt('adv.reason'), el('p', { class: 'pra-text' }, reason || '–'), reasonMore ? el('div', { class: 'small muted pra-more' }, reasonMore) : null),
      el('div', { class: 'pra-arrow', 'aria-hidden': 'true', text: '→' }),
      col('pra-answer', '3', vt('adv.answer'), (fix || []).length ? el('ol', { class: 'pra-fix' }, fix.map((s) => el('li', { text: s }))) : el('p', { class: 'small muted', text: vt('adv.none') }),
        use ? el('div', { class: 'pra-use' }, el('span', { class: 'chip ' + (USE_CLS[use] || ''), text: vt('adv.use.' + use) })) : null, extra ? el('div', { class: 'pra-extra' }, extra) : null)));
}
const briefKey = (id) => `${state.run}:brief-item:${state.lang}:${id}`;   // shared with brief.js (cleared on a decision)
export async function fetchItemBrief(id) {
  let d = state.cache.get(briefKey(id));
  if (!d) { const r = await runApi('/brief/item', { params: { id, lang: state.lang || 'en' } }); if (r.ok && r.data && r.data.headline) { d = r.data; state.cache.set(briefKey(id), d); } }
  return d || null;
}
/** Strip of one object of the run (DIAG- / FLAG- / CHK- / batch id): summary + why + fix from GET /brief/item.
    opts: { label, where (node), extra (node), fallback: { problem, reason }, onLoad, actions (true: the object's next
    steps as buttons), ctx (chat context of those buttons), compact (Basic mode, see praStrip: the "Where: group ...,
    rows ..." line and the ask buttons that name the object's id are left out too) } */
export function praForItem(id, opts = {}) {
  const host = el('div', { class: 'pra-host', dataset: { praId: id } }, el('div', { class: 'dim small', text: vt('adv.loading') + '…' }));
  const compact = !!opts.compact;
  (async () => {
    const d = await fetchItemBrief(id);
    const fb = opts.fallback || {};
    if (!d) { host.replaceChildren(praStrip({ problem: fb.problem || id, reason: fb.reason, where: opts.where, extra: opts.extra, label: opts.label, compact })); return; }
    const pts = d.points || [];
    const wherePt = pts.find((p) => /^(Where|Missä|Var)\b/.test(p));
    const rest = pts.filter((p) => p !== wherePt);
    const extra = [opts.actions ? briefActionButtons(d.actions, opts.ctx, { skipRef: id, noAsk: compact }) : null, opts.extra || null].filter(Boolean);
    host.replaceChildren(praStrip({ verdict: d.verdict, problem: d.headline, where: [wherePt && !compact ? el('div', { text: wherePt }) : null, opts.where || null], reason: (compact && d.because) || d.why || fb.reason || rest[0],
      reasonMore: (d.why ? rest : rest.slice(1)).map((p) => el('div', { text: p })), fix: d.fix || [], use: d.can_use_rows, extra: extra.length ? extra : null, label: opts.label, compact }));
    if (opts.onLoad) opts.onLoad(d);
  })();
  return host;
}
/** Basic mode: one object (finding, alarm, check, batch) as problem -> reason -> answer in a popup, with accept /
    question / override for a finding or an alarm, and its next steps. */
export function basicItemModal(id, { ctx } = {}) {
  const type = refTypeOfId(id);
  const decide = type === 'diagnosis' || type === 'flag' ? type : null;
  const askCtx = ctx || { object_type: type, object_id: id, title: id };
  const typeName = t0('ref.' + type) === 'ref.' + type ? type : t0('ref.' + type);
  const bar = decide ? el('div', { class: 'brief-decide' }, decisionBar(decide, id, { askContext: askCtx })) : null;
  modal({ title: `${typeName} ${id}`, body: el('div', { class: 'basic-item' }, praForItem(id, { extra: bar, actions: true, ctx: askCtx })), wide: true });
}
/** Basic mode shows ONE strip per page: how many more there are, and that Operator mode lists them. */
export function basicMore(n) {
  return n > 0 ? el('p', { class: 'small muted basic-more', text: vt('adv.moreBasic', { n: fmt.int(n) }) }) : null;
}
/** A capped list with "Show N more". make(i) builds item i lazily. */
export function cappedList(n, cap, make, { cls = 'pra-list' } = {}) {
  const box = el('div', { class: cls });
  let shown = 0;
  const more = el('button', { class: 'btn btn-quiet btn-sm', type: 'button' });
  const grow = (k) => { more.remove(); const end = Math.min(n, shown + k); for (let i = shown; i < end; i++) box.append(make(i)); shown = end; if (shown < n) { more.textContent = vt('adv.more', { n: Math.min(cap, n - shown) }); box.append(more); } };
  more.addEventListener('click', () => grow(cap));
  grow(cap);
  return box;
}

// ---------------------------------------------------------------- simple labelled diagrams
/** Ring chart: the share of each part inside the ring; the names with their counts in an HTML legend under it, which
    wraps on narrow boxes (labels outside the ring were cut off, and overlapped for small parts). */
/** The legend of a chart as HTML right under it: it wraps on a narrow box, where Plotly's own legend grew over the plot.
    items [{label, color, count?, share?, round?}] */
export function htmlLegend(node, items) {
  const old = node.nextElementSibling;
  if (old && old.classList.contains('viz-legend')) old.remove();
  if (!items || !items.length) return null;
  const legend = el('ul', { class: 'viz-legend' }, items.map((it) => el('li', {}, el('span', { class: 'viz-sw' + (it.round ? ' round' : ''), style: { background: it.color }, 'aria-hidden': 'true' }), it.label,
    it.count !== undefined ? [': ', el('b', { text: fmt.int(it.count) })] : null, it.share !== undefined ? el('span', { class: 'dim', text: ` (${fmt.pct(it.share)})` }) : null)));
  node.after(legend);
  return legend;
}
export function donut(node, labels, values, colors, { height = 260, center } = {}) {
  const k = tokens();
  const total = values.reduce((a, b) => a + b, 0);
  htmlLegend(node, labels.map((l, i) => ({ label: l, color: colors[i], count: values[i], share: total ? values[i] / total : 0 })));
  return plot(node, [{ type: 'pie', hole: 0.56, labels, values, sort: false, direction: 'clockwise', marker: { colors, line: { color: k.bg, width: 2 } }, textinfo: 'percent', textposition: 'inside', insidetextorientation: 'horizontal', insidetextfont: { color: '#fff', size: 12 }, hovertemplate: '%{label}: %{value} (%{percent})<extra></extra>' }],
    { height, margin: { l: 10, r: 10, t: 10, b: 10 }, showlegend: false, uniformtext: { mode: 'hide', minsize: 10 }, annotations: [{ text: `<b>${fmt.int(total)}</b><br>${center || ''}`, showarrow: false, font: { size: 15, color: k.ink } }] });
}
/** Many events: counted per stretch of the data and stacked by series (kind of event, likely cause), so thousands of
    marks stay readable. items [{x, series}]; order: series order; colors / legend: {series: colour / label}.
    onClick(rowFrom, rowTo) of the clicked stretch. */
export function binnedTimeline(node, items, { bins = 60, order, colors = {}, legend = {}, height = 300, xtitle, ytitle, onClick } = {}) {
  const xs = items.map((i) => Number(i.x)).filter((x) => isFinite(x));
  if (!xs.length) return null;
  let lo = xs[0]; let hi = xs[0];
  for (const x of xs) { if (x < lo) lo = x; if (x > hi) hi = x; }
  if (hi <= lo) hi = lo + 1;
  const n = Math.max(5, Math.min(bins, Math.ceil(items.length / 5)));
  const w = (hi - lo) / n;
  const series = (order || []).filter((s) => items.some((i) => i.series === s)).concat([...new Set(items.map((i) => i.series))].filter((s) => !(order || []).includes(s)));
  const counts = Object.fromEntries(series.map((s) => [s, new Array(n).fill(0)]));
  for (const it of items) { const x = Number(it.x); if (!isFinite(x)) continue; counts[it.series][Math.min(n - 1, Math.max(0, Math.floor((x - lo) / w)))]++; }
  const span = (b) => [Math.round(lo + b * w), Math.round(lo + (b + 1) * w)];
  const mids = Array.from({ length: n }, (_, b) => lo + (b + 0.5) * w);
  const traces = series.map((s) => ({ type: 'bar', name: legend[s] || s, x: mids, y: counts[s], width: w * 0.94, marker: { color: colors[s] }, customdata: mids.map((_, b) => span(b)),
    hovertemplate: `${String(legend[s] || s).replace(/[<>]/g, '')}: %{y}<br>%{customdata[0]:,}–%{customdata[1]:,}<extra></extra>` }));
  htmlLegend(node, series.length > 1 ? series.map((s) => ({ label: legend[s] || s, color: colors[s], count: counts[s].reduce((a, b) => a + b, 0) })) : []);
  const p = plot(node, traces, { height, barmode: 'stack', bargap: 0.04, hovermode: 'closest', showlegend: false, margin: { l: 56, r: 12, t: 12, b: 44 },
    xaxis: { title: { text: xtitle || '' }, tickformat: 'd' }, yaxis: { title: { text: ytitle || '' }, rangemode: 'tozero' } });
  if (onClick && p && p.then) p.then(() => { if (node.on) node.on('plotly_click', (ev) => { const pt = ev.points && ev.points[0]; if (pt && pt.customdata) onClick(pt.customdata[0], pt.customdata[1]); }); });
  return p;
}
export function hbar(node, labels, values, { colors, height, onClick, xtitle, text } = {}) {
  const k = tokens();
  const p = plot(node, [{ type: 'bar', orientation: 'h', y: labels, x: values, marker: { color: colors || k.accent }, text: text || values.map((v) => fmt.int(v)), textposition: 'auto', hovertemplate: '%{y}: %{x}<extra></extra>', cliponaxis: false }],
    { height: height || Math.max(180, 60 + 24 * labels.length), margin: { l: 8, r: 24, t: 8, b: xtitle ? 40 : 24 }, yaxis: { autorange: 'reversed', automargin: true, type: 'category', tickfont: { size: 12 } }, xaxis: { title: xtitle ? { text: xtitle } : undefined, rangemode: 'tozero' }, showlegend: false, hovermode: 'closest' });
  if (onClick && p && p.then) p.then(() => { if (node.on) node.on('plotly_click', (ev) => { const pt = ev.points && ev.points[0]; if (pt) onClick(pt.y, pt.pointNumber); }); });
  return p;
}
/** Events on a line: items [{x, y, series, color, size, text, id}]; click -> onClick(id). */
export function timeline(node, items, { height = 300, xtitle, ytitle, yCategories, legend, onClick, opacity = 0.85 } = {}) {
  const k = tokens();
  const groups = new Map();
  for (const it of items) { const g = it.series || ''; if (!groups.has(g)) groups.set(g, []); groups.get(g).push(it); }
  const traces = [...groups.entries()].map(([name, arr]) => ({ type: arr.length > 1500 ? 'scattergl' : 'scatter', mode: 'markers', name: (legend && legend[name]) || name, x: arr.map((i) => i.x), y: arr.map((i) => i.y), customdata: arr.map((i) => i.id), text: arr.map((i) => i.text || ''),
    marker: { color: arr[0].color, size: arr.map((i) => i.size || 9), opacity, line: { width: opacity < 0.6 ? 0 : 1, color: k.bg } }, hovertemplate: '%{text}<extra></extra>' }));
  htmlLegend(node, groups.size > 1 ? [...groups.entries()].map(([name, arr]) => ({ label: (legend && legend[name]) || name, color: arr[0].color, count: arr.length, round: true })) : []);
  const p = plot(node, traces, { height, margin: { l: 56, r: 12, t: 12, b: 44 }, hovermode: 'closest', showlegend: false, xaxis: { title: { text: xtitle || '' }, tickformat: 'd' },
    yaxis: Object.assign({ title: { text: ytitle || '' }, automargin: true }, yCategories ? { type: 'category', categoryorder: 'array', categoryarray: yCategories } : { rangemode: 'tozero' }) });
  if (onClick && p && p.then) p.then(() => { if (node.on) node.on('plotly_click', (ev) => { const pt = ev.points && ev.points[0]; if (pt && pt.customdata !== undefined) onClick(pt.customdata); }); });
  return p;
}

// ---------------------------------------------------------------- sensor kinds + network
export const SENSOR_KINDS = ['flow', 'pressure', 'temperature', 'analyzer', 'valve', 'power', 'other', 'unknown'];
const KIND_COLOR = { flow: '#4c8fd6', pressure: '#e6a83c', temperature: '#e6604f', analyzer: '#a884d8', valve: '#7fbf5a', power: '#d47fb0', other: '#5fb8d8', unknown: '#8ea0b0' };
export const sensorKindColor = (kind) => KIND_COLOR[kind] || KIND_COLOR.unknown;
/** 'flow-like (fast, noisy)' -> 'flow'. Works on the hypothesis text or a name somebody gave. */
export function sensorKind(text) {
  const s = String(text || '').toLowerCase();
  if (!s || s === 'unknown' || s === '–') return 'unknown';
  if (/flow|virtaus|flöde/.test(s)) return 'flow';
  if (/press|level|paine|pinta|tryck|nivå/.test(s)) return 'pressure';
  if (/temp|lämpö/.test(s)) return 'temperature';
  if (/analy|compos|concentr|quality/.test(s)) return 'analyzer';
  if (/valve|controller|actuator|setpoint|position|venttiili|ventil/.test(s)) return 'valve';
  if (/power|speed|current|motor|teho|effekt/.test(s)) return 'power';
  return 'other';
}
/** The kind of a sensor record: a name somebody gave wins over the guess from the values. */
export function kindOfSignal(s) {
  if (!s) return 'unknown';
  const byName = s.display_name ? sensorKind(s.display_name) : 'other';
  return byName !== 'other' && byName !== 'unknown' ? byName : sensorKind(s.instrument_hypothesis);
}
/** Legend of sensor bars coloured by what each sensor probably measures (hbar(..., { colors: kinds.map(sensorKindColor) })). */
export function sensorKindLegend(node, kinds) {
  return htmlLegend(node, SENSOR_KINDS.filter((kd) => kinds.includes(kd)).map((kd) => ({ label: vt('und.kind.' + kd), color: sensorKindColor(kd) })));
}
/** Deterministic spring layout (no randomness): nodes start on a circle ordered by cluster, linked nodes attract.
    nodes: [{id, cluster}], edges: [{a, b, w}] -> { id: {x, y} } within [-1, 1]. */
export function networkLayout(nodes, edges, { iterations = 220 } = {}) {
  const n = nodes.length; const pos = {};
  if (!n) return pos;
  const order = nodes.slice().sort((p, q) => String(p.cluster || '~').localeCompare(String(q.cluster || '~')) || String(p.id).localeCompare(String(q.id)));
  order.forEach((nd, i) => { const a = (2 * Math.PI * i) / n; pos[nd.id] = { x: Math.cos(a), y: Math.sin(a) }; });
  const kk = 1.6 / Math.sqrt(n); let temp = 0.12;
  const E = edges.filter((e) => pos[e.a] && pos[e.b] && e.a !== e.b);
  for (let it = 0; it < iterations; it++) {
    const disp = {}; for (const nd of nodes) disp[nd.id] = { x: 0, y: 0 };
    for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
      const p = pos[nodes[i].id], q = pos[nodes[j].id]; let dx = p.x - q.x, dy = p.y - q.y; let d = Math.hypot(dx, dy);
      if (d < 1e-4) { dx = 1e-3 * (i + 1); dy = 1e-3 * (j + 1); d = Math.hypot(dx, dy); }
      const f = (kk * kk) / d; disp[nodes[i].id].x += (dx / d) * f; disp[nodes[i].id].y += (dy / d) * f; disp[nodes[j].id].x -= (dx / d) * f; disp[nodes[j].id].y -= (dy / d) * f;
    }
    for (const e of E) { const p = pos[e.a], q = pos[e.b]; const dx = p.x - q.x, dy = p.y - q.y; const d = Math.hypot(dx, dy) || 1e-4; const f = ((d * d) / kk) * (0.4 + 0.6 * Math.min(1, e.w || 0.5)); disp[e.a].x -= (dx / d) * f; disp[e.a].y -= (dy / d) * f; disp[e.b].x += (dx / d) * f; disp[e.b].y += (dy / d) * f; }
    for (const nd of nodes) { const p = pos[nd.id], d = disp[nd.id]; const m = Math.hypot(d.x, d.y) || 1e-9; p.x += (d.x / m) * Math.min(m, temp) - p.x * 0.02; p.y += (d.y / m) * Math.min(m, temp) - p.y * 0.02; }
    temp = Math.max(0.004, temp * 0.985);
  }
  let mx = 1e-9; for (const nd of nodes) mx = Math.max(mx, Math.abs(pos[nd.id].x), Math.abs(pos[nd.id].y));
  for (const nd of nodes) { pos[nd.id].x /= mx; pos[nd.id].y /= mx; }
  return pos;
}
/** Strongest pairs first, one per pair of sensors, capped: [{a, b, r, lag}]. */
export function strongestEdges(pairs, { cap = 60, minR = 0.5 } = {}) {
  const seen = new Set();
  return (pairs || []).filter((p) => p && p.a && p.b && p.a !== p.b && isFinite(Number(p.r)) && Math.abs(Number(p.r)) >= minR)
    .sort((x, y) => Math.abs(y.r) - Math.abs(x.r)).filter((p) => { const key = [p.a, p.b].sort().join('|'); if (seen.has(key)) return false; seen.add(key); return true; }).slice(0, cap);
}
/** nodes: [{id, label, kind, cluster, hover}], edges from strongestEdges(); lag > 0 means a moves first, b follows. */
export function drawNetwork(node, nodes, edges, { height = 520, onClick, kindLabel = (kd) => kd } = {}) {
  const k = tokens();
  const linked = new Set(edges.flatMap((e) => [e.a, e.b]));
  const shown = nodes.filter((nd) => linked.has(nd.id));
  const ids = new Set(shown.map((nd) => nd.id));
  edges = edges.filter((e) => ids.has(e.a) && ids.has(e.b));
  const pos = networkLayout(shown, edges.map((e) => ({ a: e.a, b: e.b, w: Math.abs(e.r) })));
  const traces = [];
  for (const e of edges) {
    const p = pos[e.a], q = pos[e.b];
    traces.push({ type: 'scatter', mode: 'lines', x: [p.x, q.x], y: [p.y, q.y], line: { width: 1 + 5 * Math.max(0, Math.abs(e.r) - 0.4), color: e.r < 0 ? k.fail : k.ink3 }, opacity: 0.55, hoverinfo: 'skip', showlegend: false });
  }
  // hover target in the middle of each line
  traces.push({ type: 'scatter', mode: 'markers', x: edges.map((e) => (pos[e.a].x + pos[e.b].x) / 2), y: edges.map((e) => (pos[e.a].y + pos[e.b].y) / 2), marker: { size: 10, color: 'rgba(0,0,0,0)' }, showlegend: false,
    text: edges.map((e) => `${e.a} ↔ ${e.b}: ${e.r < 0 ? vt('und.net.opposite') : vt('und.net.together')} (r ${Number(e.r).toFixed(2)})${e.lag ? '<br>' + (e.lag > 0 ? e.b : e.a) + ' ' + vt('und.net.lag', { n: Math.abs(e.lag) }) : ''}`), hovertemplate: '%{text}<extra></extra>' });
  for (const kd of SENSOR_KINDS.filter((x) => shown.some((nd) => nd.kind === x))) {
    const arr = shown.filter((nd) => nd.kind === kd);
    traces.push({ type: 'scatter', mode: 'markers+text', name: kindLabel(kd), x: arr.map((nd) => pos[nd.id].x), y: arr.map((nd) => pos[nd.id].y), text: arr.map((nd) => nd.label || nd.id), textposition: 'top center', textfont: { size: 10.5, color: k.ink2 }, customdata: arr.map((nd) => nd.id),
      hovertext: arr.map((nd) => nd.hover || nd.id), hovertemplate: '%{hovertext}<extra></extra>', marker: { size: 15, color: sensorKindColor(kd), line: { width: 1.5, color: k.bg } } });
  }
  const annotations = edges.filter((e) => e.lag).map((e) => {
    const from = e.lag > 0 ? pos[e.a] : pos[e.b], to = e.lag > 0 ? pos[e.b] : pos[e.a];
    return { x: from.x + (to.x - from.x) * 0.82, y: from.y + (to.y - from.y) * 0.82, ax: from.x + (to.x - from.x) * 0.6, ay: from.y + (to.y - from.y) * 0.6, xref: 'x', yref: 'y', axref: 'x', ayref: 'y', showarrow: true, arrowhead: 2, arrowsize: 1.4, arrowwidth: 1.4, arrowcolor: k.ink2, text: `+${Math.abs(e.lag)}`, font: { size: 9.5, color: k.ink2 }, opacity: 0.9 };
  });
  const ax = { visible: false, range: [-1.18, 1.18] };
  // colours only: the counts of each kind are on the kind filter of the sensor cards (the network leaves out sensors without a strong link)
  htmlLegend(node, SENSOR_KINDS.filter((x) => shown.some((nd) => nd.kind === x)).map((kd) => ({ label: kindLabel(kd), color: sensorKindColor(kd), round: true })));
  const p = plot(node, traces, { height, margin: { l: 8, r: 8, t: 8, b: 8 }, hovermode: 'closest', showlegend: false, xaxis: ax, yaxis: ax, annotations });
  if (onClick && p && p.then) p.then(() => { if (node.on) node.on('plotly_click', (ev) => { const pt = ev.points && ev.points.find((x) => typeof x.customdata === 'string'); if (pt) onClick(pt.customdata); }); });
  return { shown: shown.length, promise: p };
}

/* ---------------------------------------------------------------- A2 additions (quality + diagnoses): the batches x checks
   grid, stacked bars and the next-step buttons of an item brief. Built on the block above; nothing above changed. */
export function statusColors() { const k = tokens(); return { pass: k.ok, warn: k.warn, fail: k.fail, none: k.line }; }
/** Batches x kinds of checks in ONE figure: trust bars on top (coloured by verdict), below the grid of what each kind
    of check found in each batch (z: 0 nothing, 1 warning, 2 failed; `counts` are written into the cells of small grids).
    onClick(batchId, typeLabel | null). */
export function trustGrid(node, { batches, scores, verdicts, types, z, hover, counts, height, onClick } = {}) {
  const k = tokens(); const sc = statusColors();
  const nT = Math.max(1, types.length);
  // a narrow box (a phone, large text): short names for the kinds of checks (the hover keeps the full ones) and
  // turned batch names, or the names take the whole width
  const narrow = (boxWidth(node) || 800) < 560;
  const seen = new Set();
  types = types.map((ty) => { let s = narrow && String(ty).length > 16 ? String(ty).slice(0, 15) + '…' : String(ty); while (seen.has(s)) s += ' '; seen.add(s); return s; });
  const gridShare = Math.min(0.66, 0.16 + 0.045 * nT);
  const traces = [
    { type: 'bar', x: batches, y: scores.map((s) => Math.round(Number(s) * 100)), marker: { color: verdicts.map((v) => (v === 'untrusted' ? k.fail : v === 'caution' ? k.warn : k.ok)) }, hovertemplate: '%{x}: %{y} %<extra></extra>', customdata: batches, showlegend: false },
    { type: 'heatmap', x: batches, y: types, z, text: hover, hoverinfo: 'text', xgap: 1.5, ygap: 1.5, zmin: 0, zmax: 2, showscale: false, colorscale: [[0, sc.none], [0.33, sc.none], [0.34, sc.warn], [0.66, sc.warn], [0.67, sc.fail], [1, sc.fail]], yaxis: 'y2', xaxis: 'x' },
  ];
  const annotations = [];
  if (counts && batches.length <= 30 && types.length <= 14) types.forEach((ty, i) => batches.forEach((b, j) => { const c = counts[i][j]; if (c) annotations.push({ x: b, y: ty, xref: 'x', yref: 'y2', text: String(c), showarrow: false, font: { size: 10.5, color: '#fff' } }); }));
  const h = height || Math.min(560, Math.max(280, 150 + 24 * nT));
  const p = plot(node, traces, { height: h, margin: { l: 8, r: 10, t: 8, b: 44 }, showlegend: false, hovermode: 'closest', bargap: 0.15, annotations,
    // the batch names go under the grid (anchored to its axis), not between the bars and the grid
    xaxis: { type: 'category', anchor: 'y2', tickangle: batches.length > 14 || narrow ? -60 : 0, tickfont: { size: 10.5 }, automargin: true, showgrid: false, nticks: batches.length > 40 ? 40 : undefined },
    yaxis: { domain: [gridShare + 0.06, 1], range: [0, 105], title: { text: vt('dq.viz.score') }, tickfont: { size: 10.5 }, automargin: true, fixedrange: true },
    yaxis2: { domain: [0, gridShare], type: 'category', automargin: true, tickfont: { size: 11 }, showgrid: false, fixedrange: true } });
  if (onClick && p && p.then) p.then(() => { if (node.on) node.on('plotly_click', (ev) => { const pt = ev.points && ev.points[0]; if (pt) onClick(String(pt.x), pt.data && pt.data.type === 'heatmap' ? String(pt.y) : null); }); });
  return p;
}
/** Stacked horizontal bars: series = [{name, values, color}], one bar per label; the numbers are written inside. */
export function stackedBar(node, labels, series, { height, xtitle, onClick } = {}) {
  const traces = series.map((s) => ({ type: 'bar', orientation: 'h', name: s.name, y: labels, x: s.values, marker: { color: s.color }, text: s.values.map((v) => (v ? fmt.int(v) : '')), textposition: 'inside', insidetextanchor: 'middle', textfont: { color: '#fff', size: 11 }, hovertemplate: `%{y}: %{x} ${s.name}<extra></extra>` }));
  htmlLegend(node, series.map((s) => ({ label: s.name, color: s.color })));
  const p = plot(node, traces, { height: height || Math.max(160, 70 + 24 * labels.length), barmode: 'stack', margin: { l: 8, r: 16, t: 8, b: xtitle ? 40 : 24 }, hovermode: 'closest', showlegend: false, yaxis: { autorange: 'reversed', automargin: true, type: 'category', tickfont: { size: 12 } }, xaxis: { title: xtitle ? { text: xtitle } : undefined, rangemode: 'tozero' } });
  if (onClick && p && p.then) p.then(() => { if (node.on) node.on('plotly_click', (ev) => { const pt = ev.points && ev.points[0]; if (pt) onClick(pt.y); }); });
  return p;
}
/** The next-step buttons of an item brief ("Check X on site", "Ask ..."); `skipRef` drops the button that only points
    at the object itself, `noAsk` the chat questions (a Basic-mode strip: they name the object's id). */
export function briefActionButtons(actions, ctx, { skipRef, noAsk = false } = {}) {
  // the same behaviour as the next steps of a summary card (js/brief.js), Basic mode included
  const acts = (actions || []).filter((a) => a && a.text && !(skipRef && a.ref === skipRef && !a.ask) && !hiddenInBasic(a) && !(noAsk && a.ask && !a.view));
  if (!acts.length) return null;
  return el('div', { class: 'pra-btns' }, acts.map((a) => { const ask = !!(a.ask && !a.view); return el('button', { class: 'btn btn-sm' + (ask ? ' ask' : ''), type: 'button', onClick: () => runAction(a, ctx) }, el('span', { 'aria-hidden': 'true', text: ask ? '? ' : '→ ' }), a.text); }));
}
// keep the shared imports referenced (linkifyRefs / cleanText / refLink are used by the views through core.js)
export const _r5 = { refLink, cleanText, linkifyRefs };
