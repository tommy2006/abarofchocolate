/* View 4: diagnoses. Round 5 (agent A): findings by likely cause (donut) and by group (bars), a timeline of the
   findings, every finding as a Problem -> Reason -> Answer strip with its accept / question / override bar, and the
   ranked list as a compact table (details on click). The detail keeps its prose: summary paragraphs, cause reasoning,
   ranked signals, propagation chain, numbered steps, critique, uncertainty, decisions, pattern naming - under
   "Show technical analyses". Basic mode: the donut and three strips only.
   Every reference (flag, batch, signal, evidence) is a link; ?diag=DIAG-000001 opens one, ?pattern=PATTERN-A filters. */
import { state, t, el, clear, runApi, cachedRunApi, fmt, conf, sev, chip, kindChip, causeChip, viewHead, needRun, empty, evidenceButton, evidencePanel, decisionBar, postDecision, hiddenHint, roleAllows, infStatus, toast, unavailableNote, linkifyRefs, cleanText, prose, proseList, refLink, refChips, rowsLink, confWords, sevWords, addPlainBox, navigate, table } from '../core.js';
import { openChat, diagnosisContext, flagContext, setChatContext } from '../chat.js';
import { summaryCard, techDetails, techNested, itemBrief, bt } from '../brief.js';
import { vt, vizBox, chartNode, praStrip, fetchItemBrief, donut, hbar, kindOfSignal, sensorKindColor, sensorKindLegend, timeline, binnedTimeline, causeColors, CAUSE_ORDER, briefActionButtons } from '../charts.js';

const DIRS = ['up', 'down', 'noisy', 'stuck', 'shifted'];
function dirWord(d) { return d && DIRS.includes(d) ? t('plain.dir.' + d) : (d || ''); }
// English fallbacks of this page's own keys (the i18n files are edited by several people at once)
const FB = {
  'diag.viz.causeHelp': 'How many findings have each likely cause. The number in the middle is the total.',
  'diag.viz.groupHelp': 'The groups with the most findings; click a bar to show only that group in the list.',
  'diag.viz.otherGroups': 'other groups', 'diag.filterGroup': 'Only group {g}', 'diag.col.what': 'What', 'diag.table.help': 'Click a row to open the finding above.',
  'diag.viz.sensor': 'Sensors named in the most findings', 'diag.viz.sensorHelp': 'The sensor each finding points at first; colour = what the sensor probably measures. Click a bar to see what the system knows about that sensor.',
  'diag.viz.binnedHelp': 'Each bar counts the findings in one stretch of the data: further right = later, taller = more findings, colour = likely cause. Click a bar to see those rows.',
  'brief.topFindingsHelpBasic': 'Accept, question or override each finding right here.',
};
const MANY = 300;   // more findings than this: the timeline counts them per stretch of the data instead of one dot each
const td = (k, vars) => { let v = t(k, vars); if (!v || v === k) { v = FB[k] || k; for (const [a, b] of Object.entries(vars || {})) v = v.replaceAll(`{${a}}`, String(b)); } return v; };
const causeOf = (d) => (CAUSE_ORDER.includes(d.cause_class) ? d.cause_class : 'unknown');
const causeWord = (c) => vt('diag.cause.' + c);

export async function render(main, params = {}) {
  // page = title, plain summary, the diagrams, the most important findings as strips with their decision buttons,
  // then ONE expander ("Show technical analyses") that holds everything this view rendered before
  const page = el('div', { class: 'view' });
  main.append(page);
  page.append(viewHead('4', t('nav.diagnoses'), el('span', { class: 'small muted', text: t('diag.ranked') })));
  if (!state.run) { page.append(needRun()); return page; }
  const plain = el('div', { class: 'viz-host', dataset: { view: 'diagnoses' } });
  const topHost = el('div', { class: 'brief-top' });
  const tech = techDetails('diagnoses');
  page.append(summaryCard('diagnoses'), plain, topHost, tech);
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
  const full = roleAllows('operator');
  let groupFilter = '';
  if (patternFilter) view.append(el('div', { class: 'filterbar row' }, el('span', {}, t('diag.filteredBy'), ' ', refLink('pattern', patternFilter), patterns[patternFilter] && patterns[patternFilter].name ? ` (${patterns[patternFilter].name})` : ''), el('span', { class: 'dim small', text: t('diag.nOfM', { n: diags.length, m: all.length }) }), el('button', { class: 'btn btn-sm btn-quiet', type: 'button', onClick: () => navigate('diagnoses') }, t('common.clearFilter'))));
  const grid = el('div', { class: 'cols cols-side' });
  view.append(grid);
  const list = el('div', { class: 'dlist diag-table' });
  const filterChip = el('div', { class: 'diag-filter' });
  const detail = el('div', { class: 'box detail' }, el('div', { class: 'empty', text: t('diag.pick') }));
  grid.append(el('div', {}, el('div', { class: 'diag-list-head' }, el('h3', { text: vt('diag.table.title') }), el('span', { class: 'small muted', text: td('diag.table.help') })), filterChip, list), detail);
  let selected = null; let listTable = null;
  const STATUS_OF = { accept: 'accepted', question: 'questioned', override: 'overridden', dismiss: 'dismissed' };
  const decisions = (d) => decisionBar('diagnosis', d.id, { current: d.human_status, note: d.human_note, overrideFields: [{ key: 'cause_class', label: t('diag.cause'), type: 'select', options: ['process', 'sensor', 'data', 'mixed', 'unknown'], value: d.cause_class }, { key: 'fault_type', label: t('diag.faultType'), type: 'text', value: d.fault_type }], askContext: diagnosisContext(d), onDone: (a) => { d.human_status = STATUS_OF[a]; renderList(); } });
  const titleOf = (d) => { const p = d.pattern_id && patterns[d.pattern_id]; return p && p.name ? p.name : d.fault_type; };
  /** The strongest flag with rows behind a finding: where it is on the timeline. */
  const placeOf = (d) => { const fs = (d.flag_ids || []).map((id) => flagsById[id]).filter((f) => f && f.row_start !== undefined && f.row_start !== null); return fs.length ? fs.reduce((a, b) => (b.severity > a.severity ? b : a)) : null; };
  const whereOf = (d) => {
    const p = placeOf(d); const sigs = (d.ranked_signals || []).slice(0, 3).map((s) => s.signal);
    const grp = d.group_id !== null && d.group_id !== undefined ? [t('common.group'), ' ', refLink('group', String(d.group_id))] : null;
    if (!p) return grp;
    return [t('mon.rows'), ' ', rowsLink(p.row_start, p.row_end, { signals: sigs }), grp ? [' · ', grp] : null, p.batch_id ? [' · ', refLink('batch', p.batch_id)] : null];
  };
  const open = (d) => { show(d); navigate('diagnoses', { diag: d.id, pattern: patternFilter || undefined }); };

  // ---- Problem -> Reason -> Answer of one finding (+ its decision bar); the item brief when the server has no strip data
  const stripOf = (d, b, { label } = {}) => {
    const pts = b.points || []; const wherePt = pts.find((p) => /^(Where|Missä|Var)\b/.test(p)); const rest = pts.filter((p) => p !== wherePt);
    const sigs = (d.ranked_signals || []).slice(0, 3);
    const w = whereOf(d);
    return praStrip({ verdict: b.verdict, problem: linkifyRefs(b.headline), where: [wherePt ? el('div', { text: wherePt }) : null, w ? el('div', {}, w) : null],
      reason: b.why ? linkifyRefs(b.why) : (rest[0] || cleanText(d.summary || '')),
      reasonMore: [rest.map((p) => el('div', { text: p })), el('div', { class: 'diag-strip-meta' }, causeChip(d.cause_class), sigs.length ? refChips('signal', sigs.map((s) => s.signal), { max: 3 }) : null, el('span', { class: 'dim small', text: confWords(d.confidence) }))],
      fix: b.fix || [], use: b.can_use_rows, extra: [briefActionButtons(b.actions, diagnosisContext(d), { skipRef: d.id }), el('div', { class: 'brief-decide' }, decisions(d))], label });
  };
  const stripHost = (d, opts) => {
    const host = el('div', { class: 'pra-host', dataset: { diag: d.id } }, el('div', { class: 'dim small', text: vt('adv.loading') + '…' }));
    (async () => {
      const b = await fetchItemBrief(d.id);
      if (b) host.replaceChildren(stripOf(d, b, opts));
      else host.replaceChildren(itemBrief(d.id, { ctx: diagnosisContext(d), decisionsShown: true }), el('div', { class: 'brief-decide' }, decisions(d)));   // older server
    })();
    return host;
  };

  // ---- diagrams instead of header counts: by cause (donut), by group (bars), when (timeline); basic mode: the donut only
  if (all.length) {
    const cc = causeColors();
    const counts = Object.fromEntries(CAUSE_ORDER.map((c) => [c, 0])); for (const d of all) counts[causeOf(d)]++;
    const present = CAUSE_ORDER.filter((c) => counts[c]);
    // in the page before anything is drawn: a chart drawn into a detached box takes a default width and spills out
    const row = el('div', { class: 'viz-grid' });
    plain.append(row);
    const causeNode = chartNode('short');
    row.append(vizBox(vt('diag.viz.cause'), td('diag.viz.causeHelp'), causeNode));
    donut(causeNode, present.map(causeWord), present.map((c) => counts[c]), present.map((c) => cc[c]), { height: 250, center: vt('diag.viz.findings') });
    if (full) {
      const gc = new Map(); for (const d of all) { const g = d.group_id === null || d.group_id === undefined ? '–' : String(d.group_id); gc.set(g, (gc.get(g) || 0) + 1); }
      const topG = [...gc.entries()].sort((a, b) => b[1] - a[1]).slice(0, 12);
      const inTop = topG.reduce((a, b) => a + b[1], 0);
      // groups only tell something when the findings pile up in a few of them; spread over thousands of groups, the
      // sensor each finding points at is the better picture
      if (gc.size > 1 && topG[0][1] >= 3 && inTop >= 0.25 * all.length) {
        const rest = all.length - inTop;
        const gNode = chartNode('short');
        row.append(vizBox(vt('diag.viz.group'), td('diag.viz.groupHelp'), gNode));
        hbar(gNode, [...topG.map(([g]) => `${t('common.group')} ${g}`), ...(rest ? [td('diag.viz.otherGroups')] : [])], [...topG.map(([, n]) => n), ...(rest ? [rest] : [])], { height: 250, xtitle: vt('diag.viz.findings'), onClick: (label, i) => { const hit = topG[i]; if (hit) { groupFilter = groupFilter === hit[0] ? '' : hit[0]; renderList(); } } });
      } else {
        const sc = new Map(); for (const d of all) { const s = ((d.ranked_signals || [])[0] || {}).signal; if (s) sc.set(s, (sc.get(s) || 0) + 1); }
        const topS = [...sc.entries()].sort((a, b) => b[1] - a[1]).slice(0, 12);
        if (topS.length > 1) {
          const sNode = chartNode('short');
          const sg = await cachedRunApi('signals', '/signals');
          const byId = Object.fromEntries(((sg.ok && sg.data.signals) || []).map((s) => [s.id, s]));
          const name = (id) => { const s = byId[id]; const n = (state.signalNames && state.signalNames[id]) || (s && (s.display_name || s.source_column)); return n && n !== id ? `${n} (${id})` : id; };
          row.append(vizBox(td('diag.viz.sensor'), td('diag.viz.sensorHelp'), sNode));
          const kinds = topS.map(([id]) => kindOfSignal(byId[id]));   // each bar in the colour of what the sensor probably measures
          hbar(sNode, topS.map(([id]) => name(id)), topS.map(([, n]) => n), { colors: kinds.map(sensorKindColor), height: 250, xtitle: vt('diag.viz.findings'), onClick: (label, i) => { const hit = topS[i]; if (hit) navigate('understanding', { signal: hit[0] }); } });
          sensorKindLegend(sNode, kinds);
        }
      }
    }
    if (full) {
      const legend = Object.fromEntries(CAUSE_ORDER.map((c) => [c, causeWord(c)]));
      const items = all.map((d) => { const p = placeOf(d); if (!p) return null; const c = causeOf(d); const s = sevOf(d); return { x: p.row_start, y: Math.round(s * 100), series: c, color: cc[c], size: 7 + 10 * s, id: d.id, text: `<b>${d.id}</b>: ${titleOf(d)}<br>${causeWord(c)} · ${t('common.group')} ${d.group_id ?? '–'} · ${t('mon.rows')} ${p.row_start}–${p.row_end}` }; }).filter(Boolean);
      if (items.length) {
        const tNode = chartNode();
        const many = items.length > MANY;
        plain.append(vizBox(vt('diag.viz.timeline'), many ? td('diag.viz.binnedHelp') : vt('diag.viz.timelineHelp'), tNode));
        if (many) binnedTimeline(tNode, items, { order: CAUSE_ORDER, colors: cc, legend, xtitle: vt('diag.viz.row'), ytitle: vt('diag.viz.findings'), onClick: (a, b) => navigate('monitor', { rows: `${a}-${b}` }) });
        else timeline(tNode, items, { height: 300, xtitle: vt('diag.viz.row'), ytitle: vt('diag.viz.serious'), legend, onClick: (id) => { const d = all.find((x) => x.id === id); if (d) open(d); } });
      }
    }
  }

  // ---- the list: a compact table (paged), filtered by pattern (?pattern=) and by a clicked group
  const renderList = () => {
    clear(list); clear(filterChip);
    const rows = groupFilter ? diags.filter((d) => String(d.group_id) === groupFilter) : diags;
    if (groupFilter) filterChip.append(el('span', { class: 'chip warn click', role: 'button', tabindex: '0', onClick: () => { groupFilter = ''; renderList(); }, onKeydown: (e) => { if (e.key === 'Enter') { groupFilter = ''; renderList(); } } }, td('diag.filterGroup', { g: groupFilter }), ' ✕'));
    if (!rows.length) { list.append(empty(patternFilter ? t('diag.noneForPattern', { p: patternFilter }) : t('diag.none'))); return; }
    listTable = table({
      columns: [
        { label: '', render: (d) => refLink('diagnosis', d.id, d.id.replace('DIAG-', '')) },
        { label: td('diag.col.what'), cls: 'wrap', render: (d) => el('span', {}, el('b', { text: titleOf(d) }), d.human_status ? el('span', { class: 'small', style: { marginLeft: '6px' } }, infStatus(null, { human: d.human_status })) : null) },
        { label: t('diag.cause'), render: (d) => causeChip(d.cause_class) },
        { label: t('common.group'), render: (d) => (d.group_id !== null && d.group_id !== undefined ? refLink('group', String(d.group_id)) : '–') },
        { label: t('common.severity'), render: (d) => sev(sevOf(d)) },
        { label: t('common.confidence'), render: (d) => conf(d.confidence) },
      ],
      rows, pageSize: 12, keyOf: (d) => d.id, selectedKey: selected && selected.id, onRow: (d) => open(d), emptyText: t('diag.none'),
    });
    list.append(listTable);
    if (selected) listTable.reveal(selected.id);
  };
  function show(d) {
    selected = d; renderList(); clear(detail);
    setChatContext(diagnosisContext(d));
    const p = d.pattern_id && patterns[d.pattern_id];
    detail.append(el('div', { class: 'row between' }, el('h3', {}, p && p.name ? `${p.name} ` : '', el('span', { class: p && p.name ? 'dim' : '', text: d.fault_type })), el('div', { class: 'row' }, causeChip(d.cause_class), el('button', { class: 'btn btn-sm btn-primary', type: 'button', onClick: () => openChat(diagnosisContext(d)) }, t('common.ask')))));
    // problem -> reason -> answer, and the decision, before any technical content
    detail.append(stripHost(d));
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
    if (fs.length) T.append(el('h4', { class: 'small muted', style: { marginTop: '12px' }, text: t('diag.flags') }), el('ul', { class: 'list plain flaglist' }, fs.slice(0, 40).map((f) => el('li', { class: 'row' }, refLink('flag', f.id), f.missing ? null : [kindChip(f.kind), sev(f.severity), el('span', { class: 'small muted flagstmt' }, linkifyRefs(cleanText(f.statement || ''))), el('button', { class: 'btn btn-sm btn-quiet', type: 'button', onClick: () => openChat(flagContext(f)) }, t('common.ask'))])), fs.length > 40 ? el('li', { class: 'dim small', text: `+${fs.length - 40}` }) : null));
    T.append(el('div', { style: { marginTop: '12px' } }, evidencePanel(d.evidence_ids || [])));
  }
  renderList();
  // the most important findings, as strips with their decision bar, above the expander (never a rejected one)
  // (with a pattern asked for, the most important findings of that pattern)
  const topN = diags.filter((d) => !(d.critique && d.critique.verdict === 'rejected')).slice(0, 3);
  if (topN.length) topHost.append(el('h2', { class: 'brief-top-title', text: bt('brief.topFindings') }), el('p', { class: 'small muted brief-top-help', text: (full ? bt('brief.topFindingsHelp') : td('brief.topFindingsHelpBasic')) + ' ' + vt('adv.topHelp') }),
    ...topN.map((d) => el('div', { class: 'brief-top-item', dataset: { diag: d.id } }, el('div', { class: 'brief-top-name small muted' }, refLink('diagnosis', d.id), ' · ', titleOf(d)), stripHost(d))));
  const want = params.diag || params.diagnosis;
  const first = want ? diags.find((d) => d.id === want) || all.find((d) => d.id === want) : diags[0];
  if (first) { show(first); if (want) detail.scrollIntoView({ block: 'start', behavior: 'smooth' }); }
  else if (want) detail.replaceChildren(empty(t('ref.notFound', { id: want })));
  const hh = hiddenHint(view); if (hh) view.append(hh);
  return view;
}
