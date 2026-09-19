/* Plain summary first, technical analyses second.
   - summaryCard(view): the card on top of a page, from GET /api/runs/{id}/brief?view=&lang= (verdict colour and icon,
     one large headline, up to three short points, clickable next steps under "What to do now").
   - techDetails(view, ...children): the ONE <details class="tech-details"> of a page, labelled "Show technical
     analyses" / "Hide technical analyses". It remembers open/closed per view for the session, opens by itself when the
     address points at something inside it (any ?param: a flag, a diagnosis, a batch, rows, a section...), and resizes
     the Plotly charts inside it when it opens (a chart drawn inside a closed <details> has no width).
   - itemBrief(id): the same kind of summary for one object (diagnosis, alarm, check, batch, evidence), fetched when
     the card is rendered and cached; itemBriefLocal(data) builds the same block from fields a list already has.
   - techNested(...children): the nested expander of an item, same label.
   - answerBlock(text, extra): a long answer shows its first two sentences; the rest goes into the expander.
   - revealInTech(node) / focusSection(root, name): open every closed <details> above a node before scrolling to it. */
import { state, t as t0, el, clear, runApi, navigate, bus, linkifyRefs, closeAllModals, revealAncestors, roleAllows } from './core.js';

// English fallbacks: the i18n files are edited by several people at once, the page must never show a raw key
const FALLBACK = {
  'brief.showTech': "Show technical analyses",
  'brief.hideTech': "Hide technical analyses",
  'brief.techHint': "charts, tables, evidence and every number behind this summary",
  'brief.kicker': "In short",
  'brief.todo': "What to do now",
  'brief.loading': "Writing the summary",
  'brief.verdict.ok': "All good",
  'brief.verdict.attention': "Worth a look",
  'brief.verdict.problem': "Needs attention",
  'brief.verdict.pending': "Not ready yet",
  'brief.askHint': "Opens the chat with this question",
  'brief.goHint': "Opens: {view}",
  'brief.unavailable': "The short summary is not available right now; the technical analyses are shown below.",
  'brief.topFindings': "The most important findings",
  'brief.topFindingsHelp': "Accept, question or override each finding right here. The full reasoning is under “Show technical analyses”.",
  'brief.overviewTitle': "Run {run} in short",
  'brief.sus.headline': "This reading does not fit its surroundings. It is a glitch or a manipulation; the data alone can't tell.",
  'brief.sus.headlineRows': "These readings do not fit their surroundings. They are a glitch or a manipulation; the data alone can't tell.",
  'brief.sus.signal': "{name} {dir}: about {x} times more than it normally moves.",
  'brief.sus.signalNoX': "{name} {dir}.",
  'brief.sus.decide': "Accept, question or correct the alarm",
  'brief.batch.trusted': "Nothing to do: these rows passed all checks.",
  'brief.batch.caution': "These rows can be used. Only the stretches listed in the technical part are set aside.",
  'brief.batch.untrusted': "Do not rely on findings from these rows until the sensors named here have been checked.",
  'brief.batch.wide': "Unreliable for the whole batch: {list}.",
  'brief.batch.local': "Problems in some rows ({kinds}): {list}.",
  'brief.check.pass': "nothing wrong found",
  'brief.toolTrace': "How the answer was put together ({n} look-ups in this run's results)",
  'brief.fixTitle': "What to do about it",
  'brief.basicNote': "Basic mode shows only the essentials.",
  'brief.basicSwitch': "Show more (Operator mode)",
};
export const bt = (k, vars) => { let v = t0(k, vars); if (!v || v === k) { v = FALLBACK[k] || k; for (const [a, b] of Object.entries(vars || {})) v = v.replaceAll(`{${a}}`, String(b)); } return v; };

const ICON = { ok: '✓', attention: '!', problem: '✕', pending: '…' };
const VERDICTS = ['ok', 'attention', 'problem', 'pending'];

// ---------------------------------------------------------------- remembering open / closed (per view, per session)
const mem = {
  get(view) { try { return sessionStorage.getItem('tpm.tech.' + view); } catch { return null; } },
  set(view, open) { try { sessionStorage.setItem('tpm.tech.' + view, open ? '1' : '0'); } catch { /* private mode */ } },
};
/** Does the address point at something inside the technical part?  #/monitor?flag=FLAG-000001, ?rows=, ?section= ... */
export function hashHasTarget(hash = location.hash) {
  const qs = String(hash || '').split('?')[1] || '';
  return [...new URLSearchParams(qs).values()].some((v) => v !== '');
}

// ---------------------------------------------------------------- charts inside a <details>
export function resizeCharts(root) {
  if (!root || !window.Plotly) return;
  const go = () => root.querySelectorAll('.js-plotly-plot').forEach((n) => { if (n.offsetParent !== null) { try { window.Plotly.Plots.resize(n); } catch { /* not drawn yet */ } } });
  requestAnimationFrame(go);
  setTimeout(go, 250);   // late layouts (fonts, scrollbars) settle the width once more
}
function wireToggle(det, body, onPaint) {
  det.addEventListener('toggle', (e) => {
    if (e.target !== det) return;
    if (onPaint) onPaint();
    if (det.open) resizeCharts(body);
  });
}

// ---------------------------------------------------------------- the page-level expander
export function techDetails(view, ...children) {
  if (!roleAllows('operator')) return basicStub(view, children);  // basic mode: essentials only, no technical part
  const body = el('div', { class: 'tech-body' }, children);
  const label = el('span', { class: 'tech-label' });
  const summary = el('summary', { class: 'tech-summary' }, el('span', { class: 'tech-caret', 'aria-hidden': 'true' }), label, el('span', { class: 'tech-hint', text: bt('brief.techHint') }));
  const det = el('details', { class: 'tech-details', dataset: { view } }, summary, body);
  const paint = () => { label.textContent = bt(det.open ? 'brief.hideTech' : 'brief.showTech'); };
  det.open = hashHasTarget() || mem.get(view) === '1';
  paint();
  wireToggle(det, body, paint);
  // only a person's own click is remembered: a deep link that opened the expander says nothing about their preference
  summary.addEventListener('click', () => setTimeout(() => mem.set(view, det.open), 0));
  det.body = body;
  return det;
}
/** Basic mode: the page keeps only its summary and the essentials. The technical body still exists (hidden), so the
    view code that appends into `det.body` keeps working; the person is told how to see more. */
function basicStub(view, children) {
  const body = el('div', { class: 'tech-body' }, children);
  const det = el('details', { class: 'tech-details basic-hidden', dataset: { view }, hidden: true }, el('summary', {}), body);
  det.body = body;
  const note = el('div', { class: 'basic-note' }, el('span', { text: bt('brief.basicNote') }), ' ', el('button', { class: 'btn btn-sm btn-quiet', type: 'button', onClick: () => bus.emit('role.pick') }, bt('brief.basicSwitch')));
  const wrap = el('div', { class: 'basic-wrap' }, note, det);
  wrap.body = body;
  return wrap;
}
/** Nested expander of one item (a diagnosis card, a flag, a check, an answer): same label, not remembered. */
export function techNested(...children) {
  if (!roleAllows('operator')) { const body = el('div', { class: 'tech-body' }, children); const d = el('details', { class: 'tech-details nested basic-hidden', hidden: true }, el('summary', {}), body); d.body = body; return d; }
  const body = el('div', { class: 'tech-body' }, children);
  const label = el('span', { class: 'tech-label' });
  const det = el('details', { class: 'tech-details nested' }, el('summary', { class: 'tech-summary' }, el('span', { class: 'tech-caret', 'aria-hidden': 'true' }), label), body);
  const paint = () => { label.textContent = bt(det.open ? 'brief.hideTech' : 'brief.showTech'); };
  paint();
  wireToggle(det, body, paint);
  det.body = body;
  return det;
}
/** Open every closed <details> above `node` (citation links, "open flag", deep links and the back button land inside). */
export function revealInTech(node) { return revealAncestors(node); }
/** ?section=suspicious -> open the expander, scroll to the block tagged data-brief-section="suspicious", highlight it. */
export function focusSection(root, name) {
  if (!root || !name) return false;
  const node = root.querySelector(`[data-brief-section="${String(name).replace(/[^a-z0-9_-]/gi, '')}"]`);
  if (!node) return false;
  revealInTech(node);
  node.classList.add('flash');
  // content above the block may still grow (charts, the plain-words box): scroll now and once more when it has settled
  const go = () => { if (!node.isConnected) return; const top = node.getBoundingClientRect().top; if (top < 0 || top > window.innerHeight * 0.4) { try { node.scrollIntoView({ block: 'start', behavior: 'auto' }); } catch { /* ignore */ } } };
  setTimeout(go, 60);
  setTimeout(go, 900);
  setTimeout(() => node.classList.remove('flash'), 2600);
  return true;
}

// ---------------------------------------------------------------- actions
/** {view, ref} of an action -> the view and hash parameters to navigate to. */
export function actionTarget(a) {
  const ref = String((a && a.ref) || '');
  const view = a && a.view;
  if (!ref) return { view, params: {} };
  if (ref.startsWith('section:')) return { view, params: { section: ref.slice(8) } };
  if (ref.startsWith('rows:')) { const [, rows, signals] = ref.split(':'); return { view: view || 'monitor', params: { rows, signals } }; }
  const key = /^DIAG-/.test(ref) ? 'diag' : /^FLAG-/.test(ref) ? 'flag' : /^CHK-/.test(ref) ? 'check' : /^RULE-/.test(ref) ? 'rule' : /^PATTERN-/.test(ref) ? 'pattern' : /^EGR-/.test(ref) ? 'egress' : /^B\d{4,6}$/.test(ref) ? 'batch' : /^S\d{2,3}$/.test(ref) ? 'signal' : 'id';
  return { view, params: { [key]: ref } };
}
// Basic mode keeps the technical part of every page hidden. A next step that only leads into it (a table, a list, a
// chart of the technical part) is left out; these sections stay visible in Basic mode and may be pointed at:
const BASIC_SECTIONS = { report: ['preview'], assessor: ['recommendations'], dataflow: ['profile'], settings: ['profile'] };
/** Does this action lead into a part of a page that Basic mode does not show? */
export function hiddenInBasic(a) {
  if (roleAllows('operator') || !a) return false;
  const ref = String(a.ref || '');
  return ref.startsWith('section:') && !(BASIC_SECTIONS[a.view] || []).includes(ref.slice(8));
}
/** Basic mode: a finding, an alarm, a check or a batch opens as problem -> reason -> answer in a popup (with accept /
    question / override where the object takes a decision) instead of a page whose details are hidden. */
export async function openBasicItem(id, ctx) {
  try { const { basicItemModal } = await import('./charts.js'); basicItemModal(id, { ctx }); } catch (e) { console.error(e); }
}
/** Carry out one next step: open the chat with its question, or go to the view / object it names. */
export function runAction(a, ctx) {
  if (a.ask && !a.view) { bus.emit('chat.open', { ...(ctx || {}), seed: a.ask }); return; }
  const { view, params } = actionTarget(a);
  if (!view) return;
  closeAllModals();
  if (!roleAllows('operator')) {
    const id = params.diag || params.flag || params.check || params.batch;
    if (id) { openBasicItem(id, ctx); return; }
  }
  if (view === state.view && !Object.keys(params).length) {
    const det = document.querySelector('#main details.tech-details:not(.nested)');
    if (det) { det.open = true; setTimeout(() => { try { det.scrollIntoView({ block: 'start', behavior: 'smooth' }); } catch { /* ignore */ } }, 40); }
    return;
  }
  navigate(view, params);
}
function actionButton(a, i, ctx) {
  const ask = !!(a.ask && !a.view);
  const viewName = a.view ? (t0('nav.' + a.view) === 'nav.' + a.view ? a.view : t0('nav.' + a.view)) : '';
  return el('button', { class: 'btn brief-action' + (i === 0 ? ' btn-primary' : '') + (ask ? ' ask' : ''), type: 'button', title: ask ? bt('brief.askHint') : bt('brief.goHint', { view: viewName }), onClick: () => runAction(a, ctx) },
    el('span', { class: 'brief-action-glyph', 'aria-hidden': 'true', text: ask ? '?' : '→' }), el('span', { text: a.text }));
}

// ---------------------------------------------------------------- rendering of a brief
function verdictOf(d) { return VERDICTS.includes(d && d.verdict) ? d.verdict : 'attention'; }
function paintBrief(root, d, { compact = false, ctx, extra } = {}) {
  clear(root);
  const v = verdictOf(d);
  root.dataset.verdict = v;
  root.append(el('div', { class: 'brief-head' },
    el('span', { class: 'brief-icon', role: 'img', 'aria-label': bt('brief.verdict.' + v), title: bt('brief.verdict.' + v), text: ICON[v] }),
    el('div', { class: 'brief-headtext' }, compact ? null : el('div', { class: 'brief-kicker' }, bt('brief.kicker'), ' · ', el('span', { class: 'brief-verdict', text: bt('brief.verdict.' + v) })), el('p', { class: 'brief-headline', text: d.headline || '' }))));
  if ((d.points || []).length) root.append(el('ul', { class: 'brief-points' }, d.points.map((p) => el('li', { text: p }))));
  // an object's summary also says what to do about it (the suggestion library of the server: tpm/api/advice.py)
  if (compact && (d.fix || []).length) root.append(el('div', { class: 'brief-fix' }, el('h3', { class: 'brief-todo', text: bt('brief.fixTitle') }), el('ol', { class: 'brief-fix-list' }, d.fix.map((s) => el('li', { text: s })))));
  const acts = (d.actions || []).filter((a) => a && a.text && !hiddenInBasic(a));
  if (acts.length || extra) root.append(el('div', { class: 'brief-actions' }, el('h3', { class: 'brief-todo', text: bt('brief.todo') }), el('div', { class: 'brief-btns' }, acts.map((a, i) => actionButton(a, i, ctx)), extra || null)));
  return root;
}

/** The summary card of a page (or of the whole run: view 'overview'). Fills itself; when the summary cannot be
    loaded it says so and opens the technical part, so the page is never emptier than before. */
export function summaryCard(view, { run } = {}) {
  const runId = run || state.run;
  if (!runId) return null;
  const card = el('section', { class: 'brief-card', dataset: { verdict: 'pending', view }, 'aria-live': 'polite' }, el('div', { class: 'brief-loading dim', text: bt('brief.loading') + '…' }));
  const load = async () => {
    const r = await runApi('/brief', { params: { view, lang: state.lang || 'en' } });
    if (!r.ok || !r.data || !r.data.headline) {
      clear(card); card.dataset.verdict = 'pending';
      card.append(el('p', { class: 'small muted', style: { margin: '0' }, text: bt('brief.unavailable') }));
      const det = card.parentElement && card.parentElement.querySelector(':scope > details.tech-details');
      if (det) det.open = true;
      return;
    }
    paintBrief(card, r.data);
  };
  load();
  card.reload = load;
  // a decision changes the counts in the summary ("2 of 5 reviewed"): refresh while the card is on screen
  const off = bus.on('decision', () => { if (card.isConnected) load(); else off(); });
  return card;
}

const itemKey = (id) => `${state.run}:brief-item:${state.lang}:${id}`;
bus.on('decision', () => { for (const k of [...state.cache.keys()]) if (k.includes(':brief-item:')) state.cache.delete(k); });
/** Summary of one object, fetched once per run and language. `ctx` is the chat context for its question button,
    `extra` a node shown next to the action buttons (the accept / question / override bar of a diagnosis). */
export function itemBrief(id, { ctx, extra, onLoad, decisionsShown = false } = {}) {
  const box = el('div', { class: 'brief-item', dataset: { verdict: 'pending', briefId: id } }, el('div', { class: 'brief-loading dim small', text: bt('brief.loading') + '…' }));
  (async () => {
    let d = state.cache.get(itemKey(id));
    if (!d) {
      const r = await runApi('/brief/item', { params: { id, lang: state.lang || 'en' } });
      if (r.ok && r.data && r.data.headline) { d = r.data; state.cache.set(itemKey(id), d); }
    }
    if (!d) { box.remove(); if (onLoad) onLoad(null); return; }   // older server or unknown id: the technical part speaks for itself
    // where the accept / question / override bar sits right under the summary, a button that only leads to it is noise
    const shown = decisionsShown ? { ...d, actions: (d.actions || []).filter((a) => !(a.ref === id && !a.ask) || (d.actions || []).indexOf(a) === 0) } : d;
    paintBrief(box, shown, { compact: true, ctx, extra });
    if (onLoad) onLoad(d);
  })();
  return box;
}
/** The same block from fields the page already has: { verdict, headline, points: [str], actions: [{text, view, ref, ask}|{text, onClick}] }. */
export function itemBriefLocal(d, { ctx, extra } = {}) {
  const box = el('div', { class: 'brief-item', dataset: { verdict: verdictOf(d) } });
  const custom = (d.actions || []).filter((a) => a && typeof a.onClick === 'function');
  paintBrief(box, { ...d, actions: (d.actions || []).filter((a) => a && typeof a.onClick !== 'function') }, { compact: true, ctx, extra: custom.length || extra ? [custom.map((a, i) => el('button', { class: 'btn brief-action' + (i === 0 && !(d.actions || []).some((x) => x && x.view) ? ' btn-primary' : ''), type: 'button', onClick: a.onClick }, a.text)), extra || null] : null });
  return box;
}

// ---------------------------------------------------------------- long answers
const wordCount = (s) => String(s || '').trim().split(/\s+/).filter(Boolean).length;
/** First two sentences of a text (list markers and headings never end up half-shown). */
export function firstSentences(text, n = 2) {
  const flat = String(text || '').replace(/\s*\n+\s*/g, ' ').trim();
  const parts = flat.match(/[^.!?]+[.!?]+(?=\s|$)|[^.!?]+$/g) || [flat];
  return parts.slice(0, n).map((x) => x.trim()).join(' ').trim();
}
/** Answer of the chat / the assessor: short answers stay as they are; a long one (more than ~60 words) shows its
    first two sentences and keeps the full text in the expander. `technical` nodes (tool trace, citations) always go
    into the expander. Returns a fragment of nodes. */
export function answerBlock(text, { technical = [], limit = 60 } = {}) {
  const full = String(text || '').trim();
  const long = wordCount(full) > limit;
  const tech = (technical || []).filter(Boolean);
  const out = [el('div', { class: 'answer-lead' }, linkifyRefs(long ? firstSentences(full) : (full || '–')))];
  if (long || tech.length) out.push(techNested(long ? el('div', { class: 'answer-full' }, linkifyRefs(full)) : null, tech));
  return out;
}
