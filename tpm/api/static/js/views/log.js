/* View 6: decision log — filterable hash-chained table, verify chain, export, human decisions audit.
   Object ids and ids inside payloads are links. ?id=FLAG-000001 pre-filters. */
import { state, t, el, clear, runApi, fmt, chip, st, section, table, viewHead, needRun, empty, hiddenHint, roleAllows, infStatus, evChips, toast, errText, notice, linkifyRefs, cleanText, refLink } from '../core.js';
import { summaryCard, techDetails } from '../brief.js';

const TYPE_OF_OBJECT = { flag: 'flag', diagnosis: 'diagnosis', evidence: 'evidence', check: 'check', inference: 'inference', rule: 'rule', pattern: 'pattern', signal: 'signal', batch: 'batch', trust: 'batch', egress: 'egress' };
function objectRef(objectType, objectId) {
  const type = TYPE_OF_OBJECT[objectType];
  const id = String(objectId || '');
  if (type && /^(?:[A-Z]{2,7}-[0-9A-Z]{1,7}|S\d{2,3}|B\d{4,6}|G?\d{1,6})$/.test(id)) return el('span', {}, `${objectType} `, refLink(type, id));
  return el('span', {}, `${objectType} `, linkifyRefs(id));
}

// is every decision logged? objects in the run's results vs objects with a log entry of their own
function completenessSentence(d) {
  return d.complete ? t('log.completeAll', { n: fmt.int(d.n_objects || 0) }) : t('log.completePart', { logged: fmt.int(d.n_logged || 0), n: fmt.int(d.n_objects || 0) });
}

export async function render(main, params = {}) {
  // page = title (with Verify / Export), plain summary, then ONE expander with everything this view rendered before
  const page = el('div', { class: 'view' });
  main.append(page);
  const verifyBtn = el('button', { class: 'btn btn-primary', type: 'button', dataset: { briefSection: 'verify' } }, t('log.verify'));
  const exportA = el('a', { class: 'btn', href: state.run ? `/api/runs/${encodeURIComponent(state.run)}/log/export` : '#', download: '' }, t('log.export'));
  page.append(viewHead('6', t('nav.log'), el('div', { class: 'row' }, verifyBtn, exportA)));
  if (!state.run) { page.append(needRun()); return page; }
  // the result of "Verify" is a plain sentence: it stays next to the summary, outside the expander
  const verifyOut = el('div');
  const tech = techDetails('log');
  page.append(summaryCard('log'), verifyOut, tech);
  const view = tech.body;
  view.append(el('p', { class: 'hint', text: t('log.intro') }));
  const comp = section(t('log.completeness'));
  view.append(comp.root);
  runApi('/log/completeness').then((r) => {
    if (!comp.root.isConnected) return;
    if (!r.ok) { comp.body.append(notice(errText(r), 'warn')); return; }
    const d = r.data;
    comp.body.append(notice(completenessSentence(d), d.complete ? 'ok' : 'warn'));
    comp.body.append(table({ columns: [
      { label: t('log.object'), render: (x) => x.label },
      { label: t('log.inArtifacts'), render: (x) => fmt.int(x.n_artifacts || 0), num: true },
      { label: t('log.logged'), render: (x) => (x.n_logged === null || x.n_logged === undefined ? '–' : fmt.int(x.n_logged)), num: true },
      { label: t('log.notLogged'), cls: 'wrap', render: (x) => (x.n_logged === null || x.n_logged === undefined ? el('span', { class: 'small muted', text: x.note || '' })
        : (x.n_missing ? el('span', {}, chip(fmt.int(x.n_missing), 'warn'), ' ', el('span', { class: 'small muted', text: (x.excluded || []).map((g) => g.reason).join('; ') })) : st('ok', t('log.allLogged')))) },
    ], rows: d.rows || [] }));
    comp.body.append(el('p', { class: 'small muted', text: t('log.neverLogged') }));
  });
  verifyBtn.addEventListener('click', async () => {
    verifyBtn.disabled = true; clear(verifyOut); verifyOut.append(el('span', { class: 'dim', text: t('log.verifying') + '…' }));
    const r = await runApi('/log/verify'); verifyBtn.disabled = false; clear(verifyOut);
    if (!r.ok) { verifyOut.append(notice(errText(r), 'fail')); return; }
    verifyOut.append(notice(r.data.ok ? t('log.ok', { n: r.data.checked }) : t('log.bad', { seq: r.data.first_bad_seq, n: r.data.checked }), r.data.ok ? 'ok' : 'fail'));
    const c = await runApi('/log/completeness');
    if (c.ok && verifyOut.isConnected) verifyOut.append(notice(completenessSentence(c.data), c.data.complete ? 'ok' : 'warn'));
  });

  const first = await runApi('/log', { params: { limit: 1 } });
  const meta = first.ok ? first.data : { actions: [], object_types: [], actors: [] };
  const fType = el('select', {}, [el('option', { value: '', text: t('common.all') }), ...(meta.object_types || []).map((x) => el('option', { value: x, text: x }))]);
  const fAction = el('select', {}, [el('option', { value: '', text: t('common.all') }), ...(meta.actions || []).map((x) => el('option', { value: x, text: x }))]);
  const fActor = el('select', {}, [el('option', { value: '', text: t('common.all') }), ...['human:', 'system:', 'llm:'].map((x) => el('option', { value: x, text: x })), ...(meta.actors || []).map((x) => el('option', { value: x, text: x }))]);
  const fId = el('input', { type: 'text', placeholder: 'FLAG-000001', style: { width: '150px' }, value: params.id || '' });
  const host = el('div', { style: { marginTop: '12px' } });
  view.append(el('div', { class: 'row' }, el('label', { class: 'row' }, t('log.object'), fType), el('label', { class: 'row' }, t('log.action'), fAction), el('label', { class: 'row' }, t('log.actor'), fActor), fId), host);
  async function load() {
    clear(host); host.append(el('span', { class: 'dim', text: t('common.loading') + '…' }));
    const r = await runApi('/log', { params: { object_type: fType.value, action: fAction.value, actor: fActor.value, object_id: fId.value.trim(), limit: 5000 } });
    clear(host);
    if (!r.ok) { host.append(notice(errText(r), 'fail')); return; }
    const rows = (r.data.items || []).slice().reverse();
    host.append(el('div', { class: 'small muted', style: { marginBottom: '6px' }, text: `${fmt.int(r.data.n)} / ${fmt.int(r.data.count_total)}` }));
    host.append(table({
      columns: [
        { label: t('log.seq'), key: 'seq', num: true },
        { label: t('log.ts'), render: (e) => fmt.ts(e.ts) },
        { label: t('log.actor'), render: (e) => chip(e.actor, e.actor.startsWith('human') ? 'ok' : e.actor.startsWith('llm:external') ? 'warn' : '') },
        { label: t('log.action'), key: 'action' },
        { label: t('log.object'), render: (e) => objectRef(e.object_type, e.object_id) },
        { label: t('log.payload'), cls: 'wrap', render: (e) => el('span', {}, el('span', { class: 'small' }, linkifyRefs(summarize(e.payload))), e.evidence_ids && e.evidence_ids.length ? evChips(e.evidence_ids) : null) },
        { label: t('log.hash'), render: (e) => el('span', { class: 'dim small', title: `prev ${e.prev_hash}`, text: e.hash.slice(0, 12) }) },
      ],
      rows, pageSize: 30, keyOf: (e) => e.seq,
    }));
  }
  [fType, fAction, fActor].forEach((x) => x.addEventListener('change', load));
  fId.addEventListener('change', load);
  await load();

  // ---- audit of human decisions (reviewer)
  const audit = section(t('log.overrides'), { level: 'engineer' });
  view.append(audit.root);
  const h = await runApi('/log', { params: { actor: 'human:', limit: 5000 } });
  const hs = h.ok ? h.data.items || [] : [];
  if (!hs.length) audit.body.append(empty());
  else {
    const byActor = {}, byAction = {};
    for (const e of hs) { byActor[e.actor] = (byActor[e.actor] || 0) + 1; byAction[e.action] = (byAction[e.action] || 0) + 1; }
    audit.body.append(el('div', { class: 'cols cols-2' },
      el('div', {}, el('h3', { class: 'small muted', text: t('log.byActor') }), el('ul', { class: 'list plain' }, Object.entries(byActor).sort((a, b) => b[1] - a[1]).map(([k, v]) => el('li', {}, chip(k, 'ok'), ` ${v}`)))),
      el('div', {}, el('h3', { class: 'small muted', text: t('log.byAction') }), el('ul', { class: 'list plain' }, Object.entries(byAction).sort((a, b) => b[1] - a[1]).map(([k, v]) => el('li', {}, chip(k, ['override', 'reject_rule', 'dismiss'].includes(k) ? 'warn' : ''), ` ${v}`))))));
    audit.body.append(table({ columns: [
      { label: t('log.seq'), key: 'seq', num: true }, { label: t('log.ts'), render: (e) => fmt.ts(e.ts) }, { label: t('log.actor'), render: (e) => e.actor.replace('human:', '') }, { label: t('log.action'), key: 'action' }, { label: t('log.object'), render: (e) => objectRef(e.object_type, e.object_id) }, { label: t('common.note'), cls: 'wrap', render: (e) => linkifyRefs(cleanText((e.payload && (e.payload.note || (e.payload.new_value ? JSON.stringify(e.payload.new_value) : ''))) || '')) },
    ], rows: hs.filter((e) => ['override', 'accept', 'question', 'dismiss', 'set_role', 'name_pattern', 'approve_rule', 'reject_rule', 'apply_assessor_action', 'settings'].includes(e.action)).reverse(), pageSize: 20, keyOf: (e) => e.seq }));
  }
  const hh = hiddenHint(view); if (hh) view.append(hh);
  return view;
}

function summarize(p) {
  if (!p || typeof p !== 'object') return '';
  const parts = [];
  for (const [k, v] of Object.entries(p)) {
    if (v === null || v === undefined || v === '') continue;
    if (typeof v === 'object') { const s = JSON.stringify(v); parts.push(`${k}: ${s.length > 80 ? s.slice(0, 80) + '…' : s}`); }
    else { const s = cleanText(String(v)); parts.push(`${k}: ${s.length > 160 ? s.slice(0, 160) + '…' : s}`); }
  }
  return parts.join('  ');
}
