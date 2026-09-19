/* View 2: data quality. Round 5 (agent A): the faulty data FIRST - batches x kinds of checks in ONE figure, the worst
   pieces as Problem -> Reason -> Answer strips (what / where, why, what to do, can the rows be used), then "What was
   checked" and the % score in plain words. The trust banner, the batch rows, the checks table with rule traceability
   and the rule composer stay under "Show technical analyses". Basic mode: the figure and ONE short strip only.
   ?batch=B00008 selects the batch and lists its checks; ?check=CHK-000029 highlights one; ?rule=RULE-001
   scrolls to the rule. */
import { state, t, el, clear, runApi, cachedRunApi, fmt, conf, st, chip, section, table, viewHead, needRun, empty, evidenceButton, toast, errText, hiddenHint, actorName, actorRole, unavailableNote, bus, linkifyRefs, cleanText, refLink, refChips, rowsLink, sev, sevWords, confWords, addPlainBox, flash, navigate, checkCard, roleAllows } from '../core.js';
import { openChat, flagContext } from '../chat.js';
import { summaryCard, techDetails, techNested, itemBrief, itemBriefLocal, bt, openBasicItem } from '../brief.js';
import { vt, vizBox, chartNode, praStrip, fetchItemBrief, trustGrid, stackedBar, briefActionButtons } from '../charts.js';

const TRUST_FAIL = 0.5;     // config/settings.yaml quality.trust_fail_threshold; the verdict itself always comes from the server
const PIECES = 8;           // worst pieces shown as strips (basic mode: 1)
const GRID_BATCHES = 120;   // more batches than this: only the worst ones are drawn
const GRID_KINDS = 14;
const CATEGORIES = ['completeness', 'validity', 'consistency', 'timeliness', 'rule'];
// the kinds of checks behind each question (mirrors tpm/quality/checks.py CATEGORY_OF; a test keeps the two in step)
const KINDS_OF = {
  completeness: ['missing', 'dropout', 'empty_rows'],
  validity: ['out_of_range', 'impossible_value', 'unit_shift', 'quantization_change', 'local_spike'],
  consistency: ['duplicate_rows', 'duplicate_key', 'stuck', 'saturation', 'sign_violation', 'relation_break'],
  timeliness: ['gap', 'out_of_order', 'duplicate_timestamp', 'irregular_sampling', 'stale'],
};
// problems of the whole batch rather than of a sensor (mirrors tpm/quality/trust.py BATCH_LEVEL)
const BATCH_LEVEL = new Set(['duplicate_rows', 'duplicate_key', 'gap', 'out_of_order', 'duplicate_timestamp', 'irregular_sampling', 'empty_rows']);
// English fallbacks of this page's own keys (the i18n files are edited by several people at once)
const FB = {
  'dq.piece.count': '{n} checks of this kind in this batch', 'dq.piece.rows': '{n} rows', 'dq.piece.open': 'Open this check',
  'dq.checked.passed': '{n} checks passed', 'dq.checked.on': 'Checks run on the batches shown above', 'dq.checked.kinds': 'Every check behind each question: ✕ failed / ! smaller problem, with the number of batches; ✓ found nothing in any batch.',
  'dq.checked.batches': 'batches', 'dq.viz.none': 'The data checks have not run yet.', 'dq.score.exampleLead': 'Example: batch', 'dq.score.exampleRated': 'is rated {pct}.',
  'dq.viz.noProblem': 'nothing found by any check',
  'dq.faulty.moreBasic': '{n} further problems are listed in Operator mode.',
  'dq.checked.ran': 'Checked: {list}', 'dq.score.lowered': 'Lowered by: {list}.', 'dq.score.part.sensors': '{n} of {total} sensors unusable ({list})',
  'dq.score.part.batch': '{list} in the whole batch', 'dq.score.part.small': 'smaller problems ({list})',
};
const tq = (k, vars) => { let v = t(k, vars); if (!v || v === k) { v = FB[k] || k; for (const [a, b] of Object.entries(vars || {})) v = v.replaceAll(`{${a}}`, String(b)); } return v; };
/** 'stuck' | 'rule:RULE-003' -> 'rule' : the kind of a check, rule violations together. */
export const typeKey = (ct) => (/^rule/.test(String(ct || '')) ? 'rule' : String(ct || ''));
/** '87 %' in words. The score is a grade (tpm/quality/trust.py): it starts at 100 % when every check passed and drops
    with each problem found, most for unusable sensors. It is NOT a share of data that is lost. `parts`: what lowered it
    in this batch (scoreParts in render). */
export function scoreWords(score, verdict, parts = []) {
  const x = Math.max(0, Math.min(1, Number(score) || 0));
  if (x >= 0.995) return vt('dq.score.full');
  return [vt('dq.score.means', { pct: fmt.pct(x) }), parts.length ? tq('dq.score.lowered', { list: parts.join('; ') }) : '',
    vt(verdict === 'untrusted' ? 'dq.score.rest.untrusted' : 'dq.score.rest.trusted')].filter(Boolean).join(' ');
}

export async function render(main, params = {}) {
  // page = title, plain summary, the round-5 plain part (figure, strips, what was checked, the score in words), then
  // ONE expander ("Show technical analyses") with everything this view rendered before
  const page = el('div', { class: 'view' });
  main.append(page);
  page.append(viewHead('2', t('nav.quality')));
  if (!state.run) { page.append(needRun()); return page; }
  const plain = el('div', { class: 'viz-host', dataset: { view: 'quality' } });
  const tech = techDetails('quality');
  page.append(summaryCard('quality'), plain, tech);
  const view = tech.body;
  await addPlainBox(view, 'quality');
  const [tr, batches, sg, ckAll] = await Promise.all([runApi('/trust'), runApi('/batches'), cachedRunApi('signals', '/signals'), runApi('/checks', { params: { limit: 20000 } })]);
  const sigList = sg.ok ? sg.data.signals || [] : [];
  const sigName = (id) => { const x = sigList.find((y) => y.id === id); const n = x && (x.display_name || x.source_column); return n && n !== id ? `${n} (${id})` : id; };
  const listNames = (ids, n = 3) => ids.slice(0, n).map(sigName).join(', ') + (ids.length > n ? ` +${ids.length - n}` : '');
  const trust = tr.ok ? tr.data : { items: [] };
  const items = trust.items || [];
  const byBatch = Object.fromEntries(items.map((x) => [x.batch_id, x]));
  const batchRows = Object.fromEntries(((batches.ok && batches.data.batches) || []).map((b) => [b.batch_id, b]));
  const allChecks = ckAll.ok ? ckAll.data.items || [] : [];
  const isOk = (c) => c.status === 'pass' || /_ok$/.test(c.check_type || '');
  const problems = allChecks.filter((c) => !isOk(c));
  const full = roleAllows('operator');
  const hasIssue = (x) => !x.trusted || (x.reasons || []).length > 0 || (x.local_untrusted || []).length > 0 || (x.untrusted_signals || []).length > 0;
  const verdictOf = (x) => (!x.trusted ? 'untrusted' : hasIssue(x) ? 'caution' : 'trusted');
  const whyType = (ct) => { const key = 'dq.why.' + ct; return t(key) === key ? String(ct || '').replace(/^rule:/, 'rule ').replace(/_/g, ' ') : t(key); };
  const typeWord = (ct) => (typeKey(ct) === 'rule' ? t('dq.rule') : whyType(ct));
  const revealTech = () => { const det = page.querySelector('details.tech-details:not(.nested)'); if (det && !det.hidden) det.open = true; };
  const catOf = (c) => (typeKey(c.check_type) === 'rule' ? 'rule' : (CATEGORIES.includes(c.category) ? c.category : 'validity'));
  /** The checks that ran on one batch, per kind (worst status, count, sensors) and per question (passed, or the kinds
      that found something); the *_ok records count as passed. */
  const checksBy = new Map();
  for (const c of allChecks) { if (!checksBy.has(c.batch_id)) checksBy.set(c.batch_id, []); checksBy.get(c.batch_id).push(c); }
  const checksOfBatch = (bid) => {
    const kinds = new Map(); const groups = new Map(); let passed = 0;
    for (const c of checksBy.get(bid) || []) {
      const cat = catOf(c); const gr = groups.get(cat) || { status: 'pass', kinds: new Map() }; groups.set(cat, gr);
      if (isOk(c)) { passed++; continue; }
      const k = typeKey(c.check_type); const g = kinds.get(k) || { key: k, label: typeWord(c.check_type), status: 'warn', n: 0, category: c.category, signals: new Set() };
      g.n++; if (c.status === 'fail') g.status = 'fail'; (c.signals || []).forEach((s) => g.signals.add(s)); kinds.set(k, g);
      gr.kinds.set(k, g); gr.status = c.status === 'fail' || gr.status === 'fail' ? 'fail' : 'warn';
    }
    return { kinds: [...kinds.values()].sort((a, b) => (b.status === 'fail') - (a.status === 'fail') || b.n - a.n), passed, groups };
  };
  /** What was checked on one batch: the questions every batch is asked, each passed (✓) or naming the kinds of check
      that found something (✕ failed, ! smaller problem); hovering a question names every check behind it. */
  const checkChips = (bid) => {
    const { groups } = checksOfBatch(bid);
    const cats = CATEGORIES.filter((cat) => groups.has(cat));
    return el('div', { class: 'dq-chips dq-questions' },
      cats.map((cat) => {
        const g = groups.get(cat);
        const title = cat === 'rule' ? vt('dq.group.rule') : tq('dq.checked.ran', { list: KINDS_OF[cat].map(whyType).join(', ') });
        return el('span', { class: 'dq-chip ' + (g.status === 'pass' ? 'ok' : g.status), title }, el('span', { class: 'q', text: vt('dq.group.' + cat) }), ' ',
          g.status === 'pass' ? el('b', { text: '✓' }) : [el('b', { text: g.status === 'fail' ? '✕ ' : '! ' }), [...g.kinds.values()].map((k) => k.label + (k.n > 1 ? ` ×${k.n}` : '')).join(' · ')]);
      }),
      !cats.length ? el('span', { class: 'dim small', text: t('common.notYet') }) : null);
  };
  /** What lowered the score of one batch, in the order the score weighs it (tpm/quality/trust.py): sensors that cannot
      be used, problems of the whole batch, smaller problems. */
  const bare = (s) => String(s).replace(/\s*\([^)]*\)\s*$/, '');
  const scoreParts = (x) => {
    if (!x) return [];
    const bad = new Set(x.untrusted_signals || []);
    const { kinds } = checksOfBatch(x.batch_id);
    const wide = (k) => BATCH_LEVEL.has(k.key) && k.status === 'fail';
    const ofSensors = kinds.filter((k) => !wide(k) && [...k.signals].some((s) => bad.has(s)));
    const small = kinds.filter((k) => !wide(k) && !ofSensors.includes(k));
    const out = [];
    if (bad.size) out.push(tq('dq.score.part.sensors', { n: bad.size, total: Math.max(bad.size, sigList.length), list: ofSensors.map((k) => bare(k.label)).join(', ') || '–' }));
    if (kinds.some(wide)) out.push(tq('dq.score.part.batch', { list: kinds.filter(wide).map((k) => bare(k.label)).join(', ') }));
    if (small.length) out.push(tq('dq.score.part.small', { list: small.map((k) => bare(k.label)).join(', ') }));
    return out;
  };

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
      el('div', { class: 'banner-text' }, el('div', { class: 'big' }, linkifyRefs(bannerText)), el('div', { class: 'small muted', text: t('dq.trustHelp', { pct: fmt.pct(TRUST_FAIL) }) }), trust.overall !== null && trust.overall !== undefined ? el('div', { class: 'dq-scorewords', text: scoreWords(trust.overall, untrusted.length ? 'untrusted' : 'trusted') }) : null, untrusted.length ? el('ul', { class: 'list small', style: { marginTop: '6px' } }, untrusted.slice(0, 4).map((x) => el('li', {}, refLink('batch', x.batch_id), ': ', linkifyRefs(cleanText(x.statement))))) : null),
      untrusted.length ? el('button', { class: 'btn', type: 'button', onClick: () => openChat({ object_type: 'trust', object_id: untrusted[0].batch_id, batch_id: untrusted[0].batch_id, title: untrusted[0].statement, autoAsk: t('chat.quick.why') }) }, t('common.ask')) : null));
  } else view.append(tr.unavailable ? unavailableNote(tr) : el('div', { class: 'notice', text: t('common.notYet') }));

  // ---- trust by batch: score, verdict and the REASONS per batch; group numbers only on request
  let selBatch = params.batch && byBatch[params.batch] ? params.batch : (params.batch && batchRows[params.batch] ? params.batch : '');
  const tb = section(t('dq.trustByBatch'));
  view.append(tb.root);
  const bar = el('div', { class: 'trustbar' });
  const rowsHost = el('div', { class: 'batchrows' });
  const endExclusive = !!(batches.ok && batches.data.row_end_exclusive);
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
    const verdict = verdictOf(x);
    const row = el('div', { class: `batchrow v-${verdict}` + (selected ? ' sel' : ''), dataset: { batch: x.batch_id } });
    const groups = (b.group_ids || []).map(String);
    const lastRow = b.row_end !== undefined ? (endExclusive ? b.row_end - 1 : b.row_end) : null;
    row.append(el('div', { class: 'batchrow-head' },
      el('div', { class: 'batchrow-id' }, refLink('batch', x.batch_id), ' ', st(verdict === 'untrusted' ? 'untrusted' : verdict === 'caution' ? 'warn' : 'trusted', t('dq.verdict.' + verdict))),
      el('div', { class: 'batchrow-score', title: scoreWords(x.trust_score, verdict, scoreParts(x)) }, el('b', { text: fmt.pct(x.trust_score) }), el('span', { class: 'bar' }, el('i', { style: { width: Math.max(0, Math.min(1, x.trust_score)) * 100 + '%' } })), el('span', { class: 'small dim', text: t('dq.trustScore') })),
      el('div', { class: 'batchrow-meta small dim' }, b.row_start !== undefined ? el('span', {}, t('dq.rowsWord'), ' ', rowsLink(b.row_start, lastRow)) : null, groups.length ? el('span', { text: groups.length === 1 ? `${t('common.group').toLowerCase()} ${groups[0]}` : t('dq.nGroups', { n: fmt.int(groups.length) }) }) : null, (x.check_ids || []).length ? el('span', { text: t('dq.nChecks', { n: x.check_ids.length }) }) : null),
      selected ? el('button', { class: 'btn btn-sm btn-quiet', type: 'button', onClick: () => pickBatch('') }, t('common.clearFilter')) : el('button', { class: 'btn btn-sm', type: 'button', onClick: () => pickBatch(x.batch_id) }, t('dq.showChecks')),
      // round 5: the score in words and the checks that ran on this batch, right under the number
      el('div', { class: 'dq-scorewords', text: scoreWords(x.trust_score, verdict, scoreParts(x)) })));
    row.append(el('div', { class: 'batchrow-checks' }, checkChips(x.batch_id)));
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
    // plain first: can these rows be used, what is wrong, with which sensors; the row-by-row reasons are one click away
    const kinds = [...new Set(local.map((g) => whyType(g.check_type)))];
    const localSigs = [...new Set(local.flatMap((g) => g.signals))];
    const localBrief = () => itemBriefLocal({ verdict: verdict === 'untrusted' ? 'problem' : verdict === 'caution' ? 'attention' : 'ok', headline: bt('brief.batch.' + verdict),
      points: [wide.length ? bt('brief.batch.wide', { list: listNames(wide) }) : null, kinds.length ? bt('brief.batch.local', { kinds: kinds.slice(0, 3).join(', '), list: listNames(localSigs) }) : null].filter(Boolean) });
    const whyNested = techNested(why);
    // the server's summary of the batch (problems by kind, sensors, rating); only the few rows on screen ask for one.
    // A server without that route leaves the summary built from the fields of this list.
    row.append(itemBrief(x.batch_id, { ctx: { object_type: 'trust', object_id: x.batch_id, batch_id: x.batch_id }, onLoad: (d) => { if (!d) row.insertBefore(localBrief(), whyNested); } }), whyNested);
    // group numbers: thousands on large runs, so only on request and only then put in the page
    if (groups.length > 1) {
      const list = el('div', { class: 'grouplist small', hidden: true });
      const btn = el('button', { class: 'btn btn-sm btn-quiet', type: 'button', 'aria-expanded': 'false', onClick: () => {
        const open = list.hidden;
        if (open && !list.firstChild) { if (groups.length <= 300) groups.forEach((g, i) => list.append(i ? ', ' : '', refLink('group', g))); else list.textContent = groups.join(', '); }
        list.hidden = !open; btn.setAttribute('aria-expanded', String(open)); btn.textContent = open ? t('dq.hideGroups') : t('dq.showGroups', { n: fmt.int(groups.length) });
      } }, t('dq.showGroups', { n: fmt.int(groups.length) }));
      whyNested.body.append(el('div', { class: 'batchrow-groups' }, btn, list));
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
    const b = el('button', { type: 'button', dataset: { batch: x.batch_id }, class: (x.trusted ? (hasIssue(x) ? 'warn' : '') : 'fail') + (selBatch === x.batch_id ? ' sel' : ''), style: { height: Math.max(8, x.trust_score * 100) + '%' }, title: `${x.batch_id}: ${fmt.pct(x.trust_score)} — ${scoreWords(x.trust_score, verdictOf(x), scoreParts(x))}`, 'aria-label': `${x.batch_id} ${fmt.pct(x.trust_score)}` });
    b.addEventListener('click', () => pickBatch(selBatch === x.batch_id ? '' : x.batch_id));
    bar.append(b);
  }
  tb.body.append(bar, rowsHost);
  renderBatchRows();

  // ---- checks
  const ck = section(t('dq.checks'));
  ck.root.dataset.briefSection = 'checks';
  view.append(ck.root);
  const fStatus = el('select', {}, [['', t('common.all')], ['fail', t('dq.fail')], ['warn', t('dq.warn')], ['pass', t('dq.pass')]].map(([v, l]) => el('option', { value: v, text: l })));
  const fCat = el('select', {}, [['', t('common.all')], ...CATEGORIES.map((c) => [c, c])].map(([v, l]) => el('option', { value: v, text: l })));
  const fSig = el('input', { type: 'text', placeholder: t('common.signal'), style: { width: '90px' } });
  const summary = el('div', { class: 'row' });
  const tblHost = el('div');
  const ckDetail = el('div', { class: 'box detail ck-detail', hidden: true });
  ck.body.append(el('div', { class: 'row' }, el('label', { class: 'row' }, t('common.status'), fStatus), el('label', { class: 'row' }, t('dq.category'), fCat), fSig, summary), el('p', { class: 'small muted', text: t('dq.checksHelp') }), tblHost, ckDetail);
  /** One line a person understands: what is wrong (or that nothing is), with which sensor. */
  const checkPlain = (c) => (c.status === 'pass' || /_ok$/.test(c.check_type || '') ? bt('brief.check.pass') : whyType(c.check_type) + ((c.signals || []).length ? ' — ' + listNames(c.signals, 2) : ''));
  const checkCtx = (c) => ({ object_type: 'check', object_id: c.check_id, check_id: c.check_id, batch_id: c.batch_id, title: cleanText(c.statement), kind: 'dq' });
  /** A clicked check: what is wrong and whether the rows can still be used, first; the full record under the expander. */
  const showCheck = (c) => {
    ckDetail.hidden = false; clear(ckDetail);
    ckDetail.append(el('div', { class: 'row between' }, el('h3', {}, refLink('check', c.check_id), ' ', st(c.status, t('dq.' + c.status))), el('button', { class: 'btn btn-sm btn-primary', type: 'button', onClick: () => openChat(checkCtx(c)) }, t('common.ask'))),
      itemBrief(c.check_id, { ctx: checkCtx(c) }), techNested(checkCard(c)));
    try { ckDetail.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); } catch { /* ignore */ }
  };
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
        { label: t('dq.statement'), cls: 'wrap', render: (c) => { const tn = techNested(el('span', {}, linkifyRefs(cleanText(c.statement)), c.row_start !== null && c.row_start !== undefined ? el('span', { class: 'dim small', text: ` (${t('common.rows', { a: c.row_start, b: c.row_end })})` }) : null)); tn.classList.add('inline'); tn.addEventListener('click', (e) => e.stopPropagation()); return el('div', {}, el('div', { class: 'ck-plain' }, refLink('check', c.check_id, c.check_id.replace('CHK-', '#')), ' ', el('b', { text: checkPlain(c) })), tn); } },
        { label: t('dq.rule'), render: (c) => (c.rule_id ? refLink('rule', c.rule_id) : '') },
        { label: t('common.evidence'), render: (c) => evidenceButton(c.evidence_ids) },
      ],
      rows, pageSize: 25, keyOf: (c) => c.check_id,
      rowClass: (c) => 'st-' + c.status,
      onRow: (c) => showCheck(c),
    });
    tblHost.append(tbl);
    if (firstLoad && params.check) { const trEl = tbl.reveal(params.check); const hit = rows.find((c) => c.check_id === params.check); if (hit) showCheck(hit); if (trEl) flash(trEl); else tblHost.prepend(el('div', { class: 'notice warn', text: t('ref.notFound', { id: params.check }) })); }
    if (firstLoad && params.batch && !params.check) flash(ck.root);
    firstLoad = false;
  }
  loadChecks();

  // ---- rules
  const rl = section(t('dq.rules'));
  rl.root.dataset.briefSection = 'rules';
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

  // =============================================================== round 5: the plain part above the expander
  // (i) ONE figure: where the faulty data is - batches x kinds of checks, the trust score of each batch on top
  if (items.length) {
    let shown = items.slice(); let capped = false;
    if (shown.length > GRID_BATCHES) { shown = shown.slice().sort((a, b) => a.trust_score - b.trust_score).slice(0, GRID_BATCHES); capped = true; }
    shown.sort((a, b) => String(a.batch_id).localeCompare(String(b.batch_id)));
    const bIndex = new Map(shown.map((x, i) => [x.batch_id, i]));
    const weight = new Map();
    for (const c of problems) { const k = typeKey(c.check_type); weight.set(k, (weight.get(k) || 0) + (c.status === 'fail' ? 2 : 1)); }
    const kinds = [...weight.entries()].sort((a, b) => b[1] - a[1]).slice(0, GRID_KINDS).map(([k]) => k);
    const kIndex = new Map(kinds.map((k, i) => [k, i]));
    const z = kinds.map(() => shown.map(() => 0)); const counts = kinds.map(() => shown.map(() => 0)); const worst = kinds.map(() => shown.map(() => null));
    for (const c of problems) {
      const i = kIndex.get(typeKey(c.check_type)); const j = bIndex.get(c.batch_id); if (i === undefined || j === undefined) continue;
      counts[i][j]++; const v = c.status === 'fail' ? 2 : 1; if (v > z[i][j]) z[i][j] = v;
      const w = worst[i][j]; if (!w || (c.status === 'fail' && w.status !== 'fail') || (c.status === w.status && (Number(c.severity) || 0) > (Number(w.severity) || 0))) worst[i][j] = c;
    }
    const labels = kinds.length ? kinds.map(typeWord) : [tq('dq.viz.noProblem')];
    const zz = kinds.length ? z : [shown.map(() => 0)];
    const hover = (kinds.length ? kinds : [null]).map((k, i) => shown.map((x, j) => { const n = kinds.length ? counts[i][j] : 0; if (!n) return `${x.batch_id} × ${labels[i]}: ${vt('dq.viz.cell.pass')}`; const w = worst[i][j]; return `<b>${x.batch_id} × ${labels[i]}</b>: ${n} ${vt('dq.viz.cell.' + (z[i][j] === 2 ? 'fail' : 'warn'))}<br>${cleanText(w.statement || '').replace(/[<>]/g, '').slice(0, 170)}`; }));
    const node = chartNode();
    plain.append(vizBox(vt('dq.viz.title'), vt('dq.viz.help'), node, el('div', { class: 'viz-note' }, capped ? el('p', { class: 'small muted', text: vt('dq.viz.capped', { n: shown.length, total: items.length }) }) : null)));
    trustGrid(node, { batches: shown.map((x) => x.batch_id), scores: shown.map((x) => x.trust_score), verdicts: shown.map(verdictOf), types: labels, z: zz, hover, counts: kinds.length ? counts : null, onClick: (bid) => { if (!full) { openBasicItem(bid); return; } pickBatch(bid); revealTech(); const n = rowsHost.querySelector(`[data-batch="${bid}"]`); if (n) flash(n); } });
  } else plain.append(vizBox(vt('dq.viz.title'), '', el('p', { class: 'small muted', text: tq('dq.viz.none') })));

  // (ii) the faulty pieces, worst first: one strip per (batch, kind of check) - Problem -> Reason -> Answer
  const pieceMap = new Map();
  for (const c of problems) {
    const key = c.batch_id + '|' + typeKey(c.check_type);
    let p = pieceMap.get(key);
    if (!p) { p = { batch_id: c.batch_id, kind: typeKey(c.check_type), checks: [], signals: [], a: null, b: null, sev: 0, status: 'warn', best: null }; pieceMap.set(key, p); }
    p.checks.push(c);
    for (const s of c.signals || []) if (!p.signals.includes(s)) p.signals.push(s);
    if (c.row_start !== null && c.row_start !== undefined) { const e = c.row_end === null || c.row_end === undefined ? c.row_start : c.row_end; p.a = p.a === null ? c.row_start : Math.min(p.a, c.row_start); p.b = p.b === null ? e : Math.max(p.b, e); }
    p.sev = Math.max(p.sev, Number(c.severity) || 0);
    if (c.status === 'fail') p.status = 'fail';
    if (!p.best || (c.status === 'fail' && p.best.status !== 'fail') || (c.status === p.best.status && (Number(c.severity) || 0) > (Number(p.best.severity) || 0))) p.best = c;
  }
  // worst first = the batch with the lowest trust score first (the one the summary and the lowest bar name), the pieces
  // of one batch together, failed before smaller problems
  const scoreOfBatch = (bid) => (byBatch[bid] && byBatch[bid].trust_score !== undefined ? byBatch[bid].trust_score : 1);
  const pieces = [...pieceMap.values()].sort((p, q) => (scoreOfBatch(p.batch_id) - scoreOfBatch(q.batch_id)) || String(p.batch_id).localeCompare(String(q.batch_id)) || ((q.status === 'fail') - (p.status === 'fail')) || (q.sev - p.sev));
  // Basic mode: ONE short strip, the worst batch; its question chips name every kind of problem found in it
  const cap = full ? PIECES : 1;
  const pieceStrip = (p, { batchInfo = true } = {}) => {
    const x = byBatch[p.batch_id] || null; const verdict = x ? verdictOf(x) : 'caution';
    const host = el('div', { class: 'pra-host', dataset: { piece: p.batch_id + '|' + p.kind } }, el('div', { class: 'dim small', text: vt('adv.loading') + '…' }));
    const ctx = checkCtx(p.best);
    const label = [refLink('batch', p.batch_id), ' · ', st(p.status, t('dq.' + p.status)), p.checks.length > 1 ? el('span', { class: 'dim', text: ' · ' + tq('dq.piece.count', { n: p.checks.length }) }) : null];
    // the score in words and the checks of the batch once per batch (a second piece of the same batch leaves them out)
    const where = [el('div', { class: 'dq-piece-where' }, p.a !== null ? [t('dq.rowsWord'), ' ', rowsLink(p.a, p.b, { signals: p.signals }), p.b > p.a ? el('span', { class: 'dim', text: ' (' + tq('dq.piece.rows', { n: fmt.int(p.b - p.a + 1) }) + ')' }) : null] : vt('dq.faulty.wholeBatch'), p.signals.length && full ? [' · ', refChips('signal', p.signals, { max: 4 })] : null),
      x && batchInfo ? el('div', { class: 'dq-scorewords', text: scoreWords(x.trust_score, verdict, scoreParts(x)) }) : null,
      batchInfo ? checkChips(p.batch_id) : null];   // which checks ran on this batch and what they found: every mode shows it
    const open = el('button', { class: 'btn btn-sm btn-primary', type: 'button', onClick: () => { pickBatch(p.batch_id); revealTech(); showCheck(p.best); } }, tq('dq.piece.open'));
    (async () => {
      const b = await fetchItemBrief(p.best.check_id);
      const problem = b ? linkifyRefs(b.headline) : el('span', {}, el('b', { text: typeWord(p.kind) }), p.signals.length ? ' — ' + listNames(p.signals, 3) : '');
      const pts = b ? (b.points || []).filter((s) => !/^(Where|Missä|Var)\b/.test(s)) : [];
      // Basic mode: the reason as text (the compact strip keeps its first sentence), no engine statement, no ids
      const reason = !full ? (b && b.why ? b.why : cleanText(p.best.statement || '')) : b && b.why ? linkifyRefs(b.why) : linkifyRefs(cleanText(p.best.statement || ''));
      const reasonMore = [pts.map((s) => el('div', { text: s })), b && b.why ? el('div', { class: 'dim' }, linkifyRefs(cleanText(p.best.statement || ''))) : null];
      // "Open this check" leads into the technical part, which Basic mode does not show
      host.replaceChildren(praStrip({ verdict: p.status === 'fail' && verdict === 'untrusted' ? 'problem' : 'attention', problem, where, reason, reasonMore, fix: b ? b.fix || [] : [], use: b ? b.can_use_rows : undefined, extra: [b ? briefActionButtons(b.actions, ctx, { skipRef: p.best.check_id, noAsk: !full }) : null, full ? el('div', { class: 'pra-btns' }, open) : null], label, compact: !full }));
    })();
    return host;
  };
  plain.append(...[el('h2', { class: 'r5-h2 viz-title', text: vt('dq.faulty.title') }), full ? el('p', { class: 'viz-help', text: vt('adv.topHelp') }) : null].filter(Boolean));
  if (!pieces.length) plain.append(el('p', { class: 'notice ok', text: vt('dq.faulty.none') }));
  else {
    const list = el('div', { class: 'pra-list' });
    plain.append(list);
    pieces.slice(0, cap).forEach((p, i, arr) => list.append(pieceStrip(p, { batchInfo: i === 0 || arr[i - 1].batch_id !== p.batch_id })));
    if (pieces.length > cap) plain.append(el('p', { class: 'small muted', text: full ? vt('dq.faulty.more', { n: pieces.length - cap }) : tq('dq.faulty.moreBasic', { n: pieces.length - cap }) }));
  }

  // (iii) what was checked (operator +): the questions every batch was asked, with the kinds of checks behind them
  if (full && allChecks.length) {
    const nB = Math.max(1, items.length || new Set(allChecks.map((c) => c.batch_id)).size);
    const perCat = Object.fromEntries(CATEGORIES.map((cat) => [cat, { fail: new Set(), warn: new Set(), kinds: new Map() }]));
    for (const c of problems) { const cat = perCat[catOf(c)]; (c.status === 'fail' ? cat.fail : cat.warn).add(c.batch_id); const k = typeKey(c.check_type); const g = cat.kinds.get(k) || { label: typeWord(c.check_type), fail: new Set(), warn: new Set() }; (c.status === 'fail' ? g.fail : g.warn).add(c.batch_id); cat.kinds.set(k, g); }
    const cats = CATEGORIES.filter((cat) => cat !== 'rule' || perCat.rule.kinds.size || allChecks.some((c) => c.category === 'rule'));
    const failN = cats.map((cat) => perCat[cat].fail.size);
    const warnN = cats.map((cat) => [...perCat[cat].warn].filter((b) => !perCat[cat].fail.has(b)).length);
    const passN = cats.map((cat, i) => Math.max(0, nB - failN[i] - warnN[i]));
    const node = chartNode('short');
    const kindsBox = el('div', { class: 'dq-kinds' }, el('div', { class: 'small muted', text: tq('dq.checked.kinds') }), cats.map((cat) => el('div', {}, el('span', { class: 'q', text: vt('dq.group.' + cat) }), el('span', { class: 'dq-chips' }, [...perCat[cat].kinds.values()].sort((a, b) => b.fail.size - a.fail.size || b.warn.size - a.warn.size).map((g) => el('span', { class: 'dq-chip ' + (g.fail.size ? 'fail' : 'warn') }, g.label, el('b', { text: ` ${g.fail.size ? '✕' + g.fail.size : ''}${g.fail.size && g.warn.size ? ' ' : ''}${g.warn.size ? '!' + g.warn.size : ''}` }))),
      // every other check behind the question ran too and found nothing in any batch
      (KINDS_OF[cat] || []).filter((k) => !perCat[cat].kinds.has(k)).map((k) => el('span', { class: 'dq-chip ok' }, whyType(k), el('b', { text: ' ✓' }))),
      !perCat[cat].kinds.size && !KINDS_OF[cat] ? el('span', { class: 'dq-chip ok', text: vt('dq.viz.cell.pass') }) : null))));
    const presented = [...new Set(pieces.slice(0, cap).map((p) => p.batch_id))].concat(selBatch && !pieces.slice(0, cap).some((p) => p.batch_id === selBatch) ? [selBatch] : []);
    const perBatch = presented.length ? el('div', {}, el('h4', { class: 'small muted', style: { margin: '12px 0 4px' }, text: tq('dq.checked.on') }), el('div', { class: 'dq-batchchecks' }, presented.map((bid) => [refLink('batch', bid), checkChips(bid)]))) : null;
    plain.append(vizBox(vt('dq.checked.title'), vt('dq.checked.help') + ' ' + vt('dq.checked.total', { n: fmt.int(allChecks.length), b: fmt.int(nB) }), node, el('div', { class: 'viz-note' }, kindsBox, perBatch)));
    const sc = { pass: 'var(--ok)', warn: 'var(--warn)', fail: 'var(--fail)' };
    const cs = getComputedStyle(document.documentElement); const col = (v) => cs.getPropertyValue(v.slice(4, -1)).trim() || v;
    stackedBar(node, cats.map((cat) => vt('dq.group.' + cat)), [{ name: vt('dq.viz.cell.pass'), values: passN, color: col(sc.pass) }, { name: vt('dq.viz.cell.warn'), values: warnN, color: col(sc.warn) }, { name: vt('dq.viz.cell.fail'), values: failN, color: col(sc.fail) }], { height: 60 + 34 * cats.length, xtitle: tq('dq.checked.batches') });
  }

  // (iv) the % score in plain words (operator +): legend, how it is rated, the worst batch as the example
  if (full && items.length) {
    const worstB = items.slice().sort((a, b) => a.trust_score - b.trust_score)[0];
    plain.append(vizBox(vt('dq.score.title'), vt('dq.score.how'),
      el('ul', { class: 'dq-legend' }, [['ok', vt('dq.score.legend.ok')], ['warn', vt('dq.score.legend.warn')], ['fail', vt('dq.score.legend.fail', { pct: fmt.pct(TRUST_FAIL) })]].map(([cls, txt]) => el('li', {}, el('span', { class: 'viz-sw ' + cls, 'aria-hidden': 'true' }), txt))),
      worstB ? el('p', { class: 'dq-example' }, tq('dq.score.exampleLead'), ' ', refLink('batch', worstB.batch_id), ' ', tq('dq.score.exampleRated', { pct: fmt.pct(worstB.trust_score) }), ' ', scoreWords(worstB.trust_score, verdictOf(worstB), scoreParts(worstB))) : null));
  }
  return view;
}
