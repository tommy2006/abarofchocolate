/* Plotly wrapper themed from the CSS tokens, plus inline-SVG sparklines. Plotly is served from the
   Python package at /static/vendor/plotly.min.js; when it is missing, charts degrade to a notice. */
import { el } from './core.js';

export function tokens() {
  const cs = getComputedStyle(document.documentElement);
  const g = (n) => cs.getPropertyValue(n).trim();
  return { bg: g('--bg-2'), ink: g('--ink'), ink2: g('--ink-2'), ink3: g('--ink-3'), line: g('--line'), ok: g('--ok'), warn: g('--warn'), fail: g('--fail'), info: g('--info'), accent: g('--accent'), font: g('--font') };
}
// categorical palette for signals: chosen to keep distinct hue steps in both themes
export const PALETTE = ['#4cc0b2', '#e6a83c', '#7aa7e6', '#e6604f', '#a884d8', '#7fbf5a', '#d47fb0', '#5fb8d8', '#c9a15a', '#8ea0b0', '#5f8f6a', '#c26c8a'];
export const colorFor = (i) => PALETTE[i % PALETTE.length];

export function baseLayout(extra = {}) {
  const k = tokens();
  return Object.assign({
    paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
    font: { family: k.font, color: k.ink2, size: 12 },
    margin: { l: 48, r: 16, t: 28, b: 40 },
    xaxis: { gridcolor: k.line, zerolinecolor: k.line, linecolor: k.line, tickcolor: k.line, automargin: true },
    yaxis: { gridcolor: k.line, zerolinecolor: k.line, linecolor: k.line, tickcolor: k.line, automargin: true },
    legend: { orientation: 'h', y: 1.12, x: 0, font: { size: 11.5 } },
    hoverlabel: { bgcolor: k.bg, bordercolor: k.line, font: { color: k.ink, family: k.font } },
    hovermode: 'x unified',
  }, extra);
}
export const CONFIG = { displaylogo: false, responsive: true, modeBarButtonsToRemove: ['lasso2d', 'select2d', 'toImage'], displayModeBar: 'hover' };

export function plot(node, traces, layout = {}, config = {}) {
  if (!window.Plotly) { node.replaceChildren(el('div', { class: 'notice warn', text: 'Plotly is not loaded (vendor/plotly.min.js missing).' })); return null; }
  const lay = baseLayout(layout);
  // merge axis defaults with caller's axis settings
  for (const ax of ['xaxis', 'yaxis', 'yaxis2']) if (layout[ax]) lay[ax] = Object.assign({}, baseLayout()[ax] || baseLayout().yaxis, layout[ax]);
  return window.Plotly.react(node, traces, lay, Object.assign({}, CONFIG, config));
}
export function purge(node) { if (window.Plotly && node) try { window.Plotly.purge(node); } catch { /* ignore */ } }

/** Inline SVG sparkline; values may contain nulls. */
export function sparkline(values, { w = 120, h = 28, color, fill = true, threshold } = {}) {
  const k = tokens();
  const vals = values.map((v) => (v === null || v === undefined ? NaN : Number(v)));
  const finite = vals.filter((v) => !isNaN(v));
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`); svg.setAttribute('width', w); svg.setAttribute('height', h); svg.classList.add('spark');
  if (!finite.length) return svg;
  let min = Math.min(...finite), max = Math.max(...finite);
  if (threshold !== undefined && threshold !== null) { min = Math.min(min, threshold); max = Math.max(max, threshold); }
  if (max === min) { max = min + 1; }
  const x = (i) => (i / Math.max(1, vals.length - 1)) * (w - 2) + 1;
  const y = (v) => h - 2 - ((v - min) / (max - min)) * (h - 4);
  let d = ''; let started = false;
  vals.forEach((v, i) => { if (isNaN(v)) { started = false; return; } d += (started ? ' L' : ' M') + x(i).toFixed(1) + ' ' + y(v).toFixed(1); started = true; });
  if (fill) {
    const area = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    area.setAttribute('d', d + ` L${x(vals.length - 1).toFixed(1)} ${h - 1} L1 ${h - 1} Z`);
    area.setAttribute('fill', color || k.accent); area.setAttribute('opacity', '0.15'); svg.append(area);
  }
  if (threshold !== undefined && threshold !== null) {
    const th = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    th.setAttribute('x1', 1); th.setAttribute('x2', w - 1); th.setAttribute('y1', y(threshold)); th.setAttribute('y2', y(threshold)); th.setAttribute('stroke', k.fail); th.setAttribute('stroke-dasharray', '3 2'); th.setAttribute('stroke-width', '1'); svg.append(th);
  }
  const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  p.setAttribute('d', d); p.setAttribute('fill', 'none'); p.setAttribute('stroke', color || k.accent); p.setAttribute('stroke-width', '1.4'); p.setAttribute('stroke-linejoin', 'round');
  svg.append(p);
  return svg;
}
