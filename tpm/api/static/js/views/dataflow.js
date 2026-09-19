/* View 7: data flow — external model use (plain sentence, profile switch with confirmation, external model, calls /
   tokens / seconds per call: js/externaluse.js), model status, plain statement, egress ledger. */
import { state, t, el, clear, api, runApi, fmt, chip, section, table, viewHead, empty, hiddenHint, kv, st, confirmDialog, toast, errText, bus, roleAllows, notice, linkifyRefs, cleanText, refLink, actorName, actorRole } from '../core.js';
import { summaryCard, techDetails } from '../brief.js';
import { externalUseCard } from '../externaluse.js';

export async function render(main) {
  // page = title, plain summary (did anything leave this computer?), then ONE expander ("Show technical analyses")
  // with everything this view rendered before; without a selected run there is no summary and the expander is open
  const page = el('div', { class: 'view' });
  main.append(page);
  page.append(viewHead('7', t('nav.dataflow')));
  const card = summaryCard('dataflow');
  const tech = techDetails('dataflow');
  if (card) page.append(card); else tech.open = true;
  page.append(tech);
  const view = tech.body;
  const s = state.settings || (await api('/api/settings')).data;
  const eg = state.run ? await runApi('/egress', { params: { lang: state.lang } }) : { ok: false, data: {} };
  const E = eg.ok ? eg.data : {};

  // ---- external model use: plain sentence, profile switch, external model, calls / tokens / seconds per call.
  // Stays above the expander: this is where a person decides what may leave this machine.
  const cards = el('div', { class: 'profiles' });
  const ext = externalUseCard({ settings: s, profileCards: cards, onChanged: (ns) => { Object.assign(s, ns); renderModels(); renderStatement(); if (card && card.reload) card.reload(); } });
  page.insertBefore(ext.root, tech);
  const renderCards = () => {
    clear(cards);
    for (const [name, p] of Object.entries(s.profiles || {})) {
      cards.append(el('button', { type: 'button', class: 'profile', 'aria-pressed': String(name === s.profile), onClick: async () => {
        if (name === s.profile) return;
        if (!(await confirmDialog(t('flow.changeConfirm', { p: name, desc: p.description || '' })))) return;
        const r = await api('/api/settings', { method: 'PUT', body: { profile: name } });
        if (r.ok) { toast(t('flow.changed', { p: name }), 'ok'); const ns = await api('/api/settings'); if (ns.ok) { state.settings = ns.data; Object.assign(s, ns.data); } bus.emit('settings.changed', state.settings); renderCards(); renderModels(); renderStatement(); ext.reload(s); if (card && card.reload) card.reload(); }
        else toast(errText(r), 'fail');
      } }, el('span', { class: 'name' }, el('span', { class: 'pname', text: name }), st(p.allow_external ? 'warn' : 'ok', p.allow_external ? t('status.egressPossible') : t('status.noEgress'))), el('span', { class: 'desc', text: p.description || '' }), el('span', { class: 'desc', text: `${t('flow.guard')}: ${p.guard_strict ? t('flow.strict') : t('flow.standard')}` })));
    }
  };
  renderCards();
  await ext.reload(s);

  // ---- model status
  const ms = section(t('flow.models'));
  view.append(ms.root);
  function renderModels() {
    const models = s.models || {};
    clear(ms.body).append(kv([
      [t('flow.localModel'), el('span', {}, s.local_model, ' ', st(models.local ? 'ok' : 'fail', models.local ? t('status.loaded') : t('status.notLoaded')), el('span', { class: 'dim small', text: ` ${s.local_base_url || ''}` }))],
      [t('flow.externalModel'), el('span', {}, `${s.external_provider || ''} ${s.external_model || ''}`, s.external_base_url ? el('span', { class: 'dim small', text: ` ${s.external_base_url}` }) : null)],
      [t('flow.keyConfigured'), st(s.external_key_configured ? 'ok' : 'pending', s.external_key_configured ? t('common.yes') : t('common.no'))],
      [t('flow.routeExists'), st(s.external_route_exists ? 'warn' : 'ok', s.external_route_exists ? t('status.exists') : t('status.notExists'))],
      [t('status.calls'), `${s.external_calls || 0} ${t('flow.summary.sent')}, ${s.external_blocked || 0} ${t('flow.summary.blocked')}`],
      [t('flow.guard'), s.guard_strict ? t('flow.strict') : t('flow.standard')],
    ]));
    if (roleAllows('engineer') && s.routing) ms.body.append(el('h3', { class: 'small muted', style: { marginTop: '12px' }, text: t('flow.routing') }), el('div', { class: 'sigchips' }, Object.entries(s.routing).map(([task, route]) => chip(`${task}: ${route}`, route === 'external' ? 'warn' : 'ok'))));
  }
  renderModels();

  // ---- statement
  const ss = section(t('flow.statement'));
  view.append(ss.root);
  const stmt = el('div', { class: 'statement' });
  ss.body.append(stmt);
  async function renderStatement() {
    const r = state.run ? await runApi('/egress', { params: { lang: state.lang } }) : { ok: false };
    clear(stmt);
    stmt.append(linkifyRefs(cleanText(r.ok && r.data.statement ? r.data.statement : (s.allow_external ? t('status.egressPossible') : t('status.noEgress')))));
  }
  await renderStatement();
  // who wrote the explanations: how many a language model wrote, and why the others use the evidence template
  if (E.coverage && E.coverage.sentence) {
    ss.body.append(el('h3', { class: 'small muted', style: { marginTop: '12px' }, text: t('flow.coverage') }), el('p', { text: E.coverage.sentence }));
    if ((E.coverage.details || []).length) ss.body.append(el('ul', { class: 'small muted' }, E.coverage.details.map((d) => el('li', { text: d }))));
  }

  // ---- the egress guard shown on this run's own data: a real payload before / after the guard, an operator question
  // naming the original column names, and a deliberately unsafe payload of raw rows that the guard blocks. Nothing is
  // sent from here (the command line has --send); the ledger marks these records demo_allowed / demo_blocked.
  const gs = section(t('flow.guardDemo'));
  gs.root.dataset.briefSection = 'guard';
  view.append(gs.root);
  const guardCard = (head, kind, lines) => el('div', { style: { border: '1px solid var(--line)', borderTop: `4px solid var(--${kind})`, borderRadius: 'var(--radius)', padding: '10px 12px', background: 'var(--bg-2)', minWidth: '0' } },
    el('div', { style: { marginBottom: '6px' } }, head), el('ul', { class: 'small', style: { margin: '0', paddingLeft: '18px', overflowWrap: 'anywhere' } }, (lines || []).map((x) => el('li', { text: x }))));
  const renderGuard = (gd) => {
    clear(gs.body);
    gs.body.append(el('p', { class: 'hint', text: t('flow.guardDemoHint') }));
    if (!state.run) { gs.body.append(el('div', { class: 'notice warn', text: t('runs.noRunHint') })); return; }
    const btn = el('button', { class: 'btn', type: 'button' }, gd ? t('flow.guardDemoAgain') : t('flow.guardDemoRun'));
    btn.addEventListener('click', async () => {
      btn.disabled = true; btn.textContent = t('flow.guardDemoWorking');
      const r = await runApi('/guard-demo', { method: 'POST', body: { lang: state.lang, actor: actorName(), role: actorRole() } });
      btn.disabled = false;
      if (!r.ok) { btn.textContent = gd ? t('flow.guardDemoAgain') : t('flow.guardDemoRun'); toast(errText(r), 'fail'); return; }
      if (!gs.root.isConnected) return;
      renderGuard(r.data.guard_demo);
      toast(r.data.unsafe_blocked && !r.data.headers_leaked ? t('flow.guardDemoBlocked') : t('flow.guardDemoNotBlocked'), r.data.unsafe_blocked && !r.data.headers_leaked ? 'ok' : 'fail');
    });
    gs.body.append(el('div', { class: 'row' }, btn));
    if (!gd) return;
    gs.body.append(el('h3', { style: { marginTop: '12px' }, text: gd.title }), el('p', { class: 'small muted', text: gd.intro }));
    gs.body.append(el('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(280px, 100%), 1fr))', gap: '10px', margin: '8px 0' } },
      guardCard(el('span', {}, chip(t('flow.guardSafe'), 'ok')), 'ok', gd.safe),
      guardCard(el('span', {}, chip(gd.unsafe_ok ? 'demo_blocked' : 'demo_allowed', gd.unsafe_ok ? 'fail' : 'warn'), ' ', el('b', { text: t('flow.guardUnsafe') })), gd.unsafe_ok ? 'fail' : 'warn', gd.unsafe)));
    if (gd.question) gs.body.append(el('h3', { class: 'small', text: gd.question.head }), kv([[gd.question.before_label, el('span', { class: 'mono small', style: { overflowWrap: 'anywhere' }, text: gd.question.before || '' })], [gd.question.after_label, el('span', { class: 'mono small', style: { overflowWrap: 'anywhere' }, text: gd.question.after || '' })]]));
    if ((gd.layers || []).length && roleAllows('engineer')) gs.body.append(el('details', {}, el('summary', { class: 'small', text: gd.layers_label }), el('ul', { class: 'small mono', style: { overflowWrap: 'anywhere' } }, gd.layers.map((x) => el('li', { text: x })))));
    gs.body.append(el('p', { class: 'small muted', text: `${gd.headers || ''} ${gd.ledger || ''}`.trim() }));
  };
  renderGuard(E.guard_demo);

  // ---- ledger
  const ls = section(t('flow.ledger'), { level: 'engineer', right: E.summary ? el('span', { class: 'row small muted' }, chip(`${E.summary.local || 0} ${t('flow.summary.local')}`, 'ok'), chip(`${E.summary.external_allowed || 0} ${t('flow.summary.sent')}`, E.summary.external_allowed ? 'warn' : ''), chip(`${E.summary.external_blocked || 0} ${t('flow.summary.blocked')}`)) : null });
  // "See the list of what was sent where": the ledger for reviewers, the plain statement for everybody else
  (ls.root.hidden ? ss.root : ls.root).dataset.briefSection = 'ledger';
  view.append(ls.root);
  if (!state.run) ls.body.append(el('div', { class: 'notice warn', text: t('runs.noRunHint') }));
  else if (!(E.ledger || []).length) ls.body.append(empty(t('flow.noLedger')));
  else ls.body.append(table({ columns: [
    { label: '', render: (r) => refLink('egress', r.id, r.id.replace('EGR-', '#')) }, { label: t('log.ts'), render: (r) => fmt.ts(r.ts) }, { label: t('flow.task'), key: 'task' },
    { label: t('flow.route'), render: (r) => chip(r.route, r.route === 'external' ? 'warn' : 'ok') },
    { label: t('flow.model'), cls: 'wrap', render: (r) => `${r.provider} ${r.model}` },
    { label: t('flow.artifacts'), cls: 'wrap', render: (r) => (r.artifact_types || []).join(', ') },
    { label: t('flow.bytes'), render: (r) => fmt.bytes(r.payload_bytes), num: true },
    { label: t('flow.ext.tokensCol'), render: (r) => (r.input_tokens || r.output_tokens ? `${fmt.int(r.input_tokens || 0)} / ${fmt.int(r.output_tokens || 0)}` : ''), num: true },
    { label: t('flow.guardResult'), cls: 'wrap', render: (r) => el('span', {}, chip(r.guard_result, { allowed: 'ok', blocked: 'fail', fallback: 'warn', budget: 'warn', demo_blocked: 'fail', demo_allowed: '' }[r.guard_result] || ''), r.guard_reason ? el('span', { class: 'dim small', text: ' ' + r.guard_reason }) : null) },
    { label: t('common.status'), render: (r) => st(r.ok ? 'ok' : 'fail', r.ok ? 'ok' : (r.error || 'error')) },
    // external records keep the SANITISED payload only: exactly what was sent, after the guard cleaned it
    { label: t('flow.ext.previewCol'), cls: 'wrap', render: (r) => el('span', { class: 'small dim preview' }, linkifyRefs(r.payload_preview || '')) },
  ], rows: (E.ledger || []).slice().reverse(), pageSize: 20 }));
  const hh = hiddenHint(view); if (hh) view.append(hh);
  return view;
}
