/* Renaming signals: a person can call S44 "possibly broken" and that name then shows everywhere (every page,
   chat, report) as "possibly broken (S44)"; the alias stays visible so texts still match the evidence.
   The rename is a logged human decision (`set_name` on the signal), like accept / question / override.
   Texts for en / fi / sv live here so the feature is self-contained. */
import { state, el, api, runApi, modal, toast, bus, postDecision } from './core.js';

const STR = {
  en: { title: 'Rename a signal', bar: 'Rename a signal', pick: 'Which signal', name: 'Your name for it', namePh: 'for example: possibly broken', unit: 'Unit (optional)', save: 'Save name', remove: 'Remove my name', cancel: 'Cancel',
    help: 'The name appears on every page, in chat answers and in the report, next to the original id. Your change is recorded in the decision log and can be undone.',
    header: 'Name in the file', noHeader: 'the file had no column names', saved: '{id} is now shown as "{name}" everywhere.', removed: 'Your name for {id} was removed.', failed: 'The name could not be saved: {msg}', renameBtn: 'Rename' },
  fi: { title: 'Nimeä signaali uudelleen', bar: 'Nimeä signaali uudelleen', pick: 'Mikä signaali', name: 'Oma nimesi sille', namePh: 'esimerkiksi: mahdollisesti rikki', unit: 'Yksikkö (valinnainen)', save: 'Tallenna nimi', remove: 'Poista antamani nimi', cancel: 'Peruuta',
    help: 'Nimi näkyy jokaisella sivulla, chat-vastauksissa ja raportissa alkuperäisen tunnisteen vieressä. Muutos kirjataan päätöslokiin ja sen voi perua.',
    header: 'Nimi tiedostossa', noHeader: 'tiedostossa ei ollut sarakkeiden nimiä', saved: '{id} näkyy nyt kaikkialla nimellä "{name}".', removed: 'Antamasi nimi signaalille {id} poistettiin.', failed: 'Nimeä ei voitu tallentaa: {msg}', renameBtn: 'Nimeä' },
  sv: { title: 'Byt namn på en signal', bar: 'Byt namn på en signal', pick: 'Vilken signal', name: 'Ditt namn på den', namePh: 'till exempel: möjligen trasig', unit: 'Enhet (valfritt)', save: 'Spara namn', remove: 'Ta bort mitt namn', cancel: 'Avbryt',
    help: 'Namnet visas på varje sida, i chattsvar och i rapporten, bredvid det ursprungliga id:t. Ändringen sparas i beslutsloggen och kan ångras.',
    header: 'Namn i filen', noHeader: 'filen hade inga kolumnnamn', saved: '{id} visas nu som "{name}" överallt.', removed: 'Ditt namn för {id} togs bort.', failed: 'Namnet kunde inte sparas: {msg}', renameBtn: 'Byt namn' },
};
function rt(key, vars) {
  let s = (STR[state.lang] || STR.en)[key] || STR.en[key] || key;
  if (vars) for (const [k, v] of Object.entries(vars)) s = s.replace('{' + k + '}', String(v));
  return s;
}

/** Fills state.signalNames / state.signalHeaders / state.signalUnits for the selected run (used by core.refLink). */
export async function loadSignalNames() {
  state.signalNames = {}; state.signalHeaders = {}; state.signalUnits = {};
  if (!state.run) return;
  const r = await runApi('/signal-names');
  if (!r.ok) return;
  state.signalNames = r.data.names || {};
  state.signalHeaders = r.data.headers || {};
  state.signalUnits = r.data.units || {};
}

export function signalDisplay(id) {
  const n = state.signalNames && state.signalNames[id];
  return n ? `${n} (${id})` : id;
}

async function save(id, name, unit) {
  const header = (state.signalHeaders || {})[id];
  const r = await postDecision('signal', id, 'set_name', { note: `named by operator${header ? ' (file header: ' + header + ')' : ''}`, newValue: { display_name: name, display_unit: unit } });
  if (!r.ok) { toast(rt('failed', { msg: (r.data && (r.data.detail || r.data.error)) || r.status }), 'fail'); return false; }
  await loadSignalNames();
  toast(name ? rt('saved', { id, name }) : rt('removed', { id }), 'ok');
  bus.emit('signal.renamed', { id, name });
  bus.emit('route.same');  // redraw the current page with the new name
  return true;
}

/** Dialog for one signal (from a Rename button). */
export function renameSignalDialog(id) {
  const header = (state.signalHeaders || {})[id];
  const nameIn = el('input', { type: 'text', maxlength: '80', value: (state.signalNames || {})[id] || '', placeholder: rt('namePh'), style: { width: '100%' }, 'aria-label': rt('name') });
  const unitIn = el('input', { type: 'text', maxlength: '24', value: (state.signalUnits || {})[id] || '', style: { width: '100%' }, 'aria-label': rt('unit') });
  const body = el('div', {},
    el('p', { style: { marginTop: '0' } }, el('b', { text: id }), el('span', { class: 'muted', text: ` · ${rt('header')}: ${header || rt('noHeader')}` })),
    el('label', { class: 'small muted', text: rt('name') }), nameIn,
    el('label', { class: 'small muted', style: { display: 'block', marginTop: '10px' }, text: rt('unit') }), unitIn,
    el('p', { class: 'small muted', text: rt('help') }));
  const m = modal({ title: rt('title'), body, actions: [
    { label: rt('cancel'), onClick: (c) => c() },
    (state.signalNames || {})[id] ? { label: rt('remove'), onClick: async (c) => { if (await save(id, '', '')) c(); } } : null,
    { label: rt('save'), cls: 'btn-primary', onClick: async (c) => { if (await save(id, nameIn.value.trim(), unitIn.value.trim())) c(); } },
  ].filter(Boolean) });
  nameIn.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); save(id, nameIn.value.trim(), unitIn.value.trim()).then((ok) => { if (ok) m.close(); }); } });
  nameIn.focus(); nameIn.select();
}

export function renameButton(id, { small = true } = {}) {
  return el('button', { class: 'btn btn-quiet' + (small ? ' btn-sm' : ''), type: 'button', title: rt('title'), onClick: (ev) => { ev.stopPropagation(); renameSignalDialog(id); } }, '✎ ' + rt('renameBtn'));
}

/** One-line control for the top of the Understanding page: pick a signal, type a name, save. */
export function renameBar(signalIds) {
  const ids = signalIds && signalIds.length ? signalIds : Object.keys(state.signalHeaders || {});
  if (!ids.length) return null;
  const select = el('select', { 'aria-label': rt('pick'), style: { flex: '1 1 160px', minWidth: '0', maxWidth: '100%' } }, ids.map((id) => el('option', { value: id }, signalDisplay(id) + ((state.signalHeaders || {})[id] && !(state.signalNames || {})[id] ? ` · ${(state.signalHeaders || {})[id]}` : ''))));
  const nameIn = el('input', { type: 'text', maxlength: '80', placeholder: rt('namePh'), 'aria-label': rt('name'), style: { flex: '2 1 200px', minWidth: '0' } });
  const sync = () => { nameIn.value = (state.signalNames || {})[select.value] || ''; };
  select.addEventListener('change', sync); sync();
  const go = () => save(select.value, nameIn.value.trim(), (state.signalUnits || {})[select.value] || '');
  nameIn.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); go(); } });
  return el('section', { class: 'rename-bar', style: { margin: '12px 0', padding: '12px 14px', border: '1px solid var(--line, #ccc)', borderRadius: '8px', overflowWrap: 'anywhere' } },
    el('div', { style: { fontWeight: '600', marginBottom: '6px' }, text: rt('bar') }),
    el('div', { style: { display: 'flex', gap: '8px', flexWrap: 'wrap', alignItems: 'center' } }, select, nameIn, el('button', { class: 'btn btn-primary', type: 'button', onClick: go }, rt('save'))),
    el('p', { class: 'small muted', style: { margin: '8px 0 0' }, text: rt('help') }));
}
