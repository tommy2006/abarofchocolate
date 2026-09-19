/* Core: state, i18n, API client, DOM helpers, shared widgets (confidence, status glyphs, evidence,
   decision buttons, paginated tables), hash navigation with an in-app back stack, reference links
   (FLAG-000001, B00003, S07, ...) that jump to the object, prose cleanup for model output, and
   plain-language labels for numbers. No framework, ES2020 modules. */

export const state = {
  run: null,            // selected run id
  runStatus: null,      // last status payload for the run
  runs: [],
  user: null,           // {name, role}
  lang: 'en',
  dict: {},
  dictEn: {},
  settings: null,
  view: 'runs',
  theme: 'auto',
  es: null,             // EventSource
  cache: new Map(),
};

const listeners = new Map();
export const bus = {
  on(evt, fn) { if (!listeners.has(evt)) listeners.set(evt, new Set()); listeners.get(evt).add(fn); return () => listeners.get(evt).delete(fn); },
  emit(evt, data) { (listeners.get(evt) || []).forEach((fn) => { try { fn(data); } catch (e) { console.error(e); } }); },
};

// ---------------------------------------------------------------- storage
export const store = {
  get(k, d = null) { try { const v = localStorage.getItem('tpm.' + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('tpm.' + k, JSON.stringify(v)); } catch { /* private mode */ } },
};

// ---------------------------------------------------------------- i18n
export function t(key, vars) {
  let s = state.dict[key];
  if (s === undefined) s = state.dictEn[key];
  if (s === undefined) s = key;
  if (vars) for (const [k, v] of Object.entries(vars)) s = s.replace(new RegExp('\\{' + k + '\\}', 'g'), String(v));
  return s;
}
export async function loadLang(lang) {
  if (!Object.keys(state.dictEn).length) {
    const r = await fetch('/static/i18n/en.json'); state.dictEn = await r.json();
  }
  if (lang === 'en') state.dict = state.dictEn;
  else {
    try { const r = await fetch(`/static/i18n/${lang}.json`); state.dict = r.ok ? await r.json() : {}; } catch { state.dict = {}; }
  }
  state.lang = lang;
  document.documentElement.lang = lang;
  store.set('lang', lang);
  document.querySelectorAll('[data-i18n]').forEach((n) => { n.textContent = t(n.dataset.i18n); });
}

// ---------------------------------------------------------------- API
export async function api(path, { method = 'GET', body, form, params, raw = false } = {}) {
  let url = path;
  if (params) {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== '') q.set(k, v);
    const qs = q.toString(); if (qs) url += (url.includes('?') ? '&' : '?') + qs;
  }
  const init = { method, headers: {} };
  if (form) init.body = form;
  else if (body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
  let res;
  try { res = await fetch(url, init); } catch (e) { return { ok: false, status: 0, data: { error: String(e) }, unavailable: null }; }
  if (raw) return { ok: res.ok, status: res.status, res };
  let data = null;
  const ct = res.headers.get('content-type') || '';
  if (ct.includes('json')) { try { data = await res.json(); } catch { data = null; } } else { data = { text: await res.text() }; }
  return { ok: res.ok, status: res.status, data: data || {}, unavailable: data && data.unavailable ? data.unavailable : null };
}
export const runApi = (suffix, opts) => api(`/api/runs/${encodeURIComponent(state.run)}${suffix}`, opts);
export function errText(r) {
  if (!r) return '';
  if (r.unavailable) return t('common.unavailable', { what: r.unavailable });
  const d = r.data || {};
  return d.detail ? (typeof d.detail === 'string' ? d.detail : JSON.stringify(d.detail)) : d.error || d.message || `HTTP ${r.status}`;
}
/** Per-run cache of a GET (cleared when the run changes). */
export async function cachedRunApi(key, suffix, opts) {
  const k = `${state.run}:${key}`;
  if (state.cache.has(k)) return state.cache.get(k);
  const r = await runApi(suffix, opts);
  if (r.ok) state.cache.set(k, r);
  return r;
}
bus.on('run.changed', () => state.cache.clear());

// ---------------------------------------------------------------- DOM
export function el(tag, attrs, ...children) {
  const n = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else if (k === 'html') n.innerHTML = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(n.style, v);
    else if (k.startsWith('on') && typeof v === 'function') n.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === 'dataset') Object.assign(n.dataset, v);
    else if (v === true) n.setAttribute(k, '');
    else n.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return n;
}
export const frag = (...kids) => { const f = document.createDocumentFragment(); kids.flat(Infinity).forEach((k) => { if (k !== null && k !== undefined && k !== false) f.append(k instanceof Node ? k : document.createTextNode(String(k))); }); return f; };
export function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); return node; }

export const fmt = {
  pct(x, d = 0) { if (x === null || x === undefined || isNaN(x)) return '–'; return (Number(x) * 100).toFixed(d) + ' %'; },
  num(x, d = 2) { if (x === null || x === undefined || x === '' || isNaN(x)) return '–'; const n = Number(x); if (Math.abs(n) >= 1e6) return n.toExponential(2); return Number.isInteger(n) ? String(n) : n.toFixed(d); },
  int(x) { return x === null || x === undefined ? '–' : Number(x).toLocaleString(); },
  ts(s) { if (!s) return '–'; const d = new Date(s); if (isNaN(d)) return String(s).slice(0, 19).replace('T', ' '); return d.toLocaleString(undefined, { hour12: false }); },
  time(s) { if (!s) return '–'; const d = new Date(s); return isNaN(d) ? String(s) : d.toLocaleTimeString(undefined, { hour12: false }); },
  bytes(b) { b = Number(b || 0); if (b < 1024) return b + ' B'; if (b < 1048576) return (b / 1024).toFixed(1) + ' kB'; if (b < 1073741824) return (b / 1048576).toFixed(b < 104857600 ? 2 : 1) + ' MB'; return (b / 1073741824).toFixed(2) + ' GB'; },
  sec(s) { s = Number(s || 0); if (s < 90) return s.toFixed(0) + ' s'; return (s / 60).toFixed(1) + ' min'; },
  /** a waiting time a person can plan with: 42 s, 3 min 05 s, 1 h 12 min */
  dur(s) { s = Math.max(0, Math.round(Number(s || 0))); if (!isFinite(s)) return '–'; if (s < 60) return s + ' s'; if (s < 3600) return Math.floor(s / 60) + ' min ' + String(s % 60).padStart(2, '0') + ' s'; return Math.floor(s / 3600) + ' h ' + String(Math.floor((s % 3600) / 60)).padStart(2, '0') + ' min'; },
  /** signed difference on a 0..1 scale, shown in percentage points: +3.6 pp */
  pp(x, d = 1) { if (x === null || x === undefined || isNaN(x)) return '–'; const v = Number(x) * 100; return (v > 0 ? '+' : '') + v.toFixed(d) + ' pp'; },
};

// ---------------------------------------------------------------- navigation (hash router helpers)
/** In-app history: `stack` holds the hashes the person came from, so "Back" always has somewhere to go. */
export const nav = { stack: [], last: null };
export function hashFor(view, params) {
  const clean = {};
  if (params) for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== '') clean[k] = v;
  const qs = Object.keys(clean).length ? '?' + new URLSearchParams(clean).toString() : '';
  return `#/${view}${qs}`;
}
/** Go to a view; pushes a browser history entry (location.hash assignment), never replaces. */
export function navigate(view, params) {
  const h = hashFor(view, params);
  if (location.hash === h) { bus.emit('route.same', { view, params }); return; }
  location.hash = h;
}
/** Called by the router on every hash change. A hash equal to the top of the stack is a step back. */
export function recordNavigation(hash) {
  hash = hash || location.hash || '#/runs';
  if (nav.last === hash) return;
  if (nav.stack.length && nav.stack[nav.stack.length - 1] === hash) nav.stack.pop();
  else if (nav.last !== null) { nav.stack.push(nav.last); if (nav.stack.length > 60) nav.stack.shift(); }
  nav.last = hash;
}
export function canGoBack() { return nav.stack.length > 0; }
export function goBack() {
  closeAllModals();
  if (nav.stack.length) location.hash = nav.stack[nav.stack.length - 1];
  else history.back();
}

// ---------------------------------------------------------------- pages unlock as the analysis comes in
export const STAGE_ORDER = ['ingest', 'profile', 'quality', 'detect', 'diagnose', 'assess', 'report'];
/** View -> the pipeline stages whose output it shows; the view opens as soon as ONE of them is done (the report
    page can generate a report once the diagnoses exist). Views not listed need no stage. */
export const VIEW_STAGES = { understanding: ['profile'], quality: ['quality'], monitor: ['detect'], diagnoses: ['diagnose'], assessor: ['assess'], report: ['report', 'diagnose'] };
const VIEWS_NEEDING_RUN = new Set(['understanding', 'quality', 'monitor', 'diagnoses', 'assessor', 'log', 'report']);
/** Can this view show something for the selected run?  { open: true } or
    { open: false, reason: 'norun' | 'waiting' | 'failed' | 'skipped', stage, stageState, progress, message,
      running: {stage, progress, message} | null, overall (0..1 towards the awaited stage), failedStage, error }.
    A stage counts only when it is `done`; `skipped` never opens a page. Runs without a stage record are not blocked. */
export function viewAccess(viewId, status = state.runStatus, runId = state.run) {
  if (!VIEWS_NEEDING_RUN.has(viewId)) return { open: true };
  if (!runId) return { open: false, reason: 'norun' };
  const needs = VIEW_STAGES[viewId];
  const stages = (status && status.stages) || [];
  if (!needs || !stages.length) return { open: true };
  const by = Object.fromEntries(stages.map((s) => [s.stage, s]));
  if (needs.some((n) => by[n] && by[n].state === 'done')) return { open: true };
  const stage = needs.slice().sort((a, b) => STAGE_ORDER.indexOf(a) - STAGE_ORDER.indexOf(b)).find((n) => by[n]) || needs[0];
  const target = by[stage] || { stage, state: 'pending', progress: 0, message: '' };
  const failedStage = stages.find((s) => s.state === 'failed') || null;
  const runFailed = !!failedStage || (status && status.state === 'failed');
  const running = stages.slice().reverse().find((s) => s.state === 'running') || null;  // the furthest one, should two claim to run
  const upto = stages.slice(0, Math.max(1, stages.findIndex((s) => s.stage === stage) + 1));
  const overall = upto.reduce((a, s) => a + (s.state === 'done' ? 1 : s.state === 'running' ? Math.max(0, Math.min(1, Number(s.progress) || 0)) : 0), 0) / upto.length;
  const out = { open: false, stage, stageState: target.state, progress: Number(target.progress) || 0, message: target.message || '', running: running ? { stage: running.stage, progress: Number(running.progress) || 0, message: running.message || '' } : null, overall, failedStage: failedStage ? failedStage.stage : null, error: null };
  if (runFailed) {
    out.reason = 'failed';
    const job = status && status.job;
    out.error = (failedStage && (failedStage.error || failedStage.message)) || (status && status.error) || (job && job.error) || '';
    // one line for tooltips: the stage message is the exception text, the stored error is the end of a traceback
    out.errorShort = String((failedStage && failedStage.message) || String(out.error).trim().split(/\r?\n/)[0] || '').slice(0, 200);
  } else if (target.state === 'skipped' || (status && status.state === 'done')) out.reason = 'skipped';
  else {
    out.reason = 'waiting';
    // every progress report refreshes updated_at; a long silence without a job in this service is worth a hint
    const last = status && status.updated_at ? new Date(status.updated_at).getTime() : NaN;
    out.staleMin = isNaN(last) ? 0 : Math.max(0, Math.floor((Date.now() - last) / 60000));
    out.orphan = !(status && status.job) && out.staleMin >= 20;
  }
  return out;
}
const stageName = (s) => (t('runs.stage.' + s) === 'runs.stage.' + s ? s : t('runs.stage.' + s));
/** One sentence for a locked page: tooltip of the rail item and lead of the locked panel. */
export function lockText(acc) {
  if (!acc || acc.open) return '';
  if (acc.reason === 'norun') return t('lock.norun');
  const stage = stageName(acc.stage);
  if (acc.reason === 'failed') return (acc.failedStage === acc.stage ? t('lock.failedSelf', { stage }) : t('lock.failed', { stage, failed: acc.failedStage ? stageName(acc.failedStage) : t('runs.state.failed') })) + (acc.errorShort ? ' ' + acc.errorShort : '');
  if (acc.reason === 'skipped') return t('lock.skipped', { stage, msg: acc.message || t('runs.state.skipped') });
  return t('lock.waits', { stage }) + ' (' + lockProgressText(acc) + ')';
}
/** "currently 42 %: scoring fold 3 of 5" / "now running: Quality, 42 %" / "not started yet" */
export function lockProgressText(acc) {
  if (acc.stageState === 'running') return acc.message ? t('lock.current', { pct: fmt.pct(acc.progress), msg: acc.message }) : t('lock.currentNoMsg', { pct: fmt.pct(acc.progress) });
  if (acc.running) return t('lock.nowRunning', { stage: stageName(acc.running.stage), pct: fmt.pct(acc.running.progress) }) + (acc.running.message ? ': ' + acc.running.message : '');
  return t('lock.notStarted');
}
/** Small padlock drawn inline (no emoji: it must look the same on every machine and follow the text colour). */
export function lockGlyph() {
  return el('span', { class: 'lock-glyph', 'aria-hidden': 'true', html: '<svg viewBox="0 0 12 14" width="11" height="13" focusable="false"><path d="M3.2 6V4.2a2.8 2.8 0 0 1 5.6 0V6" fill="none" stroke="currentColor" stroke-width="1.5"/><rect x="1.5" y="6" width="9" height="6.8" rx="1.3" fill="currentColor"/></svg>' });
}

// ---------------------------------------------------------------- toasts, modals
export function toast(msg, kind = '') {
  const root = document.getElementById('toasts');
  const n = el('div', { class: 'toast ' + kind, text: msg });
  root.append(n);
  setTimeout(() => n.remove(), kind === 'fail' ? 7000 : 4000);
}
const openModals = new Set();
export function modal({ title, body, actions = [], wide = false, onClose }) {
  const root = document.getElementById('modal-root');
  const back = el('div', { class: 'modal-back' });
  const box = el('div', { class: 'modal' + (wide ? ' wide' : ''), role: 'dialog', 'aria-modal': 'true', 'aria-label': title });
  const close = () => { if (!openModals.has(close)) return; openModals.delete(close); back.remove(); document.removeEventListener('keydown', esc); if (onClose) onClose(); };
  const esc = (e) => { if (e.key === 'Escape') close(); };
  document.addEventListener('keydown', esc);
  back.addEventListener('click', (e) => { if (e.target === back) close(); });
  box.append(el('div', { class: 'modal-head' }, el('h2', { text: title }), el('button', { class: 'btn btn-quiet btn-sm', type: 'button', onClick: close, 'aria-label': t('common.close') }, '✕')));
  box.append(el('div', { class: 'modal-body' }, body));
  if (actions.length) box.append(el('div', { class: 'modal-foot' }, actions.map((a) => el('button', { class: 'btn ' + (a.cls || ''), type: 'button', onClick: () => a.onClick(close) }, a.label))));
  back.append(box); root.append(back);
  openModals.add(close);
  const first = box.querySelector('input, select, textarea, button.btn-primary'); if (first) first.focus();
  return { close, box };
}
export function closeAllModals() { for (const c of [...openModals]) c(); }
export function confirmDialog(text, { okLabel, danger = false } = {}) {
  return new Promise((resolve) => {
    modal({ title: t('common.confirm'), body: el('p', { text }), onClose: () => resolve(false), actions: [
      { label: t('common.cancel'), onClick: (c) => { c(); resolve(false); } },
      { label: okLabel || t('common.confirm'), cls: danger ? 'btn-danger' : 'btn-primary', onClick: (c) => { resolve(true); c(); } },
    ] });
  });
}

// ---------------------------------------------------------------- roles
export const ROLE_LEVEL = { operator: 0, engineer: 1, reviewer: 2 };
export function roleAllows(level) { const r = state.user ? state.user.role : 'operator'; return (ROLE_LEVEL[r] || 0) >= (ROLE_LEVEL[level] || 0); }
export function actorName() { return state.user ? state.user.name : 'anonymous'; }
export function actorRole() { return state.user ? state.user.role : 'operator'; }

// ---------------------------------------------------------------- prose cleanup (model output)
const TAG_RE = /\[(llm-(?:local|external)[^\]]*|template|code|human)\]/g;
function balancedEnd(s, start) {
  // index just past the bracket/brace that closes the one at `start`; -1 when unbalanced
  const open = s[start]; const closeCh = open === '{' ? '}' : ']';
  let depth = 0; let quote = null;
  for (let i = start; i < s.length; i++) {
    const ch = s[i];
    if (quote) { if (ch === '\\') { i++; continue; } if (ch === quote) quote = null; continue; }
    if (ch === '"' || ch === "'") { quote = ch; continue; }
    if (ch === '{' || ch === '[') depth++;
    else if (ch === '}' || ch === ']') { depth--; if (depth === 0) return i + 1; }
  }
  return -1;
}
function textFromFragment(fragment) {
  // a JSON object or a Python-dict repr: pull the readable field out of it
  const end = balancedEnd(fragment, 0);
  const body = end > 0 ? fragment.slice(0, end) : fragment;
  try {
    const o = JSON.parse(body);
    if (o && typeof o === 'object') {
      for (const k of ['text', 'summary', 'objection', 'message', 'answer', 'statement']) if (typeof o[k] === 'string' && o[k].trim()) return o[k].trim();
    }
  } catch { /* not JSON */ }
  for (const k of ['text', 'summary', 'objection', 'message', 'answer', 'statement']) {
    const m = body.match(new RegExp(`['"]${k}['"]\\s*:\\s*(?:"((?:[^"\\\\]|\\\\.)*)"|'((?:[^'\\\\]|\\\\.)*)')`));
    if (m) return (m[1] !== undefined ? m[1] : m[2]).replace(/\\(["'])/g, '$1').replace(/\\n/g, ' ').trim();
  }
  return '';
}
const norm = (s) => String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
/** Drop raw JSON / dict fragments that a model pasted into a statement, keep the readable sentences,
    remove "[llm-local:...]" source tags (the source is shown separately). */
export function cleanText(s) {
  if (s === null || s === undefined) return '';
  let out = String(s);
  // "<prose>\n\n[llm-...] {json...}"  or  "[llm-...] {'text': '...'}"
  let m;
  const tagFrag = /\[(?:llm-[^\]]*|template)\]\s*(?=[{[])/;
  while ((m = tagFrag.exec(out))) {
    const fragStart = m.index + m[0].length;
    const end = balancedEnd(out, fragStart);
    const fragment = out.slice(fragStart, end > 0 ? end : out.length);
    const before = out.slice(0, m.index).trim();
    const after = end > 0 ? out.slice(end) : '';
    const inner = textFromFragment(fragment);
    const dup = inner && before && (norm(before).includes(norm(inner).slice(0, 80)) || norm(inner).includes(norm(before).slice(0, 80)));
    out = before + (inner && !dup ? (before ? '\n\n' : '') + inner : '') + after;
  }
  // a bare JSON/dict fragment with no tag
  const bare = out.trim();
  if (/^[{[]/.test(bare)) { const inner = textFromFragment(bare); const end = balancedEnd(bare, 0); out = (inner || '') + (end > 0 ? bare.slice(end) : ''); }
  out = out.replace(TAG_RE, '').replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').replace(/  +/g, ' ');
  return out.trim();
}
/** First "[llm-local:...]" tag found in a text, or null. */
export function sourceTag(s) { const m = String(s || '').match(/\[(llm-(?:local|external)[^\]]*|template)\]/); return m ? m[1] : null; }
/** "1. Something" -> "Something" (numbered lists get their own numbering in the UI). */
export function stripNumbering(s) { return String(s || '').replace(/^\s*(?:\(?\d{1,2}[.)]|[-*•])\s+/, ''); }
export function paragraphs(s) { return cleanText(s).split(/\n\s*\n/).map((p) => p.replace(/\s*\n\s*/g, ' ').trim()).filter(Boolean); }
/** Prose paragraphs with reference links. */
export function prose(s, cls = 'prose') {
  const ps = paragraphs(s);
  if (!ps.length) return el('p', { class: cls + ' dim', text: '–' });
  return el('div', { class: cls }, ps.map((p) => el('p', {}, linkifyRefs(p))));
}
/** List of sentences with reference links (ul by default, ol.steps for steps). */
export function proseList(items, { ordered = false, cls, lead } = {}) {
  const arr = (items || []).map((x) => (typeof x === 'string' ? x : x && (x.text || x.statement || x.claim) ? (x.text || x.statement || x.claim) : JSON.stringify(x))).map((x) => cleanText(stripNumbering(x))).filter(Boolean);
  if (!arr.length) return null;
  return el(ordered ? 'ol' : 'ul', { class: cls || (ordered ? 'steps' : 'list') }, arr.map((x) => el('li', {}, lead ? [lead(x), ' '] : null, linkifyRefs(x))));
}

// ---------------------------------------------------------------- reference links
const OBJ_TYPE = { FLAG: 'flag', DIAG: 'diagnosis', EV: 'evidence', CHK: 'check', INF: 'inference', EGR: 'egress' };
const REF_RE = /\b(?<obj>(?:FLAG|DIAG|EV|CHK|INF|EGR)-\d{3,7})\b|\b(?<rule>RULE-\d{2,})\b|\b(?<pattern>PATTERN-[A-Z]{1,2})\b|\b(?<grpword>[Gg]roups?\s+)(?<grp>G?\d{1,6})\b|\b(?<gid>G\d{5,6})\b|\b(?<batch>B\d{4,6})\b|\b(?<sig>S\d{2,3})\b|\b[Rr]ows?\s+(?<rowa>\d{1,10})(?:\s*(?:-|–|to)\s*(?<rowb>\d{1,10}))?\b/g;
const ROUTE_OF = {
  batch: (id) => hashFor('quality', { batch: id }),
  flag: (id) => hashFor('monitor', { flag: id }),
  diagnosis: (id) => hashFor('diagnoses', { diag: id }),
  pattern: (id) => hashFor('diagnoses', { pattern: id }),
  signal: (id) => hashFor('understanding', { signal: id }),
  group: (id) => hashFor('monitor', { group: id }),
  rule: (id) => hashFor('quality', { rule: id }),
  rows: (id) => hashFor('monitor', rowsParams(id)),
};
/** A rows reference is "312632-312999" (or one row), optionally with the signals to plot: "312632-312999:S09,S01". */
function rowsParams(id) { const [rows, signals] = String(id).split(':'); return { rows, signals }; }
/** Link to the Monitor at a row range, with the signals that matter there. */
export function rowsLink(a, b, { signals, label } = {}) {
  const range = b === undefined || b === null || Number(b) === Number(a) ? String(a) : `${a}-${b}`;
  const id = range + (signals && signals.length ? ':' + signals.slice(0, 6).join(',') : '');
  return refLink('rows', id, label === undefined ? (range.includes('-') ? `${fmt.int(a)}–${fmt.int(b)}` : fmt.int(a)) : label);
}
/** <a class="ref"> for one object; the global click handler (installRefHandler) does the jump. */
export function refLink(type, id, label) {
  const href = ROUTE_OF[type] ? ROUTE_OF[type](id) : '#';
  // a signal somebody renamed reads "possibly broken (S44)" wherever its id is shown (names: js/rename.js)
  if (type === 'signal' && (label === undefined || label === id) && state.signalNames && state.signalNames[id]) label = `${state.signalNames[id]} (${id})`;
  return el('a', { href, class: 'ref ref-' + type, dataset: { refType: type, refId: id }, title: t('ref.' + type) + ' ' + id + ' — ' + t('ref.open') }, label === undefined ? id : label);
}
/** Kind of object an id names, from its prefix: 'DIAG-000005' -> 'diagnosis', 'B00008' -> 'batch'. Unknown -> 'evidence'. */
export function refTypeOfId(id) {
  const s = String(id || '').trim().toUpperCase();
  const m = /^([A-Z]+)-/.exec(s);
  if (m && OBJ_TYPE[m[1]]) return OBJ_TYPE[m[1]];
  if (m && m[1] === 'RULE') return 'rule';
  if (m && m[1] === 'PATTERN') return 'pattern';
  return /^B\d{4,6}$/.test(s) ? 'batch' : 'evidence';
}
/** Citation link: labelled by what the id really is, and a click opens its popover (content, plain sentence,
    "Open in ..." and the object's own evidence) instead of leaving the current view. Ctrl-click opens the owning view. */
export function citeLink(id, label) {
  const type = refTypeOfId(id);
  const href = ROUTE_OF[type] ? ROUTE_OF[type](id) : '#';
  return el('a', { href, class: 'ref ref-' + type, dataset: { refType: 'cite', refId: id }, title: t('ref.' + type) + ' ' + id + ' — ' + t('ref.open') }, label === undefined ? id : label);
}
/** Turn every reference token in a text into a link. Returns a DocumentFragment. */
export function linkifyRefs(text) {
  const f = document.createDocumentFragment();
  const s = text === null || text === undefined ? '' : String(text);
  let last = 0;
  let sigBefore = null; let sigEnd = -1;  // "S09 rows 312632-312999": the rows link also carries the signal
  for (const m of s.matchAll(REF_RE)) {
    const g = m.groups || {};
    let type; let id;
    if (g.obj) { type = OBJ_TYPE[g.obj.split('-')[0]]; id = g.obj; }
    else if (g.rule) { type = 'rule'; id = g.rule; }
    else if (g.pattern) { type = 'pattern'; id = g.pattern; }
    else if (g.grp) { type = 'group'; id = g.grp; }
    else if (g.gid) { type = 'group'; id = g.gid; }
    else if (g.batch) { type = 'batch'; id = g.batch; }
    else if (g.sig) { type = 'signal'; id = g.sig; }
    else if (g.rowa) { type = 'rows'; id = g.rowa + (g.rowb && g.rowb !== g.rowa ? '-' + g.rowb : '') + (sigBefore && s.slice(sigEnd, m.index).trim() === '' ? ':' + sigBefore : ''); }
    else continue;
    if (type === 'signal') { sigBefore = id; sigEnd = m.index + m[0].length; }
    if (m.index > last) f.append(document.createTextNode(s.slice(last, m.index)));
    // "pressure sensor (S44)": the text already carries a name in front of the alias, so the link keeps the bare id
    const named = type === 'signal' && m.index > 0 && s[m.index - 1] === '(';
    f.append(refLink(type, id, named ? el('span', { text: m[0] }) : m[0]));
    last = m.index + m[0].length;
  }
  if (last < s.length) f.append(document.createTextNode(s.slice(last)));
  return f;
}
/** Chips that link to objects: signals, batches, flags... */
export function refChips(type, ids, { max = 12 } = {}) {
  return el('span', { class: 'evs' }, (ids || []).slice(0, max).map((id) => refLink(type, id)), (ids || []).length > max ? el('span', { class: 'dim small', text: ` +${ids.length - max}` }) : null);
}
export async function openRef(type, id) {
  if (!state.run && ROUTE_OF[type]) { toast(t('runs.noRunHint'), 'warn'); return; }
  switch (type) {
    case 'batch': closeAllModals(); navigate('quality', { batch: id }); return;
    case 'flag': closeAllModals(); navigate('monitor', { flag: id }); return;
    case 'diagnosis': closeAllModals(); navigate('diagnoses', { diag: id }); return;
    case 'pattern': closeAllModals(); navigate('diagnoses', { pattern: id }); return;
    case 'signal': closeAllModals(); navigate('understanding', { signal: id }); return;
    case 'group': closeAllModals(); navigate('monitor', { group: id }); return;
    case 'rule': closeAllModals(); navigate('quality', { rule: id }); return;
    case 'rows': closeAllModals(); navigate('monitor', rowsParams(id)); return;
    default: showRefModal(type, id);
  }
}
let refHandlerInstalled = false;
export function installRefHandler() {
  if (refHandlerInstalled) return;
  refHandlerInstalled = true;
  document.addEventListener('click', (e) => {
    const a = e.target && e.target.closest ? e.target.closest('a.ref') : null;
    if (!a) return;
    e.preventDefault();
    if (e.ctrlKey || e.metaKey) { if (a.getAttribute('href') !== '#') window.open(a.href, '_blank'); return; }
    openRef(a.dataset.refType, a.dataset.refId);
  });
}

// ---------------------------------------------------------------- widgets
/** Confidence in words + percent: "fairly confident (72 %)". */
export function confWords(v) {
  if (v === null || v === undefined || isNaN(v)) return t('plain.conf.unknown');
  const x = Math.max(0, Math.min(1, Number(v)));
  const key = x >= 0.9 ? 'veryHigh' : x >= 0.7 ? 'high' : x >= 0.5 ? 'medium' : x >= 0.3 ? 'low' : 'veryLow';
  return `${t('plain.conf.' + key)} (${fmt.pct(x)})`;
}
/** Severity in words + percent: "serious (78 %)". */
export function sevWords(v) {
  if (v === null || v === undefined || isNaN(v)) return t('plain.sev.unknown');
  const x = Math.max(0, Math.min(1, Number(v)));
  const key = x >= 0.85 ? 'critical' : x >= 0.65 ? 'high' : x >= 0.4 ? 'medium' : x >= 0.2 ? 'low' : 'minor';
  return `${t('plain.sev.' + key)} (${fmt.pct(x)})`;
}
/** "2.3 times the level considered normal" for a score against its threshold. */
export function timesThreshold(score, threshold) {
  if (score === null || score === undefined || !threshold) return null;
  const x = Number(score) / Number(threshold);
  if (!isFinite(x)) return null;
  return { x: x.toFixed(1), text: t('plain.timesThreshold', { x: x.toFixed(1) }) };
}
export function conf(v, { label = true, words = false } = {}) {
  const x = v === null || v === undefined ? null : Math.max(0, Math.min(1, Number(v)));
  const cls = x === null ? '' : x < 0.4 ? 'vlow' : x < 0.65 ? 'low' : '';
  const w = confWords(x);
  return el('span', { class: 'conf ' + cls, title: `${t('common.confidence')}: ${w}` }, el('span', { class: 'bar' }, el('i', { style: { width: (x === null ? 0 : x * 100) + '%' } })), label ? el('span', { text: words ? w : (x === null ? '–' : fmt.pct(x)) }) : null);
}
export function sev(v, { words = false } = {}) {
  const x = Math.max(0, Math.min(1, Number(v || 0)));
  const w = sevWords(x);
  return el('span', { class: 'sev ' + (x >= 0.7 ? 'high' : x < 0.35 ? 'low' : ''), title: `${t('common.severity')}: ${w}` }, el('span', { class: 'bar' }, el('i', { style: { width: x * 100 + '%' } })), el('span', { text: words ? w : fmt.pct(x) }));
}
const GLYPH = { inferred: '●', assumed: '◐', uncertain: '○', human: '◆' };
export function infStatus(status, { human } = {}) {
  const s = human ? 'human' : (status || 'uncertain');
  const label = human ? t('decision.' + human) : t('inference.' + s);
  return el('span', { class: 'inf ' + s, title: label }, el('span', { class: 'g', 'aria-hidden': 'true', text: GLYPH[s] || '○' }), el('span', { text: label }));
}
export function st(status, label) { return el('span', { class: 'st ' + (status || '') }, label === undefined ? (status || '') : label); }
export function chip(text, cls = '', attrs = {}) { return el('span', { class: 'chip ' + cls, ...attrs }, text); }
export function kindChip(kind) { const cls = { anomaly: 'fail', point: 'fail', drift: 'warn', changepoint: 'info', dq: 'warn', rule: 'info', cascade: 'fail' }[kind] || ''; return chip(t('mon.kind.' + kind) === 'mon.kind.' + kind ? kind : t('mon.kind.' + kind), cls); }
export function causeChip(cause) { const cls = { process: 'warn', sensor: 'info', data: '', mixed: 'fail', unknown: '' }[cause] || ''; return chip(t('mon.cause.' + cause) === 'mon.cause.' + cause ? cause : t('mon.cause.' + cause), cls, { title: t('mon.cause') }); }
export function meter(label, v, { color, right } = {}) {
  const x = Math.max(0, Math.min(1, Number(v || 0)));
  return el('div', { class: 'meter' }, el('span', {}, label), el('span', { class: 'bar' }, el('i', { style: { width: x * 100 + '%', background: color || '' } })), el('span', { class: 'right', text: right !== undefined ? right : fmt.pct(x) }));
}
export function kv(pairs) {
  const d = el('dl', { class: 'kv' });
  for (const [k, v] of pairs) { if (v === undefined || v === null || v === '') continue; d.append(el('dt', { text: k }), el('dd', {}, v)); }
  return d;
}
export function section(title, { level = 'operator', id, right } = {}) {
  const tag = level !== 'operator' ? el('span', { class: 'tag', text: t('role.tag.' + level) }) : null;
  const root = el('section', { class: 'sec', id, dataset: { level } }, el('div', { class: 'sec-head' }, el('h2', {}, title, ' ', tag), right || null));
  const body = el('div', { class: 'sec-body' });
  root.append(body);
  if (!roleAllows(level)) { root.hidden = true; root.dataset.hiddenByRole = '1'; }
  return { root, body };
}
export function hiddenHint(view) {
  const n = view.querySelectorAll('[data-hidden-by-role]').length;
  if (!n) return null;
  return el('p', { class: 'hidden-hint', text: t('role.hidden', { n, role: t('role.' + actorRole()) }) });
}
export function empty(text) { return el('div', { class: 'empty', text: text || t('common.none') }); }
export function notice(text, kind = '') { return el('div', { class: 'notice ' + kind }, text instanceof Node ? text : String(text)); }
export function unavailableNote(r) { return notice(errText(r), 'warn'); }
export function spinner() { return el('span', { class: 'dim spinner', text: t('common.loading') + '…' }); }
/** Adds the plain-language box of the lead's plain.js under a view head; silent when the module is missing. */
export async function addPlainBox(view, name) {
  try { const { plainBox } = await import('./plain.js'); const b = await plainBox(name); if (b) view.append(b); } catch (e) { console.debug('plain box unavailable', name, e && e.message); }
}

// ---------------------------------------------------------------- evidence
export function fmtValue(v, depth = 0) {
  if (v === null || v === undefined) return '–';
  if (typeof v === 'number') return fmt.num(v, Math.abs(v) < 1 ? 3 : 2);
  if (typeof v === 'boolean') return v ? t('common.yes') : t('common.no');
  if (typeof v === 'string') return v.length > 80 ? v.slice(0, 80) + '…' : v;
  if (Array.isArray(v)) { const items = v.slice(0, 6).map((x) => (typeof x === 'object' && x !== null ? fmtValue(x, depth + 1) : fmtValue(x, depth + 1))); return items.join(', ') + (v.length > 6 ? ` … +${v.length - 6}` : ''); }
  if (typeof v === 'object') {
    // plain readable "S06 0.69, S03 0.06" instead of a JSON blob
    const ents = Object.entries(v).filter(([, x]) => x !== null && x !== undefined);
    if (depth > 1) return `${ents.length} ${t('common.values')}`;
    return ents.slice(0, 6).map(([k, x]) => `${k} ${typeof x === 'object' ? fmtValue(x, depth + 1) : fmtValue(x, depth + 1)}`).join(', ') + (ents.length > 6 ? ` … +${ents.length - 6}` : '');
  }
  return String(v);
}
export function valuesLine(values, max = 10) {
  const ents = Object.entries(values || {}).filter(([, v]) => v !== null && v !== undefined);
  if (!ents.length) return null;
  return el('div', { class: 'meta values' }, `${t('common.values')}: `, ents.slice(0, max).map(([k, v], i) => el('span', { class: 'val' }, i ? ', ' : '', el('span', { class: 'k', text: k }), '=', fmtValue(v))), ents.length > max ? el('span', { class: 'dim', text: ` … +${ents.length - max}` }) : null);
}
/** Evidence by ids: {items, missing}. Never throws. The ids may name ANY citable object (EV-, DIAG-, FLAG-,
    CHK-, INF-, RULE-, PATTERN-, EGR-, batch ids): the server answers evidence-like items, each with a
    plain-language sentence (`plain`), its technical `statement`, `ref_type`, `open` (owning view) and its own
    `evidence_ids`. Found items are cached per run and language; missing ids are always asked again. */
export async function fetchEvidenceFull(ids) {
  ids = [...new Set((ids || []).filter(Boolean).map(String))];
  if (!ids.length) return { items: [], missing: [] };
  const key = (id) => `${state.run}:ev:${state.lang}:${id}`;
  const need = ids.filter((id) => !state.cache.has(key(id)));
  let missing = []; let error;
  for (let i = 0; i < need.length; i += 60) {
    const part = need.slice(i, i + 60);
    const r = await runApi('/evidence', { params: { ids: part.join(','), lang: state.lang } });
    if (!r.ok) { console.warn('evidence fetch failed', r.status, r.data); error = errText(r); missing = missing.concat(part); continue; }
    const got = r.data.items || [];
    for (const e of got) { state.cache.set(key(e.id), e); if (e.requested_id) state.cache.set(key(e.requested_id), e); }
    missing = missing.concat(r.data.missing || part.filter((id) => !got.some((e) => e.id === id || e.requested_id === id)));
  }
  const seen = new Set();
  const items = ids.map((id) => state.cache.get(key(id))).filter((e) => e && !seen.has(e.id) && seen.add(e.id));
  if (missing.length) console.warn('ids not found in this run:', missing.join(', '));
  return error ? { items, missing, error } : { items, missing };
}
export async function fetchEvidence(ids) { return (await fetchEvidenceFull(ids)).items; }
/** Footer of a non-evidence item (diagnosis, flag, check, ...): "Open in <view>" + its own evidence, loaded on demand. */
function objectFooter(e) {
  const row = el('div', { class: 'row', style: { marginTop: '6px', flexWrap: 'wrap', alignItems: 'flex-start' } });
  const open = e.open;
  if (open && open.view) {
    const viewName = t('nav.' + open.view) === 'nav.' + open.view ? open.view : t('nav.' + open.view);
    row.append(el('a', { class: 'btn btn-sm', href: hashFor(open.view, open.params), onClick: (ev) => { if (ev.ctrlKey || ev.metaKey) return; ev.preventDefault(); closeAllModals(); navigate(open.view, open.params); } }, t('ref.openIn', { view: viewName })));
  }
  const ids = (e.evidence_ids || []).concat(e.inference_ids || []).filter(Boolean);
  if (ids.length) {
    const nested = el('details', { class: 'evnested', style: { flex: '1 1 260px', minWidth: '0' } }, el('summary', { class: 'small', style: { cursor: 'pointer', padding: '4px 0' } }, t('evidence.behind', { n: ids.length })));
    let loaded = false;
    nested.addEventListener('toggle', () => { if (nested.open && !loaded) { loaded = true; nested.append(el('div', { style: { paddingLeft: '12px', borderLeft: '2px solid var(--line)' } }, evidencePanel(ids, { heading: false }))); } });
    row.append(nested);
  }
  return row.childNodes.length ? row : null;
}
/** One evidence (or evidence-like) item: the plain-language sentence first; the original technical statement,
    provenance and values sit under "Technical detail" (collapsed for operators, open for engineers/reviewers). */
export function evidenceItem(e) {
  const type = e.ref_type || refTypeOfId(e.id);
  const isObj = type !== 'evidence';
  const stmt = cleanText(e.statement);
  const plain = cleanText(e.plain || '');
  const sigs = e.signals && e.signals.length ? refChips('signal', e.signals) : null;
  const meta = [!isObj && e.kind ? el('span', { text: e.kind }) : null, sigs, e.n_samples ? el('span', { text: `${fmt.int(e.n_samples)} ${t('common.n_samples')}` }) : null, e.computed_by ? el('span', { text: `${t('common.computedBy')} ${e.computed_by}` }) : null, e.group_id ? el('span', {}, `${t('common.group')} `, refLink('group', e.group_id)) : null, e.batch_id && type !== 'batch' ? refLink('batch', e.batch_id) : null].filter(Boolean).flatMap((x, i) => (i ? [' — ', x] : [x]));
  const engineer = roleAllows('engineer');
  const tech = el('details', { class: 'evtech', open: engineer, style: { marginTop: '3px' } },
    el('summary', { class: 'small dim', style: { cursor: 'pointer' } }, t('brief.showTech') === 'brief.showTech' ? t('evidence.technical') : t('brief.showTech')),
    plain && stmt ? el('div', { class: 'small muted techstmt', style: { margin: '2px 0', overflowWrap: 'anywhere' } }, linkifyRefs(stmt)) : null,
    meta.length ? el('div', { class: 'meta' }, meta) : null,
    engineer ? valuesLine(e.values) : null);
  return el('li', { dataset: { refType: type, evId: e.id } },
    el('div', { class: 'evline' }, isObj ? citeLink(e.id) : refLink('evidence', e.id, e.id), ' ', isObj ? [chip(t('ref.' + type) === 'ref.' + type ? type : t('ref.' + type)), ' '] : null, el('span', { class: 'stmt plain' }, linkifyRefs(plain || stmt || '–'))),
    tech.childNodes.length > 1 ? tech : null,
    isObj ? objectFooter(e) : null);
}
/** Evidence list; `missing` ids are named so a stale registry never looks like "no evidence". */
export function evidenceList(items, { missing = [], expected, error } = {}) {
  items = items || [];
  const wrap = el('div', { class: 'evwrap' });
  if (items.length) wrap.append(el('ul', { class: 'evlist' }, items.map(evidenceItem)));
  const names = missing.slice(0, 6).join(', ') + (missing.length > 6 ? ` +${missing.length - 6}` : '');
  if (error) wrap.append(notice(error, 'fail'));
  else if (missing.length) wrap.append(notice(t(missing.every((i) => refTypeOfId(i) === 'evidence') ? 'evidence.missing' : 'evidence.missingAny', { ids: names }), 'warn'));
  else if (!items.length) wrap.append(empty(expected === 0 || expected === undefined ? t('evidence.noneCited') : t('common.none')));
  return wrap;
}
/** Self-filling evidence box for an object's evidence_ids. */
export function evidencePanel(ids, { heading = true } = {}) {
  ids = (ids || []).filter(Boolean);
  const box = el('div', { class: 'evpanel' }, heading ? el('h4', { class: 'small muted' }, t('common.evidence'), ' ', el('span', { class: 'dim', text: ids.length ? `(${ids.length})` : '' })) : null);
  const sp = spinner(); box.append(sp);
  fetchEvidenceFull(ids).then(({ items, missing, error }) => { sp.remove(); box.append(evidenceList(items, { missing, expected: ids.length, error })); });
  return box;
}
export function evidenceButton(ids, { label } = {}) {
  ids = (ids || []).filter(Boolean);
  const b = el('button', { class: 'btn btn-sm btn-quiet', type: 'button', disabled: !ids.length, title: ids.length ? ids.slice(0, 6).join(', ') : t('evidence.noneCited') }, label || t('common.evidenceCount', { n: ids.length }));
  b.addEventListener('click', async () => { showEvidenceModal(ids); });
  return b;
}
export async function showEvidenceModal(ids, title) {
  ids = (ids || []).filter(Boolean);
  const body = el('div', {}, spinner());
  modal({ title: title || t('common.evidence'), body, wide: true });
  const { items, missing, error } = await fetchEvidenceFull(ids);
  clear(body).append(evidenceList(items, { missing, expected: ids.length, error }));
}
/** Clickable cited ids (each opens its own popover). Citations are not always EV- ids: answers and log entries
    also cite DIAG-, FLAG-, CHK-, INF-... so every chip is typed by its prefix. */
export function evChips(ids, { max = 8 } = {}) {
  ids = [...new Set((ids || []).filter(Boolean).map(String))];
  return el('span', { class: 'evs' }, ids.slice(0, max).map((id) => citeLink(id)), ids.length > max ? el('span', { class: 'dim small', text: ` +${ids.length - max}` }) : null);
}
bus.on('decision', () => { for (const k of [...state.cache.keys()]) if (k.includes(':ev:')) state.cache.delete(k); });

// ---------------------------------------------------------------- reference popovers (evidence / check / inference / rule / egress)
async function findCheck(id) {
  const r = await cachedRunApi('checks-all', '/checks', { params: { limit: 20000 } });
  return r.ok ? (r.data.items || []).find((c) => c.check_id === id) : null;
}
export function checkCard(c) {
  return el('div', { class: 'refcard' },
    el('div', { class: 'row' }, st(c.status, t('dq.' + c.status)), chip(c.category), el('span', { class: 'dim small', text: c.check_type }), c.rule_id ? refLink('rule', c.rule_id) : null),
    el('p', { class: 'stmt' }, linkifyRefs(cleanText(c.statement))),
    kv([[t('common.batch'), c.batch_id ? refLink('batch', c.batch_id) : null], [t('common.group'), c.group_id ? refLink('group', c.group_id) : null], [t('common.signals'), (c.signals || []).length ? refChips('signal', c.signals) : null], [t('common.severity'), sev(c.severity, { words: true })], [t('mon.rows'), c.row_start !== null && c.row_start !== undefined ? `${c.row_start}–${c.row_end}` : null]]),
    valuesLine(c.values),
    evidencePanel(c.evidence_ids),
    el('div', { class: 'row', style: { marginTop: '10px' } }, el('button', { class: 'btn btn-sm', type: 'button', onClick: () => { closeAllModals(); navigate('quality', { batch: c.batch_id, check: c.check_id }); } }, t('ref.openIn', { view: t('nav.quality') }))));
}
export function inferenceCard(inf) {
  return el('div', { class: 'refcard' },
    el('div', { class: 'row' }, infStatus(inf.status, { human: inf.human_status }), conf(inf.confidence, { words: true }), inf.stage ? chip(inf.stage) : null, inf.source && inf.source !== 'code' ? el('span', { class: 'dim small', text: `${t('common.source')}: ${inf.source}` }) : null),
    el('p', { class: 'stmt' }, linkifyRefs(cleanText(inf.claim))),
    inf.reasoning ? el('p', { class: 'small muted' }, linkifyRefs(cleanText(inf.reasoning))) : null,
    (inf.alternatives || []).length ? el('p', { class: 'small dim' }, t('ref.alternatives') + ': ', linkifyRefs(inf.alternatives.join('; '))) : null,
    kv([[t('ref.subject'), /^S\d{2,3}$/.test(inf.subject || '') ? refLink('signal', inf.subject) : inf.subject]]),
    evidencePanel(inf.evidence_ids));
}
export function ruleCard(rule) {
  return el('div', { class: 'refcard' },
    el('div', { class: 'row' }, chip(t('dq.ruleStatus.' + rule.status) === 'dq.ruleStatus.' + rule.status ? rule.status : t('dq.ruleStatus.' + rule.status), { active: 'ok', approved: 'ok', draft: 'warn', rejected: 'fail' }[rule.status] || ''), conf(rule.compile_confidence, { words: true }), el('span', { class: 'dim small', text: `${t('common.source')}: ${rule.compile_source || 'template'}` })),
    el('p', { class: 'stmt' }, linkifyRefs(cleanText(rule.text))),
    rule.compile_explanation ? el('p', { class: 'small muted' }, linkifyRefs(cleanText(rule.compile_explanation))) : null,
    rule.compiled ? el('code', { class: 'spec', text: JSON.stringify(rule.compiled) }) : null,
    el('div', { class: 'row', style: { marginTop: '10px' } }, el('button', { class: 'btn btn-sm', type: 'button', onClick: () => { closeAllModals(); navigate('quality', { rule: rule.id }); } }, t('ref.openIn', { view: t('nav.quality') }))));
}
export function egressCard(r) {
  return el('div', { class: 'refcard' },
    el('div', { class: 'row' }, chip(r.route, r.route === 'external' ? 'warn' : 'ok'), chip(r.guard_result, { allowed: 'ok', blocked: 'fail', fallback: 'warn' }[r.guard_result] || ''), el('span', { class: 'small', text: `${r.provider || ''} ${r.model || ''}` })),
    kv([[t('flow.task'), r.task], [t('flow.artifacts'), (r.artifact_types || []).join(', ')], [t('flow.bytes'), fmt.bytes(r.payload_bytes)], [t('log.ts'), fmt.ts(r.ts)], [t('flow.guardResult'), r.guard_reason], [t('common.status'), r.ok ? 'ok' : (r.error || 'error')]]),
    r.payload_preview ? el('pre', { class: 'small preview', text: r.payload_preview }) : null);
}
/** Popover for any cited id. The title names what the id really is (a DIAG- id cited as "evidence" is a
    diagnosis). Evidence, diagnoses, flags, patterns and batches show the evidence-like item of /evidence (plain
    sentence, technical detail, "Open in ...", own evidence); checks, inferences, rules and model calls keep
    their detailed cards with the plain sentence on top, and fall back to the generic item when the card's
    source does not know the id. */
export async function showRefModal(type, id) {
  const actual = type === 'cite' || type === 'evidence' || !type ? refTypeOfId(id) : type;
  const body = el('div', { class: 'refbox' }, spinner());
  modal({ title: `${t('ref.' + actual)} ${id}`, body, wide: true });
  let node;
  try {
    if (!state.run) node = notice(t('runs.noRunHint'), 'warn');
    else {
      const { items, missing, error } = await fetchEvidenceFull([id]);
      const generic = () => evidenceList(items, { missing, expected: 1, error });
      const lead = items[0] && items[0].plain ? el('p', { class: 'stmt prose plain-lead' }, linkifyRefs(cleanText(items[0].plain))) : null;
      let card = null;
      if (actual === 'inference') { const r = await runApi('/inferences', { params: { ids: id } }); const it = r.ok && r.data.items ? r.data.items[0] : null; card = it ? inferenceCard(it) : null; }
      else if (actual === 'check') { const c = await findCheck(id); card = c ? checkCard(c) : null; }
      else if (actual === 'rule') { const r = await runApi('/rules'); const rule = r.ok ? (r.data.rules || []).find((x) => x.id === id) : null; card = rule ? ruleCard(rule) : null; }
      else if (actual === 'egress') { const r = await runApi('/egress'); const rec = r.ok ? (r.data.ledger || []).find((x) => x.id === id) : null; card = rec ? egressCard(rec) : null; }
      if (card && lead && (actual === 'check' || actual === 'inference')) {
        // the card's own statement is the technical wording: keep it, smaller, under the plain sentence
        const s = card.querySelector('p.stmt');
        if (s) { s.className = 'small muted techstmt'; s.prepend(el('span', { class: 'dim', text: t('evidence.technical') + ': ' })); }
      }
      node = card ? frag(lead, card) : generic();
      // plain summary of the object first (what it is, what to do now); everything above goes under "Show technical analyses"
      try {
        const { itemBrief, techNested } = await import('./brief.js');
        if (items.length) {
          const tech = techNested(node);
          tech.open = roleAllows('engineer');
          node = frag(itemBrief(id, { onLoad: (d) => { if (!d) tech.open = true; } }), tech);
        }
      } catch (e) { console.debug('brief unavailable', e && e.message); }
    }
  } catch (e) { node = notice(String(e && e.message ? e.message : e), 'fail'); }
  clear(body).append(node);
}

// ---------------------------------------------------------------- decisions
export async function postDecision(objectType, objectId, action, { note, newValue } = {}) {
  const r = await runApi('/decisions', { method: 'POST', body: { actor_name: actorName(), role: actorRole(), action, object_type: objectType, object_id: objectId, note: note || null, new_value: newValue || null } });
  if (r.ok) { toast(t('decision.recorded', { seq: r.data.log_seq }), 'ok'); bus.emit('decision', { objectType, objectId, action, result: r.data }); }
  else toast(errText(r), 'fail');
  return r;
}
/** Accept / question / override for any system conclusion.  overrideFields: [{key,label,type:'select'|'text',options}] */
export function decisionBar(objectType, objectId, { current, note, overrideFields = [], onDone, askContext } = {}) {
  const wrap = el('div', { class: 'decisions' });
  const stateEl = el('span', { class: 'state' });
  const render = () => {
    clear(stateEl);
    if (current) stateEl.append(infStatus(null, { human: current }), note ? el('span', { class: 'dim', text: ' — ' + note }) : null);
  };
  render();
  const doIt = async (action, extra) => {
    const r = await postDecision(objectType, objectId, action, extra);
    if (r.ok) { current = { accept: 'accepted', question: 'questioned', override: 'overridden', dismiss: 'dismissed' }[action] || current; note = extra && extra.note; render(); if (onDone) onDone(action, r.data); }
  };
  const askNote = (action) => new Promise((resolve) => {
    const ta = el('textarea', { rows: 2, placeholder: t('common.note') });
    modal({ title: t('decision.' + action) + ' ' + objectId, body: el('div', {}, ta), onClose: () => resolve(null), actions: [
      { label: t('common.cancel'), onClick: (c) => { c(); resolve(null); } },
      { label: t('decision.' + action), cls: 'btn-primary', onClick: (c) => { resolve({ note: ta.value.trim() }); c(); } },
    ] });
  });
  wrap.append(
    el('button', { class: 'btn btn-sm btn-accept', type: 'button', onClick: async () => { const x = await askNote('accept'); if (x) doIt('accept', x); } }, t('decision.accept')),
    el('button', { class: 'btn btn-sm btn-question', type: 'button', onClick: async () => { const x = await askNote('question'); if (x) { await doIt('question', x); if (askContext) bus.emit('chat.open', { ...askContext, seed: x.note }); } } }, t('decision.question')),
    el('button', { class: 'btn btn-sm btn-override', type: 'button', onClick: () => {
      const inputs = {};
      const form = el('div', {}, overrideFields.map((f) => {
        const input = f.type === 'select' ? el('select', {}, f.options.map((o) => el('option', { value: o.value === undefined ? o : o.value, text: o.label || o }))) : el('input', { type: 'text', value: f.value || '' });
        if (f.value !== undefined && f.type === 'select') input.value = f.value;
        inputs[f.key] = input;
        return el('label', { class: 'field' }, el('span', { text: f.label }), input);
      }), el('label', { class: 'field' }, el('span', { text: t('decision.override.reason') }), el('textarea', { rows: 2, id: 'ov-note' })));
      modal({ title: t('decision.override.title', { id: objectId }), body: form, actions: [
        { label: t('common.cancel'), onClick: (c) => c() },
        { label: t('decision.override'), cls: 'btn-primary', onClick: (c) => { const nv = {}; for (const [k, i] of Object.entries(inputs)) nv[k] = i.value; doIt('override', { note: form.querySelector('#ov-note').value.trim(), newValue: Object.keys(nv).length ? nv : null }); c(); } },
      ] });
    } }, t('decision.override')),
    stateEl,
  );
  return wrap;
}

// ---------------------------------------------------------------- tables
/** columns: [{key, label, render(row), cls, num}] ; rows: array ; onRow(row, tr) ; rowClass(row) ; pageSize */
export function table({ columns, rows, pageSize = 25, onRow, rowClass, selectedKey, keyOf = (r) => r.id, emptyText }) {
  const wrap = el('div', {});
  const tw = el('div', { class: 'tbl-wrap' });
  const tbl = el('table', { class: 'tbl' });
  tbl.append(el('thead', {}, el('tr', {}, columns.map((c) => el('th', { class: c.num ? 'num' : '', text: c.label })))));
  const tbody = el('tbody');
  tbl.append(tbody); tw.append(tbl); wrap.append(tw);
  let page = 0;
  const pager = el('div', { class: 'pager' });
  const render = () => {
    clear(tbody);
    const n = Math.max(1, Math.ceil(rows.length / pageSize));
    page = Math.min(page, n - 1);
    const slice = rows.slice(page * pageSize, (page + 1) * pageSize);
    if (!rows.length) tbody.append(el('tr', {}, el('td', { colspan: columns.length, class: 'empty', text: emptyText || t('common.none') })));
    for (const r of slice) {
      const tr = el('tr', { class: [(onRow ? 'click' : ''), rowClass ? rowClass(r) : '', selectedKey !== undefined && keyOf(r) === selectedKey ? 'sel' : ''].join(' ') });
      for (const c of columns) {
        const v = c.render ? c.render(r) : r[c.key];
        tr.append(el('td', { class: [c.cls || '', c.num ? 'num' : ''].join(' ') }, v === undefined || v === null ? '' : v));
      }
      if (onRow) { tr.addEventListener('click', (e) => { if (e.target && e.target.closest && e.target.closest('a.ref, button')) return; tbody.querySelectorAll('tr.sel').forEach((x) => x.classList.remove('sel')); tr.classList.add('sel'); onRow(r, tr); }); tr.tabIndex = 0; tr.addEventListener('keydown', (e) => { if (e.key === 'Enter' && e.target === tr) tr.click(); }); }
      tbody.append(tr);
    }
    clear(pager);
    if (rows.length > pageSize) pager.append(
      el('button', { class: 'btn btn-sm btn-quiet', type: 'button', disabled: page === 0, onClick: () => { page--; render(); } }, t('common.prev')),
      el('span', { text: t('common.page', { p: page + 1, n }) }),
      el('button', { class: 'btn btn-sm btn-quiet', type: 'button', disabled: page >= n - 1, onClick: () => { page++; render(); } }, t('common.next')),
    );
  };
  render();
  wrap.append(pager);
  wrap.update = (newRows) => { rows = newRows; page = 0; render(); };
  /** show the page that holds `key` and return its row element */
  wrap.reveal = (key) => { const i = rows.findIndex((r) => keyOf(r) === key); if (i < 0) return null; page = Math.floor(i / pageSize); render(); const tr = tbody.children[i - page * pageSize]; if (tr) { tbody.querySelectorAll('tr.sel').forEach((x) => x.classList.remove('sel')); tr.classList.add('sel'); } return tr || null; };
  return wrap;
}

export function viewHead(num, title, right) {
  const back = canGoBack() ? el('button', { class: 'btn btn-quiet btn-sm view-back', type: 'button', onClick: goBack, title: t('nav.backTitle') }, '←', ' ', t('nav.back')) : null;
  return el('div', { class: 'view-head' }, el('div', { class: 'view-title' }, back, el('h1', {}, el('span', { class: 'num', text: num }), title)), right || null);
}
export function needRun() { return el('div', { class: 'notice warn', text: t('runs.noRunHint') }); }
export function signalLabel(id, signalsById) {
  const s = signalsById && signalsById[id];
  const given = (s && s.display_name) || (state.signalNames && state.signalNames[id]);
  return el('span', { title: s && s.source_column ? `${t('und.sourceName')}: ${s.source_column}` : undefined, text: given ? `${given} (${id})` : id });
}
/** Open every closed <details> above a node: the page keeps its technical part folded ("Show technical analyses"),
    and whatever a link, a deep link or the back button points at must be visible before it is scrolled to. */
export function revealAncestors(node) {
  let opened = false;
  for (let p = node && node.parentElement; p; p = p.parentElement) if (p.tagName === 'DETAILS' && !p.open) { p.open = true; opened = true; }
  return opened;
}
/** Highlight an element briefly and scroll it into view (unfolding the technical part around it first). */
export function flash(node) {
  if (!node) return;
  revealAncestors(node);
  node.classList.add('flash');
  try { node.scrollIntoView({ block: 'center', behavior: 'smooth' }); } catch { /* ignore */ }
  setTimeout(() => node.classList.remove('flash'), 2500);
}
