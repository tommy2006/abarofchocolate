/* View 7: data flow — profile switch with confirmation, model status, plain statement, egress ledger. */
import { state, t, el, clear, api, runApi, fmt, chip, section, table, viewHead, empty, hiddenHint, kv, st, confirmDialog, toast, errText, bus, roleAllows, notice } from '../core.js';

export async function render(main) {
  const view = el('div', { class: 'view' });
  main.append(view);
  view.append(viewHead('7', t('nav.dataflow')));
  const s = state.settings || (await api('/api/settings')).data;
  const eg = state.run ? await runApi('/egress') : { ok: false, data: {} };
  const E = eg.ok ? eg.data : {};

  // ---- profile switch
  const ps = section(t('flow.profile'));
  view.append(ps.root);
  const cards = el('div', { class: 'profiles' });
  const renderCards = () => {
    clear(cards);
    for (const [name, p] of Object.entries(s.profiles || {})) {
      cards.append(el('button', { type: 'button', class: 'profile', 'aria-pressed': String(name === s.profile), onClick: async () => {
        if (name === s.profile) return;
        if (!(await confirmDialog(t('flow.changeConfirm', { p: name, desc: p.description || '' })))) return;
        const r = await api('/api/settings', { method: 'PUT', body: { profile: name } });
        if (r.ok) { toast(t('flow.changed', { p: name }), 'ok'); const ns = await api('/api/settings'); if (ns.ok) { state.settings = ns.data; Object.assign(s, ns.data); } bus.emit('settings.changed', state.settings); renderCards(); renderStatement(); }
        else toast(errText(r), 'fail');
      } }, el('span', { class: 'name' }, name, st(p.allow_external ? 'warn' : 'ok', p.allow_external ? t('status.egressPossible') : t('status.noEgress'))), el('span', { class: 'desc', text: p.description || '' }), el('span', { class: 'desc', text: `${t('flow.guard')}: ${p.guard_strict ? t('flow.strict') : t('flow.standard')}` })));
    }
  };
  renderCards();
  ps.body.append(cards);

  // ---- model status
  const ms = section(t('flow.models'));
  view.append(ms.root);
  const models = s.models || {};
  ms.body.append(kv([
    [t('flow.localModel'), el('span', {}, s.local_model, ' ', st(models.local ? 'ok' : 'fail', models.local ? t('status.loaded') : t('status.notLoaded')), el('span', { class: 'dim small', text: ` ${s.local_base_url || ''}` }))],
    [t('flow.externalModel'), el('span', {}, `${s.external_provider || ''} ${s.external_model || ''}`, s.external_base_url ? el('span', { class: 'dim small', text: ` ${s.external_base_url}` }) : null)],
    [t('flow.keyConfigured'), st(s.external_key_configured ? 'ok' : 'pending', s.external_key_configured ? t('common.yes') : t('common.no'))],
    [t('flow.routeExists'), st(s.external_route_exists ? 'warn' : 'ok', s.external_route_exists ? t('status.exists') : t('status.notExists'))],
    [t('status.calls'), `${s.external_calls || 0} ${t('flow.summary.sent')}, ${s.external_blocked || 0} ${t('flow.summary.blocked')}`],
    [t('flow.guard'), s.guard_strict ? t('flow.strict') : t('flow.standard')],
  ]));
  if (roleAllows('engineer') && s.routing) ms.body.append(el('h3', { class: 'small muted', style: { marginTop: '12px' }, text: t('flow.routing') }), el('div', { class: 'sigchips' }, Object.entries(s.routing).map(([task, route]) => chip(`${task}: ${route}`, route === 'external' ? 'warn' : 'ok'))));

  // ---- statement
  const ss = section(t('flow.statement'));
  view.append(ss.root);
  const stmt = el('div', { class: 'statement' });
  ss.body.append(stmt);
  async function renderStatement() {
    const r = state.run ? await runApi('/egress') : { ok: false };
    stmt.textContent = r.ok && r.data.statement ? r.data.statement : (s.allow_external ? t('status.egressPossible') : t('status.noEgress'));
  }
  await renderStatement();

  // ---- ledger
  const ls = section(t('flow.ledger'), { level: 'reviewer', right: E.summary ? el('span', { class: 'row small muted' }, chip(`${E.summary.local || 0} ${t('flow.summary.local')}`, 'ok'), chip(`${E.summary.external_allowed || 0} ${t('flow.summary.sent')}`, E.summary.external_allowed ? 'warn' : ''), chip(`${E.summary.external_blocked || 0} ${t('flow.summary.blocked')}`)) : null });
  view.append(ls.root);
  if (!state.run) ls.body.append(el('div', { class: 'notice warn', text: t('runs.noRunHint') }));
  else if (!(E.ledger || []).length) ls.body.append(empty(t('flow.noLedger')));
  else ls.body.append(table({ columns: [
    { label: '', key: 'id' }, { label: t('log.ts'), render: (r) => fmt.ts(r.ts) }, { label: t('flow.task'), key: 'task' },
    { label: t('flow.route'), render: (r) => chip(r.route, r.route === 'external' ? 'warn' : 'ok') },
    { label: t('flow.model'), render: (r) => `${r.provider} ${r.model}` },
    { label: t('flow.artifacts'), render: (r) => (r.artifact_types || []).join(', ') },
    { label: t('flow.bytes'), render: (r) => fmt.bytes(r.payload_bytes), num: true },
    { label: t('flow.guardResult'), render: (r) => el('span', {}, chip(r.guard_result, { allowed: 'ok', blocked: 'fail', fallback: 'warn' }[r.guard_result] || ''), r.guard_reason ? el('span', { class: 'dim small', text: ' ' + r.guard_reason }) : null) },
    { label: t('common.status'), render: (r) => st(r.ok ? 'ok' : 'fail', r.ok ? 'ok' : (r.error || 'error')) },
    { label: t('flow.preview'), cls: 'wrap', render: (r) => el('span', { class: 'small dim', text: r.payload_preview || '' }) },
  ], rows: (E.ledger || []).slice().reverse(), pageSize: 20 }));
  const hh = hiddenHint(view); if (hh) view.append(hh);
  return view;
}
