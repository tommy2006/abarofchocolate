/* View 5: assessor — fitness score, learning curve, coverage, DQ scores, recommendations with
   apply-after-approval, assessor chat, candidate file evaluation. */
import { state, t, el, clear, runApi, fmt, conf, chip, section, viewHead, needRun, empty, evidenceButton, evChips, hiddenHint, meter, toast, errText, confirmDialog, actorName, actorRole, infStatus, unavailableNote, roleAllows } from '../core.js';
import { plot, purge, tokens, colorFor } from '../charts.js';

export async function render(main) {
  const view = el('div', { class: 'view' });
  main.append(view);
  view.append(viewHead('5', t('nav.assessor')));
  if (!state.run) { view.append(needRun()); return view; }
  const r = await runApi('/assessor');
  const a = r.ok ? r.data : {};
  const k = tokens();
  view._charts = [];
  if (!r.ok || !a.available) view.append(r.unavailable ? unavailableNote(r) : el('div', { class: 'notice', text: t('common.notYet') }));

  // ---- score + components + verdict
  const top = el('div', { class: 'cols cols-2' });
  view.append(top);
  const left = el('div', { class: 'stack' }, el('h2', { text: t('ass.title') }));
  if (a.score !== undefined) left.append(el('div', { class: 'row' }, el('span', { style: { fontSize: '40px', fontWeight: 600, lineHeight: 1 }, text: fmt.pct(a.score) }), el('span', { class: 'muted', text: t('ass.score') })));
  if (a.verdict) left.append(el('p', { text: a.verdict }));
  if (a.components) left.append(el('div', {}, Object.entries(a.components).map(([kk, v]) => meter(t('ass.comp.' + kk) === 'ass.comp.' + kk ? kk.replace(/_/g, ' ') : t('ass.comp.' + kk), v))));
  top.append(left);
  const wmd = a.would_more_data_help;
  const rightBox = el('div', { class: 'box stack' }, el('h3', { text: t('ass.moreData') }));
  if (wmd) rightBox.append(el('div', { class: 'row' }, el('b', { style: { fontSize: '18px' }, text: wmd.answer }), conf(wmd.confidence), wmd.expected_gain !== undefined ? chip(`${t('ass.expectedGain')} +${fmt.pct(wmd.expected_gain)}`, 'info') : null), el('p', { class: 'small', text: wmd.explanation || '' }), evChips(wmd.evidence_ids || []));
  else rightBox.append(empty());
  // assessor chat
  const q = el('input', { type: 'text', placeholder: t('ass.chatPlaceholder'), style: { flex: 1 } });
  const ans = el('div', { class: 'stack', style: { gap: '6px' } });
  rightBox.append(el('h4', { class: 'small muted', text: t('ass.chat') }), el('form', { class: 'row', onSubmit: async (e) => { e.preventDefault(); const question = q.value.trim(); if (!question) return; q.value = ''; ans.prepend(el('div', { class: 'msg user', text: question })); const rr = await runApi('/assessor/ask', { method: 'POST', body: { question, actor: `${actorName()}(${actorRole()})` } }); if (rr.ok) { const x = rr.data.answer; const m = el('div', { class: 'msg assistant', text: x.text }); m.append(el('span', { class: 'src', text: x.source === 'template' ? t('chat.source.template') : `${t('chat.source.local')} ${x.model || ''}` }), evChips(x.evidence_ids || [])); ans.prepend(m); } else ans.prepend(el('div', { class: 'msg assistant', text: errText(rr) })); } }, q, el('button', { class: 'btn btn-primary', type: 'submit' }, t('common.send'))), ans);
  top.append(rightBox);

  // ---- charts
  const charts = el('div', { class: 'cols cols-3' });
  const cs = section(t('ass.charts'));
  view.append(cs.root);
  cs.body.append(charts);
  const lc = el('div', { class: 'chart short' }), cov = el('div', { class: 'chart short' }), dq = el('div', { class: 'chart short' });
  charts.append(el('div', {}, el('h3', { class: 'small muted', text: t('ass.learningCurve') }), lc), el('div', {}, el('h3', { class: 'small muted', text: t('ass.coverage') }), cov), el('div', {}, el('h3', { class: 'small muted', text: t('ass.dq') }), dq));
  requestAnimationFrame(() => {
    if ((a.learning_curve || []).length) plot(lc, [{ type: 'scatter', mode: 'lines+markers', x: a.learning_curve.map((p) => p.fraction), y: a.learning_curve.map((p) => p.score), error_y: { type: 'data', array: a.learning_curve.map((p) => p.std || 0), color: k.ink3 }, line: { color: k.accent }, marker: { size: 7 }, hovertemplate: '%{x:.0%} of data: %{y:.2f}<extra></extra>' }], { height: 220, xaxis: { tickformat: '.0%', title: { text: 'share of groups' } }, yaxis: { title: { text: 'stability' }, rangemode: 'tozero' }, showlegend: false, hovermode: 'closest' });
    else lc.replaceChildren(empty());
    if ((a.coverage_by_regime || []).length) plot(cov, [{ type: 'bar', x: a.coverage_by_regime.map((p) => p.regime), y: a.coverage_by_regime.map((p) => p.coverage), marker: { color: a.coverage_by_regime.map((p) => (p.coverage < 0.5 ? k.warn : k.accent)) }, text: a.coverage_by_regime.map((p) => `${p.n_groups} groups`), textposition: 'auto', hovertemplate: '%{x}: %{y:.0%}<extra></extra>' }], { height: 220, yaxis: { tickformat: '.0%', range: [0, 1] }, showlegend: false, hovermode: 'closest' });
    else cov.replaceChildren(empty());
    if (a.dq_scores) plot(dq, [{ type: 'bar', orientation: 'h', y: Object.keys(a.dq_scores), x: Object.values(a.dq_scores), marker: { color: Object.values(a.dq_scores).map((v) => (v < 0.7 ? k.warn : k.accent)) }, hovertemplate: '%{y}: %{x:.0%}<extra></extra>' }], { height: 220, xaxis: { tickformat: '.0%', range: [0, 1] }, yaxis: { tickmode: 'array', tickvals: Object.keys(a.dq_scores), ticktext: Object.keys(a.dq_scores), automargin: true }, bargap: 0.35, margin: { l: 90 }, showlegend: false, hovermode: 'closest' });
    else dq.replaceChildren(empty());
  });
  view._charts.push(lc, cov, dq);

  // ---- recommendations
  const rs = section(t('ass.recommendations'));
  view.append(rs.root);
  const recs = a.recommendations || [];
  if (!recs.length) rs.body.append(empty());
  for (const rec of recs) {
    const row = el('div', { class: 'rec' });
    const status = el('span', { class: 'small' });
    const setStatus = () => { clear(status); if (rec.status === 'approved') status.append(infStatus(null, { human: 'accepted' }), ' ', t('ass.applied')); else if (rec.status === 'dismissed') status.append(infStatus(null, { human: 'dismissed' })); };
    setStatus();
    row.append(el('div', {}, el('div', { class: 'act' }, el('span', { class: 'dim small', text: rec.id + ' ' }), rec.action), el('div', { class: 'why', text: rec.rationale || '' }), el('div', { class: 'row small muted', style: { marginTop: '4px' } }, conf(rec.confidence), rec.expected_gain !== undefined ? chip(`${t('ass.expectedGain')} +${fmt.pct(rec.expected_gain)}`) : null, evidenceButton(rec.evidence_ids || []), status)),
      el('div', { class: 'decisions' }, rec.applicable === false ? el('span', { class: 'small muted', text: t('ass.notApplicable') }) : el('button', { class: 'btn btn-sm btn-accept', type: 'button', disabled: rec.status === 'approved', onClick: async () => { if (!(await confirmDialog(t('ass.applyConfirm', { action: rec.action })))) return; const rr = await runApi('/assessor/apply', { method: 'POST', body: { action: rec.id, actor_name: actorName(), role: actorRole() } }); if (rr.ok) { rec.status = 'approved'; setStatus(); toast(t('decision.recorded', { seq: rr.data.log_seq }), 'ok'); } else toast(errText(rr), 'fail'); } }, t('ass.applyAfterApproval'))));
    rs.body.append(row);
  }
  if (roleAllows('engineer') && (a.experiments || []).length) rs.body.append(el('h4', { class: 'small muted', style: { marginTop: '12px' }, text: t('ass.experiments') }), el('ul', { class: 'list small' }, a.experiments.map((e) => el('li', { text: `${e.id}: ${e.question} — ${e.result} (${fmt.sec(e.seconds)})` }))));

  // ---- upload candidate
  const up = section(t('ass.upload'), { level: 'engineer' });
  view.append(up.root);
  const fi = el('input', { type: 'file' });
  const out = el('div');
  up.body.append(el('p', { class: 'hint', text: t('ass.uploadHelp') }), el('div', { class: 'row' }, fi, el('button', { class: 'btn btn-sm', type: 'button', onClick: async () => { if (!fi.files[0]) return; const fd = new FormData(); fd.append('file', fi.files[0]); clear(out); out.append(el('span', { class: 'dim', text: t('common.loading') + '…' })); const rr = await runApi('/assessor/upload', { method: 'POST', form: fd }); clear(out); out.append(rr.ok ? el('pre', { class: 'small', style: { whiteSpace: 'pre-wrap' }, text: JSON.stringify(rr.data.result, null, 1) }) : unavailableNote(rr)); } }, t('common.send'))), out);
  const hh = hiddenHint(view); if (hh) view.append(hh);
  view.cleanup = () => view._charts.forEach(purge);
  return view;
}
