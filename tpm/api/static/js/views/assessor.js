/* View 5: assessor — everything is visible on load: verdict cards (more data? less data?), combined
   score with its three components, learning curve, coverage by regime with thin regimes named,
   data-quality scores per category, recommendations with evidence links and apply-after-approval;
   the question box comes last. Reads the real assessor.json shape (combined_score, fitness, coverage,
   dq_scores, more_data_verdict, less_data_verdict, recommendations) and the older fixture shape. */
import { state, t, el, clear, runApi, fmt, conf, chip, section, viewHead, needRun, empty, evidenceButton, evChips, hiddenHint, meter, toast, errText, confirmDialog, actorName, actorRole, infStatus, unavailableNote, roleAllows, linkifyRefs, cleanText, prose, proseList, refLink, refChips, confWords, addPlainBox, kv, notice } from '../core.js';
import { plot, purge, tokens } from '../charts.js';

function metricName(m) { const k = 'ass.metric.' + m; return t(k) === k ? String(m || '').replace(/_/g, ' ') : t(k); }
function nGroups(n) { return Number(n) === 1 ? t('ass.oneGroup') : t('ass.nGroups', { n }); }
function actionLabel(a) {
  if (!a) return '';
  if (typeof a === 'string') return a;
  const k = 'ass.action.' + a.type;
  const base = t(k) === k ? String(a.type || '').replace(/_/g, ' ') : t(k);
  const p = a.params || {};
  const parts = Object.entries(p).filter(([, v]) => v !== null && v !== undefined && v !== '').map(([kk, v]) => `${kk} ${typeof v === 'object' ? JSON.stringify(v) : v}`);
  return parts.length ? `${base} (${parts.join(', ')})` : base;
}
/* The verdict word (Yes / No / Unclear) is shown once, as the headline of a card, the chip of an evaluation or
   the lead of a chat answer. The generated reasons used to repeat it ("Yes" + "Yes: 5 duplicate rows ...",
   "Removing bad data helps: Yes: ..."); reason() returns the reason as a sentence of its own. */
const LEAD_RE = /^\s*(?:yes|no|unclear|uncertain|kyllä|ei|epäselvä|ja|nej|oklart)\s*[:.,;!–—-]\s*/i;
const ECHO_RE = /^\s*(?:removing bad data helps|adding more data helps|more data helps|less data helps)\s*[:.]\s*/i;
const MID_RE = /([:.;]\s+)(?:yes|no|unclear|uncertain|kyllä|ei|epäselvä|ja|nej|oklart)\s*:\s*(\S)/gi;
const capFirst = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);
export function reason(text) {
  let s = cleanText(text || '').replace(ECHO_RE, '');
  for (let prev = null; prev !== s;) { prev = s; s = s.replace(LEAD_RE, ''); }
  s = s.replace(MID_RE, (m, sep, ch) => sep + (sep.trim() === '.' ? ch.toUpperCase() : ch));
  return capFirst(s.trim());
}
/** Chat answers keep one lead word: "Yes. Yes: 5 rows ..." -> "Yes. 5 rows ...". */
export function singleLead(text) {
  const s = cleanText(text || '');
  const m = s.match(LEAD_RE);
  if (!m) return s;
  const rest = reason(s.slice(m[0].length));
  return rest ? m[0].trim().replace(/[:,;!–—-]$/, '.') + ' ' + rest : s;
}
/** The summary line repeats both verdicts ("More data: yes. Less data: yes."); the cards above already say it. */
function summaryWithoutVerdicts(text) { return cleanText(text || '').replace(/\b(?:More|Less) data:\s*(?:yes|no|unclear)\.\s*/gi, '').trim(); }

/** One view-model for the real assessor.json and the older fixture shape. */
function normalize(a) {
  const fit = a.fitness || {}; const cov = a.coverage || {}; const dq = a.dq_scores || {};
  const primary = fit.primary_metric || 'stability';
  const curve = (fit.curve || a.learning_curve || []).map((p) => ({ fraction: p.fraction, y: p.primary ?? p.score ?? (p.metrics ? p.metrics[primary] : null), err: p.uncertainty ?? p.std ?? 0, n: p.n_train_units ?? p.n_groups ?? null, metrics: p.metrics || null }));
  const regimes = (cov.regimes || a.coverage_by_regime || []).map((r) => ({ id: r.regime_id || r.regime, n: r.n_units ?? r.n_groups ?? null, share: r.share ?? r.coverage ?? null, thin: r.thin !== undefined ? r.thin : (r.coverage !== undefined && r.coverage < 0.5), rows: r.n_rows ?? r.rows ?? null, distinguishing: r.distinguishing || [] }));
  const thin = (cov.thin_regimes || []).map((x) => (typeof x === 'string' ? x : x && (x.regime_id || x.regime))).filter(Boolean);
  const dqCats = ['completeness', 'validity', 'consistency', 'timeliness'].filter((k) => dq[k] !== undefined && dq[k] !== null).map((k) => ({ key: k, score: dq[k], detail: (dq.details || {})[k] || null }));
  const hasReal = a.combined_score !== undefined;
  const score = hasReal ? a.combined_score : a.score;
  const w = a.weights || {};
  const components = hasReal
    ? [{ key: 'fitness', v: fit.fitness_score, w: w.fitness }, { key: 'coverage', v: cov.coverage_score, w: w.coverage }, { key: 'data_quality', v: dq.overall, w: w.data_quality }].filter((c) => c.v !== undefined && c.v !== null)
    : Object.entries(a.components || {}).map(([key, v]) => ({ key, v }));
  const wmd = a.would_more_data_help;
  const more = a.more_data_verdict ? { ...a.more_data_verdict, metric: primary } : wmd ? { would_help: /^y/i.test(wmd.answer || '') ? true : /^n/i.test(wmd.answer || '') ? false : null, why: wmd.explanation, estimated_gain: wmd.expected_gain, evidence_ids: wmd.evidence_ids || [], confidence: wmd.confidence, metric: primary } : null;
  const less = a.less_data_verdict ? { ...a.less_data_verdict, metric: 'overall' } : null;
  const recs = (a.recommendations || []).map((r) => ({ id: r.id, text: reason(r.text || r.rationale || ''), rationale: r.text ? reason(r.rationale || '') : '', action: r.action, actionLabel: actionLabel(r.action), effect: r.expected_effect || null, gain: r.expected_gain, confidence: r.confidence, evidence_ids: r.evidence_ids || [], status: r.status, applicable: r.applicable }));
  return { score, components, curve, fit, primary, regimes, thin, cov, dq, dqCats, more, less, recs, summary: a.summary || a.verdict || '', experiments: a.experiments || [], evaluations: a.evaluations || [], hasReal };
}
function verdictCard(title, v, { gainLabel } = {}) {
  const box = el('div', { class: 'box verdict' }, el('h3', { text: title }));
  if (!v) { box.append(empty(t('common.notYet'))); return box; }
  const yes = v.would_help === true ? 'yes' : v.would_help === false ? 'no' : 'unclear';
  box.append(el('div', { class: 'row' }, el('span', { class: 'answer ' + yes, text: t('ass.answer.' + yes) }), v.estimated_gain !== undefined && v.estimated_gain !== null ? chip(`${gainLabel || t('ass.expectedGain')} ${fmt.pp(v.estimated_gain)}`, v.estimated_gain > 0 ? 'ok' : '', { title: t('ass.gainHelp', { n: (Number(v.estimated_gain) * 100).toFixed(1), metric: metricName(v.metric) }) }) : null, v.confidence !== undefined && v.confidence !== null ? conf(v.confidence, { words: true }) : null));
  const why = reason(v.why);
  if (why) box.append(el('p', { class: 'prose' }, linkifyRefs(why)));
  if (v.estimated_gain !== undefined && v.estimated_gain !== null) box.append(el('p', { class: 'small muted', text: t('ass.gainHelp', { n: (Number(v.estimated_gain) * 100).toFixed(1), metric: metricName(v.metric) }) }));
  if ((v.evidence_ids || []).length) box.append(el('div', { class: 'row small' }, el('span', { class: 'dim', text: t('common.evidence') + ':' }), evChips(v.evidence_ids)));
  return box;
}
function effectSummary(eff) {
  if (!eff) return null;
  const parts = [];
  const dqs = eff.dq_scores || {};
  for (const [k, v] of Object.entries(dqs)) if (v && typeof v === 'object' && v.delta) parts.push(`${metricName(k)} ${fmt.num(v.before, 2)} → ${fmt.num(v.after, 2)} (${fmt.pp(v.delta)})`);
  if (eff.rows_removed) parts.push(t('ass.rowsRemoved', { n: fmt.int(eff.rows_removed), pct: fmt.pct(eff.fraction || 0, 2) }));
  if (eff.estimated_gain !== undefined) parts.push(`${t('ass.expectedGain')} ${fmt.pp(eff.estimated_gain)}`);
  if ((eff.batches || []).length) parts.push(`${t('common.batch').toLowerCase()} ${eff.batches.slice(0, 6).join(', ')}`);
  return parts.length ? el('div', { class: 'small muted effect' }, t('ass.expectedEffect') + ': ', linkifyRefs(parts.join('; '))) : null;
}

export async function render(main) {
  const view = el('div', { class: 'view' });
  main.append(view);
  view.append(viewHead('5', t('nav.assessor'), el('span', { class: 'small muted', text: t('ass.title') })));
  if (!state.run) { view.append(needRun()); return view; }
  await addPlainBox(view, 'assessor');
  const r = await runApi('/assessor');
  const raw = r.ok ? r.data : {};
  const k = tokens();
  view._charts = [];
  if (!r.ok || !raw.available) { view.append(r.unavailable ? unavailableNote(r) : el('div', { class: 'notice', text: t('common.notYet') })); }
  const a = normalize(raw);

  // ---- headline: verdict cards + combined score
  const head = el('div', { class: 'cols cols-3 verdicts' });
  view.append(head);
  head.append(verdictCard(t('ass.moreData'), a.more), verdictCard(t('ass.lessData'), a.less));
  const scoreBox = el('div', { class: 'box verdict' }, el('h3', { text: a.hasReal ? t('ass.combined') : t('ass.score') }));
  if (a.score !== undefined && a.score !== null) {
    scoreBox.append(el('div', { class: 'row' }, el('span', { class: 'bignum', text: fmt.pct(a.score) }), el('span', { class: 'small muted', text: t('ass.scoreHelp') })));
    scoreBox.append(el('div', { class: 'comps' }, a.components.map((c) => meter(t('ass.comp.' + c.key) === 'ass.comp.' + c.key ? c.key.replace(/_/g, ' ') : t('ass.comp.' + c.key), c.v, { right: fmt.pct(c.v) + (c.w !== undefined ? ` · ${t('ass.weight')} ${fmt.pct(c.w)}` : '') }))));
    scoreBox.append(el('p', { class: 'small muted', text: t('ass.compHelp') }));
  } else scoreBox.append(empty(t('common.notYet')));
  head.append(scoreBox);
  const summary = a.more || a.less ? summaryWithoutVerdicts(a.summary) : cleanText(a.summary);
  if (summary) view.append(el('div', { class: 'statement', style: { marginTop: '16px' } }, linkifyRefs(summary)));

  // ---- charts
  const cs = section(t('ass.charts'));
  view.append(cs.root);
  const charts = el('div', { class: 'cols cols-3' });
  cs.body.append(charts);
  const lc = el('div', { class: 'chart short' }), cov = el('div', { class: 'chart short' }), dq = el('div', { class: 'chart short' });
  const lcNote = el('div', { class: 'small muted chartnote' }), covNote = el('div', { class: 'small muted chartnote' }), dqNote = el('div', { class: 'small muted chartnote' });
  charts.append(el('div', { class: 'chartbox' }, el('h3', { class: 'small muted', text: t('ass.learningCurve') }), el('p', { class: 'small dim', text: t('ass.learningCurveHelp', { metric: metricName(a.primary) }) }), lc, lcNote),
    el('div', { class: 'chartbox' }, el('h3', { class: 'small muted', text: t('ass.coverage') }), el('p', { class: 'small dim', text: t('ass.coverageHelp') }), cov, covNote),
    el('div', { class: 'chartbox' }, el('h3', { class: 'small muted', text: t('ass.dq') }), el('p', { class: 'small dim', text: t('ass.dqHelp') }), dq, dqNote));
  // learning-curve note: slope, gain
  if (a.fit && a.fit.slope !== undefined && a.fit.slope !== null) lcNote.append(`${t('ass.slope')}: ${fmt.pp(a.fit.slope)} ${t('ass.perFullData')}${a.fit.slope_uncertainty ? ` (±${fmt.pp(a.fit.slope_uncertainty).replace('+', '')})` : ''}`, a.fit.diminishing_returns_fraction ? ` — ${t('ass.diminishing', { p: fmt.pct(a.fit.diminishing_returns_fraction) })}` : ` — ${t('ass.noPlateau')}`);
  if (a.fit && a.fit.statement && roleAllows('engineer')) lcNote.append(el('div', {}, linkifyRefs(cleanText(a.fit.statement))));
  if ((a.fit.evidence_ids || []).length) lcNote.append(el('div', {}, evChips(a.fit.evidence_ids)));
  // coverage note: thin regimes named
  if (a.regimes.length) {
    const thinR = a.regimes.filter((x) => a.thin.includes(x.id) || x.thin);
    covNote.append(thinR.length ? el('div', { class: 'thin' }, `${t('ass.thinRegimes')}: `, thinR.map((x, i) => [i ? ', ' : '', el('b', { text: x.id }), x.n !== null ? ` (${nGroups(x.n)})` : '']).flat()) : el('div', { text: t('ass.noThin') }));
    if (roleAllows('engineer') && (a.cov.silhouette !== undefined || a.cov.balance !== undefined)) covNote.append(el('div', { class: 'dim', text: [a.cov.k ? `${a.cov.k} ${t('ass.regimes')}` : null, a.cov.silhouette !== undefined ? `silhouette ${fmt.num(a.cov.silhouette, 2)}` : null, a.cov.balance !== undefined ? `${t('ass.balance')} ${fmt.num(a.cov.balance, 2)}` : null].filter(Boolean).join(' · ') }));
    for (const x of thinR) if (x.distinguishing && x.distinguishing.length && roleAllows('engineer')) covNote.append(el('div', { class: 'dim' }, `${x.id}: `, linkifyRefs(x.distinguishing.slice(0, 4).map((d) => `${d.feature} ${d.direction} (z ${fmt.num(d.z, 1)})`).join(', '))));
    if ((a.cov.findings || []).length) covNote.append(proseList(a.cov.findings, { cls: 'list small prose' }));
    if ((a.cov.evidence_ids || []).length) covNote.append(el('div', {}, evChips(a.cov.evidence_ids)));
  }
  // dq note: per-category statements + worst signals
  for (const c of a.dqCats) if (c.detail && c.detail.statement) dqNote.append(el('div', { class: 'dqline' }, el('b', { text: metricName(c.key) + ' ' }), linkifyRefs(cleanText(c.detail.statement)), c.detail.evidence_id ? [' ', refLink('evidence', c.detail.evidence_id)] : null));
  if ((a.dq.worst_signals || []).length) dqNote.append(el('div', {}, `${t('ass.worstSignals')}: `, a.dq.worst_signals.slice(0, 4).map((w, i) => [i ? ', ' : '', refLink('signal', w.signal), ` (${(w.types || []).join(', ')})`]).flat()));
  if (a.dq.n_untrusted_batches !== undefined) dqNote.append(el('div', { class: 'dim', text: `${t('ass.untrustedBatches', { n: a.dq.n_untrusted_batches, m: a.dq.n_batches ?? '–' })}${a.dq.mean_trust !== undefined ? ` · ${t('dq.trustScore')} ${fmt.pct(a.dq.mean_trust)}` : ''}` }));
  requestAnimationFrame(() => {
    if (a.curve.length) plot(lc, [{ type: 'scatter', mode: 'lines+markers', x: a.curve.map((p) => p.fraction), y: a.curve.map((p) => p.y), error_y: { type: 'data', array: a.curve.map((p) => p.err || 0), color: k.ink3 }, line: { color: k.accent }, marker: { size: 7 }, customdata: a.curve.map((p) => p.n), hovertemplate: `%{x:.0%} ${t('ass.ofData')} (%{customdata} ${t('ass.groupsUnit')}): %{y:.2f}<extra></extra>` }], { height: 220, xaxis: { tickformat: '.0%', title: { text: t('ass.shareOfData') } }, yaxis: { title: { text: metricName(a.primary) }, rangemode: 'tozero' }, showlegend: false, hovermode: 'closest' });
    else lc.replaceChildren(empty());
    if (a.regimes.length) plot(cov, [{ type: 'bar', x: a.regimes.map((p) => p.id), y: a.regimes.map((p) => p.share), marker: { color: a.regimes.map((p) => (a.thin.includes(p.id) || p.thin ? k.warn : k.accent)) }, text: a.regimes.map((p) => (p.n !== null ? nGroups(p.n) : '')), textposition: 'auto', hovertemplate: `%{x}: %{y:.0%} ${t('ass.ofData')}<extra></extra>` }], { height: 220, yaxis: { tickformat: '.0%', range: [0, 1], title: { text: t('ass.shareOfData') } }, showlegend: false, hovermode: 'closest' });
    else cov.replaceChildren(empty());
    if (a.dqCats.length) { const ys = a.dqCats.map((c) => metricName(c.key)); const xs = a.dqCats.map((c) => c.score); plot(dq, [{ type: 'bar', orientation: 'h', y: ys, x: xs, marker: { color: xs.map((v) => (v < 0.7 ? k.warn : k.accent)) }, hovertemplate: '%{y}: %{x:.0%}<extra></extra>' }], { height: 220, xaxis: { tickformat: '.0%', range: [0, 1] }, yaxis: { tickmode: 'array', tickvals: ys, ticktext: ys, automargin: true }, bargap: 0.35, margin: { l: 90 }, showlegend: false, hovermode: 'closest' }); }
    else dq.replaceChildren(empty());
  });
  view._charts.push(lc, cov, dq);

  // ---- recommendations
  const rs = section(t('ass.recommendations'));
  view.append(rs.root);
  rs.body.append(el('p', { class: 'small muted', text: t('ass.recHelp') }));
  if (!a.recs.length) rs.body.append(empty(t('ass.noRecs')));
  for (const rec of a.recs) {
    const row = el('div', { class: 'rec' });
    const status = el('span', { class: 'small' });
    const setStatus = () => { clear(status); if (rec.status === 'approved' || rec.status === 'applied') status.append(infStatus(null, { human: 'accepted' }), ' ', t('ass.applied')); else if (rec.status === 'dismissed') status.append(infStatus(null, { human: 'dismissed' })); };
    setStatus();
    row.append(el('div', { class: 'recbody' },
      el('div', { class: 'act' }, el('span', { class: 'dim small', text: rec.id + ' ' }), rec.actionLabel),
      el('div', { class: 'why prose' }, linkifyRefs(rec.text)),
      rec.rationale ? el('div', { class: 'why small' }, linkifyRefs(rec.rationale)) : null,
      effectSummary(rec.effect),
      el('div', { class: 'row small muted', style: { marginTop: '4px' } }, conf(rec.confidence, { words: true }), rec.gain !== undefined && rec.gain !== null ? chip(`${t('ass.expectedGain')} ${fmt.pp(rec.gain)}`) : null, evidenceButton(rec.evidence_ids), status)),
      el('div', { class: 'decisions' }, rec.applicable === false ? el('span', { class: 'small muted', text: t('ass.notApplicable') }) : el('button', { class: 'btn btn-sm btn-accept', type: 'button', disabled: rec.status === 'approved' || rec.status === 'applied', onClick: async () => { if (!(await confirmDialog(t('ass.applyConfirm', { action: rec.actionLabel })))) return; const rr = await runApi('/assessor/apply', { method: 'POST', body: { action: rec.id, actor_name: actorName(), role: actorRole() } }); if (rr.ok) { rec.status = 'approved'; setStatus(); toast(t('decision.recorded', { seq: rr.data.log_seq }), 'ok'); } else toast(errText(rr), 'fail'); } }, t('ass.applyAfterApproval'))));
    rs.body.append(row);
  }
  if (roleAllows('engineer') && a.evaluations.length) rs.body.append(el('h4', { class: 'small muted', style: { marginTop: '12px' }, text: t('ass.evaluations') }), el('ul', { class: 'list small prose' }, a.evaluations.map((e) => el('li', {}, chip(e.recommendation || '', e.recommendation === 'recommend' ? 'ok' : e.recommendation === 'reject' ? 'fail' : ''), ' ', el('b', { text: actionLabel(e.action) }), ': ', linkifyRefs(reason(e.rationale || '')), e.seconds ? el('span', { class: 'dim', text: ` (${fmt.sec(e.seconds)})` }) : null))));
  if (roleAllows('engineer') && a.experiments.length) rs.body.append(el('h4', { class: 'small muted', style: { marginTop: '12px' }, text: t('ass.experiments') }), el('ul', { class: 'list small' }, a.experiments.map((e) => el('li', {}, linkifyRefs(`${e.id}: ${e.question} — ${e.result} (${fmt.sec(e.seconds)})`)))));

  // ---- question box (chat with the assessor)
  const qs = section(t('ass.chat'));
  view.append(qs.root);
  const q = el('input', { type: 'text', placeholder: t('ass.chatPlaceholder'), style: { flex: 1, minWidth: '200px' } });
  const ans = el('div', { class: 'stack asschat', style: { gap: '6px' } });
  const ask = async (question) => {
    if (!question) return;
    q.value = '';
    ans.prepend(el('div', { class: 'msg user', text: question }));
    const busy = el('div', { class: 'msg assistant dim', text: t('chat.thinking') + '…' });
    ans.prepend(busy);
    const rr = await runApi('/assessor/ask', { method: 'POST', body: { question, actor: `${actorName()}(${actorRole()})`, language: state.lang } });
    busy.remove();
    if (rr.ok && rr.data.answer) {
      const x = rr.data.answer;
      const m = el('div', { class: 'msg assistant' }, linkifyRefs(singleLead(x.text || '')));
      m.append(el('span', { class: 'src', text: !x.source || x.source === 'template' ? t('chat.source.template') : x.source.startsWith('llm-external') ? `${t('chat.source.external')} ${x.model || ''}` : `${t('chat.source.local')} ${x.model || x.source.split(':')[1] || ''}` }), (x.evidence_ids || []).length ? evChips(x.evidence_ids) : null);
      ans.prepend(m);
      if ((x.followups || []).length) ans.prepend(el('div', { class: 'row' }, x.followups.slice(0, 4).map((f) => el('button', { class: 'btn btn-sm btn-quiet', type: 'button', onClick: () => ask(f) }, f))));
    } else ans.prepend(el('div', { class: 'msg assistant', text: errText(rr) }));
  };
  qs.body.append(el('p', { class: 'small muted', text: t('ass.chatHelp') }), el('form', { class: 'row', onSubmit: async (e) => { e.preventDefault(); ask(q.value.trim()); } }, q, el('button', { class: 'btn btn-primary', type: 'submit' }, t('common.send'))), el('div', { class: 'row' }, ['moreData', 'thin', 'lessData'].map((kk) => el('button', { class: 'btn btn-sm btn-quiet', type: 'button', onClick: () => ask(t('ass.q.' + kk)) }, t('ass.q.' + kk)))), ans);

  // ---- upload candidate
  const up = section(t('ass.upload'), { level: 'engineer' });
  view.append(up.root);
  const fi = el('input', { type: 'file' });
  const out = el('div');
  up.body.append(el('p', { class: 'hint', text: t('ass.uploadHelp') }), el('div', { class: 'row' }, fi, el('button', { class: 'btn btn-sm', type: 'button', onClick: async () => { if (!fi.files[0]) return; const fd = new FormData(); fd.append('file', fi.files[0]); clear(out); out.append(el('span', { class: 'dim', text: t('common.loading') + '…' })); const rr = await runApi('/assessor/upload', { method: 'POST', form: fd }); clear(out); out.append(rr.ok ? el('pre', { class: 'small', style: { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }, text: JSON.stringify(rr.data.result, null, 1) }) : unavailableNote(rr)); } }, t('common.send'))), out);
  const hh = hiddenHint(view); if (hh) view.append(hh);
  view.cleanup = () => view._charts.forEach(purge);
  return view;
}
