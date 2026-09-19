/* View 1: what the system understood — signal catalog with evidence, hypotheses, role override,
   correlation heatmap, clusters, dataset assumptions and what remains uncertain. */
import { state, t, el, clear, runApi, fmt, conf, infStatus, chip, section, table, viewHead, needRun, empty, evidenceButton, fetchEvidence, evidenceList, decisionBar, postDecision, hiddenHint, kv, roleAllows, bus, meter, unavailableNote, navigate } from '../core.js';
import { plot, purge, tokens, colorFor, vt, vizBox, chartNode, praStrip, cappedList, sensorKind, sensorKindColor, SENSOR_KINDS, strongestEdges, basicMore } from '../charts.js';
import { drawNetwork } from '../network.js';
import { openChat, signalContext } from '../chat.js';
import { plainBox } from '../plain.js';
import { renameBar, loadSignalNames, renameSignalDialog } from '../rename.js';
import { summaryCard, techDetails, techNested } from '../brief.js';

const FB = {
  'und.kindMost': 'Most likely: {kind} ({pct})',
  'und.kindWhy': 'Why',
  'und.header': 'Header in your file',
  'und.noHeader': 'Your file has no header for this column.',
  'und.headerHelp': 'The system did not use this header to decide anything: it identifies signals by how they behave, and works with the neutral alias {id}. If you know what {id} measures, name it here. Your name is logged as a human decision and shown next to the alias.',
  'und.nameLabel': 'What {id} measures',
  'und.namePlaceholder': 'e.g. Reactor pressure',
  'und.unitPlaceholder': 'unit, e.g. kPa',
  'und.useHeader': 'Use the header as the name',
  'und.nameSaved': 'Saved and logged.',
  // the profile's guesses and reasons in plain words (tpm/profile/roles.py writes them in engine terms)
  'und.guess.lead': 'Probably {guess}',
  'und.guess.flow': 'a flow (its values change fast and are noisy)',
  'und.guess.pressure': 'a pressure or a level (its values change at a medium pace)',
  'und.guess.temperature': 'a temperature (its values change slowly and smoothly)',
  'und.guess.analyzer': 'an analyser or lab value (updated only from time to time)',
  'und.guess.valve': 'a valve or a controller output (set in steps)',
  'und.guess.power': 'a power or a speed',
  'und.unitop.cluster': 'moves together with the other sensors of group {c}: probably the same part of the plant',
  'und.why.steps': 'It changes in steps, like a valve or a set-point being moved.',
  'und.why.steps100': 'It changes in steps and always stays between 0 and 100, like a valve position in percent.',
  'und.why.held': 'Its value stays the same for about {n} readings and then jumps: it is updated only from time to time, like an analyser.',
  'und.why.bounded': 'It changes smoothly but always stays between 0 and 100, like a controller output in percent.',
  'und.why.smooth': 'It changes slowly and smoothly, like a temperature.',
  'und.why.noisy': 'It jumps a lot from one reading to the next compared with its range, like a flow.',
  'und.why.medium': 'It is neither very smooth nor very noisy, like a pressure or a level.',
  'und.why.llm': 'The local AI model guessed it from how the values behave; it never saw the column names.',
};
const tt = (k, vars) => { let v = t(k, vars); if (!v || v === k) { v = FB[k] || k; for (const [a, b] of Object.entries(vars || {})) v = v.replaceAll(`{${a}}`, b); } return v; };
/** An instrument guess in plain words: "Probably a flow (its values change fast and are noisy)"; '' without a guess. */
function guessWords(hypothesis, kind) {
  if (!hypothesis || hypothesis === 'unknown') return '';
  return tt('und.guess.lead', { guess: FB['und.guess.' + kind] ? tt('und.guess.' + kind) : hypothesis });
}
/** "shared unit operation of cluster C02" -> "moves together with the other sensors of group C02: ..." */
function unitOpWords(h) { const m = /cluster\s+(\S+)/i.exec(String(h || '')); return m ? tt('und.unitop.cluster', { c: m[1] }) : String(h || ''); }
/** The reason of a guess in plain words; a reason written by a language model (free text) stays as it is. */
function whyWords(text) {
  const s = String(text || '');
  let m;
  if (/^step-like signal bounded to 0-100/.test(s)) return tt('und.why.steps100');
  if (/^step-like signal/.test(s)) return tt('und.why.steps');
  if ((m = /^sample-and-hold with period ~([\d.]+)/.exec(s))) return tt('und.why.held', { n: Math.round(Number(m[1])) || m[1] });
  if (/^continuous but bounded to 0-100/.test(s)) return tt('und.why.bounded');
  if (/^low noise .*very high autocorrelation/.test(s)) return tt('und.why.smooth');
  if (/^high noise level .*relative to its variance/.test(s)) return tt('und.why.noisy');
  if (/^noise level [\d.]+, autocorrelation [\d.-]+$/.test(s)) return tt('und.why.medium');
  if (/^language-model hypothesis/.test(s)) return tt('und.why.llm');
  if (/^valve evidence: /.test(s)) return tt('und.why.valve', { list: s.replace(/^valve evidence: /, '').split('; ').map(valveWords).join('; ') });
  return s;
}
/** One piece of the valve / controller-output evidence (tpm/profile/roles.py manipulated_evidence) in plain words. */
function valveWords(p) {
  let m;
  const side = (x) => tt('und.why.v.' + x);
  if (/^uses its whole 0-100 % range/.test(p)) return tt('und.why.v.range100');
  if (/^uses its whole 0-1 range/.test(p)) return tt('und.why.v.range01');
  if ((m = /^bounded like a \w+ and reaches its (upper|lower) end/.exec(p))) return tt('und.why.v.reaches', { side: side(m[1]) });
  if ((m = /^sits exactly at its (upper|lower) limit in ([\d.,]+%) of readings/.exec(p))) return tt('und.why.v.pinned', { side: side(m[1]), pct: m[2] });
  if (/^moves in steps/.test(p)) return tt('und.why.v.steps');
  if ((m = /^other signals follow its moves \((.*)\)$/.exec(p))) return tt('und.why.v.followers', { list: m[1].split(', ').map((x) => { const q = /^(\S+) (\d+) samples later$/.exec(x); return q ? tt('und.why.v.after', { s: q[1], n: q[2] }) : x; }).join(', ') });
  if ((m = /^it reacts to other signals the way a controller output does \((.*)\)$/.exec(p))) return tt('und.why.v.drivers', { list: m[1].split(', ').map((x) => { const q = /^(\d+) samples after (\S+)$/.exec(x); return q ? tt('und.why.v.before', { s: q[2], n: q[1] }) : x; }).join(', ') });
  return p;
}

const ROLES = ['continuous_measured', 'actuator_like', 'held_sampled', 'constant', 'derived_redundant', 'counter', 'timestamp', 'categorical', 'text', 'identifier', 'unknown'];

export async function render(main, params = {}) {
  // page = title, plain summary, then ONE expander ("Show technical analyses") with everything this view rendered before
  const page = el('div', { class: 'view' });
  main.append(page);
  page.append(viewHead('1', t('nav.understanding')));
  if (!state.run) { page.append(needRun()); return page; }
  const tech = techDetails('understanding');
  page.append(summaryCard('understanding'), tech);
  const view = tech.body;
  if (roleAllows('operator')) { try { const pb = await plainBox('understanding'); if (pb) view.append(pb); } catch (e) { /* plain box is optional */ } }
  const [und, sig, rel, dom, sch] = await Promise.all([runApi('/understanding'), runApi('/signals'), runApi('/relations'), runApi('/domain'), runApi('/schema')]);
  const signals = sig.ok ? sig.data.signals || [] : [];
  const byId = Object.fromEntries(signals.map((s) => [s.id, s]));
  // renaming a signal is an everyday action ("call S44 'possibly broken'"): it sits above the technical part
  const rb = renameBar(signals.map((s) => s.id)); if (rb) page.insertBefore(rb, tech);
  // round 5: the diagrams and the sensor-type guesses sit ABOVE the technical part
  const plainHost = el('div', { class: 'viz-host' }); page.insertBefore(plainHost, tech);
  const U = und.data || {};

  // ---- summary + assumptions + uncertain + domain
  const top = el('div', { class: 'cols cols-2' });
  const summary = el('div', { class: 'stack' }, el('h2', { text: t('und.title') }), el('p', { text: U.summary || t('common.notYet') }));
  const lk = (dom.ok && (dom.data.domain_likelihood || dom.data.likelihood)) || (sch.ok && sch.data.domain_likelihood) || {};
  const kinds = Object.entries(lk).filter(([, v]) => typeof v === 'number').sort((a, b) => b[1] - a[1]);
  if (kinds.length) {
    const why = (dom.ok && (dom.data.explanation || dom.data.statement)) || '';
    summary.append(el('h3', { text: t('und.domain') }),
      el('p', { text: tt('und.kindMost', { kind: kinds[0][0].replace(/_/g, ' '), pct: fmt.pct ? fmt.pct(kinds[0][1]) : `${Math.round(kinds[0][1] * 100)}%` }) }),
      el('div', {}, kinds.map(([k, v]) => meter(k.replace(/_/g, ' '), v))),
      why ? el('p', { class: 'small muted', text: `${tt('und.kindWhy')}: ${why}` }) : null);
  }
  const lists = el('div', { class: 'stack' },
    el('h3', { text: t('und.assumptions') }), (U.assumptions || []).length ? el('ul', { class: 'list' }, (U.assumptions || []).map((a) => el('li', {}, infStatus('assumed'), ' ', a))) : empty(),
    el('h3', { text: t('und.uncertain') }), (U.uncertain || []).length ? el('ul', { class: 'list' }, (U.uncertain || []).map((a) => el('li', {}, infStatus('uncertain'), ' ', a))) : empty());
  top.append(summary, lists);
  view.append(top);

  // ---- dataset-level hypotheses (inferences with accept/question/override)
  const hyp = section(t('und.hypotheses'), { right: el('span', { class: 'small muted', text: t('inference.legend') }) });
  view.append(hyp.root);
  const infR = await runApi('/inferences', { params: { subject: 'dataset' } });
  const infs = infR.ok ? infR.data.items || [] : [];
  if (!infs.length) hyp.body.append(empty());
  for (const inf of infs) hyp.body.append(hypRow(inf));

  // ---- signal catalog
  const cat = section(t('und.catalog'), { right: el('span', { class: 'small muted', text: sch.ok && sch.data.n_rows ? `${fmt.int(sch.data.n_rows)} rows, ${signals.length} signals, ${sch.data.n_groups || 1} groups` : '' }) });
  cat.root.dataset.briefSection = 'catalog';
  view.append(cat.root);
  if (!sig.ok || !sig.data.available) { cat.body.append(sig.unavailable ? unavailableNote(sig) : empty(t('common.notYet'))); }
  const grid = el('div', { class: 'cols cols-side' });
  cat.body.append(grid);
  const detail = el('div', { class: 'box' }, el('div', { class: 'empty', text: t('und.pickSignal') }));
  const tbl = table({
    columns: [
      { label: t('und.alias'), render: (s) => el('span', { title: s.source_column ? `${tt('und.header')}: ${s.source_column}` : tt('und.noHeader') }, el('b', { text: s.id }), s.display_name ? el('span', { class: 'small', text: ` · ${s.display_name}${s.display_unit ? ' (' + s.display_unit + ')' : ''}` }) : null, s.excluded ? el('span', { class: 'dim small', text: ' ✕' }) : null) },
      { label: t('und.role'), render: (s) => el('span', {}, (s.human_role_override || s.structural_role || 'unknown').replace(/_/g, ' '), s.human_role_override ? el('span', { class: 'small', style: { marginLeft: '6px' } }, infStatus(null, { human: 'overridden' })) : null) },
      { label: t('common.confidence'), render: (s) => conf(s.structural_confidence) },
      { label: t('und.instrument'), render: (s) => s.instrument_hypothesis ? el('span', {}, s.instrument_hypothesis, ' ', el('span', { class: 'small' }, infStatus(s.instrument_confidence >= 0.5 ? 'assumed' : 'uncertain')), ' ', conf(s.instrument_confidence, { label: false })) : el('span', { class: 'dim', text: '–' }) },
      { label: t('und.cluster'), render: (s) => s.cluster_id || '–' },
      { label: t('common.evidence'), num: true, render: (s) => String((s.evidence_ids || []).length) },
    ],
    rows: signals, pageSize: 40, onRow: (s) => showSignal(s),
    rowClass: (s) => (s.excluded ? 'dim' : ''),
  });
  grid.append(tbl, detail);
  const SP = U.signal_plain || {};

  // ---- round 5: how the sensors interact (network) and what each sensor probably measures (cards), above the expander
  const fileName = (s) => s.display_name || (s.source_column && s.source_column !== s.id ? s.source_column : '');
  const fullName = (s) => (fileName(s) ? `${fileName(s)} (${s.id})` : s.id);
  const kindOf = (s) => { const byName = s.display_name ? sensorKind(s.display_name) : 'other'; return byName !== 'other' && byName !== 'unknown' ? byName : sensorKind(s.instrument_hypothesis); };
  const kindLabel = (kd) => vt('und.kind.' + kd);
  const basic = !roleAllows('operator');
  // Basic mode has no technical part: a clicked sensor shows its card at the top of the page instead
  const openSignal = (id) => { if (!byId[id]) return; if (basic) { navigate('understanding', { signal: id }); return; } tech.open = true; showSignal(byId[id]); setTimeout(() => { try { detail.scrollIntoView({ behavior: 'smooth', block: 'start' }); } catch { /* ignore */ } }, 80); };
  {
    const RD = rel.ok ? rel.data || {} : {};
    const pairs = RD.pairs || [];
    const strong = pairs.filter((x) => Math.abs(Number(x.r)) >= 0.5).length;
    const edges = strongestEdges(pairs, { cap: 60, minR: 0.5 });
    const netNode = chartNode('tall');
    const note = el('p', { class: 'small muted viz-note', text: strong > edges.length ? vt('und.net.capped', { n: edges.length, total: strong }) : '' });
    // what the lines, arrows and numbers mean; how to move the chart (under the chart, always visible)
    const period = Number((U.dataset || {}).sample_period_seconds) || 0;
    const durWords = (sec) => (!(sec > 0) ? '' : sec < 1 ? `${Math.round(sec * 1000)} ms` : sec < 90 ? `${+sec.toFixed(sec < 10 ? 1 : 0)} s` : sec < 5400 ? `${+(sec / 60).toFixed(sec < 600 ? 1 : 0)} min` : sec < 172800 ? `${+(sec / 3600).toFixed(1)} h` : `${+(sec / 86400).toFixed(1)} d`);
    const timeTxt = (k) => (period ? t('und.rel.time', { t: durWords(k * period) }) : '');
    // numbers in the page language (0,93 and 29 200 in Finnish and Swedish)
    const locale = { fi: 'fi-FI', sv: 'sv-SE' }[state.lang] || 'en-GB';
    const num = (v, d = 0) => Number(v).toLocaleString(locale, { minimumFractionDigits: d, maximumFractionDigits: d });
    const fmtR = (v) => (v === null || v === undefined || !Number.isFinite(Number(v)) ? '–' : (Number(v) < 0 ? '−' : '') + num(Math.abs(Number(v)), 2));
    const nm = (sid) => (byId[sid] ? fullName(byId[sid]) : sid);
    const glyph = (kind) => {
      const NSV = 'http://www.w3.org/2000/svg';
      const g = document.createElementNS(NSV, 'svg'); g.setAttribute('width', '44'); g.setAttribute('height', '16'); g.setAttribute('viewBox', '0 0 44 16'); g.setAttribute('aria-hidden', 'true');
      const ln = document.createElementNS(NSV, 'line'); ln.setAttribute('x1', '2'); ln.setAttribute('y1', '8'); ln.setAttribute('x2', kind === 'arrow' ? '36' : '42'); ln.setAttribute('y2', '8');
      ln.setAttribute('style', `stroke:${kind === 'neg' ? 'var(--fail)' : 'var(--ink-3)'};stroke-width:${kind === 'thin' ? 1.2 : 3.5}px`);
      g.append(ln);
      if (kind === 'arrow') {
        const hd = document.createElementNS(NSV, 'path'); hd.setAttribute('d', 'M35,3 L43,8 L35,13 z'); hd.setAttribute('style', 'fill:var(--ink-2)'); g.append(hd);
        const bx = document.createElementNS(NSV, 'rect'); bx.setAttribute('x', '9'); bx.setAttribute('y', '1'); bx.setAttribute('width', '18'); bx.setAttribute('height', '14'); bx.setAttribute('rx', '7'); bx.setAttribute('style', 'fill:var(--bg-2);stroke:var(--ink-3)');
        const tx = document.createElementNS(NSV, 'text'); tx.setAttribute('x', '18'); tx.setAttribute('y', '8'); tx.setAttribute('text-anchor', 'middle'); tx.setAttribute('dominant-baseline', 'central'); tx.setAttribute('style', 'font-size:10px;font-weight:600;fill:var(--ink)'); tx.textContent = '+2';
        g.append(bx, tx);
      }
      return g;
    };
    const key = edges.length ? el('div', {}, el('h3', { class: 'small', style: { margin: '10px 0 0' }, text: t('und.net.key.title') }),
      el('ul', { class: 'net-key' },
        el('li', {}, glyph('thick'), el('span', { text: t('und.net.key.line') })),
        el('li', {}, glyph('neg'), el('span', { text: t('und.net.key.neg') })),
        el('li', {}, glyph('arrow'), el('span', { text: t('und.net.key.arrow') + (period ? ' ' + t('und.net.key.period', { p: durWords(period) }) : '') })),
        el('li', {}, glyph('thin'), el('span', { text: t('und.net.key.same') }))),
      el('p', { class: 'small muted', style: { margin: '6px 0 0', maxWidth: '110ch' }, text: t('und.net.key.use') })) : null;
    // the link between two sensors, in plain words: opened by a click on a line or its number
    const relBox = el('div', { class: 'net-rel', hidden: true, 'aria-live': 'polite' });
    let net = null;
    const edgeTip = (e) => {
      const first = e.lag < 0 ? e.b : e.a, follow = e.lag < 0 ? e.a : e.b;
      const how = Number(e.r) < 0 ? vt('und.net.opposite') : vt('und.net.together');
      const lag = e.lag ? t('und.net.tipLag', { follow: nm(follow), first: nm(first), k: Math.abs(e.lag), time: timeTxt(Math.abs(e.lag)) }) : t('und.net.tipSame');
      return `${nm(e.a)} – ${nm(e.b)}: ${how}, r ${fmtR(e.r)}. ${lag} ${t('und.net.tipClick')}`;
    };
    const clusterSize = (c) => ((RD.clusters || {})[c] || []).length || signals.filter((x) => x.cluster_id === c).length;
    const isValve = (sid) => !!byId[sid] && (byId[sid].structural_role === 'actuator_like' || kindOf(byId[sid]) === 'valve');
    const explainRelation = (e) => {
      const first = e.lag < 0 ? e.b : e.a, follow = e.lag < 0 ? e.a : e.b;
      const r = Number(e.r), ar = Math.abs(r);
      const vars = { a: nm(e.a), b: nm(e.b), lead: nm(first), follow: nm(follow) };
      const ps = [t(r < 0 ? 'und.rel.opposite' : 'und.rel.together', vars)];
      ps.push(`${t('und.rel.strength', { r: fmtR(r), word: t('und.rel.word.' + (ar >= 0.9 ? 'vstrong' : ar >= 0.75 ? 'strong' : 'clear')) })} ${t('und.rel.method.' + (['diff', 'level', 'update_instants'].includes(e.method) ? e.method : 'level'), { n: num(e.n || RD.n_samples || 0) })}`);
      if (e.lag) {
        const k = Math.abs(e.lag);
        ps.push(t(k === 1 ? 'und.rel.lead1' : 'und.rel.lead', { ...vars, k, time: timeTxt(k), r: fmtR(e.r_at_lag ?? e.r) }));
        ps.push(t(isValve(first) ? 'und.rel.leadActuator' : isValve(follow) ? 'und.rel.followActuator' : 'und.rel.leadMeaning', vars));
      } else ps.push(t('und.rel.same', vars));
      const red = (RD.redundancy || []).find((x) => x && x.derived && ((x.signal === e.a && (x.partners || []).includes(e.b)) || (x.signal === e.b && (x.partners || []).includes(e.a))));
      if (red) ps.push(t('und.rel.derived', { d: nm(red.signal), src: nm(red.signal === e.a ? e.b : e.a) }));
      const ca = byId[e.a] && byId[e.a].cluster_id, cb = byId[e.b] && byId[e.b].cluster_id;
      if (ca && ca === cb) ps.push(t('und.rel.sameCluster', { c: ca, n: clusterSize(ca) }));
      else if (ca && cb) ps.push(t('und.rel.crossCluster', { c1: ca, c2: cb }));
      ps.push(t('und.rel.caution'));
      ps.push(t('und.rel.use', vars));
      const title = e.lag ? `${nm(first)} → ${nm(follow)}` : `${nm(e.a)} ↔ ${nm(e.b)}`;
      const nums = roleAllows('engineer') ? kv([['r', fmtR(e.r)], ['Spearman', fmtR(e.spearman)], [t('und.rel.num.lag'), e.lag ? `${e.lag > 0 ? '+' : '−'}${Math.abs(e.lag)} (${e.lag > 0 ? e.a : e.b} ${t('und.rel.num.first')})` : '0'], [t('und.rel.num.rAtLag'), fmtR(e.r_at_lag)], [t('und.rel.num.n'), num(e.n || 0)], [t('und.rel.num.method'), ['diff', 'level', 'update_instants'].includes(e.method) ? t('und.rel.m.' + e.method) : (e.method || '–')]]) : null;
      relBox.hidden = false;
      relBox.replaceChildren(...[
        el('div', { class: 'row between' }, el('h3', { text: title }), el('button', { class: 'btn btn-sm btn-quiet', type: 'button', onClick: () => { relBox.hidden = true; if (net) net.select(null); } }, t('und.rel.close'))),
        ...ps.map((x) => el('p', { text: x })), nums,
        el('div', { class: 'row' },
          el('button', { class: 'btn btn-sm', type: 'button', onClick: () => openSignal(e.a) }, t('und.rel.open', { s: nm(e.a) })),
          el('button', { class: 'btn btn-sm', type: 'button', onClick: () => openSignal(e.b) }, t('und.rel.open', { s: nm(e.b) })),
          el('button', { class: 'btn btn-sm ask', type: 'button', onClick: () => openChat({ object_type: 'signal', object_id: e.a, signal_id: e.a, title: `${nm(e.a)} ↔ ${nm(e.b)}`, autoAsk: t('und.rel.askQ', vars) }) }, t('und.rel.ask')),
          (e.evidence_ids || []).length ? evidenceButton(e.evidence_ids) : null)].filter(Boolean));
      try { relBox.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); } catch { /* ignore */ }
    };
    plainHost.append(vizBox(vt('und.net.title'), vt('und.net.help'), netNode, key, note, relBox));
    if (!edges.length) netNode.replaceChildren(empty(vt('und.net.none')));
    else {
      const nodes = signals.map((s) => ({ id: s.id, label: fileName(s) ? `${String(fileName(s)).slice(0, 14)}` : s.id, kind: kindOf(s), cluster: s.cluster_id, hover: `<b>${fullName(s)}</b><br>${guessWords(s.instrument_hypothesis, kindOf(s)) || kindLabel(kindOf(s))}${s.cluster_id ? '<br>' + t('und.cluster') + ' ' + s.cluster_id : ''}` }));
      requestAnimationFrame(() => {
        net = drawNetwork(netNode, nodes, edges, { height: signals.length > 30 ? 600 : 460, onClick: openSignal, kindLabel, onEdgeClick: explainRelation, edgeTitle: edgeTip, storageKey: `net.${state.run}`,
          labels: { zoomIn: t('und.net.zoomIn'), zoomOut: t('und.net.zoomOut'), fit: t('und.net.fit'), reset: t('und.net.reset'), chart: vt('und.net.title') } });
      });
    }
  }
  const guessOf = (s) => (s.instrument_hypothesis && s.instrument_hypothesis !== 'unknown' ? s.instrument_hypothesis : '');
  // the instrument guess of each sensor, as an inference a person can accept, question or correct
  const allInf = signals.length ? await runApi('/inferences') : { ok: false };
  const infBySubject = {};
  for (const inf of allInf.ok ? allInf.data.items || [] : []) if (/^instrument/i.test(inf.claim || '')) { const cur = infBySubject[inf.subject]; if (!cur || (inf.confidence ?? 0) >= (cur.confidence ?? 0)) infBySubject[inf.subject] = inf; }
  const guessDecision = (inf) => decisionBar('inference', inf.id, { current: inf.human_status, note: inf.human_note, overrideFields: [{ key: 'claim', label: t('decision.override.newValue'), type: 'text', value: inf.claim }], askContext: { object_type: 'inference', object_id: inf.id, title: inf.claim } });
  const nameButton = (s) => el('button', { class: 'btn btn-sm', type: 'button', onClick: () => renameSignalDialog(s.id) }, vt('und.unsure.name'));
  /** One sensor: what it probably measures, how sure, why, part of the plant; accept / question / correct the guess. */
  const card = (s) => {
    const kd = kindOf(s); const inf = infBySubject[s.id]; const c = Math.max(0, Math.min(1, Number(s.instrument_confidence ?? (inf && inf.confidence) ?? 0)));
    const why = inf && inf.reasoning ? whyWords(inf.reasoning) : (SP[s.id] || '');
    return el('div', { class: 'sensor-card' + (s.excluded ? ' excluded' : ''), style: { borderTopColor: sensorKindColor(kd) }, dataset: { signal: s.id } },
      el('div', { class: 'sensor-card-head' }, el('span', { class: 'sensor-dot', style: { background: sensorKindColor(kd) }, 'aria-hidden': 'true' }), el('b', { text: fullName(s) }), el('span', { class: 'chip solid', text: kindLabel(kd) })),
      el('div', { class: 'sensor-guess', text: guessWords(guessOf(s), kd) || vt('und.types.noGuess') }),
      el('div', { class: 'sensor-conf', title: vt('und.types.confidence') }, el('span', { class: 'small muted', text: vt('und.types.confidence') }), el('span', { class: 'bar' }, el('i', { style: { width: c * 100 + '%', background: c < 0.4 ? 'var(--fail)' : c < 0.65 ? 'var(--warn)' : 'var(--ok)' } })), el('span', { class: 'small', text: fmt.pct(c) })),
      s.unit_operation_hypothesis ? el('div', { class: 'small' }, el('span', { class: 'muted', text: vt('und.types.unitop') + ': ' }), unitOpWords(s.unit_operation_hypothesis), s.unit_operation_confidence !== null && s.unit_operation_confidence !== undefined ? el('span', { class: 'dim', text: ` (${fmt.pct(s.unit_operation_confidence)})` }) : null) : null,
      why ? el('div', { class: 'small sensor-why' }, el('span', { class: 'muted', text: vt('und.types.why') + ': ' }), why) : null,
      el('div', { class: 'sensor-actions' }, inf ? guessDecision(inf) : null, basic ? nameButton(s) : el('button', { class: 'btn btn-sm btn-quiet', type: 'button', onClick: () => openSignal(s.id) }, vt('und.types.details'))));
  };
  // Basic mode: a sensor somebody asked about (a sensor link, "Tell the system what your sensors measure") comes first
  if (basic && params && params.signal && byId[params.signal]) plainHost.prepend(el('div', { class: 'sensor-cards sensor-focus' }, card(byId[params.signal])));
  if (!roleAllows('operator')) {
    // basic mode: the three sensors the system is least sure about, as problem -> reason -> answer, with the two
    // things a person can do right there: name the sensor, or accept / question / correct the guess
    // (the one it is least sure about, as a short strip; how many more Operator mode lists)
    const allUnsure = signals.filter((s) => !s.excluded && !s.display_name && guessOf(s) && s.id !== params.signal).sort((a, b) => (a.instrument_confidence ?? 0) - (b.instrument_confidence ?? 0));
    const unsure = allUnsure.slice(0, 1);
    for (const s of unsure) plainHost.append(praStrip({ verdict: 'attention', problem: vt('und.unsure.problem', { name: fullName(s) }), reason: vt('und.unsure.reason', { guess: (FB['und.guess.' + kindOf(s)] ? tt('und.guess.' + kindOf(s)) : guessOf(s)) || vt('und.types.noGuess'), pct: fmt.pct(s.instrument_confidence ?? 0) }), fix: [vt('und.unsure.fix1'), vt('und.unsure.fix2')],
      extra: [el('div', { class: 'pra-btns' }, nameButton(s)), infBySubject[s.id] ? el('div', { class: 'brief-decide' }, guessDecision(infBySubject[s.id])) : null], compact: true }));
    const more = basicMore(allUnsure.length - unsure.length); if (more) plainHost.append(more);
  } else if (signals.length) {
    // operator / engineer: one card per sensor, filter by kind, capped with "show more"
    const cardsHost = el('div');
    let filter = '';
    const counts = {}; for (const s of signals) counts[kindOf(s)] = (counts[kindOf(s)] || 0) + 1;
    const chipBar = el('div', { class: 'kind-chips', role: 'group' });
    const paintCards = () => {
      clear(cardsHost);
      const list = signals.filter((s) => !filter || kindOf(s) === filter).sort((a, b) => Number(!!a.excluded) - Number(!!b.excluded) || (b.instrument_confidence ?? 0) - (a.instrument_confidence ?? 0));
      cardsHost.append(cappedList(list.length, 12, (i) => card(list[i]), { cls: 'sensor-cards' }));
      chipBar.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String((b.dataset.kind || '') === filter)));
    };
    const mkChip = (kd, label, n) => el('button', { class: 'kind-chip', type: 'button', dataset: { kind: kd }, 'aria-pressed': 'false', onClick: () => { filter = kd; paintCards(); } }, kd ? el('span', { class: 'sensor-dot', style: { background: sensorKindColor(kd) } }) : null, `${label} · ${n}`);
    chipBar.append(mkChip('', vt('und.types.all'), signals.length), ...SENSOR_KINDS.filter((kd) => counts[kd]).map((kd) => mkChip(kd, kindLabel(kd), counts[kd])));
    plainHost.append(vizBox(vt('und.types.title'), vt('und.types.help'), chipBar, cardsHost));
    paintCards();
  }
  if (!basic && params && params.signal && byId[params.signal]) { showSignal(byId[params.signal]); setTimeout(() => detail.scrollIntoView({ behavior: 'smooth', block: 'start' }), 50); }

  async function showSignal(s) {
    clear(detail);
    detail.append(el('div', { class: 'row between' }, el('h3', {}, `${t('und.detail')}: ${s.id}`, s.display_name ? el('span', { class: 'muted', text: ` · ${s.display_name}${s.display_unit ? ' (' + s.display_unit + ')' : ''}` }) : null), el('button', { class: 'btn btn-sm', type: 'button', onClick: () => openChat(signalContext(s)) }, t('common.ask'))));
    {
      const nameIn = el('input', { type: 'text', placeholder: tt('und.namePlaceholder'), value: s.display_name || '', style: { flex: '2', minWidth: '0' }, 'aria-label': tt('und.nameLabel', { id: s.id }) });
      const unitIn = el('input', { type: 'text', placeholder: tt('und.unitPlaceholder'), value: s.display_unit || '', style: { flex: '1', minWidth: '0' } });
      const saved = el('span', { class: 'small muted' });
      const save = async () => {
        const r = await postDecision('signal', s.id, 'set_name', { note: `named by operator${s.source_column ? ' (file header: ' + s.source_column + ')' : ''}`, newValue: { display_name: nameIn.value.trim(), display_unit: unitIn.value.trim() } });
        if (r.ok) { loadSignalNames(); s.display_name = nameIn.value.trim() || null; s.display_unit = unitIn.value.trim() || null; tbl.update(signals); saved.textContent = tt('und.nameSaved'); }
      };
      detail.append(el('div', { class: 'box', style: { margin: '8px 0 10px', padding: '10px 12px' } },
        el('div', { class: 'small' }, el('b', { text: `${tt('und.header')}: ` }), s.source_column ? el('code', { text: s.source_column }) : el('span', { class: 'muted', text: tt('und.noHeader') })),
        el('div', { class: 'hint', style: { margin: '4px 0 8px' }, text: tt('und.headerHelp', { id: s.id }) }),
        el('div', { class: 'row', style: { gap: '8px', flexWrap: 'wrap' } }, nameIn, unitIn,
          s.source_column ? el('button', { class: 'btn btn-sm btn-quiet', type: 'button', onClick: () => { nameIn.value = s.source_column; } }, tt('und.useHeader')) : null,
          el('button', { class: 'btn btn-sm btn-override', type: 'button', onClick: save }, t('common.save')), saved)));
    }
    const fp = s.fingerprint || {};
    if (SP[s.id]) detail.append(el('p', { class: 'plain-sig', style: { margin: '6px 0 10px', padding: '10px 12px', borderLeft: '3px solid var(--accent, #2bb5a0)', background: 'var(--bg-2, rgba(127,127,127,.08))', borderRadius: '0 6px 6px 0', overflowWrap: 'anywhere' }, text: SP[s.id] }));
    // the measurements, hypotheses, the role correction and the evidence behind the sentence above
    const nested = techNested();
    detail.append(nested);
    const T = nested.body;
    T.append(kv([
      [t('und.role'), el('span', {}, (s.human_role_override || s.structural_role).replace(/_/g, ' '), ' ', conf(s.structural_confidence))],
      [t('und.instrument'), s.instrument_hypothesis ? el('span', {}, s.instrument_hypothesis, ' ', conf(s.instrument_confidence)) : null],
      [t('und.unit'), s.unit_operation_hypothesis ? el('span', {}, s.unit_operation_hypothesis, ' ', conf(s.unit_operation_confidence)) : null],
      [t('und.cluster'), s.cluster_id],
      [t('und.excluded'), s.excluded ? (s.excluded_reason || t('common.yes')) : null],
    ]));
    if (roleAllows('engineer') && Object.keys(fp).length) {
      T.append(el('h4', { class: 'small muted', style: { marginTop: '12px' }, text: t('und.fingerprint') }), el('div', { class: 'small', text: Object.entries(fp).filter(([, v]) => v !== null && v !== undefined).map(([k, v]) => `${k} ${typeof v === 'number' ? fmt.num(v, 3) : v}`).join('   ') }));
    }
    if ((s.related_signals || []).length) T.append(el('h4', { class: 'small muted', style: { marginTop: '12px' }, text: t('und.related') }), el('div', { class: 'sigchips' }, s.related_signals.map((r) => chip(`${r.signal}  r ${fmt.num(r.r, 2)}  ${t('und.lag')} ${r.lag}`, 'click', { onClick: () => { if (byId[r.signal]) showSignal(byId[r.signal]); } }))));
    // hypotheses (inferences about this signal)
    const hb = el('div', {}, el('h4', { class: 'small muted', style: { marginTop: '12px' }, text: t('und.hypothesesFor', { id: s.id }) }));
    T.append(hb);
    const ir = await runApi('/inferences', { params: { subject: s.id } });
    for (const inf of ir.ok ? ir.data.items || [] : []) hb.append(hypRow(inf));
    // role override
    const sel = el('select', {}, ROLES.map((r) => el('option', { value: r, text: r.replace(/_/g, ' ') })));
    sel.value = s.human_role_override || s.structural_role;
    const note = el('input', { type: 'text', placeholder: t('common.note'), style: { flex: 1 } });
    T.append(el('h4', { class: 'small muted', style: { marginTop: '12px' }, text: t('und.roleOverride') }), el('div', { class: 'row' }, sel, note, el('button', { class: 'btn btn-sm btn-override', type: 'button', onClick: async () => { const r = await postDecision('signal', s.id, 'set_role', { note: note.value, newValue: { role: sel.value } }); if (r.ok) { s.human_role_override = sel.value; tbl.update(signals); showSignal(s); } } }, t('common.save'))), el('div', { class: 'hint', text: t('und.roleOverrideHelp') }));
    // evidence
    const evBox = el('div', { style: { marginTop: '12px' } }, el('h4', { class: 'small muted', text: t('common.evidence') }));
    T.append(evBox);
    const evs = await fetchEvidence(s.evidence_ids || []);
    evBox.append(evidenceList(evs));
  }

  function hypRow(inf) {
    const row = el('div', { class: 'hyp' });
    row.append(el('div', {}, el('div', { class: 'claim' }, infStatus(inf.status, { human: inf.human_status }), ' ', inf.claim, ' ', conf(inf.confidence)),
      inf.reasoning ? el('div', { class: 'why', text: inf.reasoning + (inf.source && inf.source !== 'code' ? ` (${inf.source})` : '') }) : null,
      inf.alternatives && inf.alternatives.length ? el('div', { class: 'alt', text: 'Alternatives: ' + inf.alternatives.join('; ') }) : null,
      el('div', { class: 'row', style: { marginTop: '4px' } }, evidenceButton(inf.evidence_ids), el('span', { class: 'dim small', text: inf.id }))),
      decisionBar('inference', inf.id, { current: inf.human_status, note: inf.human_note, overrideFields: [{ key: 'claim', label: t('decision.override.newValue'), type: 'text', value: inf.claim }], askContext: { object_type: 'inference', object_id: inf.id, title: inf.claim } }));
    return row;
  }

  // ---- heatmap + clusters + grouping
  if (rel.ok && rel.data.available && rel.data.correlation) {
    const hm = section(t('und.heatmap'), { level: 'engineer' });
    view.append(hm.root);
    const node = el('div', { class: 'chart tall' });
    hm.body.append(node);
    const k = tokens();
    const labels = rel.data.signals || [];
    requestAnimationFrame(() => plot(node, [{ type: 'heatmap', z: rel.data.correlation, x: labels, y: labels, zmin: -1, zmax: 1, colorscale: [[0, k.fail], [0.5, k.bg], [1, k.info]], hovertemplate: '%{y} × %{x}: r=%{z:.2f}<extra></extra>', showscale: true, colorbar: { thickness: 10, len: 0.8, tickfont: { size: 10 } } }], { height: 420, margin: { l: 50, r: 10, t: 10, b: 50 }, xaxis: { tickangle: -45 }, yaxis: { autorange: 'reversed' } }));
    (view._charts = view._charts || []).push(node);
  }
  const clusters = (rel.ok && rel.data.clusters) || (U.clusters ? Object.entries(U.clusters).map(([id, ss]) => ({ id, signals: ss })) : []);
  if (clusters.length) {
    const cl = section(t('und.clusters'));
    view.append(cl.root);
    cl.body.append(el('div', { class: 'cols cols-3' }, clusters.map((c, i) => el('div', { class: 'cluster', style: { borderTop: `3px solid ${colorFor(i)}` } }, el('h4', { text: c.id }), c.description ? el('div', { class: 'desc', text: c.description }) : null, el('div', { class: 'sigchips' }, (c.signals || []).map((sid) => chip(sid, 'click', { onClick: () => { if (byId[sid]) showSignal(byId[sid]); } })))))));
  }
  if (sch.ok && (sch.data.grouping_candidates || []).length) {
    const gs = section(t('und.grouping'), { level: 'engineer' });
    view.append(gs.root);
    gs.body.append(table({ columns: [{ label: 'method', key: 'method' }, { label: 'columns', render: (g) => (g.columns || []).join(', ') || '–' }, { label: 'groups', key: 'n_groups', num: true }, { label: 'score', render: (g) => conf(g.score) }, { label: 'rationale', key: 'rationale', cls: 'wrap' }, { label: '', render: (g) => (g.method === sch.data.grouping_method ? chip(t('mon.selected'), 'ok') : '') }], rows: sch.data.grouping_candidates }));
  }
  const hh = hiddenHint(view); if (hh) view.append(hh);
  view.cleanup = () => (view._charts || []).forEach(purge);
  return view;
}
