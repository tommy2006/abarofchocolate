/* View 4: diagnoses — ranked list; detail with fault type, cause reasoning, ranked signals,
   propagation chain, numbered steps, critique, uncertainty, decisions, pattern naming. */
import { state, t, el, clear, runApi, fmt, conf, sev, chip, kindChip, causeChip, section, viewHead, needRun, empty, evidenceButton, fetchEvidence, evidenceList, decisionBar, postDecision, hiddenHint, kv, roleAllows, infStatus, toast, unavailableNote, bus } from '../core.js';
import { openChat, diagnosisContext, flagContext } from '../chat.js';

const CAUSE_WHY = {
  process: 'Several related signals moved together and kept their usual relations: the disturbance is in the process, not in one instrument.',
  sensor: 'One signal broke its usual relation to its partners while the partners stayed consistent: the instrument, not the process, is the likely cause.',
  data: 'The change is a data artefact (scale, duplicates, gaps), not a physical event.',
  mixed: 'Both a process disturbance and an instrument problem remain plausible.',
  unknown: 'The evidence does not favour a process or a sensor explanation.',
};

export async function render(main, params = {}) {
  const view = el('div', { class: 'view' });
  main.append(view);
  view.append(viewHead('4', t('nav.diagnoses'), el('span', { class: 'small muted', text: t('diag.ranked') })));
  if (!state.run) { view.append(needRun()); return view; }
  const [dg, fl, pt] = await Promise.all([runApi('/diagnoses'), runApi('/flags', { params: { limit: 5000 } }), runApi('/patterns')]);
  if (!dg.ok || !dg.data.available) { view.append(dg.unavailable ? unavailableNote(dg) : empty(t('diag.none'))); return view; }
  const flags = fl.ok ? fl.data.items || [] : [];
  const flagsById = Object.fromEntries(flags.map((f) => [f.id, f]));
  const patterns = Object.fromEntries((pt.ok ? pt.data.patterns || [] : []).map((p) => [p.id, p]));
  const sevOf = (d) => Math.max(0, ...(d.flag_ids || []).map((id) => (flagsById[id] ? flagsById[id].severity : 0)));
  const diags = (dg.data.items || []).slice().sort((a, b) => sevOf(b) - sevOf(a) || b.confidence - a.confidence);
  const grid = el('div', { class: 'cols cols-side' });
  view.append(grid);
  const list = el('div', { class: 'dlist' });
  const detail = el('div', { class: 'box' }, el('div', { class: 'empty', text: t('diag.pick') }));
  grid.append(list, detail);
  let selected = null;
  const renderList = () => {
    clear(list);
    if (!diags.length) list.append(empty(t('diag.none')));
    for (const d of diags) {
      const p = d.pattern_id && patterns[d.pattern_id];
      const b = el('button', { type: 'button', class: `ditem c-${d.cause_class}` + (selected && selected.id === d.id ? ' sel' : ''), onClick: () => show(d) });
      b.append(el('span', { class: 'title' }, p && p.name ? p.name : d.fault_type, d.human_status ? el('span', { class: 'small', style: { marginLeft: '8px' } }, infStatus(null, { human: d.human_status })) : null), el('span', {}, sev(sevOf(d))),
        el('span', { class: 'sub' }, `${d.id} — ${t('common.group')} ${d.group_id ?? '–'} — `, causeChip(d.cause_class), ' ', conf(d.confidence)));
      list.append(b);
    }
  };
  async function show(d) {
    selected = d; renderList(); clear(detail);
    const p = d.pattern_id && patterns[d.pattern_id];
    detail.append(el('div', { class: 'row between' }, el('h3', {}, p && p.name ? `${p.name} ` : '', el('span', { class: p && p.name ? 'dim' : '', text: d.fault_type })), el('div', { class: 'row' }, causeChip(d.cause_class), conf(d.confidence), el('button', { class: 'btn btn-sm btn-primary', type: 'button', onClick: () => openChat(diagnosisContext(d)) }, t('common.ask')))));
    detail.append(el('div', { class: 'row small muted', style: { marginTop: '4px' } }, el('span', { text: d.id }), el('span', { text: `${t('common.group')} ${d.group_id ?? '–'}` }), d.pattern_id ? chip(d.pattern_id, 'info') : null, d.narrative_source ? el('span', { text: `${t('common.source')}: ${d.narrative_source}` }) : null));
    detail.append(el('h4', { class: 'small muted', style: { marginTop: '12px' }, text: t('diag.summary') }), el('p', { text: d.summary }));
    // cause reasoning
    detail.append(el('h4', { class: 'small muted', text: t('diag.causeWhy') }), el('p', {}, causeChip(d.cause_class), ' ', CAUSE_WHY[d.cause_class] || CAUSE_WHY.unknown));
    // ranked signals
    if ((d.ranked_signals || []).length) {
      detail.append(el('h4', { class: 'small muted', text: t('diag.signals') }));
      detail.append(el('div', {}, d.ranked_signals.map((s) => el('div', { class: 'meter' }, el('span', {}, el('b', { text: s.signal }), ' ', el('span', { class: 'dim small', text: s.direction || '' })), el('span', { class: 'bar' }, el('i', { style: { width: Math.min(100, s.contribution * 100) + '%' } })), el('span', { class: 'right', text: fmt.pct(s.contribution) })))));
      detail.append(el('ul', { class: 'list small' }, d.ranked_signals.map((s) => el('li', {}, s.explanation || `${s.signal}: ${fmt.pct(s.contribution)}`, s.evidence_ids && s.evidence_ids.length ? el('span', {}, ' ', evidenceButton(s.evidence_ids, { label: t('common.evidence') })) : null))));
    }
    // propagation chain
    if ((d.propagation || []).length) {
      detail.append(el('h4', { class: 'small muted', text: t('diag.propagation') }));
      const chain = el('div', { class: 'chain' });
      d.propagation.forEach((step, i) => {
        if (i === 0) chain.append(el('span', { class: 'node first', text: step.from_signal }));
        chain.append(el('span', { class: 'edge', title: step.explanation || '' }, el('span', { class: 'lag', text: step.lag !== null && step.lag !== undefined ? `+${step.lag} ${t('diag.lagUnit')}` : '' }), el('span', { class: 'arrow' }), el('span', { text: `r ${fmt.num(step.strength, 2)}` })));
        chain.append(el('span', { class: 'node', text: step.to_signal }));
      });
      detail.append(chain);
      if (roleAllows('engineer')) detail.append(el('ul', { class: 'list small muted' }, d.propagation.map((s) => el('li', { text: s.explanation }))));
    }
    // steps
    if ((d.steps || []).length) detail.append(el('h4', { class: 'small muted', text: t('diag.steps') }), el('ol', { class: 'steps' }, d.steps.map((s) => el('li', { text: s }))));
    // critique
    if (d.critique) {
      const c = d.critique;
      const box = el('div', { class: 'critique ' + c.verdict });
      box.append(el('div', { class: 'row between' }, el('b', {}, t('diag.critique'), ': ', chip(t('diag.verdict.' + c.verdict), { supported: 'ok', weakened: 'warn', rejected: 'fail' }[c.verdict] || '')), el('span', { class: 'small muted' }, c.adjusted_confidence !== null && c.adjusted_confidence !== undefined ? el('span', {}, t('diag.adjusted') + ' ', conf(c.adjusted_confidence)) : null, ` ${t('common.source')}: ${c.source || 'template'}`)));
      if ((c.objections || []).length) box.append(el('div', { class: 'small muted', style: { marginTop: '6px' }, text: t('diag.objections') }), el('ul', { class: 'list' }, c.objections.map((o) => el('li', { text: o }))));
      if (roleAllows('engineer') && (c.checks || []).length) box.append(el('div', { class: 'small muted', style: { marginTop: '6px' }, text: t('diag.checks') }), el('div', {}, c.checks.map((ch) => el('div', { class: 'check-row' }, el('span', { style: { color: ch.passed ? 'var(--ok)' : 'var(--fail)' }, text: ch.passed ? '✓' : '✕' }), el('span', {}, el('b', { text: ch.name }), ' ', el('span', { class: 'muted', text: ch.detail || '' }))))));
      detail.append(el('div', { style: { marginTop: '12px' } }, box));
    }
    // uncertainty / assumptions
    const ua = el('div', { class: 'cols cols-2', style: { marginTop: '12px' } });
    if ((d.uncertainty || []).length) ua.append(el('div', {}, el('h4', { class: 'small muted', text: t('diag.uncertainty') }), el('ul', { class: 'list small' }, d.uncertainty.map((u) => el('li', {}, infStatus('uncertain'), ' ', u)))));
    if ((d.assumptions || []).length) ua.append(el('div', {}, el('h4', { class: 'small muted', text: t('diag.assumptions') }), el('ul', { class: 'list small' }, d.assumptions.map((u) => el('li', {}, infStatus('assumed'), ' ', u)))));
    detail.append(ua);
    // decisions + pattern naming
    detail.append(el('div', { style: { marginTop: '12px' } }, decisionBar('diagnosis', d.id, { current: d.human_status, note: d.human_note, overrideFields: [{ key: 'cause_class', label: t('diag.cause'), type: 'select', options: ['process', 'sensor', 'data', 'mixed', 'unknown'], value: d.cause_class }, { key: 'fault_type', label: t('diag.faultType'), type: 'text', value: d.fault_type }], askContext: diagnosisContext(d), onDone: (a) => { d.human_status = { accept: 'accepted', question: 'questioned', override: 'overridden', dismiss: 'dismissed' }[a]; renderList(); } })));
    if (d.pattern_id) {
      const nameIn = el('input', { type: 'text', placeholder: t('diag.namePattern'), value: p && p.name ? p.name : '' });
      detail.append(el('div', { class: 'row', style: { marginTop: '10px' } }, el('span', { class: 'small muted', text: `${d.pattern_id}:` }), nameIn, el('button', { class: 'btn btn-sm', type: 'button', onClick: async () => { const name = nameIn.value.trim(); if (!name) return; const r = await postDecision('pattern', d.pattern_id, 'name_pattern', { newValue: { name } }); if (r.ok) { patterns[d.pattern_id] = { ...(patterns[d.pattern_id] || {}), name }; toast(t('diag.patternNamed', { name }), 'ok'); renderList(); show(d); } } }, t('common.save'))),
        p ? el('p', { class: 'small muted', text: `${p.description || ''} ${p.n_events ? `(${p.n_events} events, groups ${(p.groups_affected || []).join(', ')})` : ''} ${p.classifier_reliability ? `classifier reliability ${fmt.pct(p.classifier_reliability)}` : ''}` }) : null);
    }
    // flags behind it
    const fs = (d.flag_ids || []).map((id) => flagsById[id]).filter(Boolean);
    if (fs.length) detail.append(el('h4', { class: 'small muted', style: { marginTop: '12px' }, text: t('diag.flags') }), el('div', { class: 'sigchips' }, fs.map((f) => el('button', { class: 'btn btn-sm', type: 'button', onClick: () => openChat(flagContext(f)) }, kindChip(f.kind), ' ', f.id, ' ', sev(f.severity)))));
    const evBox = el('div', { style: { marginTop: '12px' } }, el('h4', { class: 'small muted', text: t('common.evidence') }));
    detail.append(evBox);
    evBox.append(evidenceList(await fetchEvidence(d.evidence_ids || [])));
  }
  renderList();
  const first = params.diagnosis ? diags.find((d) => d.id === params.diagnosis) : diags[0];
  if (first) show(first);
  const hh = hiddenHint(view); if (hh) view.append(hh);
  return view;
}
