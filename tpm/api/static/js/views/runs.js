/* View 0: analyse a file, list runs, live stage progress with the time budget, live input (replay / watch). */
import { state, t, el, clear, api, runApi, errText, toast, bus, fmt, st, section, confirmDialog, viewHead, empty, store, roleAllows } from '../core.js';

const STAGES = ['ingest', 'profile', 'quality', 'detect', 'diagnose', 'assess', 'report'];

export async function render(main) {
  const view = el('div', { class: 'view' });
  main.append(view);
  view.append(viewHead('0', t('nav.runs')));
  const grid = el('div', { class: 'cols cols-side' });
  view.append(grid);
  const left = el('div', { class: 'stack' });
  const right = el('div', { class: 'stack' });
  grid.append(left, right);

  // ---- new run form
  left.append(el('h2', { text: t('runs.new') }));
  let file = null;
  const drop = el('div', { class: 'drop', tabindex: '0', role: 'button' }, el('div', { text: t('runs.drop') }), el('div', { class: 'file' }));
  const fileInput = el('input', { type: 'file', hidden: true });
  const showFile = () => { drop.querySelector('.file').textContent = file ? `${file.name} (${fmt.bytes(file.size)})` : ''; };
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
  startBtn.addEventListener('click', async () => {
    const path = pathIn.value.trim();
    if (!file && !path) { toast(t('runs.drop'), 'fail'); return; }
    startBtn.disabled = true;
    const fd = new FormData();
    if (file) fd.append('file', file); else fd.append('path', path);
    fd.append('has_header', opts.has_header.checked ? 'true' : 'false');
    fd.append('transposed', opts.transposed.checked ? 'true' : 'false');
    for (const k of ['delimiter', 'group_columns', 'time_column', 'domain_hint']) if (opts[k].value.trim()) fd.append(k, opts[k].value.trim());
    fd.append('language', opts.language.value); fd.append('profile', opts.profile.value);
    if (opts.rules_file.files[0]) fd.append('rules_file', opts.rules_file.files[0]);
    const r = await api('/api/runs', { method: 'POST', form: fd });
    startBtn.disabled = false;
    if (!r.ok) { toast(errText(r), 'fail'); return; }
    toast(t('runs.started', { id: r.data.run_id }), 'ok');
    file = null; showFile(); pathIn.value = '';
    bus.emit('run.select', r.data.run_id);
    await refreshRuns();
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
  left.append(drop, fileInput, el('label', { class: 'field' }, el('span', { text: t('runs.orPath') }), pathIn), details, el('div', { class: 'row' }, startBtn, demoBtn));

  // ---- runs list
  right.append(el('h2', { text: t('runs.list') }));
  const listEl = el('div', { class: 'stack' });
  right.append(listEl);
  const progress = section(t('runs.progress'));
  view.append(progress.root);
  const stream = section(t('stream.title'), { level: 'engineer' });
  view.append(stream.root);

  const renderList = () => {
    clear(listEl);
    if (!state.runs.length) { listEl.append(empty(t('runs.none'))); return; }
    for (const r of state.runs) {
      const row = el('div', { class: 'runrow' + (r.run_id === state.run ? ' sel' : ''), role: 'button', tabindex: '0' });
      const mini = el('span', { class: 'mini-stages', title: t('runs.stages') }, STAGES.map((s) => { const sg = (r.stages || []).find((x) => x.stage === s); return el('i', { class: sg ? sg.state : '' }); }));
      row.append(
        el('div', {}, el('div', { class: 'id' }, st(r.state, ''), ' ', r.run_id, ' ', mini),
          el('div', { class: 'meta' }, el('span', { text: `${t('runs.source')}: ${(r.source_path || '').split(/[\\/]/).pop()}` }), el('span', { text: fmt.ts(r.created_at) }), el('span', { text: t('runs.flags', { n: r.n_flags || 0 }) }), r.profile ? el('span', { text: r.profile }) : null, r.meta && r.meta.fake ? el('span', { text: 'demo' }) : null)),
        el('div', { class: 'row' }, el('button', { class: 'btn btn-sm', type: 'button', onClick: (e) => { e.stopPropagation(); bus.emit('run.select', r.run_id); } }, r.run_id === state.run ? t('runs.selected') : t('runs.select')),
          roleAllows('engineer') ? el('button', { class: 'btn btn-sm btn-quiet btn-danger', type: 'button', onClick: async (e) => { e.stopPropagation(); if (await confirmDialog(t('runs.deleteConfirm', { id: r.run_id }), { danger: true, okLabel: t('common.delete') })) { const d = await api(`/api/runs/${r.run_id}`, { method: 'DELETE' }); if (d.ok) { toast(t('common.delete') + ': ' + r.run_id); if (state.run === r.run_id) bus.emit('run.select', null); await refreshRuns(); } else toast(errText(d), 'fail'); } } }, t('common.delete')) : null));
      row.addEventListener('click', () => bus.emit('run.select', r.run_id));
      row.addEventListener('keydown', (e) => { if (e.key === 'Enter') bus.emit('run.select', r.run_id); });
      listEl.append(row);
    }
  };
  const refreshRuns = async () => { const r = await api('/api/runs'); if (r.ok) { state.runs = r.data.runs || []; bus.emit('runs.loaded', state.runs); } renderList(); };

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
    progress.body.append(el('div', { class: 'row between' }, el('span', {}, st(s.state, t('runs.state.' + s.state)), ' ', el('b', { text: s.run_id })), el('span', { class: 'small muted', text: s.source_path || '' })), stages,
      el('div', { class: 'budget' }, el('span', { text: `${t('common.elapsed')} ${fmt.sec(elapsed)}` }), el('span', { class: 'bar' }, el('i', { style: { width: Math.min(100, elapsed / budget * 100) + '%', background: elapsed > budget ? 'var(--fail)' : '' } })), el('span', { text: `${t('common.timeBudget')} ${fmt.sec(budget)}` })));
    // the job error lives in memory only until the server restarts; status.error is the persisted copy
    const runError = (s.job && s.job.error) || s.error || (s.status && s.status.error);
    if (runError) progress.body.append(el('pre', { class: 'notice fail small', style: { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxWidth: '100%' }, text: String(runError).split('
')[0] + '

' + String(runError).split('
').slice(1).join('
') }));
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

  await refreshRuns();
  renderProgress(state.runStatus);
  renderStream();
  const offs = [
    bus.on('status', (s) => { renderProgress(s); renderList(); }),
    bus.on('run.changed', () => { renderList(); renderProgress(state.runStatus); renderStream(); }),
    bus.on('runs.loaded', renderList),
  ];
  view.cleanup = () => { offs.forEach((f) => f()); if (view._offBatch) view._offBatch(); };
  return view;
}
