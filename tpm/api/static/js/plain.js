/* "In plain words" box: a short, deterministic explanation of a view for a non-specialist, from
   GET /api/runs/{id}/plain?view=&lang=. English shows instantly; other languages (or the "Reword" button)
   ask the local model, which is slower, so the template is rendered first and replaced when the model answers. */
import { state, t as t0, el, runApi } from './core.js';

const FALLBACK = { 'plain.title': 'In plain words', 'plain.model': 'Reworded by the local model', 'plain.template': 'Written by the system from the evidence (no model)', 'plain.working': 'Asking the local model…', 'plain.reword': 'Reword with the local model' };
const t = (k) => { const v = t0(k); return (!v || v === k) ? (FALLBACK[k] || k) : v; };
const STYLE = { margin: '14px 0 18px', padding: '14px 18px', borderLeft: '4px solid var(--accent, #2bb5a0)', background: 'var(--bg-2, rgba(127,127,127,.08))', borderRadius: '0 8px 8px 0', maxWidth: '100%', overflowWrap: 'anywhere' };

function paragraphs(box, d) {
  const body = box.querySelector('.plain-body');
  body.replaceChildren(...(d.paragraphs || []).map((p) => el('p', { text: p })));
  const src = box.querySelector('.plain-src');
  src.textContent = (d.source && d.source !== 'template') ? `${t('plain.model')}: ${d.source}` : t('plain.template');
  if (d.note) src.textContent += ` — ${d.note}`;
}

export async function plainBox(view) {
  if (!state.run) return null;
  const lang = state.lang || 'en';
  const r = await runApi('/plain', { params: { view, lang } });
  if (!r.ok || !(r.data.paragraphs || []).length) return null;
  const box = el('section', { class: 'plain', style: STYLE },
    el('div', { class: 'plain-head', style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '12px', flexWrap: 'wrap' } }, el('h3', { text: t('plain.title'), style: { margin: '0' } }), el('button', { class: 'btn btn-sm', type: 'button', onClick: async (ev) => {
      const b = ev.currentTarget; b.disabled = true; b.textContent = t('plain.working');
      const m = await runApi('/plain', { params: { view, lang, enhance: 1 } });
      if (m.ok) paragraphs(box, m.data);
      b.disabled = false; b.textContent = t('plain.reword');
    } }, t('plain.reword'))),
    el('div', { class: 'plain-body' }),
    el('div', { class: 'plain-src small muted' }));
  paragraphs(box, r.data);
  if (lang !== 'en') {
    // translate in the background; the English template stays visible meanwhile
    runApi('/plain', { params: { view, lang, enhance: 1 } }).then((m) => { if (m.ok && m.data.language === lang) paragraphs(box, m.data); });
  }
  return box;
}
