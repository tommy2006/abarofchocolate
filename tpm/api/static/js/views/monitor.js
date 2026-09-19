/* View 3: monitor — ONE headline list of suspicious rows (single readings that do not fit; first when the data
   is dominated by them), ensemble score over time with threshold and clickable flag markers, per-signal
   contribution stack, filters, flag detail with evidence + decisions + chat, detector details.
   ?flag=FLAG-000001 selects the flag's group, highlights it and opens its detail; ?group=3 filters;
   ?rows=312632-312999&signals=S09,S01 zooms on those rows and plots the signals there (row links of Data quality). */
import { state, t, el, clear, runApi, fmt, conf, sev, chip, kindChip, causeChip, section, table, viewHead, needRun, empty, evidenceButton, evidencePanel, decisionBar, hiddenHint, kv, roleAllows, bus, st, infStatus, unavailableNote, meter, linkifyRefs, cleanText, proseList, refLink, refChips, confWords, sevWords, timesThreshold, addPlainBox, flash, navigate, notice } from '../core.js';
import { plot, purge, tokens, colorFor } from '../charts.js';
import { openChat, flagContext, setChatContext } from '../chat.js';

const KINDS = ['anomaly', 'point', 'drift', 'changepoint', 'dq', 'rule', 'cascade'];
const DIRS = ['up', 'down', 'noisy', 'stuck', 'shifted'];
const SUS_PAGE = 20;
const SUS_SOURCES = { detector: 'fail', range_check: 'warn', local_spike: 'info' };
function dirWord(d) { return d && DIRS.includes(d) ? t('plain.dir.' + d) : (d || ''); }
function parseRows(s) { const m = /^\s*(\d+)(?:\s*-\s*(\d+))?\s*$/.exec(String(s || '')); if (!m) return null; const a = Number(m[1]); const b = m[2] === undefined ? a : Number(m[2]); return { a: Math.min(a, b), b: Math.max(a, b) }; }
/** The cause chip of a single odd reading says what it can be; "unknown" would undersell what is known. */
function flagCause(f) { return f.kind === 'point' ? chip(t('mon.cause.point'), '', { title: t('sus.wordingShort') }) : causeChip(f.likely_cause_class); }

/** Small multiples: one strip per signal around a row range, the range marked. Raw values, local only. */
async function plotSignalsAround(node, sigs, a, b, { sigIndex = {}, pad } = {}) {
  const k = tokens();
  pad = pad === undefined ? Math.max(40, Math.min(2000, (b - a + 1) * 2)) : pad;
  const r = await runApi('/series', { params: { signals: sigs.join(','), row_start: Math.max(0, a - pad), row_end: b + pad, max_points: 800 } });
  if (!r.ok || !r.data.available || !(r.data.rows || []).length) { node.replaceChildren(el('div', { class: 'dim small', text: (r.data && r.data.error) || t('common.notYet') })); return false; }
  const have = sigs.filter((s) => r.data.series[s]);
  if (!have.length) { node.replaceChildren(el('div', { class: 'dim small', text: t('common.notYet') })); return false; }
  const n = have.length; const gap = 0.06; const h = (1 - gap * (n - 1)) / n;
  const axisBase = { gridcolor: k.line, zerolinecolor: k.line, linecolor: k.line, tickcolor: k.line, automargin: true };
  const layout = { height: 70 + 120 * n, margin: { l: 56, r: 12, t: 8, b: 36 }, showlegend: false, hovermode: 'x unified', xaxis: { title: { text: t('mon.rows').toLowerCase() }, anchor: n > 1 ? 'y' + n : 'y', tickformat: 'd', hoverformat: 'd' },
    shapes: [{ type: 'rect', x0: a - 0.5, x1: b + 0.5, y0: 0, y1: 1, yref: 'paper', fillcolor: k.warn, opacity: 0.18, line: { width: 1, color: k.warn } }] };
  const traces = [];
  have.forEach((s, i) => {
    const ax = i === 0 ? 'y' : 'y' + (i + 1);
    const top = 1 - i * (h + gap);
    layout[i === 0 ? 'yaxis' : 'yaxis' + (i + 1)] = Object.assign({}, axisBase, { domain: [Math.max(0, top - h), top], anchor: 'x', title: { text: s, font: { size: 12 } } });
    const color = colorFor(sigIndex[s] !== undefined ? sigIndex[s] : i);
    const d = r.data.series[s];
    if (r.data.bucket > 1) {   // long ranges are averaged per bucket: the min-max band keeps single spikes visible
      traces.push({ type: 'scatter', mode: 'lines', x: r.data.rows, y: d.min, yaxis: ax, line: { width: 0 }, hoverinfo: 'skip', showlegend: false });
      traces.push({ type: 'scatter', mode: 'lines', x: r.data.rows, y: d.max, yaxis: ax, fill: 'tonexty', fillcolor: color + '33', line: { width: 0 }, hoverinfo: 'skip', showlegend: false });
    }
    traces.push({ type: 'scatter', mode: r.data.rows.length < 160 ? 'lines+markers' : 'lines', name: s, x: r.data.rows, y: d.mean, yaxis: ax, line: { width: 1.3, color }, marker: { size: 4, color }, hovertemplate: `${s}: %{y:.4g}<extra></extra>` });
  });
  await plot(node, traces, layout);
  return true;
}

export async function render(main, params = {}) {
  const view = el('div', { class: 'view' });
  main.append(view);
  view.append(viewHead('3', t('nav.monitor')));
  if (!state.run) { view.append(needRun()); return view; }
  await addPlainBox(view, 'monitor');
  const k = tokens();
  view._charts = [];
  const [fl, sig, sus] = await Promise.all([runApi('/flags', { params: { limit: 5000 } }), runApi('/signals'), runApi('/suspicious', { params: { limit: SUS_PAGE, offset: 0 } })]);
  const allFlags = fl.ok ? fl.data.items || [] : [];
  const signals = sig.ok ? sig.data.signals || [] : [];
  const sigIndex = Object.fromEntries(signals.map((s, i) => [s.id, i]));
  const groups = fl.ok ? fl.data.groups || [] : [];
  let selected = null;
  const wanted = params.flag ? allFlags.find((x) => x.id === params.flag) : null;
  const focus = wanted ? null : parseRows(params.rows);
  const focusPad = focus ? Math.max(200, (focus.b - focus.a + 1) * 2) : 0;
  // the suspicious-rows list is optional: an older server (404) or a run without it ({available:false}) hides the section
  const susData = sus.ok && sus.data && sus.data.available ? sus.data : null;
  const pointDominated = !!(susData && susData.regime && susData.regime.point_dominated);
  const susTop = el('div'); const susMid = el('div'); const focusHost = el('div');
  if (pointDominated) view.append(el('div', { class: 'notice warn mon-lead', role: 'note' }, el('b', { text: t('sus.pointLead') }), ' ', t('sus.pointLeadHelp', { points: fmt.int(susData.regime.n_point_stretches), sustained: fmt.int(susData.regime.n_sustained_stretches) })));
  view.append(focusHost, susTop);

  // ---- rows in focus (a row link from Data quality or from a statement)
  if (focus) {
    const fs = section(focus.a === focus.b ? t('mon.focusTitleOne', { a: fmt.int(focus.a) }) : t('mon.focusTitle', { a: fmt.int(focus.a), b: fmt.int(focus.b) }), { right: el('button', { class: 'btn btn-sm', type: 'button', onClick: () => navigate('monitor') }, t('mon.focusClear')) });
    focusHost.append(fs.root);
    let sigs = String(params.signals || '').split(',').map((s) => s.trim()).filter((s) => sigIndex[s] !== undefined).slice(0, 4);
    const near = allFlags.filter((f) => f.row_end >= focus.a && f.row_start <= focus.b);
    if (!sigs.length) sigs = [...new Set(near.flatMap((f) => (f.signals_ranked || []).slice(0, 2).map((s) => s.signal)))].slice(0, 4);
    if (!sigs.length) sigs = signals.filter((s) => !s.excluded).slice(0, 3).map((s) => s.id);
    const node = el('div', { class: 'chart short' });
    fs.body.append(el('p', { class: 'small muted' }, t('mon.focusHelp'), ' ', sigs.length ? refChips('signal', sigs) : null), node,
      el('div', { class: 'row', style: { marginTop: '8px' } }, el('button', { class: 'btn btn-sm btn-primary', type: 'button', onClick: () => openChat({ object_type: 'rows', object_id: `rows ${focus.a}-${focus.b}`, row: focus.a, row_end: focus.b, signals: sigs, flag_id: near[0] ? near[0].id : undefined, title: t('mon.focusTitle', { a: focus.a, b: focus.b }), autoAsk: t('chat.quick.why') }) }, t('sus.askWhy')), near.length ? el('span', { class: 'small muted' }, t('mon.focusFlags', { n: near.length }), ' ', refChips('flag', near.map((f) => f.id), { max: 6 })) : el('span', { class: 'small muted', text: t('mon.focusNoFlags') })));
    view._charts.push(node);
    if (sigs.length) plotSignalsAround(node, sigs, focus.a, focus.b, { sigIndex }); else node.replaceChildren(el('div', { class: 'dim small', text: t('common.notYet') }));
    if (params.rows) setTimeout(() => flash(fs.root), 50);
  }

  // ---- ONE headline list of suspicious rows
  if (susData && (susData.n_rows > 0 || pointDominated)) {
    const ss = section(t('sus.title'), { right: el('span', { class: 'small muted', text: susData.n_rows > susData.n_listed ? t('sus.capped', { shown: fmt.int(susData.n_listed), total: fmt.int(susData.n_rows) }) : t('sus.count', { n: fmt.int(susData.n_rows) }) }) });
    ss.root.classList.add('sus');
    (pointDominated ? susTop : susMid).append(ss.root);
    ss.body.append(el('p', { class: 'sus-headline' }, linkifyRefs(cleanText(susData.headline || ''))), el('p', { class: 'sus-wording', role: 'note' }, cleanText(susData.wording || t('sus.wordingShort'))));
    const tblHost = el('div'); const pager = el('div', { class: 'pager' }); const detail = el('div', { class: 'box detail sus-detail', hidden: true });
    ss.body.append(el('p', { class: 'small muted', text: t('sus.clickHelp') }), tblHost, pager, detail);
    const pages = new Map([[0, susData.rows || []]]);
    const total = Math.max(0, Math.min(susData.n_listed === undefined ? susData.n_rows : susData.n_listed, susData.cap || Infinity));
    const nPages = Math.max(1, Math.ceil(total / SUS_PAGE));
    let page = 0; let selRow = null;
    const where = (r) => (r.row_end !== undefined && r.row_end !== null && r.row_end !== r.row ? `${fmt.int(r.row)}–${fmt.int(r.row_end)}` : fmt.int(r.row));
    const arrow = (d) => (d === 'up' ? el('span', { class: 'dir up', title: t('plain.dir.up'), 'aria-label': t('plain.dir.up'), text: '↑' }) : d === 'down' ? el('span', { class: 'dir down', title: t('plain.dir.down'), 'aria-label': t('plain.dir.down'), text: '↓' }) : null);
    const times = (x) => (Math.abs(Number(x)) >= 100 ? fmt.int(Math.round(Number(x))) : fmt.num(Number(x), 1));   // 3,388 not 3388.2
    const sigLine = (s) => el('div', { class: 'sus-sig', title: cleanText(s.explanation || '') }, refLink('signal', s.signal), ' ', arrow(s.direction), ' ', s.deviation !== null && s.deviation !== undefined ? t('sus.spread', { x: times(s.deviation) }) : el('span', { class: 'dim', text: dirWord(s.direction) }));
    const strength = (r) => { const x = Number(r.strength) || 0; return el('span', { class: 'sev ' + (x >= 12 ? 'high' : x < 6 ? 'low' : ''), title: t('sus.strengthHelp') }, el('span', { class: 'bar' }, el('i', { style: { width: Math.min(100, x / 20 * 100) + '%' } })), el('span', { text: Math.abs(x - 99) < 1e-6 ? t('sus.impossible') : times(x) + '×' })); };
    const showRow = async (r) => {
      selRow = r;
      detail.hidden = false; clear(detail);
      const sigs = (r.signals || []).map((s) => s.signal).filter(Boolean).slice(0, 4);
      const end = r.row_end === undefined || r.row_end === null ? r.row : r.row_end;
      const ctx = { object_type: 'row', object_id: `row ${r.row}`, row: r.row, row_end: end, flag_id: (r.flag_ids || [])[0], check_id: (r.check_ids || [])[0], group_id: r.group_id, batch_id: r.batch_id, signals: sigs, title: cleanText(r.statement || '') };
      setChatContext(ctx);
      const node = el('div', { class: 'chart short' });
      detail.append(el('div', { class: 'row between' }, el('h3', {}, end !== r.row ? t('mon.focusTitle', { a: fmt.int(r.row), b: fmt.int(end) }) : t('mon.focusTitleOne', { a: fmt.int(r.row) }), r.time ? el('span', { class: 'dim small', text: ' ' + fmt.ts(r.time) }) : null),
        el('div', { class: 'row' }, (r.sources || []).map((s) => chip(t('sus.src.' + s) === 'sus.src.' + s ? s : t('sus.src.' + s), SUS_SOURCES[s] || '')), el('button', { class: 'btn btn-sm btn-primary', type: 'button', onClick: () => openChat({ ...ctx, autoAsk: t('chat.quick.why') }) }, t('sus.askWhy')))),
        el('p', { class: 'stmt prose', style: { marginTop: '8px' } }, linkifyRefs(cleanText(r.statement || ''))),
        el('h4', { class: 'small muted', style: { marginTop: '10px' }, text: t('sus.plotTitle') }), node,
        el('div', { class: 'row small', style: { marginTop: '8px' } }, (r.flag_ids || []).length ? el('span', {}, t('mon.flags'), ': ', refChips('flag', r.flag_ids, { max: 4 })) : null, (r.check_ids || []).length ? el('span', {}, t('dq.checks'), ': ', refChips('check', r.check_ids, { max: 4 })) : null, evidenceButton(r.evidence_ids || [])));
      view._charts.push(node);
      try { detail.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); } catch { /* ignore */ }
      if (sigs.length) await plotSignalsAround(node, sigs, r.row, end, { sigIndex, pad: 60 }); else node.replaceChildren(el('div', { class: 'dim small', text: t('common.notYet') }));
    };
    const renderPage = async () => {
      if (!pages.has(page)) {
        tblHost.style.opacity = '0.5';
        const r = await runApi('/suspicious', { params: { limit: SUS_PAGE, offset: page * SUS_PAGE } });
        tblHost.style.opacity = '';
        if (!r.ok || !r.data.available) { clear(tblHost).append(unavailableNote(r)); return; }
        pages.set(page, r.data.rows || []);
      }
      clear(tblHost).append(table({
        columns: [
          { label: t('sus.col.row'), render: (r) => el('span', {}, el('b', { text: where(r) }), r.time ? el('div', { class: 'dim small', text: fmt.ts(r.time) }) : null) },
          { label: t('common.group'), render: (r) => el('span', {}, r.group_id !== null && r.group_id !== undefined ? refLink('group', String(r.group_id)) : '–', r.batch_id ? el('div', { class: 'small' }, refLink('batch', r.batch_id)) : null) },
          { label: t('sus.col.signals'), cls: 'wrap', render: (r) => el('div', {}, (r.signals || []).slice(0, 3).map(sigLine), (r.signals || []).length > 3 ? el('div', { class: 'dim small', text: `+${r.signals.length - 3}` }) : null) },
          { label: t('sus.col.caught'), cls: 'wrap', render: (r) => el('span', { class: 'evs' }, (r.sources || []).map((s) => chip(t('sus.src.' + s) === 'sus.src.' + s ? s : t('sus.src.' + s), SUS_SOURCES[s] || ''))) },
          { label: t('sus.col.strength'), render: strength },
        ],
        rows: pages.get(page), pageSize: SUS_PAGE, keyOf: (r) => r.row, selectedKey: selRow ? selRow.row : undefined, onRow: showRow, emptyText: t('sus.none'),
      }));
      clear(pager);
      if (nPages > 1) pager.append(
        el('button', { class: 'btn btn-sm btn-quiet', type: 'button', disabled: page === 0, onClick: () => { page--; renderPage(); } }, t('common.prev')),
        el('span', { text: t('common.page', { p: page + 1, n: nPages }) }),
        el('button', { class: 'btn btn-sm btn-quiet', type: 'button', disabled: page >= nPages - 1, onClick: () => { page++; renderPage(); } }, t('common.next')));
    };
    renderPage();
  }

  // ---- filters
  const fGroup = el('select', {}, [el('option', { value: '', text: t('common.all') }), ...groups.map((g) => el('option', { value: g, text: g }))]);
  if (wanted && wanted.group_id !== null && wanted.group_id !== undefined && groups.includes(String(wanted.group_id))) fGroup.value = String(wanted.group_id);
  else if (params.group && groups.includes(String(params.group))) fGroup.value = String(params.group);
  const fSev = el('input', { type: 'range', min: '0', max: '1', step: '0.05', value: '0', style: { width: '120px' } });
  const sevLbl = el('span', { class: 'small muted', text: '0 %' });
  const kindBoxes = Object.fromEntries(KINDS.map((kd) => [kd, el('input', { type: 'checkbox', checked: true })]));
  const filters = el('div', { class: 'row' }, el('label', { class: 'row' }, t('common.group'), fGroup), el('label', { class: 'row' }, t('mon.minSeverity'), fSev, sevLbl), ...KINDS.filter((kd) => allFlags.some((f) => f.kind === kd)).map((kd) => el('label', { class: 'check', style: { margin: 0 } }, kindBoxes[kd], kindChip(kd))));
  filters.classList.add('filterbar');
  if (focus) filters.append(el('span', { class: 'chip warn click', role: 'button', tabindex: '0', title: t('mon.focusClear'), onClick: () => navigate('monitor'), onKeydown: (e) => { if (e.key === 'Enter') navigate('monitor'); } }, t('mon.focusChip', { a: fmt.int(focus.a), b: fmt.int(focus.b) }), ' ✕'));
  view.append(filters);
  if (params.flag && !wanted) view.append(el('div', { class: 'notice warn', text: t('ref.notFound', { id: params.flag }) }));
  const scoreNode = el('div', { class: 'chart tall' });
  const contribNode = el('div', { class: 'chart' });
  const scoreSec = section(t('mon.score'), { right: el('span', { class: 'small muted', text: t('mon.clickFlag') }) });
  scoreSec.body.append(el('p', { class: 'small muted', text: t('mon.scoreHelp') }), scoreNode);
  view.append(scoreSec.root, susMid);
  const contribSec = section(t('mon.contrib'), { level: 'engineer' });
  contribSec.body.append(contribNode);
  view.append(contribSec.root);
  const flagSec = section(t('mon.flags'));
  view.append(flagSec.root);
  const grid = el('div', { class: 'cols cols-side' });
  const listHost = el('div');
  const detail = el('div', { class: 'box detail' }, el('div', { class: 'empty', text: t('mon.clickFlag') }));
  grid.append(listHost, detail);
  flagSec.body.append(grid);
  let listTable = null;

  const filtered = () => allFlags.filter((f) => (!fGroup.value || String(f.group_id) === fGroup.value) && f.severity >= Number(fSev.value) && (kindBoxes[f.kind] ? kindBoxes[f.kind].checked : true) && (!focus || (f.row_end >= focus.a - focusPad && f.row_start <= focus.b + focusPad)));

  async function loadCharts() {
    const g = fGroup.value;
    const sc = await runApi('/scores', { params: focus ? { group: g, max_points: 800, row_start: Math.max(0, focus.a - focusPad), row_end: focus.b + focusPad } : { group: g, max_points: 800 } });
    if (!sc.ok || !sc.data.available) { scoreNode.replaceChildren(sc.unavailable ? unavailableNote(sc) : el('div', { class: 'notice', text: sc.data && sc.data.error ? sc.data.error : t('common.notYet') })); contribNode.replaceChildren(); return; }
    const d = sc.data;
    const flags = filtered();
    const traces = [
      { type: 'scatter', mode: 'lines', name: t('mon.score'), x: d.rows, y: d.score, line: { color: k.accent, width: 1.4 }, hovertemplate: 'row %{x}: %{y:.2f}<extra></extra>' },
    ];
    if (d.threshold_value !== null && d.threshold_value !== undefined) traces.push({ type: 'scatter', mode: 'lines', name: t('mon.threshold'), x: [d.row_start, d.row_end], y: [d.threshold_value, d.threshold_value], line: { color: k.fail, width: 1, dash: 'dash' }, hoverinfo: 'skip' });
    const colorByKind = { anomaly: k.fail, point: k.fail, drift: k.warn, changepoint: k.info, dq: k.warn, rule: k.info, cascade: k.fail };
    const symByKind = { anomaly: 'circle', point: 'x', drift: 'triangle-up', changepoint: 'diamond', dq: 'square', rule: 'star', cascade: 'hexagram' };
    const inRange = flags.filter((f) => f.row_start >= d.row_start && f.row_start <= d.row_end);
    const ymax = Math.max(...d.score.filter((v) => v !== null), d.threshold_value || 0, 1);
    traces.push({ type: 'scatter', mode: 'markers', name: t('mon.flags'), x: inRange.map((f) => f.row_start), y: inRange.map((f) => (f.kind === 'dq' || f.kind === 'rule' ? ymax * 1.04 : Math.min(f.score || ymax, ymax * 1.04))), text: inRange.map((f) => `${f.id} ${f.kind} ${fmt.pct(f.severity)}`), customdata: inRange.map((f) => f.id), marker: { size: selected ? inRange.map((f) => (f.id === selected.id ? 16 : 11)) : 11, color: inRange.map((f) => colorByKind[f.kind] || k.ink2), symbol: inRange.map((f) => symByKind[f.kind] || 'circle'), line: { width: selected ? inRange.map((f) => (f.id === selected.id ? 2.5 : 1)) : 1, color: selected ? inRange.map((f) => (f.id === selected.id ? k.ink : k.bg)) : k.bg } }, hovertemplate: '%{text}<extra></extra>' });
    const shapes = [];
    let prev = null;
    d.group.forEach((gg, i) => { if (prev !== null && gg !== prev) shapes.push({ type: 'line', x0: d.rows[i], x1: d.rows[i], y0: 0, y1: 1, yref: 'paper', line: { color: k.line, width: 1 } }); prev = gg; });
    if (selected) shapes.push({ type: 'rect', x0: selected.row_start - 0.5, x1: selected.row_end + 0.5, y0: 0, y1: 1, yref: 'paper', fillcolor: k.warn, opacity: 0.12, line: { width: selected.row_end - selected.row_start < 3 ? 1 : 0, color: k.warn } });
    if (focus) shapes.push({ type: 'rect', x0: focus.a - 0.5, x1: focus.b + 0.5, y0: 0, y1: 1, yref: 'paper', fillcolor: k.info, opacity: 0.14, line: { width: 1, color: k.info } });
    await plot(scoreNode, traces, { height: 340, shapes, xaxis: { title: { text: 'row' } }, yaxis: { title: { text: t('mon.score') }, rangemode: 'tozero' }, hovermode: 'closest', showlegend: true });
    scoreNode.removeAllListeners && scoreNode.removeAllListeners('plotly_click');
    scoreNode.on && scoreNode.on('plotly_click', (ev) => { const p = ev.points.find((x) => x.customdata); if (p) { const f = allFlags.find((x) => x.id === p.customdata); if (f) selectFlag(f, true); } });
    if (roleAllows('engineer') && d.signals && d.signals.length) {
      const ctr = d.signals.map((s, i) => ({ type: 'scatter', mode: 'lines', stackgroup: 'one', name: s, x: d.rows, y: d.contrib[s], line: { width: 0.5, color: colorFor(sigIndex[s] !== undefined ? sigIndex[s] : i) }, hovertemplate: `${s}: %{y:.2f}<extra></extra>` }));
      await plot(contribNode, ctr, { height: 260, xaxis: { title: { text: 'row' } }, yaxis: { title: { text: 'share' }, range: [0, 1] }, showlegend: true });
    }
  }

  function renderList() {
    clear(listHost);
    const rows = filtered().slice().sort((a, b) => b.severity - a.severity || b.confidence - a.confidence);
    listTable = table({
      columns: [
        { label: '', render: (f) => refLink('flag', f.id, f.id.replace('FLAG-', '')) },
        { label: t('common.kind'), render: (f) => kindChip(f.kind) },
        { label: t('common.group'), render: (f) => (f.group_id !== null && f.group_id !== undefined ? refLink('group', String(f.group_id)) : '–') },
        { label: t('mon.rows'), render: (f) => `${f.row_start}–${f.row_end}` },
        { label: t('common.severity'), render: (f) => sev(f.severity) },
        { label: t('mon.cause'), render: flagCause },
        { label: t('common.confidence'), render: (f) => conf(f.confidence) },
        { label: t('common.status'), render: (f) => (f.human_status ? infStatus(null, { human: f.human_status }) : '') },
      ],
      rows, pageSize: 12, onRow: (f) => selectFlag(f), selectedKey: selected && selected.id, emptyText: t('mon.noFlags'),
    });
    listHost.append(listTable);
    if (selected) listTable.reveal(selected.id);
  }

  async function selectFlag(f, fromChart = false) {
    selected = f;
    if (!fromChart) loadCharts(); else { loadCharts(); renderList(); }
    setChatContext(flagContext(f));
    clear(detail);
    const tt = timesThreshold(f.score, f.threshold);
    detail.append(el('div', { class: 'row between' }, el('h3', {}, t('mon.flagDetail'), ' ', el('span', { class: 'dim', text: f.id })), el('div', { class: 'row' }, kindChip(f.kind), flagCause(f), el('button', { class: 'btn btn-sm btn-primary', type: 'button', onClick: () => openChat(flagContext(f)) }, t('common.ask')))));
    detail.append(el('p', { class: 'stmt prose', style: { marginTop: '8px' } }, linkifyRefs(cleanText(f.statement))));
    if (f.kind === 'point') detail.append(el('p', { class: 'small muted', text: t('mon.pointNote') }));
    detail.append(kv([
      [t('common.severity'), sev(f.severity, { words: true })], [t('common.confidence'), conf(f.confidence, { words: true })],
      [t('common.group'), f.group_id !== null && f.group_id !== undefined ? refLink('group', String(f.group_id)) : null], [t('common.batch'), f.batch_id ? refLink('batch', f.batch_id) : null],
      [t('mon.rows'), `${f.row_start}–${f.row_end}` + (f.time_start ? ` (${fmt.ts(f.time_start)} – ${fmt.ts(f.time_end)})` : '')],
      [t('mon.howFar'), tt ? el('span', { title: `${t('mon.score')} ${fmt.num(f.score)} / ${t('mon.threshold')} ${fmt.num(f.threshold)}` }, `${tt.x}× — ${tt.text}`) : (f.score !== null && f.score !== undefined ? fmt.num(f.score) : null)],
      [t('mon.detector'), roleAllows('engineer') ? `${f.detector} (${t('mon.score').toLowerCase()} ${fmt.num(f.score)}${f.threshold !== null && f.threshold !== undefined ? ` vs ${fmt.num(f.threshold)}` : ''})` : null],
      [t('mon.pattern'), f.kind !== 'point' && f.pattern_id ? refLink('pattern', f.pattern_id) : null],
      [t('mon.trustContext'), f.trust_context ? el('span', {}, st(f.trust_context.trusted ? 'trusted' : 'untrusted', `${f.trust_context.trusted ? t('dq.trusted') : t('dq.untrusted')} (${fmt.pct(f.trust_context.trust_score)})`), (f.trust_context.untrusted_signals || []).length ? el('span', {}, ` — ${t('dq.untrustedSignals')}: `, refChips('signal', f.trust_context.untrusted_signals)) : '') : null],
    ]));
    if ((f.signals_ranked || []).length) {
      detail.append(el('h4', { class: 'small muted', style: { marginTop: '12px' }, text: t('mon.rankedSignals') }));
      detail.append(el('div', {}, f.signals_ranked.slice(0, 6).map((s) => el('div', { class: 'meter', title: cleanText(s.explanation || '') }, el('span', {}, refLink('signal', s.signal), ' ', el('span', { class: 'dim small', text: [dirWord(s.direction), s.lag ? `${t('und.lag')} ${s.lag}` : null].filter(Boolean).join(', ') })), el('span', { class: 'bar' }, el('i', { style: { width: Math.min(100, s.contribution * 100) + '%' } })), el('span', { class: 'right', text: fmt.pct(s.contribution) })))));
      detail.append(el('p', { class: 'small dim', text: t('plain.shareHelp') }));
      const expl = f.signals_ranked.slice(0, 6).map((s) => cleanText(s.explanation || '')).filter(Boolean);
      const lst = proseList(expl, { cls: 'list small prose' });
      if (lst) detail.append(lst);
    }
    detail.append(el('div', { style: { marginTop: '12px' } }, decisionBar('flag', f.id, { current: f.human_status, note: f.human_note, overrideFields: [{ key: 'likely_cause_class', label: t('mon.cause'), type: 'select', options: ['process', 'sensor', 'data', 'mixed', 'unknown'], value: f.likely_cause_class }], askContext: flagContext(f), onDone: (action) => { f.human_status = { accept: 'accepted', question: 'questioned', override: 'overridden', dismiss: 'dismissed' }[action]; renderList(); } })));
    const evBox = evidencePanel(f.evidence_ids || []);
    // raw series behind the flag (local only)
    const sigs = (f.signals_ranked || []).slice(0, 4).map((s) => s.signal);
    let node = null;
    if (sigs.length) {
      node = el('div', { class: 'chart short' });
      detail.append(el('h4', { class: 'small muted', style: { marginTop: '12px' }, text: t('mon.rawSeries') }), node);
    }
    detail.append(el('div', { style: { marginTop: '12px' } }, evBox));
    if (fromChart || wanted === f) flash(detail);
    if (node) {
      const span = f.kind === 'point' ? 40 : Math.max(50, f.row_end - f.row_start);
      const r = await runApi('/series', { params: { signals: sigs.join(','), row_start: Math.max(0, f.row_start - span), row_end: f.row_end + Math.floor(span / 2), max_points: 400 } });
      if (r.ok && r.data.available && r.data.rows.length) {
        const tr = sigs.filter((s) => r.data.series[s]).map((s, i) => ({ type: 'scatter', mode: 'lines', name: s, x: r.data.rows, y: r.data.series[s].mean, yaxis: i === 0 ? 'y' : 'y2', line: { width: 1.2, color: colorFor(sigIndex[s] !== undefined ? sigIndex[s] : i) } }));
        plot(node, tr, { height: 220, margin: { l: 44, r: 44, t: 10, b: 30 }, shapes: [{ type: 'rect', x0: f.row_start - 0.5, x1: f.row_end + 0.5, y0: 0, y1: 1, yref: 'paper', fillcolor: k.warn, opacity: 0.16, line: { width: f.row_end - f.row_start < 3 ? 1 : 0, color: k.warn } }], yaxis2: { overlaying: 'y', side: 'right', showgrid: false }, showlegend: true });
        view._charts.push(node);
      } else node.replaceChildren(el('div', { class: 'dim small', text: t('common.notYet') }));
    }
  }

  [fGroup].forEach((x) => x.addEventListener('change', () => { loadCharts(); renderList(); }));
  fSev.addEventListener('input', () => { sevLbl.textContent = fmt.pct(fSev.value); renderList(); });
  fSev.addEventListener('change', loadCharts);
  Object.values(kindBoxes).forEach((b) => b.addEventListener('change', () => { loadCharts(); renderList(); }));
  view._charts.push(scoreNode, contribNode);
  if (wanted) selected = wanted;
  await loadCharts();
  renderList();
  if (wanted) selectFlag(wanted, true);

  // ---- detectors + baseline + evaluation (engineer)
  const [dm, bl, ev] = await Promise.all([runApi('/detect_meta'), runApi('/baseline'), runApi('/evaluation')]);
  if (dm.ok && dm.data.available) {
    const ds = section(t('mon.detectors'), { level: 'engineer' });
    view.append(ds.root);
    ds.body.append(table({ columns: [
      { label: t('mon.detector'), key: 'name' },
      { label: '', render: (d) => (d.selected ? chip(t('mon.selected'), 'ok') : chip(t('mon.notSelected'))) },
      { label: t('mon.weight'), render: (d) => fmt.num(d.weight, 2), num: true },
      { label: t('mon.stability'), render: (d) => conf(d.stability) },
      { label: t('mon.agreement'), render: (d) => conf(d.agreement) },
      { label: 's', render: (d) => fmt.num(d.fit_seconds, 1), num: true },
      { label: '', cls: 'wrap', render: (d) => d.reason || '' },
    ], rows: dm.data.detectors || [] }));
    ds.body.append(el('p', { class: 'small muted', style: { marginTop: '8px' }, text: [dm.data.fold_method, dm.data.n_folds ? `${dm.data.n_folds} folds` : null, dm.data.threshold_method ? `threshold: ${dm.data.threshold_method} (${fmt.num(dm.data.threshold)})` : null, dm.data.elapsed_s ? `${fmt.sec(dm.data.elapsed_s)}` : null].filter(Boolean).join(' — ') }));
  }
  if (bl.ok && bl.data.available) {
    const bs = section(t('mon.baseline'), { level: 'engineer' });
    view.append(bs.root);
    const b = bl.data;
    bs.body.append(el('div', { class: 'row' }, infStatus('assumed'), el('b', { text: b.chosen || b.method }), conf(b.confidence, { words: true }), evidenceButton(b.evidence_ids)),
      proseList(b.assumptions, { cls: 'list small prose' }),
      (b.candidates || []).length ? el('div', {}, b.candidates.map((c) => meter(c.method, c.score, { color: c.selected ? '' : 'var(--ink-3)' }))) : null,
      b.inference_id ? decisionBar('inference', b.inference_id, { overrideFields: [{ key: 'reference_rows', label: 'reference rows (start-end)', type: 'text' }], askContext: { object_type: 'inference', object_id: b.inference_id, title: b.chosen } }) : null);
  }
  if (ev.ok && ev.data.available) {
    const es = section(t('mon.evaluation'), { level: 'engineer' });
    view.append(es.root);
    const e = ev.data;
    es.body.append(kv(Object.entries(e).filter(([k2, v]) => k2 !== 'available' && typeof v !== 'object').map(([k2, v]) => [k2, typeof v === 'number' ? fmt.num(v, 3) : String(v)])));
  } else if (ev.ok && ev.data.reason && roleAllows('engineer')) {
    view.append(el('p', { class: 'small muted', style: { marginTop: '18px' }, text: `${t('mon.evaluation')}: ${ev.data.reason}` }));
  }
  const hh = hiddenHint(view); if (hh) view.append(hh);
  const off = bus.on('decision', (d) => { if (d.objectType === 'flag') { const f = allFlags.find((x) => x.id === d.objectId); if (f) f.human_status = { accept: 'accepted', question: 'questioned', override: 'overridden', dismiss: 'dismissed' }[d.action] || f.human_status; renderList(); } });
  view.cleanup = () => { off(); (view._charts || []).forEach(purge); };
  return view;
}
