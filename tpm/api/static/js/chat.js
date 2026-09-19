/* Global chat drawer: usable from any view, pre-loaded with the context of a flag / diagnosis / signal.
   Answers come from tpm.llm.agent.chat when a model exists, otherwise from the server's template
   answers; the source is always labelled. Every reference in an answer (FLAG-, EV-, S07, B00003...)
   is a link.

   Several chats per run: each chat has its own context, messages and title (the context it was opened
   with, else the first question). The list lives in localStorage per run; the server keeps every turn in
   chat.jsonl tagged with the chat id, so a chat's history can be reloaded on another browser.
   A context chosen on a page (Ask why) REPLACES the context of the current chat and the thread says so
   (picking several items in a row leaves one line: the context the next question is about).
   A running answer can be stopped: the fetch is aborted and the server is told, so the local model is
   not kept busy between its steps. */
import { state, t, el, clear, api, runApi, errText, bus, chip, evChips, evidencePanel, actorName, actorRole, kindChip, store, linkifyRefs, cleanText, refLink, confirmDialog, toast } from './core.js';
import { answerBlock, bt } from './brief.js';
import { sparkline } from './charts.js';

const KEEP_MSGS = 40;                       // messages kept per chat in localStorage
const chat = { open: false, chats: [], current: null, busy: false, abort: null, turnId: null, run: null, loaded: null };
let drawer, msgsEl, ctxEl, inputEl, sendBtn, stopBtn;

const uid = (p) => p + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);

export function initChat() {
  drawer = document.getElementById('drawer');
  const btn = document.getElementById('chat-btn');
  btn.addEventListener('click', () => toggle());
  bus.on('chat.open', (ctx) => openChat(ctx));
  bus.on('run.changed', () => { stop(true); loadChats(); if (chat.open) render(); });
  bus.on('lang.changed', () => { btn.textContent = t('status.chat'); if (chat.open) render(); });
  btn.textContent = t('status.chat');
  document.addEventListener('click', (e) => { const m = drawer.querySelector('.chat-menu[open]'); if (m && !m.contains(e.target)) m.open = false; });
}

export function toggle(force) {
  chat.open = force === undefined ? !chat.open : force;
  drawer.hidden = !chat.open;
  document.querySelector('.frame').classList.toggle('with-drawer', chat.open);
  document.getElementById('chat-btn').setAttribute('aria-pressed', String(chat.open));
  if (chat.open) { render(); if (inputEl) inputEl.focus(); }
}

/** Open the drawer on the current chat with this context (replacing the chat's previous context). */
export async function openChat(ctx) {
  setChatContext(ctx);
  toggle(true);
  if (ctx && ctx.seed) { inputEl.value = ctx.seed; }
  if (ctx && ctx.autoAsk) { if (chat.busy) inputEl.value = ctx.autoAsk; else send(ctx.autoAsk); }
}
/** Set the context (flag / diagnosis / signal) of the CURRENT chat without opening the drawer. On a chat that
    already has messages the new context overrides the old one and a line in the thread says so; picking several
    items in a row (the pages set the context of whatever is selected) leaves ONE line, and none when the context
    is back to the one of the last question. `cleared`: the person removed the context with the chip's x. */
export function setChatContext(ctx, { cleared = false } = {}) {
  const c = cur();
  const next = normCtx(ctx);
  if (!cleared) chat.pageCtx = { view: state.view, run: state.run, ctx: next };   // the item on the page: "+ New chat" starts from it
  const changed = ctxKey(next) !== ctxKey(c.context);
  const before = ctxKey(c.context);
  c.context = next;
  if (changed && c.messages.length) {
    const last = c.messages[c.messages.length - 1];
    const pending = last.role === 'system';                     // a context line with no question after it yet
    const from = pending ? last.from : before;
    if (pending) c.messages.pop();
    if (ctxKey(next) !== from) { const line = { role: 'system', from, kind: next || !cleared ? 'changed' : 'cleared', ctx: next && { object_id: next.object_id, diagnosis_id: next.diagnosis_id, flag_id: next.flag_id, signal_id: next.signal_id, title: next.title } }; line.text = sysText(line); c.messages.push(line); }
  }
  // an empty chat is named after its context until its first question
  if (!c.messages.some((m) => m.role === 'user') && (c.untitled || c.ctxTitled)) {
    if (next) Object.assign(c, { title: ctxTitle(next), untitled: false, ctxTitled: true });
    else if (c.ctxTitled) Object.assign(c, { title: chatN(c), untitled: true, ctxTitled: false });
  }
  saveChats();
  if (chat.open) render();
}
export function isChatOpen() { return chat.open; }

// ---------------------------------------------------------------- chats (list, storage, history)
const normCtx = (ctx) => (ctx && (ctx.object_id || ctx.flag_id || ctx.diagnosis_id || ctx.signal_id) ? ctx : null);
const ctxKey = (ctx) => (ctx ? String(ctx.object_id || ctx.flag_id || ctx.diagnosis_id || ctx.signal_id) : '');
/** A long text cut at a word: "Onset at row 2640 (group 7), abrupt…". */
function short(s, n) {
  s = String(s || '');
  if (s.length <= n) return s;
  const cut = s.lastIndexOf(' ', n - 1);
  return s.slice(0, cut > n * 0.6 ? cut : n - 1).replace(/[\s,;:.·–-]+$/, '') + '…';
}
/** The context in plain words: "DIAG-000005 · Data-quality issue on S06" (the object the chat is about, not the
    flag a diagnosis explains). */
function ctxText(ctx) {
  if (!ctx) return t('chat.noContext');
  const id = ctx.object_id || ctx.diagnosis_id || ctx.flag_id || ctx.signal_id;
  const title = cleanText(ctx.title || '');
  return title ? `${id} · ${short(title, 60)}` : String(id);
}
const ctxTitle = (ctx) => short(ctxText(ctx), 44);
/** A "context changed" line in the current language (lines saved before carry only their text). */
function sysText(m) {
  if (m.kind === 'cleared') return t('chat.contextCleared');
  if (m.kind === 'changed') return t('chat.contextChanged', { ctx: m.ctx ? ctxText(m.ctx) : t('chat.noContext') }).replace(/…\.$/, '…');
  return m.text || '';
}
const chatN = (c) => t('chat.chatN', { n: (chat.chats.indexOf(c) + 1) || chat.chats.length + 1 });
const storeKey = () => 'chats.' + (state.run || '-');

function makeChat(ctx, title) {
  const n = chat.chats.length + 1;
  return { id: uid('c'), title: title || (ctx ? ctxTitle(ctx) : t('chat.chatN', { n })), untitled: !title && !ctx, ctxTitled: !title && !!normCtx(ctx), context: normCtx(ctx), messages: [], created: new Date().toISOString() };
}
/** The current chat; a run without chats gets its "default" chat (the id old turns were written under). */
function cur() {
  if (!chat.chats.length) { const c = makeChat(null, null); c.id = 'default'; chat.chats.push(c); chat.current = c.id; }
  let c = chat.chats.find((x) => x.id === chat.current);
  if (!c) { c = chat.chats[0]; chat.current = c.id; }
  return c;
}
function saveChats() {
  const slim = chat.chats.map((c) => ({ ...c, messages: c.messages.slice(-KEEP_MSGS).map((m) => ({ role: m.role, text: m.text, source: m.source, model: m.model, route: m.route, evidence_ids: m.evidence_ids, followups: m.followups, note: m.note, stopped: m.stopped, from: m.from, kind: m.kind, ctx: m.ctx })) }));
  store.set(storeKey(), { chats: slim, current: chat.current });
}
async function loadChats() {
  chat.chats = []; chat.current = null; chat.loaded = state.run;
  if (!state.run) return;
  const saved = store.get(storeKey());
  if (saved && Array.isArray(saved.chats) && saved.chats.length) { chat.chats = saved.chats.map((c) => ({ ...c, messages: c.messages || [] })); chat.current = saved.current; }
  const run = state.run;
  const r = await runApi('/chat', { params: { limit: 1, chat_id: cur().id } });
  if (run !== state.run) return;
  if (r.ok) {
    // chats the server knows (another browser, or turns from before the chat list existed) join the list
    for (const s of r.data.chats || []) {
      if (!chat.chats.some((c) => c.id === s.chat_id)) chat.chats.push({ id: s.chat_id, title: s.first_question ? s.first_question.slice(0, 44) : (s.context && normCtx(s.context) ? ctxTitle(s.context) : t('chat.chatN', { n: chat.chats.length + 1 })), untitled: !s.first_question && !normCtx(s.context), context: normCtx(s.context), messages: [], created: s.last_ts || '' });
    }
  }
  await loadHistory(cur());
  saveChats();
  if (chat.open) render();
}
/** Server turns with each answer right after its own question: a stopped turn is written when the model step in flight
    returns, which can be after the next question was asked. */
function byTurn(items) {
  const answers = new Map(items.filter((m) => m.role !== 'user' && m.turn_id).map((m) => [m.turn_id, m]));
  const out = [];
  const placed = new Set();
  for (const m of items) {
    if (placed.has(m)) continue;
    out.push(m);
    const a = m.role === 'user' && m.turn_id ? answers.get(m.turn_id) : null;
    if (a && !placed.has(a)) { out.push(a); placed.add(a); }
  }
  return out;
}
/** Reload one chat's turns from the server (the browser copy may be shorter or from another machine). */
async function loadHistory(c) {
  if (!state.run) return;
  const run = state.run;
  const r = await runApi('/chat', { params: { limit: KEEP_MSGS, chat_id: c.id } });
  if (!r.ok || run !== state.run) return;
  const items = byTurn(r.data.items || []).map((m) => (m.stopped || m.status === 'stopped' || m.source === 'stopped'
    ? { role: 'assistant', text: '', stopped: true, source: 'stopped' }
    : { role: m.role, text: m.message, source: m.source, model: m.model, route: m.route, evidence_ids: m.evidence_ids || [], actor: m.actor, context: m.context, tool_trace: m.tool_trace || [], followups: m.followups || [], note: m.note }));
  if (items.length) {
    // the server has the turns, the browser the "context changed" lines: put each line back after the same turns
    const lines = [];
    let turns = 0;
    for (const m of c.messages) { if (m.role === 'system') lines.push({ m, at: turns }); else turns += 1; }
    c.messages = items;
    for (const x of lines.reverse()) c.messages.splice(Math.min(x.at, c.messages.length), 0, x.m);
    if (c.untitled) { const q = items.find((m) => m.role === 'user'); if (q && q.text) { c.title = q.text.slice(0, 44); c.untitled = false; } }
  }
}
function selectChat(id) {
  if (inputEl) cur().draft = inputEl.value;                 // each chat keeps the question typed in it
  if (chat.busy) stop();
  chat.current = id;
  saveChats();
  render();
  loadHistory(cur()).then(() => { saveChats(); if (chat.open) renderMsgs(); });
}
/** "+ New chat": a chat of its own, about the item selected on this page (else about the whole run). */
function newChat(ctx) {
  if (inputEl) cur().draft = inputEl.value;
  if (chat.busy) stop();
  const p = chat.pageCtx;
  const c = makeChat(ctx || (p && p.view === state.view && p.run === state.run ? p.ctx : null), null);
  chat.chats.push(c);
  chat.current = c.id;
  saveChats();
  render();
  if (inputEl) inputEl.focus();
}
async function clearHistory() {
  const c = cur();
  if (!(await confirmDialog(t('chat.confirmClear'), { okLabel: t('chat.clearHistory'), danger: true }))) return;
  if (chat.busy) stop();
  c.messages = [];
  if (!c.untitled) Object.assign(c, c.context ? { title: ctxTitle(c.context), untitled: false, ctxTitled: true } : { title: chatN(c), untitled: true, ctxTitled: false });
  saveChats(); render();
  if (state.run) { const r = await runApi('/chat/clear', { method: 'POST', body: { chat_id: c.id, actor: actorName(), role: actorRole() } }); if (!r.ok) toast(errText(r), 'fail'); else toast(t('chat.cleared'), 'ok'); }
}
async function deleteChat() {
  const c = cur();
  if (!(await confirmDialog(t('chat.confirmDelete'), { okLabel: t('chat.deleteChat'), danger: true }))) return;
  if (chat.busy) stop();
  chat.chats = chat.chats.filter((x) => x.id !== c.id);
  chat.current = chat.chats.length ? chat.chats[chat.chats.length - 1].id : null;
  saveChats(); render();
  if (state.run) { const r = await runApi('/chat/clear', { method: 'POST', body: { chat_id: c.id, actor: actorName(), role: actorRole(), delete: true } }); if (!r.ok) toast(errText(r), 'fail'); }
}

// ---------------------------------------------------------------- rendering
const CTX_TITLE_MAX = 110;
/** The context chip: the object the chat is about first (a diagnosis before the flag it explains, a row before
    its flag), where it belongs, then its statement (cut; the full text on hover). */
function ctxLabel(ctx) {
  if (!ctx) return { refs: [chip(t('chat.noContext'), 'solid')], title: null };
  const out = [];
  const refs = [['diagnosis', ctx.diagnosis_id], ['flag', ctx.flag_id], ['signal', ctx.signal_id]].filter((x) => x[1]);
  refs.sort((a, b) => Number(b[1] === ctx.object_id) - Number(a[1] === ctx.object_id));
  if (ctx.object_id && !refs.some((x) => x[1] === ctx.object_id)) out.push(chip(String(ctx.object_id), 'solid'));
  for (const [type, id] of refs) out.push(refLink(type, String(id)));
  if (ctx.kind) out.push(kindChip(ctx.kind));
  if (ctx.group_id !== undefined && ctx.group_id !== null) out.push(el('span', { class: 'small' }, `${t('common.group')} `, refLink('group', String(ctx.group_id))));
  if (ctx.batch_id) out.push(refLink('batch', ctx.batch_id));
  const full = ctx.title ? cleanText(ctx.title) : '';
  const title = full ? el('span', { class: 'small muted ctx-title', ...(full.length > CTX_TITLE_MAX ? { title: full } : {}) }, linkifyRefs(short(full, CTX_TITLE_MAX))) : null;
  return { refs: out, title };
}

function render() {
  const typed = inputEl ? inputEl.value : '';
  if (chat.loaded !== state.run) { chat.chats = []; chat.current = null; chat.loaded = state.run; const saved = store.get(storeKey()); if (saved && Array.isArray(saved.chats) && saved.chats.length) { chat.chats = saved.chats.map((c) => ({ ...c, messages: c.messages || [] })); chat.current = saved.current; } }
  const c = cur();
  const draft = chat.drawn === state.run + ':' + c.id ? typed : (c.draft || '');   // a question being typed survives a re-render
  chat.drawn = state.run + ':' + c.id;
  clear(drawer);
  drawer.append(el('div', { class: 'drawer-head' }, el('h2', { text: t('chat.title') }), el('button', { class: 'btn btn-quiet btn-sm', type: 'button', onClick: () => toggle(false), 'aria-label': t('common.close') }, '✕')));
  // chat list: pick, new, and a small menu with clear / delete
  const pick = el('select', { class: 'chat-pick', 'aria-label': t('chat.pick'), onChange: (e) => selectChat(e.target.value) }, chat.chats.map((x) => el('option', { value: x.id, text: x.title || t('chat.chatN', { n: 1 }) })));
  pick.value = c.id;
  const menu = el('details', { class: 'chat-menu' }, el('summary', { class: 'btn btn-quiet btn-sm', title: t('chat.menu'), 'aria-label': t('chat.menu') }, '⋯'),
    el('div', { class: 'menu' }, el('button', { class: 'btn btn-quiet btn-sm', type: 'button', onClick: () => { menu.open = false; clearHistory(); } }, t('chat.clearHistory')), el('button', { class: 'btn btn-quiet btn-sm', type: 'button', onClick: () => { menu.open = false; deleteChat(); } }, t('chat.deleteChat'))));
  drawer.append(el('div', { class: 'drawer-chatbar' }, pick, el('button', { class: 'btn btn-sm', type: 'button', title: t('chat.newChatHint'), onClick: () => newChat(null) }, '+ ' + t('chat.newChat')), menu));
  // context chip with an x to clear it
  const lab = ctxLabel(c.context);
  ctxEl = el('div', { class: 'drawer-ctx' }, el('span', { class: 'dim', text: t('chat.context') + ':' }), ...lab.refs,
    c.context ? el('button', { class: 'btn btn-quiet btn-sm ctx-x', type: 'button', title: t('chat.clearContext'), 'aria-label': t('chat.clearContext'), onClick: () => setChatContext(null, { cleared: true }) }, '✕') : null, lab.title);
  drawer.append(ctxEl);
  drawer.append(el('div', { class: 'drawer-quick' }, ['why', 'which', 'broken', 'todo', 'conf'].map((k) => el('button', { class: 'btn btn-sm', type: 'button', onClick: () => send(t('chat.quick.' + k)) }, t('chat.quick.' + k)))));
  msgsEl = el('div', { class: 'drawer-msgs' });
  drawer.append(msgsEl);
  renderMsgs();
  inputEl = el('textarea', { rows: 2, placeholder: t('chat.placeholder'), onKeydown: (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } } });
  inputEl.value = draft;
  sendBtn = el('button', { class: 'btn btn-primary', type: 'submit' }, t('common.send'));
  stopBtn = el('button', { class: 'btn btn-danger chat-stop', type: 'button', hidden: true, onClick: () => stop() }, '■ ' + t('chat.stop'));
  drawer.append(el('form', { class: 'drawer-form', onSubmit: (e) => { e.preventDefault(); send(); } }, inputEl, sendBtn, stopBtn));
  setBusy(chat.busy);
}

function setBusy(b) {
  const was = chat.busy;
  chat.busy = b;
  if (!sendBtn) return;
  sendBtn.hidden = b; stopBtn.hidden = !b;
  inputEl.disabled = b;
  if (!b && was) inputEl.focus();
}

function srcLabel(m) {
  const s = m.source || 'template';
  const cls = s.startsWith('llm-external') || m.route === 'external' ? 'src ext' : 'src';
  const label = s === 'template' ? t('chat.source.template') : s.startsWith('llm-external') || m.route === 'external' ? `${t('chat.source.external')} ${m.model || s.split(':')[1] || ''}` : `${t('chat.source.local')} ${m.model || s.split(':')[1] || ''}`;
  return el('span', { class: cls, text: label + (m.note ? ' — ' + m.note : '') });
}

/** Citations of an answer. An answer cites whatever supports it: evidence (EV-) but also diagnoses (DIAG-),
    flags (FLAG-), checks (CHK-), inferences (INF-)... Every chip is typed by its prefix and opens a popover
    with the content; "What the sources say" lists all of them in plain language, loaded when opened. */
const openSources = new Set();
function citations(ids, key) {
  ids = [...new Set((ids || []).filter(Boolean).map(String))];
  const box = el('div', { class: 'cites' }, evChips(ids, { max: 10 }));
  const det = el('details', { class: 'cite-sources', style: { marginTop: '4px' } }, el('summary', { class: 'small dim', style: { cursor: 'pointer' } }, t('evidence.sources', { n: ids.length })));
  let loaded = false;
  const load = () => { if (loaded) return; loaded = true; det.append(evidencePanel(ids, { heading: false })); };
  det.addEventListener('toggle', () => { if (det.open) { openSources.add(key); load(); } else openSources.delete(key); });
  if (openSources.has(key)) { det.open = true; load(); }  // keep it open across re-renders of the message list
  box.append(det);
  return box;
}

const openTech = new Set();
/** How the answer was put together: which objects of the run were looked up, in order (names and ids, no contents). */
function toolTrace(steps) {
  steps = (steps || []).filter((x) => x && (x.tool || x.error));
  if (!steps.length) return null;
  const args = (a) => Object.entries(a || {}).map(([k, v]) => `${k} ${typeof v === 'object' ? JSON.stringify(v) : v}`).join(', ');
  return el('div', { class: 'tooltrace small' }, el('div', { class: 'dim', text: bt('brief.toolTrace', { n: steps.length }) }),
    el('ol', { class: 'list small' }, steps.slice(0, 24).map((x) => el('li', {}, el('code', { text: x.tool || 'error' }), x.args ? el('span', { class: 'dim' }, ' ', linkifyRefs(args(x.args))) : null, x.thought ? el('span', { class: 'muted', text: ' — ' + String(x.thought).slice(0, 160) }) : null, x.ok === false || x.error ? el('span', { class: 'dim', text: ' ✕' }) : null))));
}

/** The series the answer looked at (downsampled, local only): a small chart under the answer. */
function seriesChart(s) {
  if (!s || !s.points || !s.points.length) return null;
  const ys = s.points.map((p) => (Array.isArray(p) ? p[1] : p && p.mean));
  return el('div', { class: 'msg-series' }, sparkline(ys, { w: 320, h: 44 }), el('div', { class: 'small dim' }, linkifyRefs(t('chat.series', { signal: s.signal || '', a: s.row_start, b: s.row_end }))));
}

function renderMsgs() {
  clear(msgsEl);
  const c = cur();
  if (!c.messages.length) msgsEl.append(el('div', { class: 'drawer-empty', text: t('chat.empty') }));
  for (const [i, m] of c.messages.entries()) {
    if (m.role === 'system') { msgsEl.append(el('div', { class: 'msg-sys small dim', text: sysText(m) })); continue; }
    if (m.stopped) { msgsEl.append(el('div', { class: 'msg-sys small dim', text: '■ ' + t('chat.stopped') })); continue; }
    const text = m.role === 'user' ? String(m.text || '') : cleanText(m.text).replace(/\*\*(.+?)\*\*/g, '$1');
    // a short answer stays as it is; a long one shows its first two sentences; the full answer, the tool trace and the
    // citations are under "Show technical analyses"
    const parts = m.role === 'user' ? [text] : answerBlock(text, { technical: [toolTrace(m.tool_trace), m.evidence_ids && m.evidence_ids.length ? citations(m.evidence_ids, `${state.run}:${c.id}:${i}`) : null] });
    const det = parts.find((x) => x && x.tagName === 'DETAILS');
    if (det) { const key = `${state.run}:${c.id}:${i}`; if (openTech.has(key)) det.open = true; det.addEventListener('toggle', (e) => { if (e.target !== det) return; if (det.open) openTech.add(key); else openTech.delete(key); }); }   // stays open across re-renders of the list
    const n = el('div', { class: 'msg ' + (m.role === 'user' ? 'user' : 'assistant') }, parts);
    if (m.role !== 'user') { const sc = seriesChart(m.series); if (sc) n.append(sc); n.append(srcLabel(m)); }
    msgsEl.append(n);
    if (m.role !== 'user' && m.followups && m.followups.length && m === c.messages[c.messages.length - 1]) {
      msgsEl.append(el('div', { class: 'drawer-quick followups', style: { padding: '0' } }, m.followups.slice(0, 4).map((q) => el('button', { class: 'btn btn-sm', type: 'button', onClick: () => send(q) }, q))));
    }
  }
  if (chat.busy) msgsEl.append(el('div', { class: 'msg assistant dim', text: t('chat.thinking') + '…' }));
  msgsEl.scrollTop = msgsEl.scrollHeight;
}

// ---------------------------------------------------------------- send / stop
async function postChat(body, signal) {
  // like core.api(), with an AbortSignal so Stop can cut the request
  try {
    const res = await fetch(`/api/runs/${encodeURIComponent(state.run)}/chat`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal });
    let data = {}; try { data = await res.json(); } catch { data = {}; }
    return { ok: res.ok, status: res.status, data: data || {}, unavailable: data && data.unavailable ? data.unavailable : null };
  } catch (e) {
    if (e && e.name === 'AbortError') return { ok: false, aborted: true, status: 0, data: {} };
    return { ok: false, status: 0, data: { error: String(e) } };
  }
}

export async function send(text) {
  text = (text !== undefined ? text : inputEl.value).trim();
  if (!text || chat.busy) return;
  const c = cur();
  if (!state.run) { c.messages.push({ role: 'assistant', text: t('runs.noRunHint'), source: 'template' }); renderMsgs(); return; }
  inputEl.value = '';
  c.messages.push({ role: 'user', text });
  if (c.untitled) { c.title = text.slice(0, 44); c.untitled = false; const opt = drawer.querySelector('.chat-pick option[value="' + c.id + '"]'); if (opt) opt.textContent = c.title; }
  saveChats();
  setBusy(true); renderMsgs();
  const history = c.messages.filter((m) => m.role !== 'system' && !m.stopped).slice(-12, -1).map((m) => ({ role: m.role, content: m.text }));
  const turnId = uid('CHAT-');
  const ctl = new AbortController();
  chat.abort = ctl; chat.turnId = turnId; chat.run = state.run;
  const r = await postChat({ message: text, context: c.context || {}, history, actor: actorName(), role: actorRole(), language: state.lang, chat_id: c.id, client_turn_id: turnId }, ctl.signal);
  if (chat.turnId !== turnId) return;                       // stopped (the stopped line is already in the thread) or another chat took over
  chat.abort = null; chat.turnId = null; chat.run = null;
  setBusy(false);
  if (r.ok && r.data.stopped) c.messages.push({ role: 'assistant', text: '', stopped: true, source: 'stopped' });
  else if (r.ok) { const a = r.data.answer; c.messages.push({ role: 'assistant', text: a.message, source: a.source, model: a.model, route: a.route, evidence_ids: a.evidence_ids || [], note: a.note, followups: a.followups || [], tool_trace: a.tool_trace || [], series: a.series || null }); }
  else c.messages.push({ role: 'assistant', text: errText(r), source: 'template' });
  saveChats();
  if (chat.open && cur() === c) renderMsgs();
}

/** Stop the answer in progress: abort the request, tell the server (so the agent ends between steps), free the input. */
export function stop(silent) {
  if (!chat.busy && !chat.abort) return;
  const turnId = chat.turnId;
  const run = chat.run || state.run;
  const c = cur();
  if (chat.abort) { try { chat.abort.abort(); } catch { /* ignore */ } }
  chat.abort = null; chat.turnId = null; chat.run = null;
  setBusy(false);
  if (!silent) { c.messages.push({ role: 'assistant', text: '', stopped: true, source: 'stopped' }); saveChats(); if (chat.open) renderMsgs(); }
  if (run && turnId) api(`/api/runs/${encodeURIComponent(run)}/chat/stop`, { method: 'POST', body: { turn_id: turnId, chat_id: c.id, actor: actorName(), role: actorRole() } });
}

/** Context helper for flags: everything the answer template needs. */
export function flagContext(f, extra = {}) { return { object_type: 'flag', object_id: f.id, flag_id: f.id, kind: f.kind, group_id: f.group_id, batch_id: f.batch_id, title: cleanText(f.statement), ...extra }; }
export function diagnosisContext(d) { return { object_type: 'diagnosis', object_id: d.id, diagnosis_id: d.id, flag_id: d.flag_ids && d.flag_ids[0], group_id: d.group_id, title: d.fault_type }; }
export function signalContext(s) { return { object_type: 'signal', object_id: s.id, signal_id: s.id, title: s.structural_role }; }
