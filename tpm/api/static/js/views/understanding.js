/* View 1: what the system understood — signal catalog with evidence, hypotheses, role override,
   correlation heatmap, clusters, dataset assumptions and what remains uncertain. */
import { state, t, el, clear, runApi, fmt, conf, infStatus, chip, section, table, viewHead, needRun, empty, evidenceButton, fetchEvidence, evidenceList, decisionBar, postDecision, hiddenHint, kv, roleAllows, bus, meter, unavailableNote } from '../core.js';
import { plot, purge, tokens, colorFor } from '../charts.js';
import { openChat, signalContext } from '../chat.js';
import { plainBox } from '../plain.js';

const ROLES = ['continuous_measured', 'actuator_like', 'held_sampled', 'constant', 'derived_redundant', 'counter', 'timestamp', 'categorical', 'text', 'identifier', 'unknown'];

export async function render(main, params = {}) {
  const view = el('div', { class: 'view' });
  main.append(view);
  view.append(viewHead('1', t('nav.understanding')));
  if (!state.run) { view.append(needRun()); return view; }
  try { const pb = await plainBox('understanding'); if (pb) view.append(pb); } catch (e) { /* plain box is optional */ }
  const [und, sig, rel, dom, sch] = await Promise.all([runApi('/understanding'), runApi('/signals'), runApi('/relations'), runApi('/domain'), runApi('/schema')]);
  const signals = sig.ok ? sig.data.signals || [] : [];
  const byId = Object.fromEntries(signals.map((s) => [s.id, s]));
  const U = und.data || {};

  // ---- summary + assumptions + uncertain + domain
  const top = el('div', { class: 'cols cols-2' });
  const summary = el('div', { class: 'stack' }, el('h2', { text: t('und.title') }), el('p', { text: U.summary || t('common.notYet') }));
  if (dom.ok && dom.data.available) {
    const lk = dom.data.likelihood || {};
    summary.append(el('h3', { text: t('und.domain') }), el('div', {}, Object.entries(lk).sort((a, b) => b[1] - a[1]).map(([k, v]) => meter(k.replace(/_/g, ' '), v))), dom.data.statement ? el('p', { class: 'small muted', text: dom.data.statement }) : null);
  }
  const lists = el('div', { class: 'stack' },
    el('h3', { text: t('und.assumptions') }), (U.assumptions || []).length ? el('ul', { class: 'list' }, (U.assumptions || []).map((a) => el('li', {}, infStatus('assumed'), ' ', a))) : empty(),
    el('h3', { text: t('und.uncertain') }), (U.uncertain || []).length ? el('ul', { class: 'list' }, (U.uncertain || []).map((a) => el('li', {}, infStatus('uncertain'), ' ', a))) : empty());
  top.append(summary, lists);
  view.append(top);

  // ---- dataset-level hypotheses (inferences with accept/question/override)
  const hyp = section(t('und.hypotheses'), { right: el('span', { class: 'small muted', text: t('inference.legend') }) });
  view.append(hyp.root);
  const infR = await runApi('/inferences', { params: { subject: 'dataset' } });
  const infs = infR.ok ? infR.data.items || [] : [];
  if (!infs.length) hyp.body.append(empty());
  for (const inf of infs) hyp.body.append(hypRow(inf));

  // ---- signal catalog
  const cat = section(t('und.catalog'), { right: el('span', { class: 'small muted', text: sch.ok && sch.data.n_rows ? `${fmt.int(sch.data.n_rows)} rows, ${signals.length} signals, ${sch.data.n_groups || 1} groups` : '' }) });
  view.append(cat.root);
  if (!sig.ok || !sig.data.available) { cat.body.append(sig.unavailable ? unavailableNote(sig) : empty(t('common.notYet'))); }
  const grid = el('div', { class: 'cols cols-side' });
  cat.body.append(grid);
  const detail = el('div', { class: 'box' }, el('div', { class: 'empty', text: t('und.pickSignal') }));
  const tbl = table({
    columns: [
      { label: t('und.alias'), render: (s) => el('span', { title: s.source_column ? `${t('und.sourceName')}: ${s.source_column}` : '' }, el('b', { text: s.id }), s.excluded ? el('span', { class: 'dim small', text: ' ✕' }) : null) },
      { label: t('und.role'), render: (s) => el('span', {}, (s.human_role_override || s.structural_role || 'unknown').replace(/_/g, ' '), s.human_role_override ? el('span', { class: 'small', style: { marginLeft: '6px' } }, infStatus(null, { human: 'overridden' })) : null) },
      { label: t('common.confidence'), render: (s) => conf(s.structural_confidence) },
      { label: t('und.instrument'), render: (s) => s.instrument_hypothesis ? el('span', {}, s.instrument_hypothesis, ' ', el('span', { class: 'small' }, infStatus(s.instrument_confidence >= 0.5 ? 'assumed' : 'uncertain')), ' ', conf(s.instrument_confidence, { label: false })) : el('span', { class: 'dim', text: '–' }) },
      { label: t('und.cluster'), render: (s) => s.cluster_id || '–' },
      { label: t('common.evidence'), num: true, render: (s) => String((s.evidence_ids || []).length) },
    ],
    rows: signals, pageSize: 40, onRow: (s) => showSignal(s),
    rowClass: (s) => (s.excluded ? 'dim' : ''),
  });
  grid.append(tbl, detail);
  const SP = U.signal_plain || {};
  if (params && params.signal && byId[params.signal]) { showSignal(byId[params.signal]); setTimeout(() => detail.scrollIntoView({ behavior: 'smooth', block: 'start' }), 50); }

  async function showSignal(s) {
    clear(detail);
    detail.append(el('div', { class: 'row between' }, el('h3', {}, `${t('und.detail')}: ${s.id}`, s.source_column ? el('span', { class: 'muted small', text: ` (${t('und.sourceName')}: ${s.source_column})` }) : null), el('button', { class: 'btn btn-sm', type: 'button', onClick: () => openChat(signalContext(s)) }, t('common.ask'))));
    const fp = s.fingerprint || {};
    if (SP[s.id]) detail.append(el('p', { class: 'plain-sig', style: { margin: '6px 0 10px', padding: '10px 12px', borderLeft: '3px solid var(--accent, #2bb5a0)', background: 'var(--bg-2, rgba(127,127,127,.08))', borderRadius: '0 6px 6px 0', overflowWrap: 'anywhere' }, text: SP[s.id] }));
    detail.append(kv([
      [t('und.role'), el('span', {}, (s.human_role_override || s.structural_role).replace(/_/g, ' '), ' ', conf(s.structural_confidence))],
      [t('und.instrument'), s.instrument_hypothesis ? el('span', {}, s.instrument_hypothesis, ' ', conf(s.instrument_confidence)) : null],
      [t('und.unit'), s.unit_operation_hypothesis ? el('span', {}, s.unit_operation_hypothesis, ' ', conf(s.unit_operation_confidence)) : null],
      [t('und.cluster'), s.cluster_id],
      [t('und.excluded'), s.excluded ? (s.excluded_reason || t('common.yes')) : null],
    ]));
    if (roleAllows('engineer') && Object.keys(fp).length) {
      detail.append(el('h4', { class: 'small muted', style: { marginTop: '12px' }, text: t('und.fingerprint') }), el('div', { class: 'small', text: Object.entries(fp).filter(([, v]) => v !== null && v !== undefined).map(([k, v]) => `${k} ${typeof v === 'number' ? fmt.num(v, 3) : v}`).join('   ') }));
    }
    if ((s.related_signals || []).length) detail.append(el('h4', { class: 'small muted', style: { marginTop: '12px' }, text: t('und.related') }), el('div', { class: 'sigchips' }, s.related_signals.map((r) => chip(`${r.signal}  r ${fmt.num(r.r, 2)}  ${t('und.lag')} ${r.lag}`, 'click', { onClick: () => { if (byId[r.signal]) showSignal(byId[r.signal]); } }))));
    // hypotheses (inferences about this signal)
    const hb = el('div', {}, el('h4', { class: 'small muted', style: { marginTop: '12px' }, text: t('und.hypothesesFor', { id: s.id }) }));
    detail.append(hb);
    const ir = await runApi('/inferences', { params: { subject: s.id } });
    for (const inf of ir.ok ? ir.data.items || [] : []) hb.append(hypRow(inf));
    // role override
    const sel = el('select', {}, ROLES.map((r) => el('option', { value: r, text: r.replace(/_/g, ' ') })));
    sel.value = s.human_role_override || s.structural_role;
    const note = el('input', { type: 'text', placeholder: t('common.note'), style: { flex: 1 } });
    detail.append(el('h4', { class: 'small muted', style: { marginTop: '12px' }, text: t('und.roleOverride') }), el('div', { class: 'row' }, sel, note, el('button', { class: 'btn btn-sm btn-override', type: 'button', onClick: async () => { const r = await postDecision('signal', s.id, 'set_role', { note: note.value, newValue: { role: sel.value } }); if (r.ok) { s.human_role_override = sel.value; tbl.update(signals); showSignal(s); } } }, t('common.save'))), el('div', { class: 'hint', text: t('und.roleOverrideHelp') }));
    // evidence
    const evBox = el('div', { style: { marginTop: '12px' } }, el('h4', { class: 'small muted', text: t('common.evidence') }));
    detail.append(evBox);
    const evs = await fetchEvidence(s.evidence_ids || []);
    evBox.append(evidenceList(evs));
  }

  function hypRow(inf) {
    const row = el('div', { class: 'hyp' });
    row.append(el('div', {}, el('div', { class: 'claim' }, infStatus(inf.status, { human: inf.human_status }), ' ', inf.claim, ' ', conf(inf.confidence)),
      inf.reasoning ? el('div', { class: 'why', text: inf.reasoning + (inf.source && inf.source !== 'code' ? ` (${inf.source})` : '') }) : null,
      inf.alternatives && inf.alternatives.length ? el('div', { class: 'alt', text: 'Alternatives: ' + inf.alternatives.join('; ') }) : null,
      el('div', { class: 'row', style: { marginTop: '4px' } }, evidenceButton(inf.evidence_ids), el('span', { class: 'dim small', text: inf.id }))),
      decisionBar('inference', inf.id, { current: inf.human_status, note: inf.human_note, overrideFields: [{ key: 'claim', label: t('decision.override.newValue'), type: 'text', value: inf.claim }], askContext: { object_type: 'inference', object_id: inf.id, title: inf.claim } }));
    return row;
  }

  // ---- heatmap + clusters + grouping
  if (rel.ok && rel.data.available && rel.data.correlation) {
    const hm = section(t('und.heatmap'), { level: 'engineer' });
    view.append(hm.root);
    const node = el('div', { class: 'chart tall' });
    hm.body.append(node);
    const k = tokens();
    const labels = rel.data.signals || [];
    requestAnimationFrame(() => plot(node, [{ type: 'heatmap', z: rel.data.correlation, x: labels, y: labels, zmin: -1, zmax: 1, colorscale: [[0, k.fail], [0.5, k.bg], [1, k.info]], hovertemplate: '%{y} × %{x}: r=%{z:.2f}<extra></extra>', showscale: true, colorbar: { thickness: 10, len: 0.8, tickfont: { size: 10 } } }], { height: 420, margin: { l: 50, r: 10, t: 10, b: 50 }, xaxis: { tickangle: -45 }, yaxis: { autorange: 'reversed' } }));
    view._charts = [node];
  }
  const clusters = (rel.ok && rel.data.clusters) || (U.clusters ? Object.entries(U.clusters).map(([id, ss]) => ({ id, signals: ss })) : []);
  if (clusters.length) {
    const cl = section(t('und.clusters'));
    view.append(cl.root);
    cl.body.append(el('div', { class: 'cols cols-3' }, clusters.map((c, i) => el('div', { class: 'cluster', style: { borderTop: `3px solid ${colorFor(i)}` } }, el('h4', { text: c.id }), c.description ? el('div', { class: 'desc', text: c.description }) : null, el('div', { class: 'sigchips' }, (c.signals || []).map((sid) => chip(sid, 'click', { onClick: () => { if (byId[sid]) showSignal(byId[sid]); } })))))));
  }
  if (sch.ok && (sch.data.grouping_candidates || []).length) {
    const gs = section(t('und.grouping'), { level: 'engineer' });
    view.append(gs.root);
    gs.body.append(table({ columns: [{ label: 'method', key: 'method' }, { label: 'columns', render: (g) => (g.columns || []).join(', ') || '–' }, { label: 'groups', key: 'n_groups', num: true }, { label: 'score', render: (g) => conf(g.score) }, { label: 'rationale', key: 'rationale', cls: 'wrap' }, { label: '', render: (g) => (g.method === sch.data.grouping_method ? chip(t('mon.selected'), 'ok') : '') }], rows: sch.data.grouping_candidates }));
  }
  const hh = hiddenHint(view); if (hh) view.append(hh);
  view.cleanup = () => (view._charts || []).forEach(purge);
  return view;
}
