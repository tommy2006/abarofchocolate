/* View 9: live monitor — a page of its own that needs no run. Follows a data file that keeps growing and re-checks
   every sensor once per analysis cycle. Two tabs: Monitor (what is happening) and Settings (sensitivity with "let the
   AI decide", simulate with a file, live data link). The server sends message keys + variables; this file translates. */
import { state, t, el, clear, api, fmt, chip, section, table, viewHead, empty, kv, st, toast, errText, roleAllows, notice, hiddenHint, actorName, store } from '../core.js';
import { plot, purge, tokens } from '../charts.js';

const POLL_MS = 3000;
const CHECKS = ['level', 'trend', 'noise', 'range'];
const DEFAULTS = { percent: { watch: 1, alarm: 2 }, spreads: { watch: 1.5, alarm: 3 } };

// ---------------------------------------------------------------- text
const num = (x) => (x === null || x === undefined || x === '' || isNaN(x) ? '–' : String(+Number(x).toPrecision(4)));
const unitOf = (u) => (u === 'sp' ? ' ' + t('live.unit.sp') : u);
/** One server message {key, vars} in the current language. */
const tm = (m) => (m ? t('live.msg.' + m.key, Object.assign({}, m.vars, m.vars && m.vars.u !== undefined ? { u: unitOf(m.vars.u) } : {})) : '');
const listOf = (items) => (items || []).map((i) => `${i.sensor} (${i.checks.map((c) => t('live.check.' + c)).join(', ')})`).join(', ');
function verdictText(v) {
  if (!v) return '';
  const one = (key, vars, items) => tm({ key, vars: Object.assign({}, vars, items ? { list: listOf(items) } : {}) });
  if (v.parts) return v.parts.map((p) => one(p.key, p.vars, p.items)).join(' ');
  return one(v.key, v.vars, v.items);
}
const NOTICE_KIND = { ok: 'ok', watch: 'warn', drift: 'fail', quality: 'warn', untrusted: 'fail', learn: '', wait: '' };
const CHIP_KIND = { ok: 'ok', watch: 'warn', alarm: 'fail', dead: 'fail', missing: 'fail' };
const LEVEL_KIND = ['', 'warn', 'fail'];

export async function render(main, params) {
  const view = el('div', { class: 'view' });
  main.append(view);
  view.append(viewHead('9', t('nav.live')));
  view.append(el('p', { class: 'hint', text: t('live.intro') }));

  let tab = params && params.tab === 'settings' ? 'settings' : store.get('live.tab', 'monitor');
  let snap = null;
  let sel = null;
  let filter = 'all';
  let sig = '';
  let timer = null;
  let chartNode = null;
  let settingsUI = null;
  let dirty = false;

  const tabs = el('div', { class: 'langs', role: 'group', 'aria-label': t('nav.live') });
  const statusBox = el('div', { class: 'live-status' });
  const content = el('div', { class: 'stack' });
  view.append(el('div', { class: 'row', style: { justifyContent: 'space-between', margin: '6px 0 10px' } }, tabs), statusBox, content);

  // ---------------------------------------------------------------- shared: what the monitor is doing right now
  function paintStatus() {
    clear(statusBox);
    if (!snap) return;
    const s = snap.source;
    const bits = [];
    if (s.kind === 'none') bits.push(el('span', { class: 'muted', text: t('live.src.none') }));
    else {
      bits.push(chip(t('live.src.' + s.kind), 'info'), ' ', el('b', { text: s.name }), ' ');
      bits.push(el('span', { class: 'small dim', text: t('live.status.rows', { n: fmt.int(s.rows) }) + ' · ' + t('live.status.every', { every: fmt.dur(snap.cfg.interval), rows: fmt.int(snap.expected_rows) }) + ' · ' + (snap.baseline_ready ? t('live.status.monitoring') : t('live.status.learning', { done: snap.learning, total: snap.cfg.baseline_cycles })) }));
      if (snap.next_at) bits.push(el('span', { class: 'small dim', text: ' · ' + t('live.status.next', { time: fmt.time(snap.next_at) }) }));
    }
    statusBox.append(el('div', {}, bits));
    if (s.detail && s.kind !== 'none') statusBox.append(el('div', { class: 'small dim', text: s.detail }));
    if (s.error) statusBox.append(el('div', { class: 'notice fail small', text: s.error }));
    if (settingsUI) settingsUI.stop.hidden = s.kind === 'none';
  }

  // ---------------------------------------------------------------- tab 1: monitor
  function renderMonitor() {
    const c = clear(content);
    if (chartNode) { purge(chartNode); chartNode = null; }
    const s = snap;
    if (!s) return;
    const v = s.verdict || {};
    c.append(el('div', { class: 'notice ' + (NOTICE_KIND[v.level] || ''), role: 'status' }, el('b', { text: t('live.vl.' + v.level) + '. ' }), verdictText(v)));
    if (!s.running && !s.baseline_ready) {
      const empt = el('div', { class: 'stack' }, el('h2', { text: t('live.empty.title') }), el('p', { class: 'muted', text: t('live.empty.body') }),
        el('div', { class: 'row' },
          roleAllows('engineer') ? el('button', { class: 'btn btn-primary', type: 'button', onClick: startDemo }, t('live.sim.demo')) : el('span', { class: 'small dim', text: t('live.empty.role') }),
          el('button', { class: 'btn', type: 'button', onClick: () => showTab('settings') }, t('live.empty.settings'))));
      c.append(el('section', { class: 'sec' }, el('div', { class: 'sec-body' }, empt)));
      return;
    }
    const T = s.table || [];
    const count = (k) => T.filter((r) => r.status === k).length;
    if (s.baseline_ready) c.append(el('div', { class: 'row' }, ['dead', 'missing', 'alarm', 'watch', 'ok'].map((k) => el('span', { class: 'row', style: { gap: '6px' } }, chip(String(count(k)), count(k) && k !== 'ok' ? CHIP_KIND[k] : (k === 'ok' ? 'ok' : ''), { style: { fontWeight: 600 } }), el('span', { class: 'small muted', text: t('live.count.' + k) })))));
    const by = s.settings_by || {};
    const u = s.settings.mode === 'percent' ? '%' : ' ' + t('live.unit.sp');
    c.append(el('p', { class: 'small muted' }, t('live.sens.now', { watch: s.settings.watch, alarm: s.settings.alarm, unit: u, by: t('live.sens.by.' + (by.by || 'default')) }), ' ',
      el('button', { class: 'btn btn-sm btn-quiet', type: 'button', onClick: () => showTab('settings') }, t('live.sens.change'))));
    if (s.advice) c.append(el('div', { class: 'notice' }, el('b', { text: t('live.advice.title') + ': ' }), s.advice, el('div', { class: 'small dim', text: t('live.advice.note') })));
    if (!s.baseline_ready) return;

    // sensors
    const sensors = section(t('live.sensors.title'));
    c.append(sensors.root);
    sensors.body.append(el('p', { class: 'hint', text: t('live.sensors.hint') }));
    const bar = el('div', { class: 'row', style: { margin: '6px 0' } }, ['all', 'problems', 'ok'].map((f) => el('button', { class: 'btn btn-sm' + (filter === f ? ' btn-primary' : ''), type: 'button', onClick: () => { filter = f; renderMonitor(); } }, t('live.filter.' + f))));
    sensors.body.append(bar);
    const rank = { dead: 0, missing: 1, alarm: 2, watch: 3, ok: 4 };
    const rows = T.filter((r) => filter === 'all' || (filter === 'ok' ? r.status === 'ok' : r.status !== 'ok')).sort((a, b) => rank[a.status] - rank[b.status] || b.score - a.score);
    if (!sel || !T.some((r) => r.sensor === sel)) sel = ([...T].sort((a, b) => b.score - a.score)[0] || {}).sensor || null;
    const detail = el('div', {});
    const drawDetail = () => paintDetail(detail);
    sensors.body.append(table({
      columns: [
        { label: t('live.col.sensor'), cls: 'wrap', render: (r) => el('span', {}, el('b', { text: r.sensor }), el('div', { class: 'small dim', text: tm(r.kind) })) },
        { label: t('live.col.status'), render: (r) => chip(t('live.st.' + r.status), CHIP_KIND[r.status]) },
        { label: t('live.col.normal'), render: (r) => `${num(r.mu)} ± ${num(r.sigma)}` },
        { label: t('live.col.now'), render: (r) => el('span', {}, num(r.mean), el('div', { class: 'small dim', text: `${r.dev > 0 ? '▲' : r.dev < 0 ? '▼' : ''} ${num(Math.abs(r.dev))} (${Math.abs(r.dev_pct)}%)` })) },
        { label: t('live.col.checks'), cls: 'wrap', render: (r) => el('span', {}, r.elements.map((e) => chip(`${t('live.check.' + e.name)} ${e.short}`, LEVEL_KIND[e.level], { title: tm(e), style: { marginRight: '4px' } }))) },
        { label: t('live.col.off'), cls: 'wrap', render: (r) => (r.cycles ? t('live.since', { dur: fmt.dur(r.off_for_s), n: r.cycles, time: fmt.time(r.since) }) : '–') },
        { label: t('live.col.why'), cls: 'wrap', render: (r) => (r.notes || []).map(tm).join(' ') },
      ],
      rows, pageSize: 50, keyOf: (r) => r.sensor, selectedKey: sel, onRow: (r) => { sel = r.sensor; drawDetail(); },
    }));
    const dsec = section(t('live.detail.title'));
    c.append(dsec.root);
    dsec.body.append(detail);
    drawDetail();

    // events
    const ev = section(t('live.events.title'));
    c.append(ev.root);
    if (!(s.events || []).length) ev.body.append(empty(t('live.events.none')));
    else ev.body.append(el('ul', { class: 'list' }, s.events.slice(0, 15).map((e) => el('li', {}, el('span', { class: 'dim small', text: fmt.time(e.t) + ' ' }), eventText(e)))));
    const hh = hiddenHint(view); if (hh) c.append(hh);
  }

  function eventText(e) {
    if (e.kind === 'status') return `${t('live.event.status', { sensor: e.sensor, old: t('live.st.' + e.old), new: t('live.st.' + e.new) })}. ${(e.notes || []).map(tm).join(' ')}`;
    if (e.kind === 'settings') {
      const x = e.settings || {};
      return t('live.event.settings', { who: t('live.event.who.' + (e.by === 'ai' ? 'ai' : 'operator')), watch: x.watch, alarm: x.alarm, noise: x.noise_ratio, range: x.outside_pct }) + (e.note ? ' ' + t('live.event.reason', { reason: e.note }) : '');
    }
    return tm(e);
  }

  function paintDetail(box) {
    clear(box);
    if (chartNode) { purge(chartNode); chartNode = null; }
    const s = snap;
    const r = (s.table || []).find((x) => x.sensor === sel);
    const b = s.baseline && s.baseline[sel];
    if (!r || !b) { box.append(empty(t('live.detail.pick'))); return; }
    box.append(el('h3', { class: 'small muted', text: `${sel}: ${t('live.detail.title2')}` }));
    box.append(el('div', { class: 'cols cols-3' },
      el('div', {}, el('h4', { text: t('live.detail.type') }), el('b', { text: tm(b.kind) }), el('ul', { class: 'small muted' }, (b.profile || []).map((p) => el('li', { text: tm(p) })))),
      el('div', {}, el('h4', { text: t('live.detail.normal') }), el('b', { text: `${num(b.mu)} ± ${num(b.sigma)}` }), el('ul', { class: 'small muted' },
        el('li', { text: t('live.detail.learned', { lo: num(b.lo), hi: num(b.hi) }) }), el('li', { text: t('live.detail.cycle', { mean: num(r.mean), lo: num(r.cur_lo), hi: num(r.cur_hi) }) }), el('li', { text: t('live.detail.basis', { basis: num(b.basis) }) }))),
      el('div', {}, el('h4', { text: t('live.detail.checks') }), el('ul', { class: 'small', style: { listStyle: 'none', padding: 0 } }, r.elements.map((e) => el('li', {}, st(['ok', 'warn', 'fail'][e.level], tm(e))))))));
    const H = s.history || [];
    const ys = H.map((h) => h.mean[sel]);
    if (ys.filter((y) => y !== undefined).length < 1) return;
    const k = tokens();
    const bw = (x) => (s.settings.mode === 'percent' ? (x / 100) * b.basis : x * b.sigma);
    const node = el('div', { class: 'chart' });
    chartNode = node;
    box.append(el('div', { class: 'chartbox' }, node), el('p', { class: 'small dim chartnote', text: t('live.detail.chart') }));
    const band = (x, color, op) => ({ type: 'rect', xref: 'paper', x0: 0, x1: 1, yref: 'y', y0: b.mu - bw(x), y1: b.mu + bw(x), fillcolor: color, opacity: op, line: { width: 0 }, layer: 'below' });
    plot(node, [{ x: H.map((h) => h.t), y: ys, mode: 'lines+markers', name: t('live.chart.mean'), line: { color: k.info, width: 2 }, marker: { size: 6 } },
      { x: [H[0].t, H[H.length - 1].t], y: [b.mu, b.mu], mode: 'lines', name: t('live.chart.normal'), line: { color: k.ink3, dash: 'dash', width: 1 }, hoverinfo: 'skip' }],
    { shapes: [band(s.settings.alarm, k.warn, 0.14), band(s.settings.watch, k.ok, 0.2)], height: 260, margin: { l: 56, r: 16, t: 10, b: 36 }, showlegend: true });
  }

  // ---------------------------------------------------------------- tab 2: settings
  function buildSettings() {
    const c = clear(content);
    const ui = {};
    settingsUI = ui;
    const inp = (id, attrs) => el('input', Object.assign({ id, type: 'number', style: { width: '110px' } }, attrs));
    const field = (label, ...kids) => el('label', { class: 'field' }, el('span', { class: 'small muted', text: label }), ...kids);
    const msg = () => el('p', { class: 'small', style: { minHeight: '1.3em', margin: '6px 0 0' } });
    const say = (node, text, kind) => { node.textContent = text; node.style.color = kind === 'ok' ? 'var(--ok)' : kind === 'fail' ? 'var(--fail)' : ''; };
    const actor = () => actorName();

    // ---- 1. sensitivity
    const sens = section(t('live.set.sens.title'));
    c.append(sens.root);
    ui.mode = el('select', {}, el('option', { value: 'percent', text: t('live.set.mode.percent') }), el('option', { value: 'spreads', text: t('live.set.mode.spreads') }));
    ui.watch = inp('lv-watch', { step: '0.1', min: '0.1' });
    ui.alarm = inp('lv-alarm', { step: '0.1', min: '0.1' });
    ui.noise = inp('lv-noise', { step: '0.5', min: '1.2' });
    ui.range = inp('lv-range', { step: '5', min: '1', max: '100' });
    ui.unit = [el('b', { class: 'small' }), el('b', { class: 'small' })];
    ui.sensMsg = msg();
    ui.by = el('div', { class: 'notice small' });
    ui.aiBtn = el('button', { class: 'btn', type: 'button', onClick: aiDecide }, '✨ ' + t('live.set.ai'));
    const setUnit = () => ui.unit.forEach((n) => { n.textContent = ' ' + (ui.mode.value === 'percent' ? '%' : t('live.unit.sp')); });
    ui.mode.addEventListener('change', () => { const d = DEFAULTS[ui.mode.value]; ui.watch.value = d.watch; ui.alarm.value = d.alarm; setUnit(); dirty = true; });
    [ui.watch, ui.alarm, ui.noise, ui.range].forEach((n) => n.addEventListener('input', () => { dirty = true; say(ui.sensMsg, ''); }));
    sens.body.append(el('p', { class: 'hint', text: t('live.set.sens.hint') }),
      el('div', { class: 'row', style: { alignItems: 'flex-end', gap: '14px' } }, field(t('live.set.mode'), ui.mode), field(t('live.set.watch'), el('span', {}, ui.watch, ui.unit[0])), field(t('live.set.alarm'), el('span', {}, ui.alarm, ui.unit[1])),
        field(t('live.set.noise'), ui.noise), field(t('live.set.range'), ui.range)),
      el('div', { class: 'row', style: { marginTop: '10px' } },
        el('button', { class: 'btn btn-primary', type: 'button', onClick: () => saveSens(read(), t('live.set.saved')) }, t('live.set.apply')),
        el('button', { class: 'btn', type: 'button', onClick: () => { fill({ mode: 'percent', watch: 1, alarm: 2, noise_ratio: 2, outside_pct: 20 }); saveSens(read(), t('live.set.defaultsDone')); } }, t('live.set.defaults')), ui.aiBtn),
      ui.sensMsg, ui.by, el('p', { class: 'small dim', text: t('live.set.ai.note') }));
    const read = () => ({ mode: ui.mode.value, watch: ui.watch.value, alarm: ui.alarm.value, noise_ratio: ui.noise.value, outside_pct: ui.range.value, actor: actor() });
    const fill = (s) => { ui.mode.value = s.mode; ui.watch.value = s.watch; ui.alarm.value = s.alarm; ui.noise.value = s.noise_ratio; ui.range.value = s.outside_pct; setUnit(); };
    async function saveSens(body, okText) {
      const r = await api('/api/live/settings', { method: 'POST', body });
      if (r.ok) { dirty = false; say(ui.sensMsg, okText, 'ok'); await poll(true); } else say(ui.sensMsg, errText(r), 'fail');
    }
    async function aiDecide() {
      ui.aiBtn.disabled = true;
      say(ui.sensMsg, t('live.set.ai.busy'));
      const r = await api('/api/live/ai-settings', { method: 'POST', body: { lang: state.lang, actor: actor() } });
      ui.aiBtn.disabled = false;
      if (r.ok) { dirty = false; fill(r.data.settings); say(ui.sensMsg, t('live.set.ai.done'), 'ok'); await poll(true); } else say(ui.sensMsg, errText(r), 'fail');
    }
    ui.syncSens = () => {
      const s = snap; if (!s) return;
      if (!dirty) fill(s.settings); else setUnit();
      const by = s.settings_by || {};
      const when = by.t ? fmt.time(by.t) : '';
      clear(ui.by);
      ui.by.append(by.by === 'ai' ? t('live.set.by.ai', { model: by.name || '', time: when, reason: by.note || '' }) : by.by === 'operator' ? t('live.set.by.operator', { name: by.name || '–', time: when }) : t('live.set.by.default'));
    };

    // ---- 2. simulate with a file (engineer)
    const sim = section(t('live.sim.title'), { level: 'engineer' });
    c.append(sim.root);
    const drop = el('div', { class: 'drop', tabindex: '0', role: 'button' }, el('div', { text: t('live.sim.drop') }), el('div', { class: 'file' }), el('div', { class: 'small dim', text: t('live.sim.dropHelp') }));
    const pick = el('input', { type: 'file', accept: '.csv,.txt,text/csv', hidden: true });
    ui.bar = el('progress', { value: 0, max: 100, hidden: true, style: { width: '100%' } });
    ui.upMsg = msg();
    ui.path = el('input', { type: 'text', class: 'wide', placeholder: 'G:\\data\\process.csv', style: { width: '100%' } });
    ui.sRate = inp('lv-srate', { step: '1', min: '0.1', value: '10' });
    ui.sInt = inp('lv-sint', { step: '1', min: '1', value: '20' });
    ui.sBase = inp('lv-sbase', { step: '1', min: '1', value: '2' });
    ui.sStart = inp('lv-sstart', { step: '1', min: '1', value: '1' });
    ui.sRows = inp('lv-srows', { step: '1', min: '0', value: '5000' });
    ui.sLoop = el('input', { type: 'checkbox' });
    ui.simCalc = el('p', { class: 'small muted' });
    ui.simMsg = msg();
    const calc = () => {
      const rate = +ui.sRate.value, iv = +ui.sInt.value, base = +ui.sBase.value;
      ui.simCalc.textContent = rate > 0 && iv > 0 ? t('live.sim.calc', { rows: fmt.int(Math.round(rate * iv)), n: base, dur: fmt.dur(base * iv), first: fmt.int(Math.round(rate * iv * base)) }) : '';
      const r2 = +ui.lRate.value, i2 = +ui.lInt.value, b2 = +ui.lBase.value;
      ui.linkCalc.textContent = r2 > 0 && i2 > 0 ? t('live.sim.calc', { rows: fmt.int(Math.round(r2 * i2)), n: b2, dur: fmt.dur(b2 * i2), first: fmt.int(Math.round(r2 * i2 * b2)) }) : '';
    };
    const upload = (file) => new Promise((resolve, reject) => {
      const x = new XMLHttpRequest();
      ui.bar.hidden = false; ui.bar.value = 0;
      x.open('POST', '/api/live/upload?name=' + encodeURIComponent(file.name));
      x.upload.onprogress = (e) => { if (e.lengthComputable) { ui.bar.value = (e.loaded / e.total) * 100; say(ui.upMsg, t('live.sim.uploading', { name: file.name, done: fmt.bytes(e.loaded), total: fmt.bytes(e.total) })); } };
      x.onload = () => { let j = {}; try { j = JSON.parse(x.responseText); } catch { /* ignore */ } x.status === 200 ? resolve(j) : reject(new Error(j.detail || j.error || `HTTP ${x.status}`)); };
      x.onerror = () => reject(new Error(t('live.sim.uploadFailed')));
      x.send(file);
    });
    const handleFile = async (file) => {
      if (!file) return;
      try { const j = await upload(file); ui.path.value = j.path; drop.querySelector('.file').textContent = file.name; say(ui.upMsg, t('live.sim.uploaded', { name: file.name, size: fmt.bytes(j.size) }), 'ok'); } catch (e) { say(ui.upMsg, e.message, 'fail'); }
      ui.bar.hidden = true;
    };
    drop.addEventListener('click', () => pick.click());
    drop.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick.click(); } });
    pick.addEventListener('change', () => handleFile(pick.files[0]));
    ['dragenter', 'dragover'].forEach((n) => drop.addEventListener(n, (e) => { e.preventDefault(); drop.classList.add('over'); }));
    ['dragleave', 'drop'].forEach((n) => drop.addEventListener(n, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
    drop.addEventListener('drop', (e) => handleFile(e.dataTransfer.files[0]));
    ui.simGo = el('button', { class: 'btn btn-primary', type: 'button', onClick: async () => {
      ui.simGo.disabled = true; say(ui.simMsg, t('live.working'));
      const r = await api('/api/live/source/simulate', { method: 'POST', body: { path: ui.path.value, rate: ui.sRate.value, interval: ui.sInt.value, baseline_cycles: ui.sBase.value, start_row: ui.sStart.value, max_rows: ui.sRows.value, loop: ui.sLoop.checked, actor: actor() } });
      ui.simGo.disabled = false;
      if (r.ok) { say(ui.simMsg, t('live.sim.started'), 'ok'); await poll(true); } else say(ui.simMsg, errText(r), 'fail');
    } }, '▶ ' + t('live.sim.go'));
    sim.body.append(el('p', { class: 'hint', text: t('live.sim.hint') }), drop, pick, ui.bar, ui.upMsg, field(t('live.sim.path'), ui.path),
      el('div', { class: 'row', style: { alignItems: 'flex-end', gap: '14px', marginTop: '8px' } }, field(t('live.sim.rate'), ui.sRate), field(t('live.sim.interval'), ui.sInt), field(t('live.sim.base'), ui.sBase), field(t('live.sim.start'), ui.sStart), field(t('live.sim.rows'), ui.sRows), el('label', { class: 'row small muted', style: { gap: '6px' } }, ui.sLoop, t('live.sim.loop'))),
      ui.simCalc,
      el('div', { class: 'row' }, ui.simGo,
        el('button', { class: 'btn', type: 'button', onClick: () => { ui.sRate.value = 10; ui.sInt.value = 20; ui.sBase.value = 2; calc(); } }, t('live.sim.fast')),
        el('button', { class: 'btn', type: 'button', onClick: () => { ui.sRate.value = 1; ui.sInt.value = 900; ui.sBase.value = 4; calc(); } }, t('live.sim.real')),
        el('button', { class: 'btn', type: 'button', onClick: startDemo }, t('live.sim.demo'))),
      el('p', { class: 'small dim', style: { marginTop: '8px' }, text: t('live.sim.note') }), ui.simMsg);

    // ---- 3. live data link (engineer)
    const link = section(t('live.link.title'), { level: 'engineer' });
    c.append(link.root);
    ui.target = el('input', { type: 'text', placeholder: 'https://example.com/plant/live.csv   |   C:\\data\\plant_live.csv', style: { width: '100%' } });
    ui.lRate = inp('lv-lrate', { step: '0.1', min: '0.01', value: '1' });
    ui.lInt = inp('lv-lint', { step: '1', min: '1', value: '900' });
    ui.lBase = inp('lv-lbase', { step: '1', min: '1', value: '4' });
    ui.linkCalc = el('p', { class: 'small muted' });
    ui.linkMsg = msg();
    ui.linkGo = el('button', { class: 'btn btn-primary', type: 'button', onClick: async () => {
      ui.linkGo.disabled = true; say(ui.linkMsg, t('live.working'));
      const r = await api('/api/live/source/live', { method: 'POST', body: { target: ui.target.value, rows_per_sec: ui.lRate.value, interval: ui.lInt.value, baseline_cycles: ui.lBase.value, actor: actor() } });
      ui.linkGo.disabled = false;
      if (r.ok) { say(ui.linkMsg, t('live.link.connected'), 'ok'); await poll(true); } else say(ui.linkMsg, errText(r), 'fail');
    } }, '🔗 ' + t('live.link.go'));
    ui.stop = el('button', { class: 'btn', type: 'button', hidden: true, onClick: async () => { const r = await api('/api/live/source/stop', { method: 'POST', body: { actor: actor() } }); if (r.ok) { say(ui.linkMsg, t('live.link.stopped'), 'ok'); await poll(true); } } }, t('live.link.stop'));
    link.body.append(el('p', { class: 'hint', text: t('live.link.hint') }), field(t('live.link.target'), ui.target),
      el('div', { class: 'row', style: { alignItems: 'flex-end', gap: '14px', marginTop: '8px' } }, field(t('live.link.rate'), ui.lRate), field(t('live.link.interval'), ui.lInt), field(t('live.link.base'), ui.lBase)),
      ui.linkCalc, el('div', { class: 'row' }, ui.linkGo, ui.stop), ui.linkMsg, el('p', { class: 'small dim', style: { marginTop: '8px' }, text: t('live.link.note') }));
    [ui.sRate, ui.sInt, ui.sBase, ui.lRate, ui.lInt, ui.lBase].forEach((n) => n.addEventListener('input', calc));
    calc();
    const hh = hiddenHint(view); if (hh) c.append(hh);
    ui.syncSens();
    paintStatus();
  }

  async function startDemo() {
    const r = await api('/api/live/source/demo', { method: 'POST', body: { actor: actorName() } });
    if (r.ok) { toast(t('live.sim.demoStarted'), 'ok'); showTab('monitor'); await poll(true); } else toast(errText(r), 'fail');
  }

  // ---------------------------------------------------------------- tabs + polling
  function paintTabs() {
    clear(tabs);
    for (const id of ['monitor', 'settings']) tabs.append(el('button', { type: 'button', 'aria-pressed': String(tab === id), onClick: () => showTab(id) }, t('live.tab.' + id)));
  }
  function showTab(id) {
    tab = id;
    store.set('live.tab', id);
    settingsUI = null;
    sig = '';
    paintTabs();
    if (id === 'settings') buildSettings(); else renderMonitor();
    paintStatus();
  }
  async function poll(force) {
    const r = await api('/api/live/state');
    if (!r.ok) return;
    snap = r.data;
    paintStatus();
    if (tab === 'settings') { if (settingsUI) settingsUI.syncSens(); return; }
    const s = JSON.stringify([snap.cycles, snap.updated, snap.settings, snap.settings_by, snap.running, snap.verdict, snap.advice, snap.source.kind, snap.baseline_ready]);
    if (force || s !== sig) { sig = s; renderMonitor(); }
  }

  paintTabs();
  await poll(true);
  if (tab === 'settings') buildSettings();
  timer = setInterval(poll, POLL_MS);
  view.cleanup = () => { clearInterval(timer); if (chartNode) purge(chartNode); };
  return view;
}
