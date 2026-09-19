/* View 8: report — language select, open / download (HTML, PDF, PowerPoint), email, inline preview.
   Switching language asks the API for the report's status first (`?status=1`): the template report is
   generated in a few seconds and cached per language; the model-written summary is added by a worker on
   the server, so the view shows what is happening (generating → ready, summary pending → added) and
   reloads the preview when the summary arrives. The preview is the report HTML in a same-origin iframe
   (`&embed=1`: no auto-reload); a cache-busting `v=` makes sure a regenerated report is what is shown.
   "Download PDF" / "Download PowerPoint" fetch `/report.pdf` / `/report.pptx` (generated on demand, cached per
   language on the server), show a generating state while the server works and the error text when it fails. */
import { state, t, el, clear, runApi, fmt, section, viewHead, needRun, toast, errText, notice, roleAllows } from '../core.js';
import { summaryCard, techDetails } from '../brief.js';

const IFRAME_FIX = `
.page { max-width: 100%; padding-left: 16px; padding-right: 16px; }
`;
const POLL_MS = 3000;
const POLL_MAX_MS = 6 * 60 * 1000;

export async function render(main) {
  // page = title, plain summary, then ONE expander with everything this view rendered before (buttons, e-mail, preview)
  const page = el('div', { class: 'view' });
  main.append(page);
  page.append(viewHead('6', t('nav.report')));
  if (!state.run) { page.append(needRun()); return page; }
  const tech = techDetails('report');
  page.append(summaryCard('report'), tech);
  const view = tech.body;
  const langs = state.settings ? state.settings.languages : ['en', 'fi', 'sv'];
  const sel = el('select', {}, langs.map((l) => el('option', { value: l, text: l.toUpperCase() })));
  sel.value = langs.includes(state.lang) ? state.lang : langs[0];
  const base = () => `/api/runs/${encodeURIComponent(state.run)}/report?lang=${sel.value}`;
  const openA = el('a', { class: 'btn btn-primary', href: base(), target: '_blank', rel: 'noopener' }, t('rep.open'));
  const dlA = el('a', { class: 'btn', href: base() + '&download=1' }, t('rep.download'));
  const regen = el('button', { class: 'btn btn-quiet', type: 'button', title: t('rep.regenerateHelp') }, t('rep.regenerate'));
  // PDF / PowerPoint: generated on demand by the server (cached per language); the button shows progress and errors
  const exportStatus = el('div', { class: 'small muted', role: 'status', 'aria-live': 'polite' });
  const exportBtn = (kind, label) => {
    const btn = el('button', { class: 'btn', type: 'button', title: t('rep.exportHelp') }, label);
    btn.addEventListener('click', async () => {
      const lang = sel.value;
      const what = `${kind === 'pdf' ? 'PDF' : 'PowerPoint'} (${lang.toUpperCase()})`;
      btn.disabled = true; btn.textContent = t('rep.exportGenerating');
      clear(exportStatus); exportStatus.append(el('span', { class: 'spinner', text: '● ' }), t('rep.exportWorking', { what }));
      const r = await runApi(`/report.${kind}`, { params: { lang }, raw: true });
      btn.disabled = false; btn.textContent = label;
      if (!view.isConnected) return;
      if (!r.ok || !r.res) {
        let msg = r.data && r.data.error ? r.data.error : `HTTP ${r.status}`;
        try { const d = await r.res.json(); msg = d.detail || d.message || msg; } catch { /* not JSON */ }
        clear(exportStatus); exportStatus.append(notice(`${t('rep.exportFailed', { what })} ${msg}`, 'fail'));
        toast(t('rep.exportFailed', { what }), 'fail');
        return;
      }
      const blob = await r.res.blob();
      const cd = r.res.headers.get('content-disposition') || '';
      const m = cd.match(/filename="?([^";]+)"?/);
      const name = m ? m[1] : `tpm_${state.run}_${lang}.${kind}`;
      const url = URL.createObjectURL(blob);
      const a = el('a', { href: url, download: name, style: { display: 'none' } });
      document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
      clear(exportStatus); exportStatus.append(t('rep.exportReady', { what, size: fmt_bytes(blob.size) }));
    });
    return btn;
  };
  const fmt_bytes = (n) => (fmt.bytes ? fmt.bytes(n) : `${Math.round(n / 1024)} kB`);
  const pdfBtn = exportBtn('pdf', t('rep.downloadPdf'));
  const pptxBtn = exportBtn('pptx', t('rep.downloadPptx'));
  const to = el('input', { type: 'email', required: true, placeholder: 'name@example.com', style: { width: '240px', maxWidth: '100%' } });
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
  // the report itself is what this page is for: open / download sit above the technical part in every mode (in Basic
  // mode they are also where "Open the report" of the summary leads)
  const basic = !roleAllows('operator');
  page.insertBefore(el('div', { class: 'report-actions', dataset: basic ? { briefSection: 'preview' } : undefined }, el('div', { class: 'row' }, el('label', { class: 'row' }, t('rep.language'), sel), openA, dlA, pdfBtn, pptxBtn, basic ? null : regen), exportStatus), tech);
  const em = section(t('rep.email'));
  em.root.dataset.briefSection = 'email';
  view.append(em.root);
  // the HTML report is always attached; PDF / PowerPoint are generated on the server when missing, so sending can take
  // a few seconds: the button is disabled meanwhile and the result (attached files or the mail server's answer) stays visible
  const attPdf = el('input', { type: 'checkbox', checked: true });
  const attPptx = el('input', { type: 'checkbox', checked: true });
  const sendBtn = el('button', { class: 'btn', type: 'submit' }, t('common.send'));
  const emailStatus = el('div', { class: 'small', role: 'status', 'aria-live': 'polite' });
  const sendEmail = async (e) => {
    e.preventDefault();
    const addr = to.value.trim();
    if (!addr) return;
    sendBtn.disabled = true; sendBtn.textContent = t('rep.sending');
    clear(emailStatus); emailStatus.append(el('span', { class: 'spinner', text: '● ' }), t('rep.sendingTo', { to: addr }));
    const r = await runApi('/report/email', { method: 'POST', body: { to: addr, lang: sel.value, attach_pdf: attPdf.checked, attach_pptx: attPptx.checked } });
    sendBtn.disabled = false; sendBtn.textContent = t('common.send');
    if (!view.isConnected) return;
    clear(emailStatus);
    if (r.ok) {
      const files = (r.data && r.data.result && r.data.result.attachments) || [];
      emailStatus.append(notice(t('rep.sentFiles', { to: addr, files: files.join(', ') }), 'ok'));
      toast(t('rep.sent', { to: addr }), 'ok');
    } else {
      emailStatus.append(notice(`${t('rep.sendFailed')} ${errText(r)}`, 'fail'));
      toast(t('rep.sendFailed'), 'fail');
    }
  };
  em.body.append(
    el('form', { class: 'row', onSubmit: sendEmail },
      el('label', { class: 'row' }, t('rep.to'), to),
      el('label', { class: 'row' }, attPdf, t('rep.attachPdf')),
      el('label', { class: 'row' }, attPptx, t('rep.attachPptx')),
      sendBtn),
    el('div', { class: 'small muted', text: t('rep.emailHelp') }),
    emailStatus);
  const pv = section(t('rep.preview'));
  pv.root.dataset.briefSection = 'preview';
  view.append(pv.root);
  pv.body.append(status, preview);
  view.cleanup = () => { seq++; stopPolling(); };
  refresh(); // not awaited: the view is usable while the report is generated
  return view;
}
