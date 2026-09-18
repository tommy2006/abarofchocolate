/* Core: state, i18n, API client, DOM helpers, shared widgets (confidence, status glyphs, evidence,
   decision buttons, paginated tables). No framework, ES2020 modules. */

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
  bytes(b) { b = Number(b || 0); if (b < 1024) return b + ' B'; if (b < 1048576) return (b / 1024).toFixed(1) + ' kB'; return (b / 1048576).toFixed(2) + ' MB'; },
  sec(s) { s = Number(s || 0); if (s < 90) return s.toFixed(0) + ' s'; return (s / 60).toFixed(1) + ' min'; },
};

// ---------------------------------------------------------------- toasts, modals
export function toast(msg, kind = '') {
  const root = document.getElementById('toasts');
  const n = el('div', { class: 'toast ' + kind, text: msg });
  root.append(n);
  setTimeout(() => n.remove(), kind === 'fail' ? 7000 : 4000);
}
export function modal({ title, body, actions = [], wide = false, onClose }) {
  const root = document.getElementById('modal-root');
  const back = el('div', { class: 'modal-back' });
  const box = el('div', { class: 'modal' + (wide ? ' wide' : ''), role: 'dialog', 'aria-modal': 'true', 'aria-label': title });
  const close = () => { back.remove(); document.removeEventListener('keydown', esc); if (onClose) onClose(); };
  const esc = (e) => { if (e.key === 'Escape') close(); };
  document.addEventListener('keydown', esc);
  back.addEventListener('click', (e) => { if (e.target === back) close(); });
  box.append(el('div', { class: 'modal-head' }, el('h2', { text: title }), el('button', { class: 'btn btn-quiet btn-sm', type: 'button', onClick: close, 'aria-label': t('common.close') }, '✕')));
  box.append(el('div', { class: 'modal-body' }, body));
  if (actions.length) box.append(el('div', { class: 'modal-foot' }, actions.map((a) => el('button', { class: 'btn ' + (a.cls || ''), type: 'button', onClick: () => a.onClick(close) }, a.label))));
  back.append(box); root.append(back);
  const first = box.querySelector('input, select, textarea, button.btn-primary'); if (first) first.focus();
  return { close, box };
}
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

// ---------------------------------------------------------------- widgets
export function conf(v, { label = true } = {}) {
  const x = v === null || v === undefined ? null : Math.max(0, Math.min(1, Number(v)));
  const cls = x === null ? '' : x < 0.4 ? 'vlow' : x < 0.65 ? 'low' : '';
  return el('span', { class: 'conf ' + cls, title: t('common.confidence') }, el('span', { class: 'bar' }, el('i', { style: { width: (x === null ? 0 : x * 100) + '%' } })), label ? el('span', { text: x === null ? '–' : fmt.pct(x) }) : null);
}
export function sev(v) {
  const x = Math.max(0, Math.min(1, Number(v || 0)));
  return el('span', { class: 'sev ' + (x >= 0.7 ? 'high' : x < 0.35 ? 'low' : ''), title: t('common.severity') }, el('span', { class: 'bar' }, el('i', { style: { width: x * 100 + '%' } })), el('span', { text: fmt.pct(x) }));
}
const GLYPH = { inferred: '●', assumed: '◐', uncertain: '○', human: '◆' };
export function infStatus(status, { human } = {}) {
  const s = human ? 'human' : (status || 'uncertain');
  const label = human ? t('decision.' + human) : t('inference.' + s);
  return el('span', { class: 'inf ' + s, title: label }, el('span', { class: 'g', 'aria-hidden': 'true', text: GLYPH[s] || '○' }), el('span', { text: label }));
}
export function st(status, label) { return el('span', { class: 'st ' + (status || '') }, label === undefined ? (status || '') : label); }
export function chip(text, cls = '', attrs = {}) { return el('span', { class: 'chip ' + cls, ...attrs }, text); }
export function kindChip(kind) { const cls = { anomaly: 'fail', drift: 'warn', changepoint: 'info', dq: 'warn', rule: 'info', cascade: 'fail' }[kind] || ''; return chip(t('mon.kind.' + kind) === 'mon.kind.' + kind ? kind : t('mon.kind.' + kind), cls); }
export function causeChip(cause) { const cls = { process: 'warn', sensor: 'info', data: '', mixed: 'fail', unknown: '' }[cause] || ''; return chip(t('mon.cause.' + cause) === 'mon.cause.' + cause ? cause : t('mon.cause.' + cause), cls); }
export function meter(label, v, { color } = {}) {
  const x = Math.max(0, Math.min(1, Number(v || 0)));
  return el('div', { class: 'meter' }, el('span', { text: label }), el('span', { class: 'bar' }, el('i', { style: { width: x * 100 + '%', background: color || '' } })), el('span', { class: 'right', text: fmt.pct(x) }));
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
export function notice(text, kind = '') { return el('div', { class: 'notice ' + kind, text }); }
export function unavailableNote(r) { return notice(errText(r), 'warn'); }
export function spinner() { return el('span', { class: 'dim', text: t('common.loading') + '…' }); }

// ---------------------------------------------------------------- evidence
export async function fetchEvidence(ids) {
  if (!ids || !ids.length) return [];
  const r = await runApi('/evidence', { params: { ids: ids.join(',') } });
  return r.ok ? r.data.items || [] : [];
}
export function evidenceList(items) {
  if (!items.length) return empty();
  return el('ul', { class: 'evlist' }, items.map((e) => el('li', {},
    el('span', { class: 'id', text: e.id }), el('span', { text: e.statement }),
    el('div', { class: 'meta' }, [e.kind, e.signals && e.signals.length ? e.signals.join(', ') : null, e.n_samples ? `${fmt.int(e.n_samples)} ${t('common.n_samples')}` : null, e.computed_by ? `${t('common.computedBy')} ${e.computed_by}` : null, e.group_id ? `${t('common.group')} ${e.group_id}` : null, e.batch_id || null].filter(Boolean).join(' — ')),
    roleAllows('engineer') && e.values && Object.keys(e.values).length ? el('div', { class: 'meta', text: t('common.values') + ': ' + Object.entries(e.values).map(([k, v]) => `${k}=${typeof v === 'number' ? fmt.num(v, 3) : JSON.stringify(v)}`).join(', ') }) : null)));
}
export function evidenceButton(ids, { label } = {}) {
  ids = ids || [];
  const b = el('button', { class: 'btn btn-sm btn-quiet', type: 'button', disabled: !ids.length }, label || t('common.evidenceCount', { n: ids.length }));
  b.addEventListener('click', async () => { showEvidenceModal(ids); });
  return b;
}
export async function showEvidenceModal(ids, title) {
  const body = el('div', {}, spinner());
  modal({ title: title || t('common.evidence'), body, wide: true });
  const items = await fetchEvidence(ids);
  clear(body).append(evidenceList(items));
}
export function evChips(ids) {
  return el('span', { class: 'evs' }, (ids || []).slice(0, 8).map((id) => chip(id, 'click', { onClick: () => showEvidenceModal([id]), role: 'button', tabindex: '0' })));
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
      if (onRow) { tr.addEventListener('click', () => { tbody.querySelectorAll('tr.sel').forEach((x) => x.classList.remove('sel')); tr.classList.add('sel'); onRow(r, tr); }); tr.tabIndex = 0; tr.addEventListener('keydown', (e) => { if (e.key === 'Enter') tr.click(); }); }
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
  return wrap;
}

export function viewHead(num, title, right) {
  return el('div', { class: 'view-head' }, el('h1', {}, el('span', { class: 'num', text: num }), title), right || null);
}
export function needRun() { return el('div', { class: 'notice warn', text: t('runs.noRunHint') }); }
export function signalLabel(id, signalsById) {
  const s = signalsById && signalsById[id];
  return el('span', { title: s && s.source_column ? `${t('und.sourceName')}: ${s.source_column}` : undefined, text: id });
}
