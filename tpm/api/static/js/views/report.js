/* View 8: report — language select, open / download, email, inline preview. */
import { state, t, el, clear, runApi, section, viewHead, needRun, toast, errText, notice, unavailableNote } from '../core.js';

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
  const to = el('input', { type: 'email', placeholder: 'name@example.com', style: { width: '240px' } });
  const status = el('div', { style: { marginTop: '10px' } });
  const preview = el('iframe', { title: t('rep.preview'), style: { width: '100%', height: '70vh', border: '1px solid var(--line)', borderRadius: '4px', background: '#fff' } });
  const refresh = async () => {
    openA.href = base(); dlA.href = base() + '&download=1';
    clear(status);
    const r = await runApi(`/report`, { params: { lang: sel.value }, raw: true });
    if (r.ok) { preview.hidden = false; preview.src = base(); }
    else { preview.hidden = true; let msg = `HTTP ${r.status}`; try { const d = await r.res.json(); msg = d.message || d.detail || d.unavailable || msg; } catch { /* ignore */ } status.append(notice(msg, 'warn')); }
  };
  sel.addEventListener('change', refresh);
  view.append(el('div', { class: 'row' }, el('label', { class: 'row' }, t('rep.language'), sel), openA, dlA));
  const em = section(t('rep.email'));
  view.append(em.root);
  em.body.append(el('form', { class: 'row', onSubmit: async (e) => { e.preventDefault(); const r = await runApi('/report/email', { method: 'POST', body: { to: to.value.trim(), lang: sel.value } }); if (r.ok) toast(t('rep.sent', { to: to.value.trim() }), 'ok'); else toast(errText(r), 'fail'); } }, el('label', { class: 'row' }, t('rep.to'), to), el('button', { class: 'btn', type: 'submit' }, t('common.send'))));
  const pv = section(t('rep.preview'));
  view.append(pv.root);
  pv.body.append(status, preview);
  await refresh();
  return view;
}
