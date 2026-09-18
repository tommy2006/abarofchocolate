/* Global chat drawer: usable from any view, pre-loaded with the context of a flag / diagnosis / signal.
   Answers come from tpm.llm.agent.chat when a model exists, otherwise from the server's template
   answers; the source is always labelled. */
import { state, t, el, clear, runApi, errText, bus, chip, evChips, actorName, actorRole, kindChip, store } from './core.js';

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
  chat.context = ctx && (ctx.object_id || ctx.flag_id || ctx.diagnosis_id || ctx.signal_id) ? ctx : null;
  toggle(true);
  if (ctx && ctx.seed) { inputEl.value = ctx.seed; }
  if (ctx && ctx.autoAsk) send(ctx.autoAsk);
}

async function loadHistory() {
  if (!state.run) return;
  const r = await runApi('/chat', { params: { limit: 40 } });
  if (r.ok) { chat.messages = (r.data.items || []).map((m) => ({ role: m.role, text: m.message, source: m.source, model: m.model, route: m.route, evidence_ids: m.evidence_ids || [], actor: m.actor, context: m.context })); if (chat.open) render(); }
}

function ctxLabel(ctx) {
  if (!ctx) return [chip(t('chat.noContext'), 'solid')];
  const out = [];
  if (ctx.flag_id) out.push(chip(ctx.flag_id, 'solid'));
  if (ctx.diagnosis_id) out.push(chip(ctx.diagnosis_id, 'solid'));
  if (ctx.signal_id) out.push(chip(ctx.signal_id, 'solid'));
  if (ctx.kind) out.push(kindChip(ctx.kind));
  if (ctx.group_id !== undefined && ctx.group_id !== null) out.push(chip(`${t('common.group')} ${ctx.group_id}`));
  if (ctx.title) out.push(el('span', { class: 'small muted', text: ctx.title }));
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

function renderMsgs() {
  clear(msgsEl);
  if (!chat.messages.length) msgsEl.append(el('div', { class: 'drawer-empty', text: t('chat.empty') }));
  for (const m of chat.messages) {
    const n = el('div', { class: 'msg ' + (m.role === 'user' ? 'user' : 'assistant') }, m.text);
    if (m.role !== 'user') { n.append(srcLabel(m)); if (m.evidence_ids && m.evidence_ids.length) n.append(evChips(m.evidence_ids)); }
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
  if (r.ok) { const a = r.data.answer; chat.messages.push({ role: 'assistant', text: a.message, source: a.source, model: a.model, route: a.route, evidence_ids: a.evidence_ids || [], note: a.note, followups: a.followups || [] }); }
  else chat.messages.push({ role: 'assistant', text: errText(r), source: 'template' });
  renderMsgs();
}

/** Context helper for flags: everything the answer template needs. */
export function flagContext(f, extra = {}) { return { object_type: 'flag', object_id: f.id, flag_id: f.id, kind: f.kind, group_id: f.group_id, batch_id: f.batch_id, title: f.statement, ...extra }; }
export function diagnosisContext(d) { return { object_type: 'diagnosis', object_id: d.id, diagnosis_id: d.id, flag_id: d.flag_ids && d.flag_ids[0], group_id: d.group_id, title: d.fault_type }; }
export function signalContext(s) { return { object_type: 'signal', object_id: s.id, signal_id: s.id, title: s.structural_role }; }
