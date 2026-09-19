/* "External model use" card of the Data-flow view (hybrid / eu-hosted profiles).
   One plain sentence first: what leaves this machine, what never leaves, which model, and that Fable-class models
   are refused because of their data retention. Then the profile cards (handed in by the view), the external model
   picker (Sonnet 5 / Opus 5; the server refuses anything else that is not allowed), calls and tokens against the
   run's budget, seconds per call local vs external, the latency table of `tpm bench-llm` when the run has one, and
   what the egress guard changed before sending.
   Numbers come from GET /api/runs/{id}/llm/usage. Without a run, with an older server, or in a no-egress profile the
   card still says something true ("nothing leaves this machine") from GET /api/settings. */
import { state, t, el, clear, api, runApi, fmt, chip, section, table, toast, errText, bus, notice, roleAllows } from './core.js';

// what the picker offers; the server's list (external_models_allowed) decides which of them can be chosen
const CHOICES = [
  { id: 'claude-sonnet-5', label: 'Sonnet 5', hint: 'flow.ext.sonnetHint' },
  { id: 'claude-opus-5', label: 'Opus 5', hint: 'flow.ext.opusHint' },
];
export function modelLabel(id) { const c = CHOICES.find((x) => x.id === id); return c ? c.label : String(id || ''); }

// ---------------------------------------------------------------- facts (pure: checked under node)
/** 2140 -> "2.1", 31200 -> "31": seconds of one model call, short enough for a tile; null when not measured */
export function callSeconds(ms) {
  if (ms === null || ms === undefined || ms === '' || isNaN(ms)) return null;
  const s = Number(ms) / 1000;
  return s < 10 ? s.toFixed(1) : s.toFixed(0);
}
/** 'off' (the profile lets nothing leave), 'unusable' (allowed, but no key / refused model / no EU endpoint) or 'on'. */
export function externalMode(d) {
  if (!d || !d.allow_external) return 'off';
  return d.external_unavailable_reason || d.external_model_blocked_reason ? 'unusable' : 'on';
}
/** i18n key of the reason the external model is not used; 'flow.ext.reason.other' shows the server's own words. */
export function unusableKey(d) {
  const why = String((d && d.external_unavailable_reason) || '');
  if (d && d.external_model_blocked_reason) return 'flow.ext.reason.blocked';
  if (/^no API key/i.test(why)) return 'flow.ext.reason.key';
  if (/base_url|endpoint/i.test(why)) return 'flow.ext.reason.endpoint';
  return 'flow.ext.reason.other';
}
/** The numbers of the card from the /llm/usage answer. */
export function usageFacts(d) {
  const u = (d && d.usage) || {};
  const caps = (d && d.caps) || {};
  const num = (v) => (v === null || v === undefined || isNaN(v) ? null : Number(v));
  const cap = num(u.max_calls_per_run) !== null ? num(u.max_calls_per_run) : num(caps.max_calls_per_run);
  const used = num(u.external_ok) || 0;
  const localMs = num(u.avg_latency_ms_local);
  const externalMs = num(u.avg_latency_ms_external);
  return {
    used, cap,
    left: num(u.budget_left_calls) !== null ? num(u.budget_left_calls) : (cap === null ? null : Math.max(0, cap - used)),
    tokensIn: num(u.input_tokens) || 0, tokensOut: num(u.output_tokens) || 0,
    blocked: num(u.blocked) || 0, refused: num(u.budget_refused) || 0,
    localMs, externalMs,
    speedup: localMs && externalMs ? Math.round((localMs / externalMs) * 10) / 10 : null,
    byTask: Object.entries(u.by_task || {}).map(([task, v]) => ({ task, ...v })),
  };
}
/** The card's data when there is no run (or an older server): the same fields, taken from GET /api/settings. */
export function factsFromSettings(s) {
  s = s || {};
  return { profile: s.profile, allow_external: !!s.allow_external, external_model: s.external_model, external_models_allowed: s.external_models_allowed, external_model_blocked_reason: s.external_model_blocked_reason || null, external_unavailable_reason: s.external_unavailable_reason || null, caps: s.external_caps || {}, guard: { external_sig_digits: s.external_sig_digits }, usage: null, sanitizer: null, benchmark: null };
}

// ---------------------------------------------------------------- the card
/** `profileCards`: the view's profile selector (a node), shown inside the card. `onChanged(settings)`: called after
    the model was changed so the view can refresh what it shows from the settings. Returns { root, reload(settings) }. */
export function externalUseCard({ settings, profileCards, onChanged } = {}) {
  const badge = el('span', { class: 'row small' });
  const sec = section(t('flow.ext.title'), { right: badge });
  sec.root.classList.add('extuse');
  sec.root.dataset.briefSection = 'external';
  const lead = el('div', { class: 'extuse-lead' });
  const picker = el('div', { class: 'profiles' });
  const pickerNote = el('p', { class: 'small muted extuse-note' });
  const numbers = el('div', { class: 'extuse-numbers' });
  // the summary card's "choose what may leave" action points at section:profile
  sec.body.append(lead,
    profileCards ? el('div', { dataset: { briefSection: 'profile' } }, el('h3', { class: 'extuse-h', text: t('flow.profile') }), profileCards) : null,
    el('h3', { class: 'extuse-h', text: t('flow.ext.model') }), picker, pickerNote, numbers);
  let current = settings || state.settings || {};

  async function choose(id) {
    const r = await api('/api/settings', { method: 'PUT', body: { external_llm: { model: id } } });
    if (!r.ok) { toast(errText(r), 'fail'); return; }
    toast(t('flow.ext.modelChanged', { m: modelLabel(id) }), 'ok');
    await reload({ ...current, external_model: id });   // repaint at once; the full settings follow (they probe the local model server)
    const ns = await api('/api/settings');
    if (ns.ok) { state.settings = ns.data; current = ns.data; bus.emit('settings.changed', state.settings); if (onChanged) onChanged(ns.data); }
  }

  function paintLead(d, mode) {
    const model = modelLabel(d.external_model);
    const digits = (d.guard && d.guard.external_sig_digits) || 3;
    clear(lead);
    clear(badge).append(chip(t({ on: 'flow.ext.badgeOn', off: 'flow.ext.badgeOff', unusable: 'flow.ext.badgeUnusable' }[mode], { m: model }), { on: 'warn', off: 'ok', unusable: '' }[mode]));
    lead.append(el('p', { class: 'extuse-first', text: t(mode === 'on' ? 'flow.ext.leadOn' : mode === 'off' ? 'flow.ext.leadOff' : 'flow.ext.leadUnusable', { model }) }));
    if (mode === 'unusable') {
      const key = unusableKey(d);
      lead.append(notice(key === 'flow.ext.reason.other' ? t(key, { reason: d.external_unavailable_reason || d.external_model_blocked_reason || '' }) : t(key), 'warn'));
    }
    lead.append(el('p', { class: 'extuse-more', text: t(mode === 'off' ? 'flow.ext.leadOffMore' : 'flow.ext.leadOnMore', { model, digits }) }));
  }

  function paintPicker(d, mode) {
    clear(picker);
    const allowed = d.external_models_allowed || CHOICES.map((c) => c.id);
    for (const c of CHOICES) {
      const ok = allowed.includes(c.id);
      picker.append(el('button', { type: 'button', class: 'profile', disabled: !ok, 'aria-pressed': String(c.id === d.external_model), onClick: () => { if (c.id !== d.external_model) choose(c.id); } },
        el('span', { class: 'name' }, el('span', { class: 'pname', text: c.label }), el('span', { class: 'dim small', text: c.id })),
        el('span', { class: 'desc', text: t(c.hint) })));
    }
    const custom = d.external_model && !CHOICES.some((c) => c.id === d.external_model);
    pickerNote.textContent = [custom ? t('flow.ext.modelCustom', { m: d.external_model }) : '', t('flow.ext.modelNote'), mode === 'off' ? t('flow.ext.modelOffNote') : ''].filter(Boolean).join(' ');
  }

  function tile(value, label, sub) { return el('div', { class: 'extuse-stat' }, el('b', { text: value }), el('span', { text: label }), sub ? el('span', { class: 'sub', text: sub }) : null); }

  function paintNumbers(d, mode) {
    clear(numbers);
    if (!state.run) { numbers.append(el('p', { class: 'small muted', text: t('flow.ext.noRun') })); return; }
    if (!d.usage) return;
    const f = usageFacts(d);
    if (mode === 'off' && !f.used && !f.blocked && !f.refused) return;   // nothing was ever sent from this run: the sentence above says it all
    const sLocal = callSeconds(f.localMs), sExt = callSeconds(f.externalMs);
    const notes = [f.left !== null ? t('flow.ext.callsLeft', { n: fmt.int(f.left) }) : '', f.blocked ? t('flow.ext.blockedN', { n: f.blocked }) : '', f.refused ? t('flow.ext.refusedN', { n: f.refused }) : ''].filter(Boolean).join(' · ');
    numbers.append(el('div', { class: 'extuse-stats' },
      tile(f.cap !== null ? t('flow.ext.callsOf', { n: fmt.int(f.used), cap: fmt.int(f.cap) }) : fmt.int(f.used), t('flow.ext.calls'), notes),
      tile(`${fmt.int(f.tokensIn)} / ${fmt.int(f.tokensOut)}`, t('flow.ext.tokens')),
      tile(sLocal === null ? '–' : `${sLocal} s`, t('flow.ext.secLocal'), sLocal === null ? t('flow.ext.notMeasured') : ''),
      tile(sExt === null ? '–' : `${sExt} s`, t('flow.ext.secExternal'), sExt === null ? t('flow.ext.notMeasured') : (f.speedup && f.speedup > 1 ? t('flow.ext.faster', { x: f.speedup }) : ''))));

    // what the guard changed before sending
    const san = d.sanitizer || {};
    numbers.append(el('h3', { class: 'extuse-h', text: t('flow.ext.sanTitle') }));
    if (!san.payloads) numbers.append(el('p', { class: 'small muted', text: t('flow.ext.sanNone') }));
    else numbers.append(el('div', { class: 'extuse-stats' }, tile(fmt.int(san.numbers_rounded), t('flow.ext.sanRounded')), tile(fmt.int(san.names_aliased), t('flow.ext.sanAliased')), tile(fmt.int(san.values_withheld), t('flow.ext.sanWithheld'))),
      el('p', { class: 'small muted extuse-note', text: t('flow.ext.sanNote', { n: fmt.int(san.payloads), digits: (d.guard && d.guard.external_sig_digits) || 3 }) }));

    // measured speed: `tpm bench-llm` wrote llm_benchmark.json
    const rows = (d.benchmark && d.benchmark.rows) || [];
    const sec2 = (v) => (v === null || v === undefined ? '–' : fmt.num(v, v < 10 ? 1 : 0));
    if (rows.length) numbers.append(el('h3', { class: 'extuse-h', text: t('flow.ext.bench') }), table({ columns: [
      { label: t('flow.task'), key: 'task' },
      { label: t('flow.ext.benchLocal'), render: (r) => sec2(r.local_s), num: true },
      { label: t('flow.ext.benchExternal'), render: (r) => sec2(r.external_s), num: true },
      { label: t('flow.ext.benchSpeedup'), render: (r) => (r.speedup ? `${r.speedup}×` : '–'), num: true },
    ], rows, pageSize: 20, keyOf: (r) => r.task }));
    else if (roleAllows('engineer') && f.byTask.length) numbers.append(el('h3', { class: 'extuse-h', text: t('flow.ext.byTask') }), table({ columns: [
      { label: t('flow.task'), key: 'task' },
      { label: t('flow.ext.callsLocal'), render: (r) => fmt.int(r.local_ok || 0), num: true },
      { label: t('flow.ext.callsExternal'), render: (r) => fmt.int(r.external_ok || 0), num: true },
      { label: t('flow.ext.benchLocal'), render: (r) => { const s = callSeconds(r.avg_latency_ms_local); return s === null ? '–' : s; }, num: true },
      { label: t('flow.ext.benchExternal'), render: (r) => { const s = callSeconds(r.avg_latency_ms_external); return s === null ? '–' : s; }, num: true },
    ], rows: f.byTask, pageSize: 20, keyOf: (r) => r.task }));
    if (f.used) numbers.append(el('p', { class: 'small muted extuse-note', text: t('flow.ext.ledgerHint') }));
  }

  async function reload(newSettings) {
    if (newSettings) current = newSettings;
    const r = state.run ? await runApi('/llm/usage') : { ok: false };
    const d = r.ok && r.data && r.data.caps ? r.data : factsFromSettings(current);
    const mode = externalMode(d);
    sec.root.dataset.mode = mode;
    paintLead(d, mode);
    paintPicker(d, mode);
    paintNumbers(d, mode);
  }

  return { root: sec.root, reload };
}
