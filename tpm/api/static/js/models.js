/* "AI models on this computer" panel: which local model answers, choose another installed one, download a new
   one from Ollama with progress, install or start Ollama when the machine has none. Opened from the local-model
   lamp in the top bar. Plain summary and the next step first; the details sit under "Show technical analyses".
   Texts for en / fi / sv live here so the panel is self-contained. */
import { state, el, clear, api, modal, toast } from './core.js';

const STR = {
  en: {
    title: 'AI models on this computer',
    ready: 'Ready. Explanations and chat answers are written by {chat}, running only on this computer.',
    readyNoSearch: 'Search uses simple word matching until a search model is installed.',
    noOllama: 'No local AI is installed yet. The analysis still works; explanations are then written from fixed templates instead of a model.',
    notRunning: 'Ollama is installed but not running, so no local AI model can answer right now.',
    noChat: 'Ollama is running, but no chat model is installed yet.',
    doInstall: 'Install Ollama', doStart: 'Start Ollama', doSetup: 'Download the two standard models (about 6.4 GB)',
    installNote: 'Downloads the official installer from ollama.com, checks its digital signature and opens it. Only software is downloaded; none of your data is sent.',
    manual: 'Open the Ollama download page',
    chatModel: 'Model that writes explanations and answers', searchModel: 'Model used for search',
    auto: 'Automatic (best installed model for this computer)', none: 'none installed', inUse: 'in use',
    why: 'Why this one', getMore: 'Get another model', getHint: 'Type any model name from ollama.com/library, or pick a suggestion.',
    download: 'Download', cancel: 'Cancel', installed: 'installed', use: 'Use', placeholder: 'for example qwen3:4b',
    fit_fast: 'runs well here', fit_slow: 'runs, but slowly here', fit_too_big: 'too big for this computer',
    done: '{name} is ready to use.', failed: 'Download failed: {error}', saved: 'Saved. {name} is now used.', savedAuto: 'Saved. The app picks the best installed model by itself.',
    tech: 'Show technical analyses', techHide: 'Hide technical analyses',
    colName: 'Model', colKind: 'Kind', colSize: 'Size', colParams: 'Parameters', colQuant: 'Precision', colCaps: 'Abilities', colFit: 'On this computer',
    kind_chat: 'chat', kind_embedding: 'search', machine: 'This computer', ram: 'memory', gpu: 'graphics card', disk: 'free disk', ollama: 'Ollama',
    lampHint: 'Click to choose, download or install local AI models',
  },
  fi: {
    title: 'Tämän tietokoneen tekoälymallit',
    ready: 'Valmis. Selitykset ja chat-vastaukset kirjoittaa {chat}, joka toimii vain tällä tietokoneella.',
    readyNoSearch: 'Haku käyttää yksinkertaista sanahakua, kunnes hakumalli on asennettu.',
    noOllama: 'Paikallista tekoälyä ei ole vielä asennettu. Analyysi toimii silti; selitykset kirjoitetaan silloin valmiista pohjista.',
    notRunning: 'Ollama on asennettu, mutta ei käynnissä, joten paikallinen malli ei voi vastata juuri nyt.',
    noChat: 'Ollama on käynnissä, mutta chat-mallia ei ole vielä asennettu.',
    doInstall: 'Asenna Ollama', doStart: 'Käynnistä Ollama', doSetup: 'Lataa kaksi vakiomallia (noin 6,4 Gt)',
    installNote: 'Lataa virallisen asennusohjelman osoitteesta ollama.com, tarkistaa sen digitaalisen allekirjoituksen ja avaa sen. Vain ohjelmisto ladataan; tietojasi ei lähetetä.',
    manual: 'Avaa Ollaman lataussivu',
    chatModel: 'Malli, joka kirjoittaa selitykset ja vastaukset', searchModel: 'Haun käyttämä malli',
    auto: 'Automaattinen (paras asennettu malli tälle koneelle)', none: 'ei asennettu', inUse: 'käytössä',
    why: 'Miksi tämä', getMore: 'Hae toinen malli', getHint: 'Kirjoita mikä tahansa mallin nimi sivulta ollama.com/library tai valitse ehdotus.',
    download: 'Lataa', cancel: 'Peruuta', installed: 'asennettu', use: 'Käytä', placeholder: 'esimerkiksi qwen3:4b',
    fit_fast: 'toimii hyvin tällä koneella', fit_slow: 'toimii, mutta hitaasti', fit_too_big: 'liian suuri tälle koneelle',
    done: '{name} on valmis käyttöön.', failed: 'Lataus epäonnistui: {error}', saved: 'Tallennettu. Nyt käytössä {name}.', savedAuto: 'Tallennettu. Sovellus valitsee parhaan asennetun mallin itse.',
    tech: 'Näytä tekniset analyysit', techHide: 'Piilota tekniset analyysit',
    colName: 'Malli', colKind: 'Tyyppi', colSize: 'Koko', colParams: 'Parametrit', colQuant: 'Tarkkuus', colCaps: 'Kyvyt', colFit: 'Tällä koneella',
    kind_chat: 'chat', kind_embedding: 'haku', machine: 'Tämä tietokone', ram: 'muisti', gpu: 'näytönohjain', disk: 'vapaa levytila', ollama: 'Ollama',
    lampHint: 'Valitse, lataa tai asenna paikallisia tekoälymalleja napsauttamalla',
  },
  sv: {
    title: 'AI-modeller på den här datorn',
    ready: 'Klart. Förklaringar och chattsvar skrivs av {chat}, som bara körs på den här datorn.',
    readyNoSearch: 'Sökningen använder enkel ordmatchning tills en sökmodell är installerad.',
    noOllama: 'Ingen lokal AI är installerad ännu. Analysen fungerar ändå; förklaringarna skrivs då från fasta mallar.',
    notRunning: 'Ollama är installerat men körs inte, så ingen lokal modell kan svara just nu.',
    noChat: 'Ollama körs, men ingen chattmodell är installerad ännu.',
    doInstall: 'Installera Ollama', doStart: 'Starta Ollama', doSetup: 'Ladda ner de två standardmodellerna (cirka 6,4 GB)',
    installNote: 'Laddar ner det officiella installationsprogrammet från ollama.com, kontrollerar dess digitala signatur och öppnar det. Endast programvara laddas ner; inga av dina data skickas.',
    manual: 'Öppna Ollamas nedladdningssida',
    chatModel: 'Modell som skriver förklaringar och svar', searchModel: 'Modell som används för sökning',
    auto: 'Automatiskt (bästa installerade modell för den här datorn)', none: 'ingen installerad', inUse: 'används',
    why: 'Varför den här', getMore: 'Hämta en annan modell', getHint: 'Skriv ett modellnamn från ollama.com/library eller välj ett förslag.',
    download: 'Ladda ner', cancel: 'Avbryt', installed: 'installerad', use: 'Använd', placeholder: 'till exempel qwen3:4b',
    fit_fast: 'fungerar bra här', fit_slow: 'fungerar, men långsamt här', fit_too_big: 'för stor för den här datorn',
    done: '{name} är klar att användas.', failed: 'Nedladdningen misslyckades: {error}', saved: 'Sparat. {name} används nu.', savedAuto: 'Sparat. Appen väljer själv den bästa installerade modellen.',
    tech: 'Visa tekniska analyser', techHide: 'Dölj tekniska analyser',
    colName: 'Modell', colKind: 'Typ', colSize: 'Storlek', colParams: 'Parametrar', colQuant: 'Precision', colCaps: 'Förmågor', colFit: 'På den här datorn',
    kind_chat: 'chatt', kind_embedding: 'sökning', machine: 'Den här datorn', ram: 'minne', gpu: 'grafikkort', disk: 'ledigt diskutrymme', ollama: 'Ollama',
    lampHint: 'Klicka för att välja, ladda ner eller installera lokala AI-modeller',
  },
};
export function mt(key, vars) {
  const dict = STR[state.lang] || STR.en;
  let s = dict[key] || STR.en[key] || key;
  if (vars) for (const [k, v] of Object.entries(vars)) s = s.replace('{' + k + '}', String(v));
  return s;
}
const gb = (v) => (v === null || v === undefined ? '–' : `${Number(v).toFixed(v < 1 ? 2 : 1)} GB`);

let timer = null;
function stopPolling() { if (timer) { clearInterval(timer); timer = null; } }

export async function openModelsPanel(onChange) {
  const body = el('div', { class: 'models-panel' }, el('p', { class: 'dim', text: '…' }));
  let data = null;
  const changed = () => { if (onChange) onChange(); };

  async function load() {
    const r = await api('/api/models');
    if (!r.ok) { clear(body).append(el('div', { class: 'notice warn', text: (r.data && (r.data.detail || r.data.error)) || 'The models service is not available.' })); return; }
    data = r.data;
    render();
    const busy = (data.pulls || []).some((p) => ['queued', 'running'].includes(p.state)) || ['downloading', 'verifying', 'installing'].includes((data.install || {}).state);
    if (busy && !timer) timer = setInterval(load, 1200);
    if (!busy && timer) { stopPolling(); changed(); }
  }

  async function post(path, payload, okMsg) {
    const r = await api(path, { method: 'POST', body: payload || {} });
    if (!r.ok) toast((r.data && (r.data.detail || r.data.error)) || 'Request failed', 'fail');
    else if (okMsg) toast(okMsg, 'ok');
    await load();
    return r;
  }

  function summary() {
    const sel = data.selected || {};
    const step = data.next_step;
    const box = el('section', { class: 'brief-card', style: { padding: '14px 16px', borderLeft: '4px solid var(--accent, #2bb5a0)', background: 'var(--bg-3, rgba(127,127,127,.08))', borderRadius: '0 8px 8px 0', marginBottom: '16px', overflowWrap: 'anywhere' } });
    const head = (txt) => el('p', { style: { fontSize: '1.05rem', margin: '0 0 8px', fontWeight: '600' }, text: txt });
    const inst = data.install || {};
    if (step === 'install_ollama') {
      box.append(head(mt('noOllama')));
      if (data.ollama.platform === 'win32') box.append(el('button', { class: 'btn btn-primary', type: 'button', disabled: ['downloading', 'verifying', 'installing'].includes(inst.state), onClick: () => post('/api/ollama/install') }, mt('doInstall')), el('p', { class: 'small muted', style: { margin: '8px 0 0' }, text: mt('installNote') }));
      else box.append(el('a', { class: 'btn btn-primary', href: data.download_page, target: '_blank', rel: 'noopener' }, mt('manual')));
    } else if (step === 'start_ollama') {
      box.append(head(mt('notRunning')), el('button', { class: 'btn btn-primary', type: 'button', onClick: (ev) => { ev.currentTarget.disabled = true; post('/api/ollama/start').then(changed); } }, mt('doStart')));
    } else if (step === 'pull_chat_model') {
      box.append(head(mt('noChat')), el('button', { class: 'btn btn-primary', type: 'button', onClick: () => post('/api/models/setup') }, mt('doSetup')));
    } else {
      box.append(head(mt('ready', { chat: sel.chat || '–' })));
      if (!sel.embedding) box.append(el('p', { class: 'small', style: { margin: '0 0 8px' }, text: mt('readyNoSearch') }), el('button', { class: 'btn btn-sm', type: 'button', onClick: () => post('/api/models/setup') }, mt('download') + ' nomic-embed-text'));
    }
    if (inst.state && inst.state !== 'idle') {
      box.append(el('p', { class: 'small', style: { margin: '10px 0 4px' }, text: inst.error || inst.status || '' }));
      if (inst.state === 'downloading') box.append(progressBar(inst.percent));
    }
    return box;
  }

  function progressBar(pct) {
    return el('div', { style: { height: '8px', background: 'var(--line, #ccc)', borderRadius: '4px', overflow: 'hidden' }, role: 'progressbar', 'aria-valuenow': String(Math.round(pct || 0)), 'aria-valuemin': '0', 'aria-valuemax': '100' }, el('div', { style: { width: `${Math.max(0, Math.min(100, pct || 0))}%`, height: '100%', background: 'var(--accent, #2bb5a0)', transition: 'width .4s' } }));
  }

  function chooser(kind, label) {
    const sel = data.selected || {};
    const current = kind === 'chat' ? sel.chat : sel.embedding;
    const source = kind === 'chat' ? sel.chat_source : sel.embedding_source;
    const list = (data.models || []).filter((m) => m.kind === kind);
    const select = el('select', { style: { maxWidth: '100%', minWidth: '0', width: '100%' }, 'aria-label': label, disabled: !list.length, onChange: async (ev) => {
      const name = ev.currentTarget.value;
      await post('/api/models/select', { kind, name }, name === 'auto' ? mt('savedAuto') : mt('saved', { name }));
      changed();
    } },
      el('option', { value: 'auto', selected: source !== 'user' }, mt('auto')),
      list.map((m) => el('option', { value: m.name, selected: source === 'user' && m.name === current }, `${m.name} · ${gb(m.size_gb)}${m.fit ? ' · ' + mt('fit_' + m.fit) : ''}`)));
    const reason = kind === 'chat' ? sel.chat_reason : sel.embedding_reason;
    return el('div', { style: { margin: '0 0 14px' } },
      el('label', { class: 'small muted', style: { display: 'block', marginBottom: '4px' }, text: label }),
      select,
      el('p', { class: 'small', style: { margin: '6px 0 0' } }, el('strong', { text: current || mt('none') }), current ? ` – ${mt('inUse')}. ` : '. ', el('span', { class: 'muted', text: reason || '' })));
  }

  function getMore() {
    const input = el('input', { type: 'text', placeholder: mt('placeholder'), style: { flex: '1 1 180px', minWidth: '0' }, 'aria-label': mt('getMore'), disabled: !data.ollama.running });
    const go = (name) => { const n = String(name || input.value || '').trim(); if (n) post('/api/models/pull', { name: n }); };
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
    const pulls = (data.pulls || []).slice().reverse().slice(0, 4).map((p) => el('div', { style: { margin: '10px 0 0' } },
      el('div', { class: 'small', style: { display: 'flex', justifyContent: 'space-between', gap: '8px', flexWrap: 'wrap' } },
        el('span', {}, el('strong', { text: p.name }), ` – ${p.state === 'done' ? mt('done', { name: p.name }) : p.state === 'failed' ? mt('failed', { error: p.error || '' }) : `${p.status} ${p.total_gb ? `(${gb(p.completed_gb)} / ${gb(p.total_gb)})` : ''}`}`),
        ['queued', 'running'].includes(p.state) ? el('button', { class: 'btn btn-sm btn-quiet', type: 'button', onClick: async () => { await api(`/api/models/pull/${encodeURIComponent(p.id)}`, { method: 'DELETE' }); load(); } }, mt('cancel')) : null),
      ['queued', 'running'].includes(p.state) ? progressBar(p.percent) : null));
    const chips = (data.suggested || []).map((s) => el('button', { class: 'btn btn-sm', type: 'button', disabled: s.installed || !data.ollama.running, title: s.note, style: { textAlign: 'left', whiteSpace: 'normal' }, onClick: () => go(s.name) },
      `${s.name} · ${gb(s.size_gb)}`, el('span', { class: 'small muted', text: s.installed ? ` · ${mt('installed')}` : (s.kind === 'chat' ? ` · ${mt('fit_' + s.fit)}` : ` · ${mt('kind_embedding')}`) })));
    return el('div', { style: { margin: '4px 0 16px' } },
      el('h3', { style: { margin: '0 0 4px' }, text: mt('getMore') }),
      el('p', { class: 'small muted', style: { margin: '0 0 8px' }, text: mt('getHint') }),
      el('div', { style: { display: 'flex', gap: '8px', flexWrap: 'wrap' } }, input, el('button', { class: 'btn btn-primary', type: 'button', disabled: !data.ollama.running, onClick: () => go() }, mt('download'))),
      el('div', { style: { display: 'flex', gap: '6px', flexWrap: 'wrap', marginTop: '10px' } }, chips),
      pulls);
  }

  function technical() {
    const m = data.machine || {};
    const rows = (data.models || []).map((x) => el('tr', {},
      el('td', {}, x.name, x.in_use ? el('span', { class: 'chip ok', style: { marginLeft: '6px' }, text: mt('inUse') }) : null),
      el('td', { text: mt('kind_' + x.kind) }), el('td', { text: gb(x.size_gb) }), el('td', { text: x.parameters || '–' }), el('td', { text: x.quantization || '–' }),
      el('td', { text: (x.capabilities || []).join(', ') || '–' }), el('td', { text: x.fit ? mt('fit_' + x.fit) : '–' })));
    const details = el('details', { class: 'tech-details' },
      el('summary', { style: { cursor: 'pointer', fontWeight: '600' }, text: mt('tech') }),
      el('div', { style: { overflowX: 'auto', marginTop: '10px' } }, el('table', { class: 'tbl', style: { width: '100%' } },
        el('thead', {}, el('tr', {}, ['colName', 'colKind', 'colSize', 'colParams', 'colQuant', 'colCaps', 'colFit'].map((k) => el('th', { text: mt(k) })))),
        el('tbody', {}, rows.length ? rows : el('tr', {}, el('td', { colspan: '7', class: 'muted', text: mt('none') }))))),
      el('p', { class: 'small muted', style: { marginTop: '10px' }, text: `${mt('machine')}: ${mt('ram')} ${gb(m.ram_gb)}${m.gpu ? `, ${mt('gpu')} ${m.gpu.name} (${gb(m.gpu.vram_gb)})` : ''}, ${mt('disk')} ${gb(m.disk_free_gb)}. ${mt('ollama')}: ${data.ollama.running ? (data.ollama.version || 'running') : (data.ollama.installed ? 'installed, not running' : 'not installed')} · ${data.ollama.base_url}` }));
    details.addEventListener('toggle', () => { details.querySelector('summary').textContent = details.open ? mt('techHide') : mt('tech'); });
    return details;
  }

  function render() {
    const open = body.querySelector('details.tech-details');
    const wasOpen = open ? open.open : false;
    const before = body.querySelector('input[type=text]');
    const typed = before ? before.value : '';
    const focused = before && document.activeElement === before;
    clear(body).append(summary(), chooser('chat', mt('chatModel')), chooser('embedding', mt('searchModel')), getMore(), technical());
    if (wasOpen) { const d = body.querySelector('details.tech-details'); d.open = true; }
    const input = body.querySelector('input[type=text]');
    if (typed) input.value = typed;
    if (focused) { input.focus(); input.setSelectionRange(typed.length, typed.length); }  // a progress refresh must not interrupt typing
  }

  modal({ title: mt('title'), body, wide: true, onClose: () => { stopPolling(); changed(); } });
  await load();
}
