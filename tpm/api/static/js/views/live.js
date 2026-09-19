/* View 7: live monitor — a page of its own that needs no run. Follows a data file that keeps growing and re-checks
   every sensor once per analysis cycle. Two tabs: Monitor (what is happening) and Settings (sensitivity with "let the
   AI decide", simulate with a file, live data link). The server sends message keys + variables; this file translates.

   When something is wrong the page shows ONE alarm card with a clear path: ALARM (what tripped, when) -> PROBLEM
   (which sensors behave how) -> CAUSE (the known failure type that matches, or "generic drift") -> SUGGESTION. The
   known failure types come from config/failure_signatures.yaml (+ files a judge drops into the monitor's folder) and
   are matched on sensor behaviour by the server; this file only draws them. In the basic role the page shows the
   alarm card, the overall verdict and one chart, nothing else. */
import { state, t, el, clear, api, fmt, chip, section, table, viewHead, empty, kv, st, toast, errText, roleAllows, notice, hiddenHint, actorName, store } from '../core.js';
import { plot, purge, tokens, sparkline } from '../charts.js';

const POLL_MS = 3000;
const CHECKS = ['level', 'trend', 'noise', 'range'];
const DEFAULTS = { percent: { watch: 1, alarm: 2 }, spreads: { watch: 1.5, alarm: 3 } };

// ---------------------------------------------------------------- text
const num = (x) => (x === null || x === undefined || x === '' || isNaN(x) ? '–' : String(+Number(x).toPrecision(4)));
const unitOf = (u) => (u === 'sp' ? ' ' + t('live.unit.sp') : u);
/** One server message {key, vars, text} in the current language (the server's English text while a key is not translated). */
const tm = (m) => { if (!m) return ''; const k = 'live.msg.' + m.key; const s = t(k, Object.assign({}, m.vars, m.vars && m.vars.u !== undefined ? { u: unitOf(m.vars.u) } : {})); return s === k && m.text ? m.text : s; };
const listOf = (items) => (items || []).map((i) => `${i.sensor} (${i.checks.map((c) => t('live.check.' + c)).join(', ')})`).join(', ');
function verdictText(v) {
  if (!v) return '';
  const one = (key, vars, items) => tm({ key, vars: Object.assign({}, vars, items ? { list: listOf(items) } : {}) });
  if (v.parts) return v.parts.map((p) => one(p.key, p.vars, p.items)).join(' ');
  return one(v.key, v.vars, v.items);
}
/** A known failure type's name / suggestion in the current language (the catalogue may carry translations). */
const sigName = (x) => (x && ((x.name_i18n && x.name_i18n[state.lang]) || x.name)) || '';
/** A generic pattern's name without its "Generic:" prefix ("mean shift on some sensors"): it is no known failure type. */
const bareName = (x) => sigName(x).replace(/^[^:]{1,24}:\s*/, '');
const sugText = (sug) => (sug && ((sug.text_i18n && sug.text_i18n[state.lang]) || sug.text)) || '';
const pct = (x) => Math.round(Number(x || 0) * 100);
// English fallbacks of this page's newest keys (the i18n files are edited by several people at once)
const FB = {
  'live.status.finished': 'finished: no more rows will arrive; the last analysis stays on screen',
  'live.alarm.titleImminent': 'Early warning',
  'live.sig.coveredBy': 'same sensors as {name}, which explains it',
  'live.event.replaces': '(replaces the earlier generic alert: {list})',
  'live.alarm.trip.genericOccurring': 'The process is drifting: {name}',
  'live.alarm.trip.genericImminent': 'The process is starting to drift: {name}',
  'live.alarm.toast.genericImminent': 'Warning: the process is starting to drift',
  'live.event.generic.occurring': 'The process is drifting: {name} (match {score}%) on {sensors}.',
  'live.event.generic.imminent': 'The process is starting to drift: {name} (match {score}%) on {sensors}.',
  'live.alarm.trip.towards': 'The process is drifting towards a known failure type: {name}',
  'live.alarm.toast.towards': 'ALARM: drifting towards {name}',
};
const tx = (k, vars) => { let v = t(k, vars); if (!v || v === k) { v = FB[k] || k; for (const [a, b] of Object.entries(vars || {})) v = v.replaceAll(`{${a}}`, String(b)); } return v; };
const NOTICE_KIND = { ok: 'ok', watch: 'warn', drift: 'fail', quality: 'warn', untrusted: 'fail', learn: '', wait: '' };
const CHIP_KIND = { ok: 'ok', watch: 'warn', alarm: 'fail', dead: 'fail', missing: 'fail' };
const SIG_KIND = { quiet: '', imminent: 'warn', occurring: 'fail', na: '', invisible: '' };
const LEVEL_KIND = ['', 'warn', 'fail'];

// ---------------------------------------------------------------- alarm toasts (once per alarm, remembered across pages)
/** The toast of an alarm card. Used by this page and by the app-wide watcher (app.js), so an alarm reaches the user on
    every page. */
export function alarmToast(a) {
  const trip = a.trip || {};
  if (a.kind === 'drift') return t('live.alarm.toast.drift');
  if (a.kind === 'quality') return t('live.alarm.toast.quality');
  if (a.cause && a.cause.generic) return a.kind === 'imminent' ? tx('live.alarm.toast.genericImminent') : t('live.alarm.toast.drift');
  if (trip.key === 'sig.towards') return tx('live.alarm.toast.towards', { name: sigName(trip) });
  return t('live.alarm.toast.' + a.kind, { name: sigName(trip) });
}
/** Toast a new alarm once (the id changes when the alarm or its cause changes); returns true when it was new. */
export function noteAlarm(a) {
  const id = a ? a.id : '';
  if (id === store.get('live.lastAlarm', '')) return false;
  store.set('live.lastAlarm', id);
  if (a) toast(alarmToast(a), a.kind === 'imminent' || a.kind === 'quality' ? 'warn' : 'fail');
  return !!a;
}

export async function render(main, params) {
  const view = el('div', { class: 'view' });
  main.append(view);
  view.append(viewHead('7', t('nav.live')));
  view.append(el('p', { class: 'hint', text: t('live.intro') }));
  const full = roleAllows('operator');                 // basic role: alarm card + verdict + one chart only
  if (!full) view.append(el('p', { class: 'small dim', text: t('live.basic.hint') }));

  let tab = full && params && params.tab === 'settings' ? 'settings' : (full ? store.get('live.tab', 'monitor') : 'monitor');
  let snap = null;
  let sel = null;
  let filter = 'all';
  let sig = '';
  let timer = null;
  let chartNode = null;
  let settingsUI = null;
  let dirty = false;
  let sigCatalogue = null;                             // GET /api/live/signatures, for the inject chooser

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
      bits.push(chip(t('live.src.' + s.kind), 'info'), ' ', el('b', { text: s.name_msg ? tm(s.name_msg) : s.name }), ' ');
      bits.push(el('span', { class: 'small dim', text: t('live.status.rows', { n: fmt.int(s.rows) }) + ' · ' + t('live.status.every', { every: fmt.dur(snap.cfg.interval), rows: fmt.int(snap.expected_rows) }) + ' · ' + (!snap.running ? tx('live.status.finished') : snap.baseline_ready ? t('live.status.monitoring') : t('live.status.learning', { done: snap.learning, total: snap.cfg.baseline_cycles })) }));
      if (snap.next_at && snap.running) bits.push(el('span', { class: 'small dim', text: ' · ' + t('live.status.next', { time: fmt.time(snap.next_at) }) }));
    }
    statusBox.append(el('div', {}, bits));
    // the server sends the status line as message keys (translated here); its English text is the fallback
    if (s.detail && s.kind !== 'none') statusBox.append(el('div', { class: 'small dim', text: (s.detail_msg || []).length ? s.detail_msg.map(tm).join(' ') : s.detail }));
    if (s.error) statusBox.append(el('div', { class: 'notice fail small', text: s.error }));
    if (settingsUI) settingsUI.stop.hidden = s.kind === 'none';
  }

  // ---------------------------------------------------------------- the alarm card: alarm -> problem -> cause -> suggestion
  function alarmCard(s) {
    const a = s.alarm;
    if (!a) return null;
    const trip = a.trip || {};
    const cause = a.cause || {};
    const sug = a.suggestion || {};
    const kindCls = a.kind === 'imminent' ? 'imminent' : a.kind === 'quality' ? 'quality' : 'occurring';
    const step = (n, label, ...body) => el('div', { class: 'alarm-step step-' + label }, el('div', { class: 'step-label' }, el('span', { class: 'step-num', text: String(n) }), t('live.alarm.step.' + label)), el('div', { class: 'step-body' }, ...body));

    // 1. ALARM: what tripped, when
    const generic = !!(cause.sig && cause.generic);
    const tripText = trip.key === 'drift' ? t('live.alarm.trip.drift', { n: trip.n }) : trip.key === 'quality' ? t('live.alarm.trip.quality', { n: trip.n })
      : trip.key === 'sig.towards' ? tx('live.alarm.trip.towards', { name: sigName(trip) })
      : generic ? tx(a.kind === 'imminent' ? 'live.alarm.trip.genericImminent' : 'live.alarm.trip.genericOccurring', { name: bareName(trip) }) : t('live.alarm.trip.' + a.kind, { name: sigName(trip) });
    const alarmStep = step(1, 'alarm', el('b', { text: tripText }), el('span', { class: 'sure', text: t('live.alarm.since', { time: fmt.time(a.since) }) + (trip.score !== undefined ? ' · ' + t('live.alarm.match', { score: pct(trip.score) }) : '') }));

    // 2. PROBLEM: which sensors behave how, with a sparkline of their cycle averages
    const k = tokens();
    const lines = (a.problem || []).map((p) => {
      const how = p.how ? tm(p.how) : (p.notes || []).map(tm).join(' ');
      const vals = (p.spark || []).filter((v) => v !== null && v !== undefined);
      return el('div', { class: 'sensor-line' }, el('b', { text: p.sensor }), vals.length > 1 ? sparkline(p.spark, { w: 90, h: 24, color: p.status === 'ok' ? k.info : k.fail, threshold: p.mu }) : null, el('span', { class: 'how', text: how }));
    });
    const problemStep = step(2, 'problem', lines.length ? lines : el('span', { class: 'dim', text: '–' }));

    // 3. CAUSE: the known failure type that matches, how sure, or "no known failure type matches"
    const causeBody = [];
    if (cause.sig && !cause.generic) {
      causeBody.push(el('b', { text: sigName(cause) }));
      causeBody.push(el('span', { class: 'sure', text: t('live.alarm.cause.sure.' + (cause.confidence || 'hypothesis'), { score: pct(cause.score) }) }));
      if (cause.partial) causeBody.push(el('span', { class: 'sure', text: t('live.alarm.cause.partial', { present: cause.n, n: ((s.signatures || []).find((x) => x.id === cause.sig) || { columns: [] }).columns.length }) }));
      if (cause.status === 'imminent') causeBody.push(el('span', { class: 'sure', text: t('live.alarm.cause.imminentNote', { dir: t('live.sig.dir.' + (cause.direction || 'flat')) }) }));
    } else if (cause.sig && cause.generic) {
      causeBody.push(el('b', { text: t('live.alarm.cause.generic', { name: bareName(cause) }) }));
      causeBody.push(el('span', { class: 'sure', text: t('live.alarm.match', { score: pct(cause.score) }) }));
    } else if (cause.quality) {
      causeBody.push(el('b', { text: t('live.alarm.cause.quality') }));
    } else {
      causeBody.push(el('b', { text: t('live.alarm.cause.none') }));
    }
    const also = (cause.also || []).filter((x) => x.sig);
    if (also.length) causeBody.push(el('span', { class: 'sure', text: t('live.alarm.cause.also', { list: also.map((x) => `${sigName(x)} (${t('live.sig.status.' + x.status)}, ${pct(x.score)}%)`).join('; ') }) }));
    const causeStep = step(3, 'cause', ...causeBody);

    // 4. SUGGESTION: the failure type's advice, or generic advice
    const text = sugText(sug) || (sug.key === 'generic.quality' ? t('live.alarm.sug.generic.quality') : t('live.alarm.sug.generic.drift'));
    const sugStep = step(4, 'suggestion', a.kind === 'imminent' ? el('div', { class: 'small dim', text: t('live.alarm.sug.imminent') }) : null, el('div', { class: 'step-sug', text: text }));

    return el('div', { class: 'alarm-card ' + kindCls, role: 'alert' },
      el('div', { class: 'alarm-head' }, el('h2', {}, el('span', { class: 'alarm-lamp', 'aria-hidden': 'true' }), a.kind === 'imminent' ? tx('live.alarm.titleImminent') : t('live.alarm.title') + ': ' + t('live.vl.' + (a.kind === 'quality' ? 'quality' : 'drift'))), el('span', { class: 'small dim', text: fmt.time(a.t) })),
      el('div', { class: 'alarm-steps' }, alarmStep, problemStep, causeStep, sugStep),
      full ? el('div', { class: 'alarm-foot', text: t('live.alarm.foot', { time: fmt.time(a.since) }) }) : null);
  }

  // ---------------------------------------------------------------- known failure types: how close is the process to each
  function sigPanel(s) {
    const sigs = s.signatures || [];
    const sec = section(t('live.sig.title'));
    sec.body.append(el('p', { class: 'hint', text: t('live.sig.hint') }));
    if (!sigs.length) { sec.body.append(empty(t('live.sig.none'))); return sec.root; }
    const row = (r) => {
      const sub = [t('live.sig.pat.' + r.pattern), t('live.sig.conf.' + r.confidence)];
      const cols = r.status === 'na' || r.status === 'invisible' ? (r.columns.length ? t('live.sig.missing', { list: r.missing.length ? r.missing.join(', ') : r.columns.join(', ') }) : t('live.sig.pat.none'))
        : (r.generic ? t('live.sig.generic') + (r.resolved.length ? ': ' + r.resolved.map((x) => x.sensor).join(', ') : '') : t('live.sig.cols', { list: r.resolved.map((x) => x.sensor + (x.ind >= 0.5 ? ' ✓' : '')).join(', ') }) + (r.missing.length ? ' · ' + t('live.sig.missing', { list: r.missing.join(', ') }) : ''));
      const scored = r.status !== 'na' && r.status !== 'invisible';
      const cover = r.covered_by ? (sigs.find((x) => x.id === r.covered_by) || { name: r.covered_by }) : null;
      return el('div', { class: 'sig-row ' + r.status + (cover ? ' covered' : ''), title: r.note || '' },
        el('div', {}, el('div', { class: 'sig-name', text: sigName(r) }), el('div', { class: 'sig-sub', text: sub.join(' · ') })),
        chip(t('live.sig.status.' + r.status), cover ? '' : SIG_KIND[r.status]),
        el('div', {}, scored ? el('div', { class: 'sig-score' }, el('span', { class: 'sig-bar' }, el('i', { style: { width: pct(r.score) + '%' } })), el('span', { text: pct(r.score) + '%' })) : null, el('div', { class: 'sig-cols', text: cols }),
          cover ? el('div', { class: 'sig-cols', text: tx('live.sig.coveredBy', { name: sigName(cover) }) }) : null),
        el('div', { class: 'sig-dir', text: scored ? t('live.sig.dir.' + r.direction) + (r.matched && !r.generic ? ` · ${r.matched}/${r.n}` : '') : '' }));
    };
    const active = sigs.filter((r) => r.status !== 'na' && r.status !== 'invisible');
    const rest = sigs.filter((r) => r.status === 'na' || r.status === 'invisible');
    sec.body.append(el('div', { class: 'sig-list' }, active.map(row)));
    if (rest.length) sec.body.append(el('details', { style: { marginTop: '8px' } }, el('summary', { class: 'small dim', style: { cursor: 'pointer' }, text: t('live.sig.more', { n: rest.length }) }), el('div', { class: 'sig-list', style: { marginTop: '6px' } }, rest.map(row))));
    sec.body.append(el('p', { class: 'live-sig-note', text: t('live.sig.dir', { dir: s.signature_dir || '' }) }));
    return sec.root;
  }

  // ---------------------------------------------------------------- tab 1: monitor
  function renderMonitor() {
    const c = clear(content);
    if (chartNode) { purge(chartNode); chartNode = null; }
    const s = snap;
    if (!s) return;
    const v = s.verdict || {};
    c.append(el('div', { class: 'notice ' + (NOTICE_KIND[v.level] || ''), role: 'status' }, el('b', { text: t('live.vl.' + v.level) + '. ' }), verdictText(v)));
    const startRow = () => el('div', { class: 'row' },
      roleAllows('engineer') ? el('button', { class: 'btn btn-primary', type: 'button', onClick: () => startDemo('plant') }, t('live.sim.demo')) : el('span', { class: 'small dim', text: t('live.empty.role') }),
      roleAllows('engineer') ? el('button', { class: 'btn', type: 'button', title: t('live.demo.teHint'), onClick: () => startDemo('te') }, t('live.demo.te')) : null,
      full ? el('button', { class: 'btn', type: 'button', onClick: () => showTab('settings') }, t('live.empty.settings')) : null);
    if (!s.running && !s.baseline_ready) {
      const empt = el('div', { class: 'stack' }, el('h2', { text: t('live.empty.title') }), el('p', { class: 'muted', text: t('live.empty.body') }), startRow());
      c.append(el('section', { class: 'sec' }, el('div', { class: 'sec-body' }, empt)));
      return;
    }
    if (!s.running && roleAllows('engineer')) c.append(startRow());   // stopped or finished: start again without the Settings tab
    const card = alarmCard(s);
    if (card) c.append(card);
    const T = s.table || [];
    if (!full) {                                        // basic role: one chart of the most affected sensor, no tables
      if (!s.baseline_ready) return;
      const lead = (s.alarm && s.alarm.problem && s.alarm.problem.length ? s.alarm.problem[0].sensor : null) || ([...T].sort((a, b) => b.score - a.score)[0] || {}).sensor;
      if (lead) { const box = el('div', { class: 'live-basic-chart' }); c.append(el('section', { class: 'sec' }, el('div', { class: 'sec-head' }, el('h2', { text: t('live.basic.chart', { sensor: lead }) })), el('div', { class: 'sec-body' }, box))); drawChart(box, lead); }
      return;
    }
    const count = (k) => T.filter((r) => r.status === k).length;
    if (s.baseline_ready) c.append(el('div', { class: 'row' }, ['dead', 'missing', 'alarm', 'watch', 'ok'].map((k) => el('span', { class: 'row', style: { gap: '6px' } }, chip(String(count(k)), count(k) && k !== 'ok' ? CHIP_KIND[k] : (k === 'ok' ? 'ok' : ''), { style: { fontWeight: 600 } }), el('span', { class: 'small muted', text: t('live.count.' + k) })))));
    const by = s.settings_by || {};
    const u = s.settings.mode === 'percent' ? '%' : ' ' + t('live.unit.sp');
    c.append(el('p', { class: 'small muted' }, t('live.sens.now', { watch: s.settings.watch, alarm: s.settings.alarm, unit: u, by: t('live.sens.by.' + (by.by || 'default')) }), ' ',
      el('button', { class: 'btn btn-sm btn-quiet', type: 'button', onClick: () => showTab('settings') }, t('live.sens.change'))));
    if (s.advice) c.append(el('div', { class: 'notice' }, el('b', { text: t('live.advice.title') + ': ' }), s.advice, el('div', { class: 'small dim', text: t('live.advice.note') })));
    if (!s.baseline_ready) return;

    // known failure types
    c.append(sigPanel(s));

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
    if (e.kind === 'signature') {
      const was = (e.replaces || []).map((id) => sigName((snap.signatures || []).find((x) => x.id === id)) || id);
      const v = e.vars || {};
      const text = e.generic && (e.new === 'imminent' || e.new === 'occurring') ? tx('live.event.generic.' + e.new, { name: bareName(e), score: v.score, sensors: v.sensors }) : tm({ key: e.key, vars: Object.assign({}, v, { name: sigName(e) }) });
      return text + (was.length ? ' ' + tx('live.event.replaces', { list: was.join(', ') }) : '');
    }
    return tm(e);
  }

  /** The cycle-average chart of one sensor with the watch / alarm bands. */
  function drawChart(box, sensor) {
    const s = snap;
    const b = s.baseline && s.baseline[sensor];
    const r = (s.table || []).find((x) => x.sensor === sensor);
    if (!b || !r) return;
    const H = s.history || [];
    const ys = H.map((h) => h.mean[sensor]);
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
    drawChart(box, sel);
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
    ui.inject = el('select', {}, el('option', { value: '', text: t('live.sim.inject.none') }), el('option', { value: 'auto', text: t('live.sim.inject.auto') }));
    ui.injectAfter = inp('lv-sinject', { step: '1', min: '0', value: '0' });
    ui.simCalc = el('p', { class: 'small muted' });
    ui.simMsg = msg();
    const fillInject = () => { if (!sigCatalogue) return; for (const x of sigCatalogue.filter((x) => x.visible && x.pattern !== 'none')) ui.inject.append(el('option', { value: x.id, text: sigName(x) })); };
    if (sigCatalogue) fillInject(); else api('/api/live/signatures').then((r) => { if (r.ok) { sigCatalogue = r.data.items || []; fillInject(); } });
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
      const r = await api('/api/live/source/simulate', { method: 'POST', body: { path: ui.path.value, rate: ui.sRate.value, interval: ui.sInt.value, baseline_cycles: ui.sBase.value, start_row: ui.sStart.value, max_rows: ui.sRows.value, loop: ui.sLoop.checked, inject: ui.inject.value, inject_after: ui.injectAfter.value, actor: actor() } });
      ui.simGo.disabled = false;
      if (r.ok) { say(ui.simMsg, t('live.sim.started'), 'ok'); await poll(true); } else say(ui.simMsg, errText(r), 'fail');
    } }, '▶ ' + t('live.sim.go'));
    sim.body.append(el('p', { class: 'hint', text: t('live.sim.hint') }), drop, pick, ui.bar, ui.upMsg, field(t('live.sim.path'), ui.path),
      el('div', { class: 'row', style: { alignItems: 'flex-end', gap: '14px', marginTop: '8px' } }, field(t('live.sim.rate'), ui.sRate), field(t('live.sim.interval'), ui.sInt), field(t('live.sim.base'), ui.sBase), field(t('live.sim.start'), ui.sStart), field(t('live.sim.rows'), ui.sRows), el('label', { class: 'row small muted', style: { gap: '6px' } }, ui.sLoop, t('live.sim.loop'))),
      ui.simCalc,
      el('div', { class: 'row', style: { alignItems: 'flex-end', gap: '14px' } }, field(t('live.sim.inject'), ui.inject), field(t('live.sim.injectAfter'), ui.injectAfter)),
      el('p', { class: 'small dim', text: t('live.sim.injectHint') }),
      el('div', { class: 'row' }, ui.simGo,
        el('button', { class: 'btn', type: 'button', onClick: () => { ui.sRate.value = 10; ui.sInt.value = 20; ui.sBase.value = 2; calc(); } }, t('live.sim.fast')),
        el('button', { class: 'btn', type: 'button', onClick: () => { ui.sRate.value = 1; ui.sInt.value = 900; ui.sBase.value = 4; calc(); } }, t('live.sim.real')),
        el('button', { class: 'btn', type: 'button', onClick: () => startDemo('plant') }, t('live.sim.demo')),
        el('button', { class: 'btn', type: 'button', title: t('live.demo.teHint'), onClick: () => startDemo('te') }, t('live.demo.te'))),
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

  async function startDemo(scenario) {
    const r = await api('/api/live/source/demo', { method: 'POST', body: { actor: actorName(), scenario: scenario || 'plant' } });
    if (r.ok) { toast(t('live.sim.demoStarted'), 'ok'); showTab('monitor'); await poll(true); } else toast(errText(r), 'fail');
  }

  const checkAlarm = () => noteAlarm(snap && snap.alarm);

  // ---------------------------------------------------------------- tabs + polling
  function paintTabs() {
    clear(tabs);
    if (!full) return;
    for (const id of ['monitor', 'settings']) tabs.append(el('button', { type: 'button', 'aria-pressed': String(tab === id), onClick: () => showTab(id) }, t('live.tab.' + id)));
  }
  function showTab(id) {
    tab = full ? id : 'monitor';
    store.set('live.tab', tab);
    settingsUI = null;
    sig = '';
    paintTabs();
    if (tab === 'settings') buildSettings(); else renderMonitor();
    paintStatus();
  }
  async function poll(force) {
    const r = await api('/api/live/state');
    if (!r.ok) return;
    snap = r.data;
    paintStatus();
    checkAlarm();
    if (tab === 'settings') { if (settingsUI) settingsUI.syncSens(); return; }
    const s = JSON.stringify([snap.cycles, snap.updated, snap.settings, snap.settings_by, snap.running, snap.verdict, snap.advice, snap.source.kind, snap.baseline_ready, snap.alarm && snap.alarm.id, (snap.signatures || []).map((x) => x.id + x.status + x.score)]);
    if (force || s !== sig) { sig = s; renderMonitor(); }
  }

  paintTabs();
  await poll(true);
  if (tab === 'settings') buildSettings();
  timer = setInterval(poll, POLL_MS);
  view.cleanup = () => { clearInterval(timer); if (chartNode) purge(chartNode); };
  return view;
}
