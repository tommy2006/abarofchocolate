/* Trustworthy Process Monitor — frontend entry. Boot, router (hash), numbered rail, data-control
   lamps, role picker, language + theme switch, SSE subscription for the selected run. */
import { state, t, el, clear, api, loadLang, store, bus, toast, modal, st, fmt, roleAllows, navigate, recordNavigation, installRefHandler } from './js/core.js';
import { initChat } from './js/chat.js';
import * as runs from './js/views/runs.js';
import * as understanding from './js/views/understanding.js';
import * as quality from './js/views/quality.js';
import * as monitor from './js/views/monitor.js';
import * as diagnoses from './js/views/diagnoses.js';
import * as assessor from './js/views/assessor.js';
import * as log from './js/views/log.js';
import * as dataflow from './js/views/dataflow.js';
import * as report from './js/views/report.js';

export { navigate };

const VIEWS = [
  { id: 'runs', num: '0', key: 'nav.runs', mod: runs },
  { id: 'understanding', num: '1', key: 'nav.understanding', mod: understanding },
  { id: 'quality', num: '2', key: 'nav.quality', mod: quality },
  { id: 'monitor', num: '3', key: 'nav.monitor', mod: monitor },
  { id: 'diagnoses', num: '4', key: 'nav.diagnoses', mod: diagnoses },
  { id: 'assessor', num: '5', key: 'nav.assessor', mod: assessor },
  { id: 'log', num: '6', key: 'nav.log', mod: log, level: 'reviewer' },
  { id: 'dataflow', num: '7', key: 'nav.dataflow', mod: dataflow },
  { id: 'report', num: '8', key: 'nav.report', mod: report },
];
let current = null;
let rendering = false;

// ---------------------------------------------------------------- router
function parseHash() {
  const h = location.hash.replace(/^#\/?/, '');
  const [path, qs] = h.split('?');
  const params = Object.fromEntries(new URLSearchParams(qs || ''));
  return { view: path || 'runs', params };
}
async function route() {
  recordNavigation(location.hash || '#/runs');
  const { view, params } = parseHash();
  const def = VIEWS.find((v) => v.id === view) || VIEWS[0];
  state.view = def.id;
  document.querySelectorAll('.rail-item').forEach((b) => b.setAttribute('aria-current', b.dataset.view === def.id ? 'page' : 'false'));
  const main = document.getElementById('main');
  if (current && current.cleanup) { try { current.cleanup(); } catch { /* ignore */ } }
  clear(main);
  rendering = true;
  try { current = await def.mod.render(main, params); }
  catch (e) { console.error(e); main.append(el('div', { class: 'notice fail', text: t('common.error', { msg: e.message }) })); current = null; }
  rendering = false;
  main.focus({ preventScroll: true });
}
window.addEventListener('hashchange', route);
bus.on('route.same', () => route());

// ---------------------------------------------------------------- rail
function renderRail() {
  const rail = document.getElementById('rail');
  clear(rail);
  rail.append(el('div', { class: 'rail-tag', text: t('app.tagline') }));
  for (const v of VIEWS) {
    const b = el('button', { class: 'rail-item', type: 'button', dataset: { view: v.id }, 'aria-current': state.view === v.id ? 'page' : 'false', onClick: () => navigate(v.id) }, el('span', { class: 'rail-num', text: v.num }), el('span', {}, t(v.key), v.level && !roleAllows(v.level) ? el('span', { class: 'badge', title: t('role.tag.' + v.level), text: v.level[0] }) : null));
    rail.append(b);
  }
}

// ---------------------------------------------------------------- lamps (data-control strip)
function renderLamps() {
  const s = state.settings || {};
  const lamps = document.getElementById('lamps');
  clear(lamps);
  const lamp = (stateCls, label, value, title) => el('div', { class: 'lamp', 'data-state': stateCls, title: title || '' }, el('span', { class: 'lamp-dot' }), el('span', { class: 'lamp-label', text: label }), el('span', { class: 'lamp-value', text: value }));
  const models = s.models || {};
  lamps.append(
    lamp(s.allow_external ? 'warn' : 'ok', t('status.profile'), s.profile || '–', s.allow_external ? t('status.egressPossible') : t('status.noEgress')),
    lamp(models.local ? 'ok' : 'off', t('status.local'), (models.local_model || s.local_model || '–') + (models.local ? '' : ` (${t('status.notLoaded')})`), models.local ? t('status.loaded') : t('status.notLoaded')),
    lamp(s.external_route_exists ? 'warn' : 'off', t('status.external'), s.external_route_exists ? `${t('status.exists')}: ${s.external_model}` : t('status.notExists')),
    lamp(s.external_calls ? 'warn' : 'ok', t('status.calls'), String(s.external_calls || 0) + (s.external_blocked ? ` (+${s.external_blocked} ${t('flow.summary.blocked')})` : '')),
  );
}
async function refreshSettings() { const r = await api('/api/settings'); if (r.ok) { state.settings = r.data; renderLamps(); } }

// ---------------------------------------------------------------- language / theme / user
function renderLangs() {
  const box = document.getElementById('langs');
  clear(box);
  for (const l of (state.settings ? state.settings.languages : ['en', 'fi', 'sv'])) box.append(el('button', { type: 'button', 'aria-pressed': String(state.lang === l), onClick: async () => { await loadLang(l); bus.emit('lang.changed', l); renderAll(); route(); } }, l.toUpperCase()));
}
function applyTheme() {
  const th = state.theme;
  if (th === 'auto') document.documentElement.removeAttribute('data-theme'); else document.documentElement.setAttribute('data-theme', th);
  const b = document.getElementById('theme-btn');
  b.textContent = { auto: '◐', light: '☼', dark: '☾' }[th];
  b.title = `${t('status.theme')}: ${t('status.theme.' + th)}`;
  b.setAttribute('aria-label', b.title);
}
function renderUser() {
  const b = document.getElementById('user-btn');
  clear(b);
  if (state.user) b.append(el('span', { text: state.user.name }), el('span', { class: 'role', text: t('role.' + state.user.role) }));
  else b.append(t('role.pick.title'));
  b.title = t('role.change');
}
function pickRole() {
  return new Promise((resolve) => {
    const name = el('input', { type: 'text', value: state.user ? state.user.name : '', placeholder: t('role.pick.name'), required: true, style: { width: '100%' } });
    const opts = ['operator', 'engineer', 'reviewer'].map((r) => { const inp = el('input', { type: 'radio', name: 'role', value: r }); if ((state.user && state.user.role === r) || (!state.user && r === 'operator')) inp.checked = true; return el('label', { class: 'opt' }, inp, el('span', {}, el('b', { text: t('role.' + r) }), el('span', { text: t('role.pick.' + r) }))); });
    const body = el('div', { class: 'stack' }, el('p', { class: 'hint', text: t('role.pick.intro') }), el('label', { class: 'field' }, el('span', { text: t('role.pick.name') }), name), el('div', { class: 'rolepick' }, opts));
    const m = modal({ title: t('role.pick.title'), body, onClose: () => resolve(state.user), actions: [{ label: t('role.pick.start'), cls: 'btn-primary', onClick: (close) => { const n = name.value.trim(); if (!n) { name.focus(); return; } const role = body.querySelector('input[name=role]:checked').value; state.user = { name: n, role }; store.set('user', state.user); renderUser(); renderRail(); bus.emit('user.changed', state.user); close(); resolve(state.user); } }] });
    name.focus();
  });
}

// ---------------------------------------------------------------- runs + SSE
async function selectRun(id) {
  if (state.es) { state.es.close(); state.es = null; }
  state.run = id || null;
  state.runStatus = null;
  store.set('run', state.run);
  const sel = document.getElementById('run-select');
  sel.value = state.run || '';
  if (state.run) {
    const r = await api(`/api/runs/${encodeURIComponent(state.run)}/status`);
    if (r.ok) state.runStatus = r.data; else { state.run = null; store.set('run', null); }
  }
  bus.emit('run.changed', state.run);
  if (state.run) subscribe();
}
function subscribe() {
  if (!state.run || !window.EventSource) return;
  const es = new EventSource(`/api/runs/${encodeURIComponent(state.run)}/events`);
  state.es = es;
  es.addEventListener('status', (e) => { try { const s = JSON.parse(e.data); const prev = state.runStatus; state.runStatus = Object.assign({}, prev || {}, s); bus.emit('status', state.runStatus); if (prev && prev.state !== 'done' && s.state === 'done') { state.cache.clear(); toast(t('toast.runDone', { id: s.run_id, state: s.state }), 'ok'); } if (prev && prev.state !== 'failed' && s.state === 'failed') toast(t('toast.runDone', { id: s.run_id, state: s.state }), 'fail'); } catch { /* ignore */ } });
  es.addEventListener('progress', (e) => { try { const p = JSON.parse(e.data); if (state.runStatus) { const sg = (state.runStatus.stages || []).find((x) => x.stage === p.stage); if (sg) { sg.progress = p.progress; sg.message = p.message; sg.state = 'running'; bus.emit('status', state.runStatus); } } } catch { /* ignore */ } });
  es.addEventListener('flags', (e) => { try { const d = JSON.parse(e.data); toast(t('toast.newFlags', { n: (d.latest || []).length }), 'warn'); bus.emit('flags', d); } catch { /* ignore */ } });
  es.addEventListener('batch', (e) => { try { const d = JSON.parse(e.data); toast(t('toast.batch', { id: d.batch_id, n: d.n_rows })); bus.emit('batch', d); } catch { /* ignore */ } });
  es.addEventListener('decision', (e) => { try { bus.emit('decision.remote', JSON.parse(e.data)); } catch { /* ignore */ } });
  es.onerror = () => { /* browser retries automatically */ };
}
function renderRunSelect() {
  const sel = document.getElementById('run-select');
  clear(sel);
  sel.append(el('option', { value: '', text: t('status.noRun') }));
  for (const r of state.runs) sel.append(el('option', { value: r.run_id, text: `${r.run_id} (${t('runs.state.' + r.state)})` }));
  sel.value = state.run || '';
}

function renderAll() { renderRail(); renderLamps(); renderLangs(); renderUser(); renderRunSelect(); applyTheme(); document.querySelectorAll('[data-i18n]').forEach((n) => { n.textContent = t(n.dataset.i18n); }); syncTopbarHeight(); }
/** The lamps may wrap to a second row on narrow screens; keep the sticky rail/drawer offsets in sync with the real bar height. */
function syncTopbarHeight() {
  const bar = document.getElementById('topbar');
  const h = bar ? bar.offsetHeight : 0;
  if (h > 0) document.documentElement.style.setProperty('--topbar-h', h + 'px');
}
window.addEventListener('resize', syncTopbarHeight);

// ---------------------------------------------------------------- boot
async function boot() {
  state.lang = store.get('lang', (navigator.language || 'en').slice(0, 2));
  if (!['en', 'fi', 'sv'].includes(state.lang)) state.lang = 'en';
  state.theme = store.get('theme', 'auto');
  state.user = store.get('user', null);
  await loadLang(state.lang);
  await refreshSettings();
  const rr = await api('/api/runs');
  state.runs = rr.ok ? rr.data.runs || [] : [];
  renderAll();
  initChat();
  installRefHandler();
  document.getElementById('theme-btn').addEventListener('click', () => { state.theme = { auto: 'light', light: 'dark', dark: 'auto' }[state.theme]; store.set('theme', state.theme); applyTheme(); bus.emit('theme.changed', state.theme); route(); });
  document.getElementById('user-btn').addEventListener('click', () => pickRole().then(() => route()));
  document.getElementById('run-select').addEventListener('change', (e) => selectRun(e.target.value || null));
  bus.on('run.select', (id) => { selectRun(id); if (id && state.view === 'runs') { /* stay */ } });
  bus.on('runs.loaded', (runs) => { state.runs = runs; renderRunSelect(); });
  bus.on('settings.changed', () => renderLamps());
  bus.on('status', () => { const r = state.runs.find((x) => x.run_id === state.run); if (r && state.runStatus) { r.state = state.runStatus.state; r.stages = state.runStatus.stages; renderRunSelect(); } });
  bus.on('run.changed', () => { route(); });
  setInterval(refreshSettings, 30000);
  if (!state.user) await pickRole();
  const saved = store.get('run', null);
  const initial = saved && state.runs.some((r) => r.run_id === saved) ? saved : (state.runs[0] ? state.runs[0].run_id : null);
  if (initial) await selectRun(initial); else route();
}
boot();
