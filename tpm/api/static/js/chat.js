/* Global chat drawer: usable from any view, pre-loaded with the context of a flag / diagnosis / signal.
   Answers come from tpm.llm.agent.chat when a model exists, otherwise from the server's template
   answers; the source is always labelled. Every reference in an answer (FLAG-, EV-, S07, B00003...)
   is a link. */
import { state, t, el, clear, runApi, errText, bus, chip, evChips, evidencePanel, actorName, actorRole, kindChip, store, linkifyRefs, cleanText, refLink } from './core.js';
import { answerBlock, bt } from './brief.js';

const chat = { open: false, context: null, messages: [], busy: false };
let drawer, msgsEl, ctxEl, inputEl;

export function initChat() {
  drawer = document.getElementById('drawer');
  const btn = document.getElementById('chat-btn');
  btn.addEventListener('click', () => toggle());
  bus.on('chat.open', (ctx) => openChat(ctx));
  bus.on('run.changed', () => { chat.messages = []; chat.context = null; if (chat.open) render(); loadHistory(); });
  bus.on('lang.changed', () => { btn.textContent = t('status.chat'); if (chat.open) render(); });
  btn.textContent = t('status.chat');
}

export function toggle(force) {
  chat.open = force === undefined ? !chat.open : force;
  drawer.hidden = !chat.open;
  document.querySelector('.frame').classList.toggle('with-drawer', chat.open);
  document.getElementById('chat-btn').setAttribute('aria-pressed', String(chat.open));
  if (chat.open) { render(); if (inputEl) inputEl.focus(); }
}

export async function openChat(ctx) {
  setChatContext(ctx);
  toggle(true);
  if (ctx && ctx.seed) { inputEl.value = ctx.seed; }
  if (ctx && ctx.autoAsk) send(ctx.autoAsk);
}
/** Set the context (flag / diagnosis / signal) without opening the drawer; the drawer shows it when opened. */
export function setChatContext(ctx) {
  chat.context = ctx && (ctx.object_id || ctx.flag_id || ctx.diagnosis_id || ctx.signal_id) ? ctx : null;
  if (chat.open) render();
}
export function isChatOpen() { return chat.open; }

async function loadHistory() {
  if (!state.run) return;
  const r = await runApi('/chat', { params: { limit: 40 } });
  if (r.ok) { chat.messages = (r.data.items || []).map((m) => ({ role: m.role, text: m.message, source: m.source, model: m.model, route: m.route, evidence_ids: m.evidence_ids || [], actor: m.actor, context: m.context })); if (chat.open) render(); }
}

function ctxLabel(ctx) {
  if (!ctx) return [chip(t('chat.noContext'), 'solid')];
  const out = [];
  if (ctx.flag_id) out.push(refLink('flag', ctx.flag_id));
  if (ctx.diagnosis_id) out.push(refLink('diagnosis', ctx.diagnosis_id));
  if (ctx.signal_id) out.push(refLink('signal', ctx.signal_id));
  if (!ctx.flag_id && !ctx.diagnosis_id && !ctx.signal_id && ctx.object_id) out.push(chip(ctx.object_id, 'solid'));
  if (ctx.kind) out.push(kindChip(ctx.kind));
  if (ctx.group_id !== undefined && ctx.group_id !== null) out.push(el('span', { class: 'small' }, `${t('common.group')} `, refLink('group', String(ctx.group_id))));
  if (ctx.batch_id) out.push(refLink('batch', ctx.batch_id));
  if (ctx.title) out.push(el('span', { class: 'small muted ctx-title' }, linkifyRefs(cleanText(ctx.title))));
  return out;
}

function render() {
  clear(drawer);
  drawer.append(el('div', { class: 'drawer-head' }, el('h2', { text: t('chat.title') }), el('button', { class: 'btn btn-quiet btn-sm', type: 'button', onClick: () => toggle(false), 'aria-label': t('common.close') }, '✕')));
  ctxEl = el('div', { class: 'drawer-ctx' }, el('span', { class: 'dim', text: t('chat.context') + ':' }), ...ctxLabel(chat.context), chat.context ? el('button', { class: 'btn btn-quiet btn-sm', type: 'button', onClick: () => { chat.context = null; render(); } }, t('chat.clearContext')) : null);
  drawer.append(ctxEl);
  drawer.append(el('div', { class: 'drawer-quick' }, ['why', 'which', 'broken', 'todo', 'conf'].map((k) => el('button', { class: 'btn btn-sm', type: 'button', onClick: () => send(t('chat.quick.' + k)) }, t('chat.quick.' + k)))));
  msgsEl = el('div', { class: 'drawer-msgs' });
  drawer.append(msgsEl);
  renderMsgs();
  inputEl = el('textarea', { rows: 2, placeholder: t('chat.placeholder'), onKeydown: (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } } });
  drawer.append(el('form', { class: 'drawer-form', onSubmit: (e) => { e.preventDefault(); send(); } }, inputEl, el('button', { class: 'btn btn-primary', type: 'submit' }, t('common.send'))));
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

function renderMsgs() {
  clear(msgsEl);
  if (!chat.messages.length) msgsEl.append(el('div', { class: 'drawer-empty', text: t('chat.empty') }));
  for (const [i, m] of chat.messages.entries()) {
    const text = m.role === 'user' ? String(m.text || '') : cleanText(m.text);
    // a short answer stays as it is; a long one shows its first two sentences; the full answer, the tool trace and the
    // citations are under "Show technical analyses"
    const parts = m.role === 'user' ? [text] : answerBlock(text, { technical: [toolTrace(m.tool_trace), m.evidence_ids && m.evidence_ids.length ? citations(m.evidence_ids, `${state.run}:${i}`) : null] });
    const det = parts.find((x) => x && x.tagName === 'DETAILS');
    if (det) { const key = `${state.run}:${i}`; if (openTech.has(key)) det.open = true; det.addEventListener('toggle', (e) => { if (e.target !== det) return; if (det.open) openTech.add(key); else openTech.delete(key); }); }   // stays open across re-renders of the list
    const n = el('div', { class: 'msg ' + (m.role === 'user' ? 'user' : 'assistant') }, parts);
    if (m.role !== 'user') n.append(srcLabel(m));
    msgsEl.append(n);
    if (m.role !== 'user' && m.followups && m.followups.length && m === chat.messages[chat.messages.length - 1]) {
      msgsEl.append(el('div', { class: 'drawer-quick', style: { padding: '0' } }, m.followups.slice(0, 4).map((q) => el('button', { class: 'btn btn-sm btn-quiet', type: 'button', onClick: () => send(q) }, q))));
    }
  }
  if (chat.busy) msgsEl.append(el('div', { class: 'msg assistant dim', text: t('chat.thinking') + '…' }));
  msgsEl.scrollTop = msgsEl.scrollHeight;
}

export async function send(text) {
  text = (text !== undefined ? text : inputEl.value).trim();
  if (!text || chat.busy) return;
  if (!state.run) { chat.messages.push({ role: 'assistant', text: t('runs.noRunHint'), source: 'template' }); renderMsgs(); return; }
  inputEl.value = '';
  chat.messages.push({ role: 'user', text });
  chat.busy = true; renderMsgs();
  const history = chat.messages.slice(-12, -1).map((m) => ({ role: m.role, content: m.text }));
  const r = await runApi('/chat', { method: 'POST', body: { message: text, context: chat.context || {}, history, actor: actorName(), role: actorRole(), language: state.lang } });
  chat.busy = false;
  if (r.ok) { const a = r.data.answer; chat.messages.push({ role: 'assistant', text: a.message, source: a.source, model: a.model, route: a.route, evidence_ids: a.evidence_ids || [], note: a.note, followups: a.followups || [], tool_trace: a.tool_trace || [] }); }
  else chat.messages.push({ role: 'assistant', text: errText(r), source: 'template' });
  renderMsgs();
}

/** Context helper for flags: everything the answer template needs. */
export function flagContext(f, extra = {}) { return { object_type: 'flag', object_id: f.id, flag_id: f.id, kind: f.kind, group_id: f.group_id, batch_id: f.batch_id, title: cleanText(f.statement), ...extra }; }
export function diagnosisContext(d) { return { object_type: 'diagnosis', object_id: d.id, diagnosis_id: d.id, flag_id: d.flag_ids && d.flag_ids[0], group_id: d.group_id, title: d.fault_type }; }
export function signalContext(s) { return { object_type: 'signal', object_id: s.id, signal_id: s.id, title: s.structural_role }; }
