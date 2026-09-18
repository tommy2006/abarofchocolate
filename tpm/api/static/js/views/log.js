/* View 6: decision log — filterable hash-chained table, verify chain, export, human decisions audit. */
import { state, t, el, clear, runApi, fmt, chip, section, table, viewHead, needRun, empty, hiddenHint, roleAllows, infStatus, evChips, toast, errText, notice } from '../core.js';

export async function render(main) {
  const view = el('div', { class: 'view' });
  main.append(view);
  const verifyBtn = el('button', { class: 'btn btn-primary', type: 'button' }, t('log.verify'));
  const exportA = el('a', { class: 'btn', href: state.run ? `/api/runs/${encodeURIComponent(state.run)}/log/export` : '#', download: '' }, t('log.export'));
  view.append(viewHead('6', t('nav.log'), el('div', { class: 'row' }, verifyBtn, exportA)));
  if (!state.run) { view.append(needRun()); return view; }
  view.append(el('p', { class: 'hint', text: t('log.intro') }));
  const verifyOut = el('div');
  view.append(verifyOut);
  verifyBtn.addEventListener('click', async () => {
    verifyBtn.disabled = true; clear(verifyOut); verifyOut.append(el('span', { class: 'dim', text: t('log.verifying') + '…' }));
    const r = await runApi('/log/verify'); verifyBtn.disabled = false; clear(verifyOut);
    if (!r.ok) { verifyOut.append(notice(errText(r), 'fail')); return; }
    verifyOut.append(notice(r.data.ok ? t('log.ok', { n: r.data.checked }) : t('log.bad', { seq: r.data.first_bad_seq, n: r.data.checked }), r.data.ok ? 'ok' : 'fail'));
  });

  const first = await runApi('/log', { params: { limit: 1 } });
  const meta = first.ok ? first.data : { actions: [], object_types: [], actors: [] };
  const fType = el('select', {}, [el('option', { value: '', text: t('common.all') }), ...(meta.object_types || []).map((x) => el('option', { value: x, text: x }))]);
  const fAction = el('select', {}, [el('option', { value: '', text: t('common.all') }), ...(meta.actions || []).map((x) => el('option', { value: x, text: x }))]);
  const fActor = el('select', {}, [el('option', { value: '', text: t('common.all') }), ...['human:', 'system:', 'llm:'].map((x) => el('option', { value: x, text: x })), ...(meta.actors || []).map((x) => el('option', { value: x, text: x }))]);
  const fId = el('input', { type: 'text', placeholder: 'FLAG-000001', style: { width: '150px' } });
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
        { label: t('log.object'), render: (e) => `${e.object_type} ${e.object_id}` },
        { label: t('log.payload'), cls: 'wrap', render: (e) => el('span', {}, el('span', { class: 'small', text: summarize(e.payload) }), e.evidence_ids && e.evidence_ids.length ? evChips(e.evidence_ids) : null) },
        { label: t('log.hash'), render: (e) => el('span', { class: 'dim small', title: `prev ${e.prev_hash}`, text: e.hash.slice(0, 12) }) },
      ],
      rows, pageSize: 30,
    }));
  }
  [fType, fAction, fActor].forEach((x) => x.addEventListener('change', load));
  fId.addEventListener('change', load);
  await load();

  // ---- audit of human decisions (reviewer)
  const audit = section(t('log.overrides'), { level: 'reviewer' });
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
      { label: t('log.seq'), key: 'seq', num: true }, { label: t('log.ts'), render: (e) => fmt.ts(e.ts) }, { label: t('log.actor'), render: (e) => e.actor.replace('human:', '') }, { label: t('log.action'), key: 'action' }, { label: t('log.object'), render: (e) => `${e.object_type} ${e.object_id}` }, { label: t('common.note'), cls: 'wrap', render: (e) => (e.payload && (e.payload.note || (e.payload.new_value ? JSON.stringify(e.payload.new_value) : ''))) || '' },
    ], rows: hs.filter((e) => ['override', 'accept', 'question', 'dismiss', 'set_role', 'name_pattern', 'approve_rule', 'reject_rule', 'apply_assessor_action', 'settings'].includes(e.action)).reverse(), pageSize: 20 }));
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
    else parts.push(`${k}: ${String(v).length > 120 ? String(v).slice(0, 120) + '…' : v}`);
  }
  return parts.join('  ');
}
