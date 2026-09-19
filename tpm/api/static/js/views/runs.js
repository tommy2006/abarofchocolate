/* View 0: analyse a file, list runs, live stage progress with the time budget, live input (replay / watch).
   A chosen file is sent with XMLHttpRequest (fetch has no upload progress): a full-width screen shows bytes
   sent, speed, time remaining and Cancel, then hands over to the stage progress. A file given by path is read
   in place and the screen says so. The upload lives at module level, so it survives a visit to another view. */
import { state, t, el, clear, api, runApi, errText, toast, bus, fmt, st, section, confirmDialog, viewHead, empty, roleAllows, flash } from '../core.js';

const STAGES = ['ingest', 'profile', 'quality', 'detect', 'diagnose', 'assess', 'report'];
const RECENT_RUNS = 5;
let showAllRuns = false;       // the list repaints on every status event; the choice must survive that
let workspaceDir = '';
let handoverRun = null;        // run whose progress panel is highlighted once after an upload
let chosenFile = null;         // the picked file survives a visit to another view (and a cancelled or failed upload)

// ---------------------------------------------------------------- start of a run (upload or path), module level
let job = null;                // { mode, name, size, path, runId, xhr, phase, loaded, total, startedAt, samples, speed, eta, error, args }
const jobListeners = new Set();
const notifyJob = () => jobListeners.forEach((fn) => { try { fn(); } catch (e) { console.error(e); } });
const isLocalHost = () => ['localhost', '127.0.0.1', '[::1]', '::1', ''].includes(location.hostname);
function newRunId() {
  const d = new Date(); const p = (n) => String(n).padStart(2, '0');
  const rnd = Array.from(crypto.getRandomValues(new Uint8Array(3)), (b) => b.toString(16).padStart(2, '0')).join('');
  return `run_${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}_${rnd}`;
}
const warnUnload = (e) => { e.preventDefault(); e.returnValue = ''; };

function startJob(args) {
  const { file, path, fields, rulesFile } = args;
  const runId = newRunId();
  const fd = new FormData();
  fd.append('run_id', runId);                       // fields first: the server knows the run before the file arrives
  for (const [k, v] of fields) fd.append(k, v);
  if (rulesFile) fd.append('rules_file', rulesFile);
  if (file) fd.append('file', file, file.name); else fd.append('path', path);
  const xhr = new XMLHttpRequest();
  job = { mode: file ? 'upload' : 'path', name: file ? file.name : path.split(/[\\/]/).pop(), size: file ? file.size : 0, path: file ? '' : path, runId, xhr, phase: file ? 'sending' : 'checking', loaded: 0, total: file ? file.size : 0, startedAt: performance.now(), samples: [], speed: 0, eta: null, error: '', args };
  const mine = job;
  xhr.open('POST', '/api/runs');
  xhr.upload.addEventListener('progress', (e) => {
    if (job !== mine || !e.lengthComputable) return;
    const now = performance.now();
    mine.loaded = e.loaded; mine.total = e.total;
    mine.samples.push({ t: now, loaded: e.loaded });
    while (mine.samples.length > 2 && now - mine.samples[0].t > 5000) mine.samples.shift();   // speed over the last 5 s
    const first = mine.samples[0];
    const dt = (now - first.t) / 1000;
    if (dt > 0.25) { mine.speed = (e.loaded - first.loaded) / dt; mine.eta = mine.speed > 0 ? (e.total - e.loaded) / mine.speed : null; }
    notifyJob();
  });
  xhr.upload.addEventListener('load', () => { if (job === mine && mine.phase === 'sending') { mine.loaded = mine.total; mine.phase = 'storing'; notifyJob(); } });
  xhr.addEventListener('load', () => {
    if (job !== mine) return;
    window.removeEventListener('beforeunload', warnUnload);
    let data = {};
    try { data = JSON.parse(xhr.responseText || '{}'); } catch { data = { detail: (xhr.responseText || '').slice(0, 300) }; }
    if (xhr.status >= 200 && xhr.status < 300 && data.run_id) {
      mine.phase = 'starting'; mine.runId = data.run_id; mine.seconds = (performance.now() - mine.startedAt) / 1000;
      notifyJob();
      setTimeout(() => finishJob(mine), 1600);       // long enough to read "Upload complete", then the stage progress takes over
    } else failJob(mine, errText({ status: xhr.status, data }), xhr.status);
  });
  xhr.addEventListener('error', () => { window.removeEventListener('beforeunload', warnUnload); if (job === mine) failJob(mine, t('upload.connectionLost'), 0); });
  xhr.addEventListener('abort', () => {
    window.removeEventListener('beforeunload', warnUnload);
    if (job !== mine) return;
    job = null; notifyJob();
    toast(t('upload.cancelled'), 'warn');
    // The server drops a partial upload by itself (it never becomes a run). Only when every byte had already left
    // can the run exist: then delete it, or say that its analysis has started.
    if (mine.total > 0 && mine.loaded >= mine.total) api(`/api/runs/${encodeURIComponent(mine.runId)}`, { method: 'DELETE' }).then((d) => { if (d.status === 409) toast(t('upload.cancelTooLate', { id: mine.runId }), 'warn'); });
  });
  if (file) window.addEventListener('beforeunload', warnUnload);
  xhr.send(fd);
  notifyJob();
}
function failJob(j, message, status) { j.phase = 'failed'; j.error = message; j.status = status; notifyJob(); }
function cancelJob() { if (job && job.xhr && (job.phase === 'sending' || job.phase === 'checking')) job.xhr.abort(); }
function dismissJob() { job = null; notifyJob(); }
async function finishJob(j) {
  if (job !== j) return;
  job = null;
  chosenFile = null;
  handoverRun = j.runId;
  toast(t('runs.started', { id: j.runId }), 'ok');
  const r = await api('/api/runs');
  if (r.ok) { state.runs = r.data.runs || []; bus.emit('runs.loaded', state.runs); }
  bus.emit('run.select', j.runId);                   // re-renders the view: the stage progress panel takes over
}

/** The full-width screen shown while a run is being started. Built once, updated in place on every progress event. */
function jobScreen() {
  const root = el('section', { class: 'upload-screen', 'aria-labelledby': 'upload-title' });
  const title = el('h2', { id: 'upload-title' });
  const sub = el('p', { class: 'upload-sub muted' });
  const pct = el('div', { class: 'upload-pct', 'aria-hidden': 'true' });
  const fill = el('i');
  const bar = el('div', { class: 'upload-bar', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-labelledby': 'upload-title' }, fill);
  const stat = (label) => { const v = el('dd'); return { node: el('div', {}, el('dt', { text: label }), v), v }; };
  const sSent = stat(t('upload.sent')); const sSpeed = stat(t('upload.speed')); const sLeft = stat(t('upload.remaining')); const sElapsed = stat(t('common.elapsed'));
  const stats = el('dl', { class: 'upload-stats' }, sSent.node, sSpeed.node, sLeft.node, sElapsed.node);
  const phase = el('p', { class: 'upload-phase', role: 'status', 'aria-live': 'polite' });
  const local = el('p', { class: 'upload-local' });
  const errBox = el('div', { class: 'notice fail upload-error', hidden: true });
  const cancelBtn = el('button', { class: 'btn btn-danger', type: 'button', onClick: cancelJob }, t('upload.cancel'));
  const backBtn = el('button', { class: 'btn', type: 'button', onClick: dismissJob }, t('upload.back'));
  const retryBtn = el('button', { class: 'btn btn-primary', type: 'button', onClick: () => { const a = job && job.args; if (a) startJob(a); } }, t('upload.retry'));
  const actions = el('div', { class: 'row upload-actions' }, cancelBtn, retryBtn, backBtn);
  root.append(el('div', { class: 'upload-head' }, el('div', { class: 'upload-titles' }, title, sub), pct), bar, stats, phase, errBox, local, actions);
  let lastPaint = 0; let lastPhase = '';
  const paint = (force = false) => {
    const j = job; if (!j) return;
    const now = performance.now();
    if (!force && j.phase === lastPhase && now - lastPaint < 200) return;   // progress events come every few ms; text stays readable
    lastPaint = now; lastPhase = j.phase;
    const upload = j.mode === 'upload';
    root.dataset.phase = j.phase; root.dataset.mode = j.mode;
    title.textContent = upload ? t(j.phase === 'storing' || j.phase === 'starting' ? 'upload.titleDone' : 'upload.title', { name: j.name }) : t('upload.titlePath', { name: j.name });
    sub.textContent = upload ? t('upload.fileSize', { size: fmt.bytes(j.size) }) : j.path;
    const frac = upload ? (j.total ? Math.min(1, j.loaded / j.total) : 0) : (j.phase === 'starting' ? 1 : 0);
    const done = j.phase === 'storing' || j.phase === 'starting';
    fill.style.width = ((done ? 1 : frac) * 100).toFixed(2) + '%';
    bar.setAttribute('aria-valuenow', String(Math.round((done ? 1 : frac) * 100)));
    bar.classList.toggle('indeterminate', j.phase === 'checking');
    pct.textContent = upload ? fmt.pct(done ? 1 : frac, frac > 0 && frac < 0.1 ? 1 : 0) : '';
    stats.hidden = !upload;
    if (upload) {
      const elapsed = (j.seconds !== undefined ? j.seconds : (now - j.startedAt) / 1000);
      sSent.v.textContent = t('upload.sentOf', { sent: fmt.bytes(Math.min(j.loaded, j.size)), total: fmt.bytes(j.size) });
      sSpeed.v.textContent = j.phase === 'sending' ? (j.speed ? fmt.bytes(j.speed) + '/s' : '–') : (elapsed > 0 ? t('upload.average', { speed: fmt.bytes(j.size / elapsed) + '/s' }) : '–');
      sLeft.v.textContent = j.phase === 'sending' ? (j.eta === null ? t('upload.estimating') : fmt.dur(j.eta)) : (j.phase === 'failed' ? '–' : fmt.dur(0));
      sElapsed.v.textContent = fmt.dur(elapsed);
    }
    phase.textContent = { sending: t('upload.phase.sending'), storing: t('upload.phase.storing'), checking: t('upload.inPlace'), starting: upload ? t('upload.complete') : t('upload.completePath'), failed: upload ? t('upload.failed') : t('upload.failedPath') }[j.phase] || '';
    errBox.hidden = j.phase !== 'failed';
    if (j.phase === 'failed') errBox.textContent = j.error || '';
    local.textContent = upload ? (isLocalHost() ? t('upload.staysLocal', { dir: workspaceDir || 'workspace' }) : t('upload.staysServer', { host: location.host, dir: workspaceDir || 'workspace' })) : t('upload.inPlaceHelp');
    cancelBtn.hidden = j.phase !== 'sending';       // a path run answers in milliseconds: nothing to cancel
    retryBtn.hidden = backBtn.hidden = j.phase !== 'failed';
  };
  root.paint = paint;
  return root;
}

export async function render(main) {
  const view = el('div', { class: 'view' });
  main.append(view);
  view.append(viewHead('0', t('nav.runs')));
  const jobHost = el('div', { class: 'upload-host', hidden: true });
  view.append(jobHost);
  const grid = el('div', { class: 'cols cols-side' });
  view.append(grid);
  const left = el('div', { class: 'stack' });
  const right = el('div', { class: 'stack' });
  grid.append(left, right);

  // ---- new run form
  left.append(el('h2', { text: t('runs.new') }));
  let file = chosenFile;
  const drop = el('div', { class: 'drop', tabindex: '0', role: 'button' }, el('div', { text: t('runs.drop') }), el('div', { class: 'file' }), el('div', { class: 'small dim', text: t('runs.dropHelp') }));
  const fileInput = el('input', { type: 'file', hidden: true });
  const showFile = () => { chosenFile = file; drop.querySelector('.file').textContent = file ? `${file.name} (${fmt.bytes(file.size)})` : ''; };
  drop.addEventListener('click', () => fileInput.click());
  drop.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); } });
  fileInput.addEventListener('change', () => { file = fileInput.files[0] || null; showFile(); });
  ['dragenter', 'dragover'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', (e) => { if (e.dataTransfer.files[0]) { file = e.dataTransfer.files[0]; showFile(); } });
  const pathIn = el('input', { type: 'text', placeholder: 'C:\\data\\process.csv', style: { width: '100%' } });
  const opts = {
    has_header: el('input', { type: 'checkbox', checked: true }),
    delimiter: el('input', { type: 'text', placeholder: ', ; tab space', style: { width: '120px' } }),
    transposed: el('input', { type: 'checkbox' }),
    group_columns: el('input', { type: 'text', style: { width: '100%' } }),
    time_column: el('input', { type: 'text', style: { width: '100%' } }),
    domain_hint: el('input', { type: 'text', style: { width: '100%' } }),
    rules_file: el('input', { type: 'file', accept: '.md,.txt,.rules' }),
    language: el('select', {}, (state.settings ? state.settings.languages : ['en', 'fi', 'sv']).map((l) => el('option', { value: l, text: l.toUpperCase() }))),
    profile: el('select', {}, Object.keys(state.settings ? state.settings.profiles : { 'no-egress': 1 }).map((p) => el('option', { value: p, text: p }))),
  };
  opts.language.value = state.lang;
  if (state.settings) opts.profile.value = state.settings.profile;
  const details = el('details', {}, el('summary', { text: t('runs.options') }),
    el('div', { class: 'stack', style: { marginTop: '10px' } },
      el('label', { class: 'check' }, opts.has_header, t('runs.hasHeader')),
      el('label', { class: 'check' }, opts.transposed, t('runs.transposed')),
      el('label', { class: 'field' }, el('span', { text: t('runs.delimiter') }), opts.delimiter),
      el('label', { class: 'field' }, el('span', { text: t('runs.groupColumns') }), opts.group_columns),
      el('label', { class: 'field' }, el('span', { text: t('runs.timeColumn') }), opts.time_column),
      el('label', { class: 'field' }, el('span', { text: t('runs.domainHint') }), opts.domain_hint),
      el('label', { class: 'field' }, el('span', { text: t('runs.rulesFile') }), opts.rules_file),
      el('div', { class: 'row' }, el('label', { class: 'field' }, el('span', { text: t('runs.language') }), opts.language), el('label', { class: 'field' }, el('span', { text: t('runs.profile') }), opts.profile))));
  const startBtn = el('button', { class: 'btn btn-primary', type: 'button' }, t('runs.start'));
  const demoBtn = el('button', { class: 'btn btn-quiet', type: 'button' }, t('runs.demo'));
  startBtn.addEventListener('click', () => {
    const path = pathIn.value.trim();
    if (!file && !path) { toast(t('runs.drop'), 'fail'); return; }
    const fields = [['has_header', opts.has_header.checked ? 'true' : 'false'], ['transposed', opts.transposed.checked ? 'true' : 'false']];
    for (const k of ['delimiter', 'group_columns', 'time_column', 'domain_hint']) if (opts[k].value.trim()) fields.push([k, opts[k].value.trim()]);
    fields.push(['language', opts.language.value], ['profile', opts.profile.value]);
    startJob({ file, path, fields, rulesFile: opts.rules_file.files[0] || null });
  });
  demoBtn.addEventListener('click', async () => {
    // runs the real pipeline on samples/demo_process.csv in the background (same path as "Start analysis")
    demoBtn.disabled = true;
    const r = await api('/api/demo', { method: 'POST', body: { language: opts.language.value, profile: opts.profile.value } });
    demoBtn.disabled = false;
    if (!r.ok) { toast(errText(r), 'fail'); return; }
    toast(t('runs.demoStarted', { id: r.data.run_id, src: (r.data.source_path || '').split(/[\\/]/).pop() }), 'ok');
    await refreshRuns();
    bus.emit('run.select', r.data.run_id);
  });
  showFile();
  left.append(drop, fileInput, el('label', { class: 'field' }, el('span', { text: t('runs.orPath') }), pathIn, el('span', { class: 'small dim', style: { marginTop: '3px' }, text: t('runs.pathHelp') })), details, el('div', { class: 'row' }, startBtn, demoBtn));

  // ---- runs list
  right.append(el('h2', { text: t('runs.list') }));
  const listEl = el('div', { class: 'stack runlist' });
  right.append(listEl);
  const progress = section(t('runs.progress'));
  view.append(progress.root);
  const stream = section(t('stream.title'), { level: 'engineer' });
  view.append(stream.root);

  const runRow = (r) => {
    const row = el('div', { class: 'runrow' + (r.run_id === state.run ? ' sel' : ''), role: 'button', tabindex: '0', dataset: { run: r.run_id } });
    const mini = el('span', { class: 'mini-stages', title: t('runs.stages') }, STAGES.map((s) => { const sg = (r.stages || []).find((x) => x.stage === s); return el('i', { class: sg ? sg.state : '' }); }));
    row.append(
      el('div', {}, el('div', { class: 'id' }, st(r.state, ''), ' ', r.run_id, ' ', mini),
        el('div', { class: 'meta' }, el('span', { text: `${t('runs.source')}: ${(r.source_path || '').split(/[\\/]/).pop()}` }), el('span', { text: fmt.ts(r.created_at) }), el('span', { text: t('runs.flags', { n: r.n_flags || 0 }) }), r.profile ? el('span', { text: r.profile }) : null, r.meta && r.meta.fake ? el('span', { text: 'demo' }) : null)),
      el('div', { class: 'row' }, el('button', { class: 'btn btn-sm', type: 'button', onClick: (e) => { e.stopPropagation(); bus.emit('run.select', r.run_id); } }, r.run_id === state.run ? t('runs.selected') : t('runs.select')),
        roleAllows('engineer') ? el('button', { class: 'btn btn-sm btn-quiet btn-danger', type: 'button', onClick: async (e) => { e.stopPropagation(); if (await confirmDialog(t('runs.deleteConfirm', { id: r.run_id }), { danger: true, okLabel: t('common.delete') })) { const d = await api(`/api/runs/${r.run_id}`, { method: 'DELETE' }); if (d.ok) { toast(t('common.delete') + ': ' + r.run_id); if (state.run === r.run_id) bus.emit('run.select', null); await refreshRuns(); } else toast(errText(d), 'fail'); } } }, t('common.delete')) : null));
    row.addEventListener('click', () => bus.emit('run.select', r.run_id));
    row.addEventListener('keydown', (e) => { if (e.key === 'Enter' && e.target === row) bus.emit('run.select', r.run_id); });
    return row;
  };
  /** The five most recent runs; older ones behind "Show more past runs (N)". The selected run stays visible even
      when it is older: it is listed under the recent ones, marked as older. */
  const renderList = () => {
    clear(listEl);
    const runs = state.runs || [];
    if (!runs.length) { listEl.append(empty(t('runs.none'))); return; }
    const recent = runs.slice(0, RECENT_RUNS);
    const older = runs.slice(RECENT_RUNS);
    for (const r of (showAllRuns ? runs : recent)) listEl.append(runRow(r));
    if (!older.length) return;
    if (!showAllRuns) {
      const sel = older.find((r) => r.run_id === state.run);
      if (sel) listEl.append(el('div', { class: 'runlist-older small dim', text: t('runs.selectedOlder') }), runRow(sel));
    }
    const hidden = older.length - (!showAllRuns && older.some((r) => r.run_id === state.run) ? 1 : 0);
    const more = el('button', { class: 'btn btn-quiet runlist-more', type: 'button', 'aria-expanded': String(showAllRuns), onClick: () => { showAllRuns = !showAllRuns; renderList(); if (!showAllRuns) listEl.scrollIntoView({ block: 'nearest' }); } }, showAllRuns ? t('runs.showFewer') : t('runs.showMore', { n: hidden }));
    if (showAllRuns || hidden > 0) listEl.append(more);
  };
  const refreshRuns = async () => { const r = await api('/api/runs'); if (r.ok) { state.runs = r.data.runs || []; workspaceDir = r.data.workspace_dir || workspaceDir; bus.emit('runs.loaded', state.runs); } renderList(); };

  const renderProgress = (s) => {
    clear(progress.body);
    if (!s) { progress.body.append(empty(t('runs.noRunHint'))); return; }
    const stages = el('div', { class: 'stages' });
    (s.stages || []).forEach((sg, i) => {
      stages.append(el('div', { class: 'stage ' + sg.state }, el('span', { class: 'num', text: String(i + 1) }), st(sg.state, t('runs.stage.' + sg.stage)), el('span', { class: 'bar' }, el('i', { style: { width: ((sg.state === 'done' ? 1 : sg.progress || 0) * 100) + '%' } })), el('span', { class: 'msg', title: sg.error || sg.message || '', text: sg.error ? (sg.message || 'failed') : (sg.message || t('runs.state.' + sg.state)) })));
    });
    const started = s.stages && s.stages.find((x) => x.started_at);
    const first = started ? new Date(started.started_at) : new Date(s.created_at);
    const lastDone = (s.stages || []).filter((x) => x.finished_at).map((x) => new Date(x.finished_at)).sort((a, b) => b - a)[0];
    const end = s.state === 'running' || s.state === 'pending' ? new Date() : (lastDone || new Date(s.updated_at));
    const elapsed = Math.max(0, (end - first) / 1000);
    const budget = s.time_budget_s || 1200;
    progress.body.append(el('div', { class: 'row between' }, el('span', {}, st(s.state, t('runs.state.' + s.state)), ' ', el('b', { text: s.run_id })), el('span', { class: 'small muted', style: { overflowWrap: 'anywhere', minWidth: '0' }, text: s.source_path || '' })), stages,
      el('div', { class: 'budget' }, el('span', { text: `${t('common.elapsed')} ${fmt.sec(elapsed)}` }), el('span', { class: 'bar' }, el('i', { style: { width: Math.min(100, elapsed / budget * 100) + '%', background: elapsed > budget ? 'var(--fail)' : '' } })), el('span', { text: `${t('common.timeBudget')} ${fmt.sec(budget)}` })));
    if (s.state === 'running' || s.state === 'pending') progress.body.append(el('p', { class: 'small muted', style: { marginTop: '8px' }, text: t('runs.unlockHint') }));
    // the job error lives in memory only until the server restarts; status.error is the persisted copy
    const runError = (s.job && s.job.error) || s.error || (s.status && s.status.error);
    if (runError) progress.body.append(el('pre', { class: 'notice fail small', style: { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxWidth: '100%' }, text: String(runError) }));
  };

  const renderStream = async () => {
    clear(stream.body);
    if (!state.run) { stream.body.append(empty(t('runs.noRunHint'))); return; }
    const speed = el('input', { type: 'number', value: '2', min: '0.1', step: '0.5', style: { width: '90px' } });
    const maxb = el('input', { type: 'number', value: '10', min: '1', style: { width: '90px' } });
    const folder = el('input', { type: 'text', placeholder: 'C:\\incoming', style: { width: '260px' } });
    const status = el('div', { class: 'small muted' });
    const refreshStatus = async () => { const r = await runApi('/stream/status'); if (r.ok) { const d = r.data; clear(status); status.append(`${t('stream.pushed')}: ${d.pushed || 0}`, d.last ? ` — ${t('stream.last')}: ${d.last.batch_id} (${d.last.n_rows} rows, ${(d.last.flags || []).length} flags)` : '', d.replay ? ` — replay ${d.replay.state} ${d.replay.done}/${d.replay.max_batches}` : '', d.watch ? ` — watch ${d.watch.state} ${d.watch.folder}` : ''); } };
    stream.body.append(
      el('div', { class: 'row' }, el('b', { text: t('stream.replay') }), el('label', { class: 'row' }, speed, t('stream.speed')), el('label', { class: 'row' }, maxb, t('stream.maxBatches')),
        el('button', { class: 'btn btn-sm', type: 'button', onClick: async () => { const r = await runApi('/stream/replay', { method: 'POST', body: { speed: Number(speed.value), max_batches: Number(maxb.value) } }); if (!r.ok) toast(errText(r), 'fail'); refreshStatus(); } }, t('stream.start')),
        el('button', { class: 'btn btn-sm btn-quiet', type: 'button', onClick: async () => { await runApi('/stream/stop', { method: 'POST', body: {} }); refreshStatus(); } }, t('stream.stop'))),
      el('div', { class: 'row' }, el('b', { text: t('stream.watch') }), folder, el('button', { class: 'btn btn-sm', type: 'button', onClick: async () => { const r = await runApi('/stream/watch', { method: 'POST', body: { folder: folder.value.trim() } }); if (!r.ok) toast(errText(r), 'fail'); refreshStatus(); } }, t('stream.watchStart'))),
      el('div', { class: 'row' }, el('b', { text: t('stream.status') }), status, el('button', { class: 'btn btn-sm btn-quiet', type: 'button', onClick: refreshStatus }, t('common.refresh'))));
    refreshStatus();
    view._offBatch = bus.on('batch', refreshStatus);
  };

  // ---- the start screen replaces the form, the list and the progress panels while a run is being started
  let screen = null;
  const tick = setInterval(() => { if (job && screen) screen.paint(true); }, 1000);   // elapsed time moves even when no byte does
  const paintJob = () => {
    const active = !!job;
    jobHost.hidden = !active;
    grid.hidden = active;
    progress.root.hidden = active || !!progress.root.dataset.hiddenByRole;
    stream.root.hidden = active || !!stream.root.dataset.hiddenByRole;
    if (!active) { if (screen) { clear(jobHost); screen = null; showFile(); } return; }
    if (!screen) { screen = jobScreen(); jobHost.append(screen); screen.paint(true); window.scrollTo({ top: 0 }); const c = screen.querySelector('button:not([hidden])'); if (c) c.focus({ preventScroll: true }); } else screen.paint();
  };
  jobListeners.add(paintJob);

  await refreshRuns();
  renderProgress(state.runStatus);
  renderStream();
  paintJob();
  if (handoverRun && handoverRun === state.run) { handoverRun = null; flash(progress.root); }
  const offs = [
    bus.on('status', (s) => { renderProgress(s); renderList(); }),
    bus.on('run.changed', () => { renderList(); renderProgress(state.runStatus); renderStream(); }),
    bus.on('runs.loaded', renderList),
  ];
  view.cleanup = () => { offs.forEach((f) => f()); if (view._offBatch) view._offBatch(); jobListeners.delete(paintJob); clearInterval(tick); };
  return view;
}
