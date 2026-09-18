/* View 3: monitor — ensemble score over time with threshold and clickable flag markers, per-signal
   contribution stack, filters, flag detail with evidence + decisions + chat, detector details. */
import { state, t, el, clear, runApi, fmt, conf, sev, chip, kindChip, causeChip, section, table, viewHead, needRun, empty, evidenceButton, fetchEvidence, evidenceList, decisionBar, hiddenHint, kv, roleAllows, bus, st, infStatus, unavailableNote, meter } from '../core.js';
import { plot, purge, tokens, colorFor } from '../charts.js';
import { openChat, flagContext } from '../chat.js';

const KINDS = ['anomaly', 'drift', 'changepoint', 'dq', 'rule', 'cascade'];

export async function render(main, params = {}) {
  const view = el('div', { class: 'view' });
  main.append(view);
  view.append(viewHead('3', t('nav.monitor')));
  if (!state.run) { view.append(needRun()); return view; }
  const k = tokens();
  const [fl, sig] = await Promise.all([runApi('/flags', { params: { limit: 5000 } }), runApi('/signals')]);
  const allFlags = fl.ok ? fl.data.items || [] : [];
  const signals = sig.ok ? sig.data.signals || [] : [];
  const sigIndex = Object.fromEntries(signals.map((s, i) => [s.id, i]));
  const groups = fl.ok ? fl.data.groups || [] : [];
  let selected = null;

  // ---- filters
  const fGroup = el('select', {}, [el('option', { value: '', text: t('common.all') }), ...groups.map((g) => el('option', { value: g, text: g }))]);
  const fSev = el('input', { type: 'range', min: '0', max: '1', step: '0.05', value: '0', style: { width: '120px' } });
  const sevLbl = el('span', { class: 'small muted', text: '0 %' });
  const kindBoxes = Object.fromEntries(KINDS.map((kd) => [kd, el('input', { type: 'checkbox', checked: true })]));
  const filters = el('div', { class: 'row' }, el('label', { class: 'row' }, t('common.group'), fGroup), el('label', { class: 'row' }, t('mon.minSeverity'), fSev, sevLbl), ...KINDS.filter((kd) => allFlags.some((f) => f.kind === kd)).map((kd) => el('label', { class: 'check', style: { margin: 0 } }, kindBoxes[kd], kindChip(kd))));
  view.append(filters);
  const scoreNode = el('div', { class: 'chart tall' });
  const contribNode = el('div', { class: 'chart' });
  const scoreSec = section(t('mon.score'), { right: el('span', { class: 'small muted', text: t('mon.clickFlag') }) });
  scoreSec.body.append(scoreNode);
  view.append(scoreSec.root);
  const contribSec = section(t('mon.contrib'), { level: 'engineer' });
  contribSec.body.append(contribNode);
  view.append(contribSec.root);
  const flagSec = section(t('mon.flags'));
  view.append(flagSec.root);
  const grid = el('div', { class: 'cols cols-side' });
  const listHost = el('div');
  const detail = el('div', { class: 'box' }, el('div', { class: 'empty', text: t('mon.clickFlag') }));
  grid.append(listHost, detail);
  flagSec.body.append(grid);

  const filtered = () => allFlags.filter((f) => (!fGroup.value || String(f.group_id) === fGroup.value) && f.severity >= Number(fSev.value) && (kindBoxes[f.kind] ? kindBoxes[f.kind].checked : true));

  async function loadCharts() {
    const g = fGroup.value;
    const [sc, series] = await Promise.all([runApi('/scores', { params: { group: g, max_points: 800 } }), Promise.resolve(null)]);
    if (!sc.ok || !sc.data.available) { scoreNode.replaceChildren(sc.unavailable ? unavailableNote(sc) : el('div', { class: 'notice', text: sc.data && sc.data.error ? sc.data.error : t('common.notYet') })); contribNode.replaceChildren(); return; }
    const d = sc.data;
    const flags = filtered();
    const traces = [
      { type: 'scatter', mode: 'lines', name: t('mon.score'), x: d.rows, y: d.score, line: { color: k.accent, width: 1.4 }, hovertemplate: 'row %{x}: %{y:.2f}<extra></extra>' },
    ];
    if (d.threshold_value !== null && d.threshold_value !== undefined) traces.push({ type: 'scatter', mode: 'lines', name: t('mon.threshold'), x: [d.row_start, d.row_end], y: [d.threshold_value, d.threshold_value], line: { color: k.fail, width: 1, dash: 'dash' }, hoverinfo: 'skip' });
    // flags as markers at their onset row, height = score (or threshold when absent)
    const colorByKind = { anomaly: k.fail, drift: k.warn, changepoint: k.info, dq: k.warn, rule: k.info, cascade: k.fail };
    const symByKind = { anomaly: 'circle', drift: 'triangle-up', changepoint: 'diamond', dq: 'square', rule: 'star', cascade: 'hexagram' };
    const inRange = flags.filter((f) => f.row_start >= d.row_start && f.row_start <= d.row_end);
    const ymax = Math.max(...d.score.filter((v) => v !== null), d.threshold_value || 0, 1);
    traces.push({ type: 'scatter', mode: 'markers', name: t('mon.flags'), x: inRange.map((f) => f.row_start), y: inRange.map((f) => (f.kind === 'dq' || f.kind === 'rule' ? ymax * 1.04 : Math.min(f.score || ymax, ymax * 1.04))), text: inRange.map((f) => `${f.id} ${f.kind} ${fmt.pct(f.severity)}`), customdata: inRange.map((f) => f.id), marker: { size: 11, color: inRange.map((f) => colorByKind[f.kind] || k.ink2), symbol: inRange.map((f) => symByKind[f.kind] || 'circle'), line: { width: 1, color: k.bg } }, hovertemplate: '%{text}<extra></extra>' });
    // group boundaries
    const shapes = [];
    let prev = null;
    d.group.forEach((gg, i) => { if (prev !== null && gg !== prev) shapes.push({ type: 'line', x0: d.rows[i], x1: d.rows[i], y0: 0, y1: 1, yref: 'paper', line: { color: k.line, width: 1 } }); prev = gg; });
    if (selected) shapes.push({ type: 'rect', x0: selected.row_start, x1: selected.row_end, y0: 0, y1: 1, yref: 'paper', fillcolor: k.warn, opacity: 0.12, line: { width: 0 } });
    await plot(scoreNode, traces, { height: 340, shapes, xaxis: { title: { text: 'row' } }, yaxis: { title: { text: t('mon.score') }, rangemode: 'tozero' }, hovermode: 'closest', showlegend: true });
    scoreNode.removeAllListeners && scoreNode.removeAllListeners('plotly_click');
    scoreNode.on('plotly_click', (ev) => { const p = ev.points.find((x) => x.customdata); if (p) { const f = allFlags.find((x) => x.id === p.customdata); if (f) selectFlag(f, true); } });
    // contributions (stacked)
    if (roleAllows('engineer') && d.signals && d.signals.length) {
      const ctr = d.signals.map((s, i) => ({ type: 'scatter', mode: 'lines', stackgroup: 'one', name: s, x: d.rows, y: d.contrib[s], line: { width: 0.5, color: colorFor(sigIndex[s] !== undefined ? sigIndex[s] : i) }, hovertemplate: `${s}: %{y:.2f}<extra></extra>` }));
      await plot(contribNode, ctr, { height: 260, xaxis: { title: { text: 'row' } }, yaxis: { title: { text: 'share' }, range: [0, 1] }, showlegend: true });
    }
  }

  function renderList() {
    clear(listHost);
    const rows = filtered().slice().sort((a, b) => b.severity - a.severity || b.confidence - a.confidence);
    listHost.append(table({
      columns: [
        { label: '', render: (f) => el('span', { class: 'dim small', text: f.id.replace('FLAG-', '') }) },
        { label: t('common.kind'), render: (f) => kindChip(f.kind) },
        { label: t('common.group'), render: (f) => (f.group_id ?? '–') },
        { label: t('mon.rows'), render: (f) => `${f.row_start}–${f.row_end}` },
        { label: t('common.severity'), render: (f) => sev(f.severity) },
        { label: t('mon.cause'), render: (f) => causeChip(f.likely_cause_class) },
        { label: t('common.confidence'), render: (f) => conf(f.confidence) },
        { label: t('common.status'), render: (f) => (f.human_status ? infStatus(null, { human: f.human_status }) : '') },
      ],
      rows, pageSize: 12, onRow: (f) => selectFlag(f), selectedKey: selected && selected.id, emptyText: t('mon.noFlags'),
    }));
  }

  async function selectFlag(f, fromChart = false) {
    selected = f;
    if (!fromChart) loadCharts(); else renderList();
    clear(detail);
    detail.append(el('div', { class: 'row between' }, el('h3', {}, t('mon.flagDetail'), ' ', el('span', { class: 'dim', text: f.id })), el('div', { class: 'row' }, kindChip(f.kind), causeChip(f.likely_cause_class), el('button', { class: 'btn btn-sm btn-primary', type: 'button', onClick: () => openChat(flagContext(f)) }, t('common.ask')))));
    detail.append(el('p', { style: { marginTop: '8px' }, text: f.statement }));
    detail.append(kv([
      [t('common.severity'), sev(f.severity)], [t('common.confidence'), conf(f.confidence)],
      [t('common.group'), f.group_id ?? null], [t('common.batch'), f.batch_id],
      [t('mon.rows'), `${f.row_start}–${f.row_end}` + (f.time_start ? ` (${fmt.ts(f.time_start)} – ${fmt.ts(f.time_end)})` : '')],
      [t('mon.detector'), roleAllows('engineer') ? `${f.detector} (score ${fmt.num(f.score)}${f.threshold !== null && f.threshold !== undefined ? ` vs ${fmt.num(f.threshold)}` : ''})` : null],
      [t('mon.pattern'), f.pattern_id],
      [t('mon.trustContext'), f.trust_context ? el('span', {}, st(f.trust_context.trusted ? 'trusted' : 'untrusted', fmt.pct(f.trust_context.trust_score)), (f.trust_context.untrusted_signals || []).length ? ` — ${f.trust_context.untrusted_signals.join(', ')}` : '') : null],
    ]));
    if ((f.signals_ranked || []).length) {
      detail.append(el('h4', { class: 'small muted', style: { marginTop: '12px' }, text: t('mon.rankedSignals') }));
      detail.append(el('div', {}, f.signals_ranked.slice(0, 6).map((s) => el('div', { class: 'meter', title: s.explanation || '' }, el('span', {}, el('b', { text: s.signal }), ' ', el('span', { class: 'dim small', text: [s.direction, s.lag ? `${t('und.lag')} ${s.lag}` : null].filter(Boolean).join(', ') })), el('span', { class: 'bar' }, el('i', { style: { width: Math.min(100, s.contribution * 100) + '%' } })), el('span', { class: 'right', text: fmt.pct(s.contribution) })))));
      if (roleAllows('engineer')) detail.append(el('ul', { class: 'list small muted' }, f.signals_ranked.slice(0, 4).map((s) => el('li', { text: s.explanation || '' }))));
    }
    detail.append(el('div', { style: { marginTop: '12px' } }, decisionBar('flag', f.id, { current: f.human_status, note: f.human_note, overrideFields: [{ key: 'likely_cause_class', label: t('mon.cause'), type: 'select', options: ['process', 'sensor', 'data', 'mixed', 'unknown'], value: f.likely_cause_class }], askContext: flagContext(f), onDone: (action) => { f.human_status = { accept: 'accepted', question: 'questioned', override: 'overridden', dismiss: 'dismissed' }[action]; renderList(); } })));
    // raw series behind the flag (local only)
    const sigs = (f.signals_ranked || []).slice(0, 4).map((s) => s.signal);
    if (sigs.length) {
      const node = el('div', { class: 'chart short' });
      detail.append(el('h4', { class: 'small muted', style: { marginTop: '12px' }, text: t('mon.rawSeries') }), node);
      const span = Math.max(50, f.row_end - f.row_start);
      const r = await runApi('/series', { params: { signals: sigs.join(','), row_start: Math.max(0, f.row_start - span), row_end: f.row_end + Math.floor(span / 2), max_points: 400 } });
      if (r.ok && r.data.available && r.data.rows.length) {
        const tr = sigs.filter((s) => r.data.series[s]).map((s, i) => ({ type: 'scatter', mode: 'lines', name: s, x: r.data.rows, y: r.data.series[s].mean, yaxis: i === 0 ? 'y' : 'y2', line: { width: 1.2, color: colorFor(sigIndex[s] !== undefined ? sigIndex[s] : i) } }));
        plot(node, tr, { height: 220, margin: { l: 44, r: 44, t: 10, b: 30 }, shapes: [{ type: 'rect', x0: f.row_start, x1: f.row_end, y0: 0, y1: 1, yref: 'paper', fillcolor: k.warn, opacity: 0.12, line: { width: 0 } }], yaxis2: { overlaying: 'y', side: 'right', showgrid: false }, showlegend: true });
        view._charts.push(node);
      } else node.replaceChildren(el('div', { class: 'dim small', text: t('common.notYet') }));
    }
    const evBox = el('div', { style: { marginTop: '12px' } }, el('h4', { class: 'small muted', text: t('common.evidence') }));
    detail.append(evBox);
    evBox.append(evidenceList(await fetchEvidence(f.evidence_ids || [])));
    if (fromChart) detail.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }

  [fGroup].forEach((x) => x.addEventListener('change', () => { loadCharts(); renderList(); }));
  fSev.addEventListener('input', () => { sevLbl.textContent = fmt.pct(fSev.value); renderList(); });
  fSev.addEventListener('change', loadCharts);
  Object.values(kindBoxes).forEach((b) => b.addEventListener('change', () => { loadCharts(); renderList(); }));
  view._charts = [scoreNode, contribNode];
  await loadCharts();
  renderList();
  if (params.flag) { const f = allFlags.find((x) => x.id === params.flag); if (f) selectFlag(f, true); }

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
      { label: '', render: (d) => d.reason || '' },
    ], rows: dm.data.detectors || [] }));
    ds.body.append(el('p', { class: 'small muted', style: { marginTop: '8px' }, text: [dm.data.fold_method, dm.data.n_folds ? `${dm.data.n_folds} folds` : null, dm.data.threshold_method ? `threshold: ${dm.data.threshold_method} (${fmt.num(dm.data.threshold)})` : null, dm.data.elapsed_s ? `${fmt.sec(dm.data.elapsed_s)}` : null].filter(Boolean).join(' — ') }));
  }
  if (bl.ok && bl.data.available) {
    const bs = section(t('mon.baseline'), { level: 'engineer' });
    view.append(bs.root);
    const b = bl.data;
    bs.body.append(el('div', { class: 'row' }, infStatus('assumed'), el('b', { text: b.chosen || b.method }), conf(b.confidence), evidenceButton(b.evidence_ids)),
      (b.assumptions || []).length ? el('ul', { class: 'list small' }, b.assumptions.map((a) => el('li', { text: a }))) : null,
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
