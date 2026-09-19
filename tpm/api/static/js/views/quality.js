/* View 2: data quality — trust banner, trust by batch, checks with rule traceability, rule composer.
   ?batch=B00008 selects the batch and lists its checks; ?check=CHK-000029 highlights one; ?rule=RULE-001
   scrolls to the rule. */
import { state, t, el, clear, runApi, fmt, conf, st, chip, section, table, viewHead, needRun, empty, evidenceButton, toast, errText, hiddenHint, actorName, actorRole, unavailableNote, bus, linkifyRefs, cleanText, refLink, refChips, sev, sevWords, confWords, addPlainBox, flash, navigate } from '../core.js';
import { openChat, flagContext } from '../chat.js';

export async function render(main, params = {}) {
  const view = el('div', { class: 'view' });
  main.append(view);
  view.append(viewHead('2', t('nav.quality')));
  if (!state.run) { view.append(needRun()); return view; }
  await addPlainBox(view, 'quality');
  const [tr, batches] = await Promise.all([runApi('/trust'), runApi('/batches')]);
  const trust = tr.ok ? tr.data : { items: [] };
  const items = trust.items || [];
  const byBatch = Object.fromEntries(items.map((x) => [x.batch_id, x]));
  const batchRows = Object.fromEntries(((batches.ok && batches.data.batches) || []).map((b) => [b.batch_id, b]));

  // ---- banner
  const untrusted = trust.untrusted || [];
  const caution = items.filter((x) => x.trusted && x.reasons && x.reasons.length);
  const bannerCls = untrusted.length ? '' : caution.length ? 'warn' : 'ok';
  const sigs = [...new Set(untrusted.flatMap((x) => x.untrusted_signals || []))];
  const bannerText = untrusted.length ? t('dq.bannerFail', { signals: sigs.join(', ') || '–', batches: untrusted.map((x) => x.batch_id).join(', ') }) : caution.length ? t('dq.bannerWarn', { n: caution.length }) : t('dq.bannerOk', { n: items.length });
  if (tr.ok && tr.data.available) {
    view.append(el('div', { class: 'trust-banner ' + bannerCls, role: 'status' },
      el('div', { class: 'score' }, fmt.pct(trust.overall), el('small', { text: t('dq.trustScore') })),
      el('div', { class: 'banner-text' }, el('div', { class: 'big' }, linkifyRefs(bannerText)), el('div', { class: 'small muted', text: t('dq.trustHelp') }), untrusted.length ? el('ul', { class: 'list small', style: { marginTop: '6px' } }, untrusted.slice(0, 4).map((x) => el('li', {}, refLink('batch', x.batch_id), ': ', linkifyRefs(cleanText(x.statement))))) : null),
      untrusted.length ? el('button', { class: 'btn', type: 'button', onClick: () => openChat({ object_type: 'trust', object_id: untrusted[0].batch_id, batch_id: untrusted[0].batch_id, title: untrusted[0].statement, autoAsk: t('chat.quick.why') }) }, t('common.ask')) : null));
  } else view.append(tr.unavailable ? unavailableNote(tr) : el('div', { class: 'notice', text: t('common.notYet') }));

  // ---- trust by batch
  let selBatch = params.batch && byBatch[params.batch] ? params.batch : (params.batch && batchRows[params.batch] ? params.batch : '');
  const tb = section(t('dq.trustByBatch'));
  view.append(tb.root);
  const bar = el('div', { class: 'trustbar' });
  const bInfo = el('div', { class: 'small muted batchinfo', style: { marginTop: '6px' } });
  const showBatchInfo = (x) => {
    clear(bInfo);
    if (!x) { bInfo.append(el('span', { class: 'dim', text: t('dq.pickBatch') })); return; }
    const b = batchRows[x.batch_id] || {};
    bInfo.append(el('b', {}, refLink('batch', x.batch_id), ' '), st(x.trusted ? 'trusted' : 'untrusted', `${x.trusted ? t('dq.trusted') : t('dq.untrusted')} (${fmt.pct(x.trust_score)})`), ' ', linkifyRefs(cleanText(x.statement)),
      (x.untrusted_signals || []).length ? el('span', {}, ` — ${t('dq.untrustedSignals')}: `, refChips('signal', x.untrusted_signals)) : null,
      b.row_start !== undefined ? el('span', { class: 'dim', text: ` — ${t('common.rows', { a: b.row_start, b: b.row_end })}${(b.group_ids || []).length ? `, ${t('common.group').toLowerCase()} ${b.group_ids.join(', ')}` : ''}` }) : null,
      (x.reasons || []).length ? el('ul', { class: 'list small' }, x.reasons.slice(0, 6).map((r) => el('li', {}, linkifyRefs(cleanText(r))))) : null,
      el('button', { class: 'btn btn-sm btn-quiet', type: 'button', onClick: () => { selBatch = ''; bar.querySelectorAll('button').forEach((y) => y.classList.remove('sel')); showBatchInfo(null); loadChecks(); } }, t('common.clearFilter')));
  };
  for (const x of items) {
    const b = el('button', { type: 'button', class: (x.trusted ? (x.reasons && x.reasons.length ? 'warn' : '') : 'fail') + (selBatch === x.batch_id ? ' sel' : ''), style: { height: Math.max(8, x.trust_score * 100) + '%' }, title: `${x.batch_id}: ${fmt.pct(x.trust_score)}`, 'aria-label': `${x.batch_id} ${fmt.pct(x.trust_score)}` });
    b.addEventListener('click', () => { selBatch = selBatch === x.batch_id ? '' : x.batch_id; bar.querySelectorAll('button').forEach((y) => y.classList.remove('sel')); if (selBatch) b.classList.add('sel'); showBatchInfo(selBatch ? x : null); loadChecks(); });
    bar.append(b);
  }
  tb.body.append(bar, bInfo);
  showBatchInfo(selBatch ? byBatch[selBatch] : null);

  // ---- checks
  const ck = section(t('dq.checks'));
  view.append(ck.root);
  const fStatus = el('select', {}, [['', t('common.all')], ['fail', t('dq.fail')], ['warn', t('dq.warn')], ['pass', t('dq.pass')]].map(([v, l]) => el('option', { value: v, text: l })));
  const fCat = el('select', {}, [['', t('common.all')], ...['completeness', 'validity', 'consistency', 'timeliness', 'rule'].map((c) => [c, c])].map(([v, l]) => el('option', { value: v, text: l })));
  const fSig = el('input', { type: 'text', placeholder: t('common.signal'), style: { width: '90px' } });
  const summary = el('div', { class: 'row' });
  const tblHost = el('div');
  ck.body.append(el('div', { class: 'row' }, el('label', { class: 'row' }, t('common.status'), fStatus), el('label', { class: 'row' }, t('dq.category'), fCat), fSig, summary), el('p', { class: 'small muted', text: t('dq.checksHelp') }), tblHost);
  fStatus.value = selBatch || params.check ? '' : 'fail';
  [fStatus, fCat].forEach((x) => x.addEventListener('change', loadChecks));
  fSig.addEventListener('change', loadChecks);
  let firstLoad = true;
  async function loadChecks() {
    clear(tblHost); tblHost.append(el('div', { class: 'dim', text: t('common.loading') + '…' }));
    const r = await runApi('/checks', { params: { status: fStatus.value, category: fCat.value, signal: fSig.value.trim(), batch: selBatch, limit: 2000 } });
    clear(tblHost); clear(summary);
    if (!r.ok) { tblHost.append(unavailableNote(r)); return; }
    for (const [cat, s] of Object.entries(r.data.summary || {})) summary.append(chip(`${cat}: ${s.fail || 0} ${t('dq.fail')}, ${s.warn || 0} ${t('dq.warn')}, ${s.pass || 0} ${t('dq.pass')}`, s.fail ? 'fail' : s.warn ? 'warn' : 'ok'));
    const rows = r.data.items || [];
    const tbl = table({
      columns: [
        { label: t('common.status'), render: (c) => st(c.status, t('dq.' + c.status)) },
        { label: t('common.batch'), render: (c) => el('span', { title: byBatch[c.batch_id] ? `${t('dq.trustScore')} ${fmt.pct(byBatch[c.batch_id].trust_score)}` : '' }, refLink('batch', c.batch_id), c.group_id ? el('span', { class: 'dim small' }, ' (', t('common.group').toLowerCase(), ' ', refLink('group', String(c.group_id)), ')') : null) },
        { label: t('dq.category'), key: 'category' },
        { label: t('dq.type'), render: (c) => (c.check_type || '').replace(/_/g, ' ') },
        { label: t('common.signals'), render: (c) => refChips('signal', c.signals || []) },
        { label: t('common.severity'), render: (c) => el('span', { title: sevWords(c.severity), text: fmt.pct(c.severity) }), num: true },
        { label: t('dq.statement'), cls: 'wrap', render: (c) => el('span', {}, refLink('check', c.check_id, c.check_id.replace('CHK-', '#')), ' ', linkifyRefs(cleanText(c.statement)), c.row_start !== null && c.row_start !== undefined ? el('span', { class: 'dim small', text: ` (${t('common.rows', { a: c.row_start, b: c.row_end })})` }) : null) },
        { label: t('dq.rule'), render: (c) => (c.rule_id ? refLink('rule', c.rule_id) : '') },
        { label: t('common.evidence'), render: (c) => evidenceButton(c.evidence_ids) },
      ],
      rows, pageSize: 25, keyOf: (c) => c.check_id,
      rowClass: (c) => 'st-' + c.status,
      onRow: (c) => openChat({ object_type: 'check', object_id: c.check_id, batch_id: c.batch_id, title: cleanText(c.statement), kind: 'dq' }),
    });
    tblHost.append(tbl);
    if (firstLoad && params.check) { const trEl = tbl.reveal(params.check); if (trEl) flash(trEl); else tblHost.prepend(el('div', { class: 'notice warn', text: t('ref.notFound', { id: params.check }) })); }
    if (firstLoad && params.batch && !params.check) flash(ck.root);
    firstLoad = false;
  }
  loadChecks();

  // ---- rules
  const rl = section(t('dq.rules'));
  view.append(rl.root);
  rl.body.append(el('p', { class: 'hint', text: t('dq.rulesIntro') }));
  const ta = el('textarea', { rows: 2, placeholder: t('dq.rulePlaceholder') });
  const compileBtn = el('button', { class: 'btn btn-primary', type: 'button' }, t('dq.compile'));
  const draftBox = el('div');
  const rulesHost = el('div');
  const fileIn = el('input', { type: 'file', accept: '.md,.txt,.rules,.yaml' });
  rl.body.append(el('div', { class: 'composer' }, ta, compileBtn), draftBox,
    el('div', { class: 'row', style: { marginTop: '12px' } }, el('label', { class: 'row' }, t('dq.uploadRules'), fileIn), el('button', { class: 'btn btn-sm', type: 'button', onClick: async () => { if (!fileIn.files[0]) return; const fd = new FormData(); fd.append('file', fileIn.files[0]); fd.append('actor', `${actorName()}(${actorRole()})`); const r = await runApi('/rules/upload', { method: 'POST', form: fd }); if (r.ok) { toast(t('dq.rulesUploaded', { n: r.data.n }), 'ok'); loadRules(); } else toast(errText(r), 'fail'); } }, t('common.send')),
      el('button', { class: 'btn btn-sm', type: 'button', onClick: async () => { const r = await runApi('/rules/run', { method: 'POST', body: {} }); if (r.ok) { toast(t('toast.saved'), 'ok'); loadChecks(); } else toast(errText(r), 'fail'); } }, t('dq.runRules'))),
    rulesHost);
  compileBtn.addEventListener('click', async () => {
    const text = ta.value.trim(); if (!text) return;
    compileBtn.disabled = true; clear(draftBox); draftBox.append(el('div', { class: 'dim', text: t('common.loading') + '…' }));
    const r = await runApi('/rules', { method: 'POST', body: { text, actor: `${actorName()}(${actorRole()})` } });
    compileBtn.disabled = false; clear(draftBox);
    if (!r.ok) { draftBox.append(unavailableNote(r)); return; }
    ta.value = '';
    draftBox.append(ruleRow(r.data.rule, true));
    loadRules();
  });
  function ruleRow(rule, fresh = false) {
    const stCls = { active: 'ok', approved: 'ok', draft: 'warn', rejected: 'fail', retired: '' }[rule.status] || '';
    const row = el('div', { class: 'rule' + (fresh ? ' notice ok' : ''), dataset: { rule: rule.id } });
    row.append(el('div', { class: 'stack', style: { gap: '4px' } },
      el('div', { class: 'row' }, el('span', { class: 'dim small', text: rule.id }), chip(t('dq.ruleStatus.' + rule.status), stCls), el('span', { class: 'text' }, linkifyRefs(cleanText(rule.text)))),
      rule.compiled ? el('div', {}, el('span', { class: 'small muted', text: t('dq.compiled') + ': ' }), el('code', { class: 'spec', text: JSON.stringify(rule.compiled) })) : null,
      el('div', { class: 'expl' }, el('span', { class: 'small muted', text: t('dq.explanation') + ': ' }), linkifyRefs(cleanText(rule.compile_explanation || '–'))),
      el('div', { class: 'row small muted' }, conf(rule.compile_confidence, { words: true }), el('span', { text: `${t('common.source')}: ${rule.compile_source || 'template'}` }), rule.author ? el('span', { text: rule.author }) : null)),
      el('div', { class: 'decisions' }, ['draft', 'approved'].includes(rule.status) ? el('button', { class: 'btn btn-sm btn-accept', type: 'button', onClick: () => decide(rule, 'approve') }, t('dq.approve')) : null,
        rule.status !== 'rejected' ? el('button', { class: 'btn btn-sm btn-override', type: 'button', onClick: () => decide(rule, 'reject') }, t('dq.reject')) : null));
    return row;
  }
  async function decide(rule, verb) {
    const r = await runApi(`/rules/${rule.id}/${verb}`, { method: 'POST', body: { actor_name: actorName(), role: actorRole() } });
    if (r.ok) { toast(t('decision.recorded', { seq: r.data.log_seq }), 'ok'); loadRules(); } else toast(errText(r), 'fail');
  }
  let firstRules = true;
  async function loadRules() {
    const r = await runApi('/rules'); clear(rulesHost);
    const rules = r.ok ? r.data.rules || [] : [];
    if (!rules.length) { rulesHost.append(empty(t('dq.noRules'))); return; }
    rules.slice().reverse().forEach((rule) => rulesHost.append(ruleRow(rule)));
    if (firstRules && params.rule) { const n = rulesHost.querySelector(`[data-rule="${params.rule}"]`); if (n) flash(n); }
    firstRules = false;
  }
  loadRules();
  const hh = hiddenHint(view); if (hh) view.append(hh);
  return view;
}
