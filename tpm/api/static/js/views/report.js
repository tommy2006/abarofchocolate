/* View 8: report — language select, open / download, email, inline preview.
   Switching language asks the API for the report's status first (`?status=1`): the template report is
   generated in a few seconds and cached per language; the model-written summary is added by a worker on
   the server, so the view shows what is happening (generating → ready, summary pending → added) and
   reloads the preview when the summary arrives. The preview is the report HTML in a same-origin iframe
   (`&embed=1`: no auto-reload); a cache-busting `v=` makes sure a regenerated report is what is shown. */
import { state, t, el, clear, runApi, fmt, section, viewHead, needRun, toast, errText, notice } from '../core.js';

const IFRAME_FIX = `
.page { max-width: 100%; padding-left: 16px; padding-right: 16px; }
`;
const POLL_MS = 3000;
const POLL_MAX_MS = 6 * 60 * 1000;

export async function render(main) {
  const view = el('div', { class: 'view' });
  main.append(view);
  view.append(viewHead('8', t('nav.report')));
  if (!state.run) { view.append(needRun()); return view; }
  const langs = state.settings ? state.settings.languages : ['en', 'fi', 'sv'];
  const sel = el('select', {}, langs.map((l) => el('option', { value: l, text: l.toUpperCase() })));
  sel.value = langs.includes(state.lang) ? state.lang : langs[0];
  const base = () => `/api/runs/${encodeURIComponent(state.run)}/report?lang=${sel.value}`;
  const openA = el('a', { class: 'btn btn-primary', href: base(), target: '_blank', rel: 'noopener' }, t('rep.open'));
  const dlA = el('a', { class: 'btn', href: base() + '&download=1' }, t('rep.download'));
  const regen = el('button', { class: 'btn btn-quiet', type: 'button', title: t('rep.regenerateHelp') }, t('rep.regenerate'));
  const to = el('input', { type: 'email', placeholder: 'name@example.com', style: { width: '240px', maxWidth: '100%' } });
  const status = el('div', { class: 'stack', style: { gap: '6px', marginBottom: '10px' }, role: 'status', 'aria-live': 'polite' });
  const preview = el('iframe', { title: t('rep.preview'), class: 'report-frame' });
  preview.hidden = true;
  preview.addEventListener('load', () => {
    try {
      const d = preview.contentDocument;
      if (!d || !d.head) return;
      if (!d.getElementById('tpm-iframe-fix')) { const s = d.createElement('style'); s.id = 'tpm-iframe-fix'; s.textContent = IFRAME_FIX; d.head.append(s); }
    } catch { /* cross-origin or not loaded */ }
  });

  let seq = 0; // every language change / regenerate gets a number; late answers of older ones are ignored
  let timer = null;
  const stopPolling = () => { if (timer) { clearTimeout(timer); timer = null; } };
  const busy = (text) => el('div', { class: 'notice' }, el('span', { class: 'spinner', text: '● ' }), text);
  const show = (...nodes) => { clear(status); status.append(...nodes.filter(Boolean)); };
  const load = () => { preview.hidden = false; preview.src = `${base()}&embed=1&v=${Date.now()}`; };
  const capNote = (d) => {
    const parts = [];
    for (const [key, label] of [['flags', t('rep.flags')], ['diagnoses', t('rep.diagnoses')]]) {
      const m = String(d[key] || '').match(/^(\d+)\/(\d+)$/);
      if (m && Number(m[2]) > Number(m[1])) parts.push(t('rep.capped', { shown: fmt.int(Number(m[1])), total: fmt.int(Number(m[2])), what: label }));
    }
    return parts.length ? el('div', { class: 'small muted', text: parts.join(' ') }) : null;
  };
  const describe = (d) => {
    const lang = String(d.lang || sel.value).toUpperCase();
    const facts = el('div', { class: 'small muted', text: [d.generated_at ? `${t('rep.generatedAt')} ${d.generated_at}` : null, d.bytes ? fmt.bytes(d.bytes) : null].filter(Boolean).join(' · ') });
    if (d.llm === 'pending') return [busy(t('rep.llmPending', { lang })), facts, capNote(d)];
    if (d.llm === 'ready') return [notice(t('rep.ready', { lang }) + ' ' + t('rep.llmReady'), 'ok'), facts, capNote(d)];
    return [notice(t('rep.ready', { lang }) + ' ' + t('rep.llmNone'), ''), facts, capNote(d)];
  };
  const fail = (r, my) => {
    const d = (r && r.data) || {};
    const msg = d.error || d.message || errText(r) || `HTTP ${r ? r.status : 0}`;
    show(notice(`${t('rep.failed', { lang: sel.value.toUpperCase() })} ${msg}`, 'fail'), el('div', {}, el('button', { class: 'btn btn-sm', type: 'button', onClick: () => { if (my === seq) refresh(); } }, t('rep.retry'))));
  };
  const poll = (my, started) => {
    stopPolling();
    timer = setTimeout(async () => {
      if (my !== seq || !view.isConnected) return;
      const r = await runApi('/report', { params: { lang: sel.value, status: 1 } });
      if (my !== seq || !view.isConnected) return;
      if (!r.ok || !r.data || r.data.ok === false) { show(...describe({ lang: sel.value, llm: 'none' })); return; }
      if (r.data.llm === 'pending' && Date.now() - started < POLL_MAX_MS) { if (r.data.regenerated) load(); poll(my, started); return; }
      show(...describe(r.data));
      load(); // the report on disk now contains the summary (or states that none is included)
    }, POLL_MS);
  };
  async function refresh({ force = false } = {}) {
    const my = ++seq;
    stopPolling();
    openA.href = base(); dlA.href = base() + '&download=1';
    show(busy(t('rep.generating', { lang: sel.value.toUpperCase() })));
    sel.disabled = true; regen.disabled = true;
    const r = await runApi('/report', { params: { lang: sel.value, status: 1, refresh: force ? 1 : undefined } });
    if (my !== seq) return;
    sel.disabled = false; regen.disabled = false;
    if (!r.ok || !r.data || r.data.ok === false) {
      if (r.data && r.data.exists) load(); else preview.hidden = true;
      fail(r, my);
      return;
    }
    show(...describe(r.data));
    load();
    if (r.data.llm === 'pending') poll(my, Date.now());
  }
  sel.addEventListener('change', () => refresh());
  regen.addEventListener('click', () => refresh({ force: true }));
  view.append(el('div', { class: 'row' }, el('label', { class: 'row' }, t('rep.language'), sel), openA, dlA, regen));
  const em = section(t('rep.email'));
  view.append(em.root);
  em.body.append(el('form', { class: 'row', onSubmit: async (e) => { e.preventDefault(); const r = await runApi('/report/email', { method: 'POST', body: { to: to.value.trim(), lang: sel.value } }); if (r.ok) toast(t('rep.sent', { to: to.value.trim() }), 'ok'); else toast(errText(r), 'fail'); } }, el('label', { class: 'row' }, t('rep.to'), to), el('button', { class: 'btn', type: 'submit' }, t('common.send'))));
  const pv = section(t('rep.preview'));
  view.append(pv.root);
  pv.body.append(status, preview);
  view.cleanup = () => { seq++; stopPolling(); };
  refresh(); // not awaited: the view is usable while the report is generated
  return view;
}
