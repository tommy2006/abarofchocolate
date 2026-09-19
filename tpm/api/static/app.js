/* Trustworthy Process Monitor — frontend entry. Boot, router (hash), numbered rail, data-control
   lamps, role picker, language + theme switch, SSE subscription for the selected run. */
import { state, t, el, clear, api, loadLang, store, bus, toast, modal, st, fmt, roleAllows, navigate, recordNavigation, installRefHandler, viewAccess, lockText, lockProgressText, lockGlyph, viewHead } from './js/core.js';
import { initChat } from './js/chat.js';
import { focusSection } from './js/brief.js';
import { openModelsPanel, mt as modelsText } from './js/models.js';
import { loadSignalNames } from './js/rename.js';
import * as runs from './js/views/runs.js';
import * as understanding from './js/views/understanding.js';
import * as quality from './js/views/quality.js';
import * as monitor from './js/views/monitor.js';
import * as diagnoses from './js/views/diagnoses.js';
import * as assessor from './js/views/assessor.js';
import * as log from './js/views/log.js';
import * as dataflow from './js/views/dataflow.js';
import * as report from './js/views/report.js';
import * as live from './js/views/live.js';

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
  { id: 'live', num: '9', key: 'nav.live', mod: live },
];
let current = null;
let rendering = false;
let routeSeq = 0;

// ---------------------------------------------------------------- router
function parseHash() {
  const h = location.hash.replace(/^#\/?/, '');
  const [path, qs] = h.split('?');
  const params = Object.fromEntries(new URLSearchParams(qs || ''));
  return { view: path || 'runs', params };
}
async function route() {
  const seq = ++routeSeq;
  recordNavigation(location.hash || '#/runs');
  const { view, params } = parseHash();
  const def = VIEWS.find((v) => v.id === view) || VIEWS[0];
  state.view = def.id;
  document.querySelectorAll('.rail-item').forEach((b) => b.setAttribute('aria-current', b.dataset.view === def.id ? 'page' : 'false'));
  const main = document.getElementById('main');
  if (current && current.cleanup) { try { current.cleanup(); } catch { /* ignore */ } }
  current = null;
  clear(main);
  rendering = true;
  // a page whose stage has produced nothing yet shows why and when it opens, not an empty view
  const acc = viewAccess(def.id);
  const locked = !acc.open && acc.reason !== 'norun';
  let made = null;
  try { made = locked ? renderLocked(main, def) : await def.mod.render(main, params); }
  catch (e) { console.error(e); if (seq === routeSeq) main.append(el('div', { class: 'notice fail', text: t('common.error', { msg: e.message }) })); }
  if (seq !== routeSeq) { if (made && made.cleanup) { try { made.cleanup(); } catch { /* ignore */ } } return; }  // a newer navigation took over while this one loaded
  current = made;
  rendering = false;
  // an action of a summary card may point at a block inside the folded technical part: unfold it and scroll there
  if (params.section && !locked) focusSection(main, params.section);
  main.focus({ preventScroll: true });
}
window.addEventListener('hashchange', route);
bus.on('route.same', () => route());

/** Stand-in for a view that waits for its pipeline stage: says which stage, shows live progress, opens by itself. */
function renderLocked(main, def) {
  const view = el('div', { class: 'view locked-view' });
  main.append(view);
  view.append(viewHead(def.num, t(def.key)));
  const stageLabel = (s) => (t('runs.stage.' + s) === 'runs.stage.' + s ? s : t('runs.stage.' + s));
  const title = el('h2', { class: 'lockpanel-title' });
  const now = el('p', { class: 'lockpanel-now' });
  const body = el('p', { class: 'muted' });
  const fill = el('i');
  const pct = el('span', { class: 'small muted' });
  const meterRow = el('div', { class: 'lockpanel-meter' }, el('span', { class: 'small muted', text: t('lock.towards') }), el('span', { class: 'bar' }, fill), pct);
  const stagesEl = el('div', { class: 'stages' });
  const errEl = el('pre', { class: 'notice fail small lockpanel-error', hidden: true });
  const staleEl = el('div', { class: 'notice warn small', hidden: true });
  const panel = el('div', { class: 'lockpanel', role: 'status' }, el('div', { class: 'lockpanel-head' }, lockGlyph(), title), now, meterRow, stagesEl, body, staleEl, errEl,
    el('div', { class: 'row' }, el('button', { class: 'btn', type: 'button', onClick: () => navigate('runs') }, t('lock.watch'))));
  view.append(panel);
  const paint = () => {
    const acc = viewAccess(def.id);
    if (acc.open) { if (state.view === def.id) route(); return; }
    if (acc.reason === 'norun') { route(); return; }
    const vars = { stage: stageLabel(acc.stage), failed: acc.failedStage ? stageLabel(acc.failedStage) : t('runs.state.failed'), view: t(def.key), run: state.run || '', msg: acc.message || t('runs.state.skipped') };
    panel.dataset.reason = acc.reason;
    title.textContent = t('lock.title.' + acc.reason, vars);
    body.textContent = t('lock.body.' + acc.reason, vars);
    const waiting = acc.reason === 'waiting';
    now.hidden = !waiting; meterRow.hidden = !waiting;
    if (waiting) { now.textContent = lockProgressText(acc); fill.style.width = (acc.overall * 100).toFixed(1) + '%'; pct.textContent = fmt.pct(acc.overall); }
    staleEl.hidden = !(waiting && acc.orphan);
    if (!staleEl.hidden) staleEl.textContent = t('lock.stale', { min: acc.staleMin });
    errEl.hidden = !(acc.reason === 'failed' && acc.error);
    if (!errEl.hidden) errEl.textContent = String(acc.error).slice(-1500);
    // the stages on the way to this page, in the same form as the Runs progress panel
    const stages = (state.runStatus && state.runStatus.stages) || [];
    const upto = stages.slice(0, stages.findIndex((s) => s.stage === acc.stage) + 1);
    clear(stagesEl);
    upto.forEach((sg, i) => stagesEl.append(el('div', { class: 'stage ' + sg.state }, el('span', { class: 'num', text: String(i + 1) }), st(sg.state, stageLabel(sg.stage)), el('span', { class: 'bar' }, el('i', { style: { width: ((sg.state === 'done' ? 1 : sg.progress || 0) * 100) + '%' } })), el('span', { class: 'msg', title: sg.message || '', text: sg.state === 'running' ? `${fmt.pct(sg.progress || 0)} ${sg.message || ''}` : (sg.state === 'failed' ? (sg.message || t('runs.state.failed')) : t('runs.state.' + sg.state)) }))));
  };
  paint();
  const off = bus.on('status', paint);
  view.cleanup = () => off();
  return view;
}

// ---------------------------------------------------------------- rail
const railLocks = { run: null, locked: {} };
function renderRail() {
  const rail = document.getElementById('rail');
  clear(rail);
  rail.append(el('div', { class: 'rail-tag', text: t('app.tagline') }));
  for (const v of VIEWS) {
    const b = el('button', { class: 'rail-item', type: 'button', dataset: { view: v.id }, 'aria-current': state.view === v.id ? 'page' : 'false', onClick: () => navigate(v.id) }, el('span', { class: 'rail-num', text: v.num }), el('span', { class: 'rail-label' }, t(v.key), v.level && !roleAllows(v.level) ? el('span', { class: 'badge', title: t('role.tag.' + v.level), text: v.level[0] }) : null), el('span', { class: 'rail-lock' }), el('span', { class: 'rail-prog', 'aria-hidden': 'true' }, el('i')));
    rail.append(b);
  }
  updateRailLocks(false);
}
/** Lock state of every rail item for the selected run, updated in place on each status event. An item that was
    locked a moment ago and is open now gets a brief highlight (never when the run itself was just switched). */
function updateRailLocks(animate = true) {
  const sameRun = railLocks.run === state.run;
  if (!sameRun) { railLocks.run = state.run; railLocks.locked = {}; }
  document.querySelectorAll('#rail .rail-item').forEach((b) => {
    const id = b.dataset.view;
    const acc = viewAccess(id);
    const locked = !acc.open;
    const was = railLocks.locked[id];
    railLocks.locked[id] = locked;
    b.classList.toggle('locked', locked);
    if (locked) { b.setAttribute('aria-disabled', 'true'); b.title = lockText(acc); b.dataset.lock = acc.reason; } else { b.removeAttribute('aria-disabled'); b.removeAttribute('title'); delete b.dataset.lock; }
    const slot = b.querySelector('.rail-lock');
    if (locked && !slot.firstChild) slot.append(lockGlyph(), el('span', { class: 'sr-only', text: t('lock.locked') }));
    if (!locked && slot.firstChild) clear(slot);
    // a thin progress line only under the pages whose own stage is running right now
    const showProg = locked && acc.reason === 'waiting' && acc.stageState === 'running';
    if (showProg) b.dataset.prog = '1'; else delete b.dataset.prog;
    b.querySelector('.rail-prog i').style.width = showProg ? (acc.progress * 100).toFixed(1) + '%' : '0';
    if (animate && sameRun && was === true && !locked) { b.classList.remove('unlocked'); void b.offsetWidth; b.classList.add('unlocked'); setTimeout(() => b.classList.remove('unlocked'), 3200); }
  });
}
bus.on('status', () => updateRailLocks(true));
bus.on('run.changed', () => updateRailLocks(false));

// ---------------------------------------------------------------- lamps (data-control strip)
function renderLamps() {
  const s = state.settings || {};
  const lamps = document.getElementById('lamps');
  clear(lamps);
  const lamp = (stateCls, label, value, title) => el('div', { class: 'lamp', 'data-state': stateCls, title: title || '' }, el('span', { class: 'lamp-dot' }), el('span', { class: 'lamp-label', text: label }), el('span', { class: 'lamp-value', text: value }));
  const models = s.models || {};
  lamps.append(
    lamp(s.allow_external ? 'warn' : 'ok', t('status.profile'), s.profile || '–', s.allow_external ? t('status.egressPossible') : t('status.noEgress')),
    // the local-model lamp opens the models panel: choose another installed model, download one, install Ollama
    (() => { const l = lamp(models.local ? 'ok' : 'off', t('status.local'), (models.local_model || s.local_model || '–') + (models.local ? '' : ` (${t('status.notLoaded')})`) + ' ▾', modelsText('lampHint')); l.setAttribute('role', 'button'); l.setAttribute('tabindex', '0'); l.style.cursor = 'pointer'; const open = () => openModelsPanel(refreshSettings); l.addEventListener('click', open); l.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } }); return l; })(),
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
  await loadSignalNames();  // names people gave to signals, shown wherever a signal id appears
  bus.emit('run.changed', state.run);
  if (state.run) subscribe();
}
function subscribe() {
  if (!state.run || !window.EventSource) return;
  const es = new EventSource(`/api/runs/${encodeURIComponent(state.run)}/events`);
  state.es = es;
  es.addEventListener('status', (e) => { try { const s = JSON.parse(e.data); const prev = state.runStatus; state.runStatus = Object.assign({}, prev || {}, s); bus.emit('status', state.runStatus); if (prev && prev.state !== 'done' && s.state === 'done') { state.cache.clear(); toast(t('toast.runDone', { id: s.run_id, state: s.state }), 'ok'); } if (prev && prev.state !== 'failed' && s.state === 'failed') toast(t('toast.runDone', { id: s.run_id, state: s.state }), 'fail'); } catch { /* ignore */ } });
  es.addEventListener('progress', (e) => { try { const p = JSON.parse(e.data); if (state.runStatus) { const sg = (state.runStatus.stages || []).find((x) => x.stage === p.stage); if (sg && !['done', 'failed', 'skipped'].includes(sg.state)) { sg.progress = p.progress; sg.message = p.message; sg.state = 'running'; bus.emit('status', state.runStatus); } /* a late progress event never reopens a finished stage */ } } catch { /* ignore */ } });
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
  // The event stream drives the rail and the locked pages; when it is not delivering (proxy, sleeping tab) fall
  // back to polling the status of a run that is still being analysed.
  setInterval(async () => {
    const s = state.runStatus;
    if (!state.run || !s || !['running', 'pending'].includes(s.state)) return;
    if (state.es && state.es.readyState === 1) return;
    const id = state.run;
    const r = await api(`/api/runs/${encodeURIComponent(id)}/status`);
    if (r.ok && state.run === id) { state.runStatus = r.data; bus.emit('status', state.runStatus); }
  }, 5000);
  if (!state.user) await pickRole();
  const saved = store.get('run', null);
  const initial = saved && state.runs.some((r) => r.run_id === saved) ? saved : (state.runs[0] ? state.runs[0].run_id : null);
  if (initial) await selectRun(initial); else route();
  // A computer without Ollama or without any chat model: offer the setup panel once per browser session.
  try {
    if (!sessionStorage.getItem('tpm.modelsOffered')) {
      const m = await api('/api/models');
      if (m.ok && ['install_ollama', 'start_ollama', 'pull_chat_model'].includes(m.data.next_step)) { sessionStorage.setItem('tpm.modelsOffered', '1'); openModelsPanel(refreshSettings); }
    }
  } catch (e) { /* the panel stays reachable from the Local model lamp */ }
}
boot();
