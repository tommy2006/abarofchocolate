/* Settings (unnumbered, gear at the bottom of the rail): everything that is not a step of the analysis.
   Tabs: person & mode, display (theme, text size, language), AI models, data flow & privacy (the former
   view 7), decision log (the former view 6, engineer mode). The two former views are embedded unchanged. */
import { state, t as t0, el, clear, bus, roleAllows, navigate, viewHead, loadLang, store } from '../core.js';
import * as dataflow from './dataflow.js';
import * as log from './log.js';
import { openModelsPanel, mt as modelsText } from '../models.js';

const FB = {
  'nav.settings': 'Settings', 'settings.person': 'Person & mode', 'settings.display': 'Display', 'settings.models': 'AI models',
  'settings.dataflow': 'Data flow & privacy', 'settings.log': 'Decision log', 'settings.theme': 'Colours', 'settings.textSize': 'Text size',
  'settings.language': 'Language', 'settings.modeIntro': 'The mode sets how much the pages show and gives the app its colour.',
  'settings.change': 'Change person or mode', 'settings.modelsIntro': 'Which local model writes the explanations, download another one, install Ollama.',
  'settings.openModels': 'Open the models panel', 'settings.logLocked': 'The decision log is shown in Engineer mode.',
  'settings.textSizeHelp': 'The whole page grows or shrinks; boxes and charts rearrange themselves.',
  'zoom.smaller': 'Smaller text', 'zoom.larger': 'Larger text', 'zoom.reset': 'Normal size',
  'status.theme.auto': 'Auto', 'status.theme.light': 'Light', 'status.theme.dark': 'Dark',
};
const t = (k, v) => { const s = t0(k, v); return (!s || s === k) ? (FB[k] || k) : s; };
const TABS = ['person', 'display', 'models', 'dataflow', 'log'];

export const ZOOM_STEPS = [0.85, 0.95, 1, 1.1, 1.25, 1.4, 1.6];
export function applyZoom(z) {
  const zoom = Number(z) || 1;
  const changed = zoom !== state.zoom;
  state.zoom = zoom;
  store.set('zoom', zoom);
  document.body.style.zoom = String(zoom);                    // Chromium / Edge / Firefox 126+: layout re-flows
  document.documentElement.style.setProperty('--zoom', String(zoom));   // viewport heights and charts undo the zoom (styles.css)
  document.documentElement.setAttribute('data-zoom', zoom === 1 ? '1' : (zoom > 1 ? 'large' : 'small'));
  if (changed) bus.emit('zoom.changed', zoom);                // charts are drawn again with their text scaled (charts.js)
  setTimeout(() => window.dispatchEvent(new Event('resize')), 50);  // the top bar height and the chart widths follow
}
export function stepZoom(dir) {
  const i = ZOOM_STEPS.indexOf(state.zoom || 1);
  const j = Math.max(0, Math.min(ZOOM_STEPS.length - 1, (i < 0 ? 2 : i) + dir));
  applyZoom(ZOOM_STEPS[j]);
}

export async function render(main, params = {}) {
  const page = el('div', { class: 'view settings-view' });
  main.append(page);
  page.append(viewHead('', t('nav.settings')));
  let tab = TABS.includes(params.tab) ? params.tab : 'person';
  const logLocked = tab === 'log' && !roleAllows('engineer');   // an old link to the decision log, opened in another mode
  if (logLocked) tab = 'person';
  const strip = el('div', { class: 'settings-tabs', role: 'tablist' }, TABS.filter((k) => k !== 'log' || roleAllows('engineer')).map((k) => el('button', { class: 'settings-tab', type: 'button', role: 'tab', 'aria-selected': String(k === tab), onClick: () => navigate('settings', { ...params, tab: k }) }, t('settings.' + k))));
  const body = el('div', { class: 'settings-body' });
  page.append(strip, body);
  let embedded = null;

  if (tab === 'person') {
    if (logLocked) body.append(el('div', { class: 'notice warn', text: t('settings.logLocked') }));
    body.append(el('p', { class: 'hint', text: t('settings.modeIntro') }),
      el('p', {}, el('b', { text: state.user ? state.user.name : '' }), state.user ? ` · ${t0('role.' + state.user.role)}` : ''),
      el('button', { class: 'btn btn-primary', type: 'button', onClick: () => bus.emit('role.pick') }, t('settings.change')));
  } else if (tab === 'display') {
    const themeRow = el('div', { class: 'row', style: { gap: '8px', flexWrap: 'wrap' } }, ['auto', 'light', 'dark'].map((th) => el('button', { class: 'btn' + (state.theme === th ? ' btn-primary' : ''), type: 'button', onClick: () => bus.emit('theme.set', th) }, t('status.theme.' + th))));
    const zoomRow = el('div', { class: 'row', style: { gap: '8px', alignItems: 'center', flexWrap: 'wrap' } },
      el('button', { class: 'btn', type: 'button', onClick: () => { stepZoom(-1); paintZoom(); } }, 'A−'),
      el('span', { class: 'zoom-value', text: `${Math.round((state.zoom || 1) * 100)} %` }),
      el('button', { class: 'btn', type: 'button', onClick: () => { stepZoom(1); paintZoom(); } }, 'A+'),
      el('button', { class: 'btn btn-quiet', type: 'button', onClick: () => { applyZoom(1); paintZoom(); } }, t('zoom.reset')));
    const paintZoom = () => { zoomRow.querySelector('.zoom-value').textContent = `${Math.round((state.zoom || 1) * 100)} %`; };
    const langs = (state.settings ? state.settings.languages : ['en', 'fi', 'sv']) || ['en', 'fi', 'sv'];
    const langRow = el('div', { class: 'row', style: { gap: '8px' } }, langs.map((l) => el('button', { class: 'btn' + (state.lang === l ? ' btn-primary' : ''), type: 'button', onClick: async () => { await loadLang(l); bus.emit('lang.changed', l); bus.emit('route.same'); } }, l.toUpperCase())));
    body.append(el('h3', { text: t('settings.theme') }), themeRow, el('h3', { text: t('settings.textSize') }), zoomRow, el('p', { class: 'hint', text: t('settings.textSizeHelp') }), el('h3', { text: t('settings.language') }), langRow);
  } else if (tab === 'models') {
    body.append(el('p', { class: 'hint', text: t('settings.modelsIntro') }), el('button', { class: 'btn btn-primary', type: 'button', onClick: () => openModelsPanel(() => bus.emit('settings.changed')) }, t('settings.openModels')), el('p', { class: 'small muted', text: modelsText('lampHint') }));
  } else if (tab === 'dataflow' || tab === 'log') {
    const host = el('div', { class: 'settings-embed' });
    body.append(host);
    try { embedded = await (tab === 'dataflow' ? dataflow : log).render(host, params); } catch (e) { host.append(el('div', { class: 'notice fail', text: String(e && e.message || e) })); }
  }
  page.cleanup = () => { if (embedded && embedded.cleanup) { try { embedded.cleanup(); } catch { /* ignore */ } } };
  return page;
}
