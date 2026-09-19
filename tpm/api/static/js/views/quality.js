/* View 2: data quality — trust banner, trust by batch, checks with rule traceability, rule composer.
   ?batch=B00008 selects the batch and lists its checks; ?check=CHK-000029 highlights one; ?rule=RULE-001
   scrolls to the rule. */
import { state, t, el, clear, runApi, fmt, conf, st, chip, section, table, viewHead, needRun, empty, evidenceButton, toast, errText, hiddenHint, actorName, actorRole, unavailableNote, bus, linkifyRefs, cleanText, refLink, refChips, rowsLink, sev, sevWords, confWords, addPlainBox, flash, navigate } from '../core.js';
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
  const capList = (arr, n) => arr.slice(0, n).join(', ') + (arr.length > n ? ` +${arr.length - n}` : '');
  const bannerText = untrusted.length ? t('dq.bannerFail', { signals: capList(sigs, 8) || '–', batches: capList(untrusted.map((x) => x.batch_id), 8) }) : caution.length ? t('dq.bannerWarn', { n: caution.length }) : t('dq.bannerOk', { n: items.length });
  if (tr.ok && tr.data.available) {
    view.append(el('div', { class: 'trust-banner ' + bannerCls, role: 'status' },
      el('div', { class: 'score' }, fmt.pct(trust.overall), el('small', { text: t('dq.trustScore') })),
      el('div', { class: 'banner-text' }, el('div', { class: 'big' }, linkifyRefs(bannerText)), el('div', { class: 'small muted', text: t('dq.trustHelp') }), untrusted.length ? el('ul', { class: 'list small', style: { marginTop: '6px' } }, untrusted.slice(0, 4).map((x) => el('li', {}, refLink('batch', x.batch_id), ': ', linkifyRefs(cleanText(x.statement))))) : null),
      untrusted.length ? el('button', { class: 'btn', type: 'button', onClick: () => openChat({ object_type: 'trust', object_id: untrusted[0].batch_id, batch_id: untrusted[0].batch_id, title: untrusted[0].statement, autoAsk: t('chat.quick.why') }) }, t('common.ask')) : null));
  } else view.append(tr.unavailable ? unavailableNote(tr) : el('div', { class: 'notice', text: t('common.notYet') }));

  // ---- trust by batch: score, verdict and the REASONS per batch; group numbers only on request
  let selBatch = params.batch && byBatch[params.batch] ? params.batch : (params.batch && batchRows[params.batch] ? params.batch : '');
  const tb = section(t('dq.trustByBatch'));
  view.append(tb.root);
  const bar = el('div', { class: 'trustbar' });
  const rowsHost = el('div', { class: 'batchrows' });
  const endExclusive = !!(batches.ok && batches.data.row_end_exclusive);
  const hasIssue = (x) => !x.trusted || (x.reasons || []).length > 0 || (x.local_untrusted || []).length > 0 || (x.untrusted_signals || []).length > 0;
  const whyType = (ct) => { const key = 'dq.why.' + ct; return t(key) === key ? String(ct || '').replace(/^rule:/, 'rule ').replace(/_/g, ' ') : t(key); };
  /** Row-scoped problems of a batch, one line per (kind, row range): signals hit in the same rows are listed together.
      Ordered by how much data they touch: rows x severity x signals. */
  const localProblems = (x) => {
    const m = new Map();
    const wideSet = new Set(x.untrusted_signals || []);   // their rows are named on the whole-batch line instead
    for (const l of x.local_untrusted || []) {
      if (wideSet.has(l.signal)) continue;
      const key = `${l.check_type}|${l.row_start}|${l.row_end}`;
      let g = m.get(key);
      if (!g) { g = { check_type: l.check_type, row_start: Number(l.row_start), row_end: Number(l.row_end), severity: 0, signals: [], check_ids: [] }; m.set(key, g); }
      if (l.signal && !g.signals.includes(l.signal)) g.signals.push(l.signal);
      g.severity = Math.max(g.severity, Number(l.severity) || 0);
      if (l.check_id && !g.check_ids.includes(l.check_id)) g.check_ids.push(l.check_id);
    }
    const weight = (g) => (g.row_end - g.row_start + 1) * Math.max(0.05, g.severity) * Math.max(1, g.signals.length);
    const sorted = [...m.values()].sort((a, b) => weight(b) - weight(a));
    // the heaviest problem of EACH kind first (frozen, scale change, out of range ...), then the rest by weight:
    // five lines of the same kind would hide that other kinds exist
    const seen = new Set(); const head = []; const rest = [];
    for (const g of sorted) { if (seen.has(g.check_type)) rest.push(g); else { seen.add(g.check_type); head.push(g); } }
    return head.concat(rest);
  };
  const localLine = (g) => {
    const n = g.row_end - g.row_start + 1;
    return el('li', {},
      g.signals.length > 3 ? el('span', { title: g.signals.join(', ') }, t('dq.nSignals', { n: g.signals.length }), ' (', refChips('signal', g.signals, { max: 3 }), ')') : refChips('signal', g.signals, { max: 3 }),
      ' ', t(n === 1 ? 'dq.rowWord' : 'dq.rowsWord'), ' ', rowsLink(g.row_start, g.row_end, { signals: g.signals }), ': ', el('b', { text: whyType(g.check_type) }),
      el('span', { class: 'dim' }, n > 1 ? ` — ${t('dq.nRows', { n: fmt.int(n) })}` : '', g.check_ids.length ? ' ' : '', g.check_ids.length ? refLink('check', g.check_ids[0], g.check_ids[0].replace('CHK-', '#')) : null, g.check_ids.length > 1 ? ` +${g.check_ids.length - 1}` : ''));
  };
  const LOCAL_TOP = 5;
  const batchRow = (x, { selected = false } = {}) => {
    const b = batchRows[x.batch_id] || {};
    const verdict = !x.trusted ? 'untrusted' : hasIssue(x) ? 'caution' : 'trusted';
    const row = el('div', { class: `batchrow v-${verdict}` + (selected ? ' sel' : ''), dataset: { batch: x.batch_id } });
    const groups = (b.group_ids || []).map(String);
    const lastRow = b.row_end !== undefined ? (endExclusive ? b.row_end - 1 : b.row_end) : null;
    row.append(el('div', { class: 'batchrow-head' },
      el('div', { class: 'batchrow-id' }, refLink('batch', x.batch_id), ' ', st(verdict === 'untrusted' ? 'untrusted' : verdict === 'caution' ? 'warn' : 'trusted', t('dq.verdict.' + verdict))),
      el('div', { class: 'batchrow-score', title: t('dq.trustHelp') }, el('b', { text: fmt.pct(x.trust_score) }), el('span', { class: 'bar' }, el('i', { style: { width: Math.max(0, Math.min(1, x.trust_score)) * 100 + '%' } })), el('span', { class: 'small dim', text: t('dq.trustScore') })),
      el('div', { class: 'batchrow-meta small dim' }, b.row_start !== undefined ? el('span', {}, t('dq.rowsWord'), ' ', rowsLink(b.row_start, lastRow)) : null, groups.length ? el('span', { text: groups.length === 1 ? `${t('common.group').toLowerCase()} ${groups[0]}` : t('dq.nGroups', { n: fmt.int(groups.length) }) }) : null, (x.check_ids || []).length ? el('span', { text: t('dq.nChecks', { n: x.check_ids.length }) }) : null),
      selected ? el('button', { class: 'btn btn-sm btn-quiet', type: 'button', onClick: () => pickBatch('') }, t('common.clearFilter')) : el('button', { class: 'btn btn-sm', type: 'button', onClick: () => pickBatch(x.batch_id) }, t('dq.showChecks'))));
    // reasons: whole-batch signals first, then structural reasons, then row-scoped problems
    const why = el('div', { class: 'batchrow-why' });
    const reasons = (x.reasons || []).map((r) => cleanText(r));
    const sigReason = (s) => { const r = reasons.find((y) => y.startsWith(s + ':')); return r ? r.slice(s.length + 1).trim() : ''; };
    const wide = (x.untrusted_signals || []);
    const structural = reasons.filter((r) => !wide.some((s) => r.startsWith(s + ':')));
    if (wide.length || structural.length) {
      why.append(el('h4', { class: 'small muted', text: t('dq.wholeBatch') }));
      const ul = el('ul', { class: 'list small' });
      const WIDE_TOP = 6;
      let firstStructural = null;
      const wideLine = (s) => {
        const ranges = (x.local_untrusted || []).filter((l) => l.signal === s).slice(0, 2);
        return el('li', {}, refLink('signal', s), ': ', el('b', {}, linkifyRefs(sigReason(s) || t('dq.unreliable'))), ranges.length ? el('span', {}, ' (', t('dq.rowsWord'), ' ', ranges.map((l, i) => [i ? ', ' : '', rowsLink(l.row_start, l.row_end, { signals: [s] })]), ')') : null, el('span', { class: 'dim', text: ' — ' + t('dq.wholeBatchNote') }));
      };
      wide.slice(0, WIDE_TOP).forEach((s) => ul.append(wideLine(s)));
      if (wide.length > WIDE_TOP) { const more = el('li', {}, el('button', { class: 'btn btn-sm btn-quiet', type: 'button', onClick: () => { more.remove(); wide.slice(WIDE_TOP).forEach((s) => ul.insertBefore(wideLine(s), firstStructural)); } }, t('dq.moreReasons', { n: wide.length - WIDE_TOP }))); ul.append(more); }
      structural.slice(0, 6).forEach((r, i) => { const li = el('li', {}, linkifyRefs(r)); if (i === 0) firstStructural = li; ul.append(li); });
      why.append(ul);
    }
    const local = localProblems(x);
    if (local.length) {
      const nSig = new Set(local.flatMap((g) => g.signals)).size;
      why.append(el('h4', { class: 'small muted', text: t('dq.onlyRows', { n: nSig }) }));
      const ul = el('ul', { class: 'list small' }, local.slice(0, LOCAL_TOP).map(localLine));
      if (local.length > LOCAL_TOP) {
        const more = el('li', { class: 'more' }, el('button', { class: 'btn btn-sm btn-quiet', type: 'button', 'aria-expanded': 'false', onClick: () => { more.remove(); local.slice(LOCAL_TOP).forEach((g) => ul.append(localLine(g))); } }, t('dq.moreRanges', { n: local.length - LOCAL_TOP })));
        ul.append(more);
      }
      why.append(ul, el('p', { class: 'small dim', style: { margin: '2px 0 0' }, text: t('dq.onlyRowsNote') }));
    }
    if (!why.childNodes.length) why.append(el('p', { class: 'small muted', style: { margin: 0 }, text: t('dq.passedAll') }));
    row.append(why);
    // group numbers: thousands on large runs, so only on request and only then put in the page
    if (groups.length > 1) {
      const list = el('div', { class: 'grouplist small', hidden: true });
      const btn = el('button', { class: 'btn btn-sm btn-quiet', type: 'button', 'aria-expanded': 'false', onClick: () => {
        const open = list.hidden;
        if (open && !list.firstChild) { if (groups.length <= 300) groups.forEach((g, i) => list.append(i ? ', ' : '', refLink('group', g))); else list.textContent = groups.join(', '); }
        list.hidden = !open; btn.setAttribute('aria-expanded', String(open)); btn.textContent = open ? t('dq.hideGroups') : t('dq.showGroups', { n: fmt.int(groups.length) });
      } }, t('dq.showGroups', { n: fmt.int(groups.length) }));
      row.append(el('div', { class: 'batchrow-groups' }, btn, list));
    }
    return row;
  };
  let shownBatches = 4;
  const renderBatchRows = () => {
    clear(rowsHost);
    if (selBatch) {
      const x = byBatch[selBatch];
      if (x) rowsHost.append(batchRow(x, { selected: true })); else rowsHost.append(el('p', { class: 'small muted', text: t('dq.noVerdict', { id: selBatch }) }), el('button', { class: 'btn btn-sm btn-quiet', type: 'button', onClick: () => pickBatch('') }, t('common.clearFilter')));
      return;
    }
    const worst = items.filter(hasIssue).sort((a, b) => a.trust_score - b.trust_score || String(a.batch_id).localeCompare(String(b.batch_id)));
    rowsHost.append(el('p', { class: 'small muted', style: { margin: '0 0 8px' }, text: worst.length ? t('dq.batchesAttention', { n: worst.length, total: items.length }) + ' ' + t('dq.pickBatch') : (items.length ? t('dq.batchesAllPassed', { n: items.length }) + ' ' + t('dq.pickBatch') : t('common.notYet')) }));
    worst.slice(0, shownBatches).forEach((x) => rowsHost.append(batchRow(x)));
    if (worst.length > shownBatches) rowsHost.append(el('button', { class: 'btn btn-quiet', type: 'button', onClick: () => { shownBatches += 10; renderBatchRows(); } }, t('dq.moreBatches', { n: worst.length - shownBatches })));
  };
  function pickBatch(id) {
    selBatch = id;
    bar.querySelectorAll('button').forEach((y) => y.classList.toggle('sel', !!id && y.dataset.batch === id));
    renderBatchRows();
    loadChecks();
  }
  for (const x of items) {
    const b = el('button', { type: 'button', dataset: { batch: x.batch_id }, class: (x.trusted ? (hasIssue(x) ? 'warn' : '') : 'fail') + (selBatch === x.batch_id ? ' sel' : ''), style: { height: Math.max(8, x.trust_score * 100) + '%' }, title: `${x.batch_id}: ${fmt.pct(x.trust_score)}`, 'aria-label': `${x.batch_id} ${fmt.pct(x.trust_score)}` });
    b.addEventListener('click', () => pickBatch(selBatch === x.batch_id ? '' : x.batch_id));
    bar.append(b);
  }
  tb.body.append(bar, rowsHost);
  renderBatchRows();

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
