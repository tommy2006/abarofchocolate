/* View 4: diagnoses — ranked list; detail as continuous prose: summary paragraphs, cause reasoning,
   ranked signals, propagation chain, numbered steps, critique, uncertainty, decisions, pattern naming.
   Every reference (flag, batch, signal, evidence) is a link; ?diag=DIAG-000001 opens one,
   ?pattern=PATTERN-A filters the list. */
import { state, t, el, clear, runApi, fmt, conf, sev, chip, kindChip, causeChip, viewHead, needRun, empty, evidenceButton, evidencePanel, decisionBar, postDecision, hiddenHint, roleAllows, infStatus, toast, unavailableNote, linkifyRefs, cleanText, prose, proseList, refLink, refChips, confWords, sevWords, addPlainBox, navigate } from '../core.js';
import { openChat, diagnosisContext, flagContext, setChatContext } from '../chat.js';
import { summaryCard, techDetails, techNested, itemBrief, bt } from '../brief.js';

const DIRS = ['up', 'down', 'noisy', 'stuck', 'shifted'];
function dirWord(d) { return d && DIRS.includes(d) ? t('plain.dir.' + d) : (d || ''); }

export async function render(main, params = {}) {
  // page = title, plain summary, the most important findings with their decision buttons, then ONE expander
  // ("Show technical analyses") that holds everything this view rendered before
  const page = el('div', { class: 'view' });
  main.append(page);
  page.append(viewHead('4', t('nav.diagnoses'), el('span', { class: 'small muted', text: t('diag.ranked') })));
  if (!state.run) { page.append(needRun()); return page; }
  const topHost = el('div', { class: 'brief-top' });
  const tech = techDetails('diagnoses');
  page.append(summaryCard('diagnoses'), topHost, tech);
  const view = tech.body;
  await addPlainBox(view, 'diagnoses');
  const [dg, fl, pt] = await Promise.all([runApi('/diagnoses'), runApi('/flags', { params: { limit: 5000 } }), runApi('/patterns')]);
  if (!dg.ok || !dg.data.available) { view.append(dg.unavailable ? unavailableNote(dg) : empty(t('diag.none'))); return view; }
  const flags = fl.ok ? fl.data.items || [] : [];
  const flagsById = Object.fromEntries(flags.map((f) => [f.id, f]));
  const patterns = Object.fromEntries((pt.ok ? pt.data.patterns || [] : []).map((p) => [p.id, p]));
  const sevOf = (d) => Math.max(0, ...(d.flag_ids || []).map((id) => (flagsById[id] ? flagsById[id].severity : 0)));
  const all = (dg.data.items || []).slice().sort((a, b) => sevOf(b) - sevOf(a) || b.confidence - a.confidence);
  const patternFilter = params.pattern || '';
  const diags = patternFilter ? all.filter((d) => d.pattern_id === patternFilter) : all;
  if (patternFilter) view.append(el('div', { class: 'filterbar row' }, el('span', {}, t('diag.filteredBy'), ' ', refLink('pattern', patternFilter), patterns[patternFilter] && patterns[patternFilter].name ? ` (${patterns[patternFilter].name})` : ''), el('span', { class: 'dim small', text: t('diag.nOfM', { n: diags.length, m: all.length }) }), el('button', { class: 'btn btn-sm btn-quiet', type: 'button', onClick: () => navigate('diagnoses') }, t('common.clearFilter'))));
  const grid = el('div', { class: 'cols cols-side' });
  view.append(grid);
  const list = el('div', { class: 'dlist' });
  const detail = el('div', { class: 'box detail' }, el('div', { class: 'empty', text: t('diag.pick') }));
  grid.append(list, detail);
  let selected = null;
  const STATUS_OF = { accept: 'accepted', question: 'questioned', override: 'overridden', dismiss: 'dismissed' };
  const decisions = (d) => decisionBar('diagnosis', d.id, { current: d.human_status, note: d.human_note, overrideFields: [{ key: 'cause_class', label: t('diag.cause'), type: 'select', options: ['process', 'sensor', 'data', 'mixed', 'unknown'], value: d.cause_class }, { key: 'fault_type', label: t('diag.faultType'), type: 'text', value: d.fault_type }], askContext: diagnosisContext(d), onDone: (a) => { d.human_status = STATUS_OF[a]; renderList(); } });
  const titleOf = (d) => { const p = d.pattern_id && patterns[d.pattern_id]; return p && p.name ? p.name : d.fault_type; };
  const renderList = () => {
    clear(list);
    if (!diags.length) list.append(empty(patternFilter ? t('diag.noneForPattern', { p: patternFilter }) : t('diag.none')));
    for (const d of diags) {
      const b = el('button', { type: 'button', class: `ditem c-${d.cause_class}` + (selected && selected.id === d.id ? ' sel' : ''), onClick: () => { show(d); navigate('diagnoses', { diag: d.id, pattern: patternFilter || undefined }); } });
      b.append(el('span', { class: 'title' }, titleOf(d), d.human_status ? el('span', { class: 'small', style: { marginLeft: '8px' } }, infStatus(null, { human: d.human_status })) : null), el('span', {}, sev(sevOf(d))),
        el('span', { class: 'sub' }, `${d.id} — ${t('common.group')} ${d.group_id ?? '–'} — `, causeChip(d.cause_class), ' ', conf(d.confidence)));
      list.append(b);
    }
  };
  function show(d) {
    selected = d; renderList(); clear(detail);
    setChatContext(diagnosisContext(d));
    const p = d.pattern_id && patterns[d.pattern_id];
    detail.append(el('div', { class: 'row between' }, el('h3', {}, p && p.name ? `${p.name} ` : '', el('span', { class: p && p.name ? 'dim' : '', text: d.fault_type })), el('div', { class: 'row' }, causeChip(d.cause_class), el('button', { class: 'btn btn-sm btn-primary', type: 'button', onClick: () => openChat(diagnosisContext(d)) }, t('common.ask')))));
    // what probably happened, how sure, where, what to check on site - and the decision - before any technical content
    detail.append(itemBrief(d.id, { ctx: diagnosisContext(d), decisionsShown: true }), el('div', { class: 'brief-decide' }, decisions(d)));
    const nested = techNested();
    detail.append(nested);
    const T = nested.body;
    T.append(el('div', { class: 'row small muted', style: { marginTop: '4px' } }, el('span', { text: d.id }), el('span', {}, `${t('common.group')} `, d.group_id !== null && d.group_id !== undefined ? refLink('group', String(d.group_id)) : '–'), d.pattern_id ? refLink('pattern', d.pattern_id) : null, d.narrative_source ? el('span', { text: `${t('common.source')}: ${d.narrative_source}` }) : null));
    // plain verdict line: what, how sure, how serious
    T.append(el('p', { class: 'verdictline' }, el('b', {}, t('diag.howSure')), ' ', confWords(d.confidence), ' · ', el('b', {}, t('common.severity')), ' ', sevWords(sevOf(d))));
    // summary as prose
    T.append(el('h4', { class: 'small muted', style: { marginTop: '12px' }, text: t('diag.summary') }), prose(d.summary || d.fault_type));
    // cause reasoning
    const whyKey = 'diag.why.' + (d.cause_class || 'unknown');
    T.append(el('h4', { class: 'small muted', text: t('diag.causeWhy') }), el('p', { class: 'prose' }, causeChip(d.cause_class), ' ', t(whyKey) === whyKey ? t('diag.why.unknown') : t(whyKey)));
    // ranked signals
    if ((d.ranked_signals || []).length) {
      T.append(el('h4', { class: 'small muted', text: t('diag.signals') }));
      T.append(el('div', {}, d.ranked_signals.map((s) => el('div', { class: 'meter', title: cleanText(s.explanation || '') }, el('span', {}, refLink('signal', s.signal), ' ', el('span', { class: 'dim small', text: dirWord(s.direction) })), el('span', { class: 'bar' }, el('i', { style: { width: Math.min(100, s.contribution * 100) + '%' } })), el('span', { class: 'right', text: fmt.pct(s.contribution) })))));
      T.append(el('p', { class: 'small dim', text: t('plain.shareHelp') }));
      const expl = d.ranked_signals.filter((s) => s.explanation).map((s) => ({ text: cleanText(s.explanation), ev: s.evidence_ids || [] }));
      if (expl.length) T.append(el('ul', { class: 'list small prose' }, expl.map((x) => el('li', {}, linkifyRefs(x.text), x.ev.length ? [' ', evidenceButton(x.ev, { label: t('common.evidence') })] : null))));
    }
    // propagation chain
    if ((d.propagation || []).length) {
      T.append(el('h4', { class: 'small muted', text: t('diag.propagation') }));
      const chain = el('div', { class: 'chain' });
      d.propagation.forEach((step, i) => {
        if (i === 0) chain.append(el('span', { class: 'node first' }, refLink('signal', step.from_signal)));
        chain.append(el('span', { class: 'edge', title: cleanText(step.explanation || '') }, el('span', { class: 'lag', text: step.lag !== null && step.lag !== undefined ? (step.lag === 0 ? t('diag.sameTime') : `+${step.lag} ${t('diag.lagUnit')}`) : '' }), el('span', { class: 'arrow' }), el('span', { text: `r ${fmt.num(step.strength, 2)}` })));
        chain.append(el('span', { class: 'node' }, refLink('signal', step.to_signal)));
      });
      T.append(chain);
      const sentences = d.propagation.map((s) => cleanText(s.explanation) || t('diag.propSentence', { a: s.from_signal, b: s.to_signal, lag: s.lag ?? 0 }));
      T.append(proseList(sentences, { cls: 'list small prose' }));
    }
    // steps as numbered full sentences
    if ((d.steps || []).length) T.append(el('h4', { class: 'small muted', text: t('diag.steps') }), proseList(d.steps, { ordered: true }));
    // critique
    if (d.critique) {
      const c = d.critique;
      const box = el('div', { class: 'critique ' + c.verdict });
      box.append(el('div', { class: 'row between' }, el('b', {}, t('diag.critique'), ': ', chip(t('diag.verdict.' + c.verdict), { supported: 'ok', weakened: 'warn', rejected: 'fail' }[c.verdict] || '')), el('span', { class: 'small muted' }, c.adjusted_confidence !== null && c.adjusted_confidence !== undefined ? el('span', {}, t('diag.adjusted') + ': ' + confWords(c.adjusted_confidence)) : null, ` — ${t('common.source')}: ${c.source || 'template'}`)));
      box.append(el('p', { class: 'small muted', style: { marginTop: '6px' }, text: t('diag.verdictHelp.' + c.verdict) === 'diag.verdictHelp.' + c.verdict ? '' : t('diag.verdictHelp.' + c.verdict) }));
      const obj = proseList(c.objections, { cls: 'list prose' });
      if (obj) box.append(el('div', { class: 'small muted', text: t('diag.objections') }), obj);
      if (roleAllows('engineer') && (c.checks || []).length) box.append(el('div', { class: 'small muted', style: { marginTop: '6px' }, text: t('diag.checks') }), el('div', {}, c.checks.map((ch) => el('div', { class: 'check-row' }, el('span', { style: { color: ch.passed ? 'var(--ok)' : 'var(--fail)' }, text: ch.passed ? '✓' : '✕' }), el('span', {}, el('b', { text: (ch.name || '').replace(/_/g, ' ') }), ' ', el('span', { class: 'muted' }, linkifyRefs(cleanText(ch.detail || ''))))))));
      T.append(el('div', { style: { marginTop: '12px' } }, box));
    }
    // uncertainty / assumptions
    const ua = el('div', { class: 'cols cols-2', style: { marginTop: '12px' } });
    const unc = proseList(d.uncertainty, { cls: 'list small prose', lead: () => infStatus('uncertain') });
    const asm = proseList(d.assumptions, { cls: 'list small prose', lead: () => infStatus('assumed') });
    if (unc) ua.append(el('div', {}, el('h4', { class: 'small muted', text: t('diag.uncertainty') }), unc));
    if (asm) ua.append(el('div', {}, el('h4', { class: 'small muted', text: t('diag.assumptions') }), asm));
    if (unc || asm) T.append(ua);
    // decisions + pattern naming
    if (d.pattern_id) {
      const nameIn = el('input', { type: 'text', placeholder: t('diag.namePattern'), value: p && p.name ? p.name : '' });
      T.append(el('div', { class: 'row', style: { marginTop: '10px' } }, el('span', { class: 'small muted' }, refLink('pattern', d.pattern_id), ':'), nameIn, el('button', { class: 'btn btn-sm', type: 'button', onClick: async () => { const name = nameIn.value.trim(); if (!name) return; const r = await postDecision('pattern', d.pattern_id, 'name_pattern', { newValue: { name } }); if (r.ok) { patterns[d.pattern_id] = { ...(patterns[d.pattern_id] || {}), name }; toast(t('diag.patternNamed', { name }), 'ok'); renderList(); show(d); } } }, t('common.save'))),
        p ? el('p', { class: 'small muted prose' }, linkifyRefs(cleanText(p.description || '')), p.n_events ? ` (${t('diag.patternEvents', { n: p.n_events })}${(p.groups_affected || []).length ? `, ${t('common.group').toLowerCase()} ${(p.groups_affected || []).join(', ')}` : ''})` : '', p.classifier_reliability ? ` ${t('diag.classifierReliability')}: ${confWords(p.classifier_reliability)}` : '') : null);
    }
    // flags behind it
    const fs = (d.flag_ids || []).map((id) => flagsById[id] || { id, missing: true });
    if (fs.length) T.append(el('h4', { class: 'small muted', style: { marginTop: '12px' }, text: t('diag.flags') }), el('ul', { class: 'list plain flaglist' }, fs.map((f) => el('li', { class: 'row' }, refLink('flag', f.id), f.missing ? null : [kindChip(f.kind), sev(f.severity), el('span', { class: 'small muted flagstmt' }, linkifyRefs(cleanText(f.statement || ''))), el('button', { class: 'btn btn-sm btn-quiet', type: 'button', onClick: () => openChat(flagContext(f)) }, t('common.ask'))]))));
    T.append(el('div', { style: { marginTop: '12px' } }, evidencePanel(d.evidence_ids || [])));
  }
  renderList();
  const topN = all.filter((d) => !(d.critique && d.critique.verdict === 'rejected')).slice(0, 3);
  if (topN.length) topHost.append(el('h2', { class: 'brief-top-title', text: bt('brief.topFindings') }), el('p', { class: 'small muted brief-top-help', text: bt('brief.topFindingsHelp') }),
    ...topN.map((d) => el('div', { class: 'brief-top-item', dataset: { diag: d.id } }, el('div', { class: 'brief-top-name small muted' }, refLink('diagnosis', d.id), ' · ', titleOf(d)), itemBrief(d.id, { ctx: diagnosisContext(d), decisionsShown: true }), el('div', { class: 'brief-decide' }, decisions(d)))));
  const want = params.diag || params.diagnosis;
  const first = want ? diags.find((d) => d.id === want) || all.find((d) => d.id === want) : diags[0];
  if (first) { show(first); if (want) detail.scrollIntoView({ block: 'start', behavior: 'smooth' }); }
  else if (want) detail.replaceChildren(empty(t('ref.notFound', { id: want })));
  const hh = hiddenHint(view); if (hh) view.append(hh);
  return view;
}
