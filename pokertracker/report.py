"""Assemble the self-contained HTML dashboard."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from . import charts, db, stats
from .charts import esc, fmt
from .positions import POSITION_ORDER

# Reference values drawn on the rolling-trend chart. These are targets, not
# measurements: edit them to match the strategy you are actually running.
TARGETS = {"vpip": 22.0, "pfr": 18.0, "threebet": 7.0, "btn_open": 45.0}

CSS = """
:root {
  color-scheme: light;
  --page: #f9f9f7;
  --surface: #fcfcfb;
  --ink: #0b0b0b;
  --ink-2: #52514e;
  --muted: #898781;
  --grid: #e1e0d9;
  --axis: #c3c2b7;
  --border: rgba(11,11,11,0.10);
  --total: #008300;
  --showdown: #2a78d6;
  --nonshowdown: #e34948;
  --ev: #898781;
  --s1: #2a78d6;
  --s2: #eb6834;
  --s3: #1baf7a;
  --pos: #2a78d6;
  --neg: #e34948;
  --good: #0ca30c;
  --warn: #fab219;
  --bad: #d03b3b;
  --seq: #2a78d6;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d;
    --surface: #1a1a19;
    --ink: #ffffff;
    --ink-2: #c3c2b7;
    --muted: #898781;
    --grid: #2c2c2a;
    --axis: #383835;
    --border: rgba(255,255,255,0.10);
    --total: #008300;
    --showdown: #3987e5;
    --nonshowdown: #e66767;
    --ev: #898781;
    --s1: #3987e5;
    --s2: #d95926;
    --s3: #199e70;
    --pos: #3987e5;
    --neg: #e66767;
    --seq: #3987e5;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d;
  --surface: #1a1a19;
  --ink: #ffffff;
  --ink-2: #c3c2b7;
  --muted: #898781;
  --grid: #2c2c2a;
  --axis: #383835;
  --border: rgba(255,255,255,0.10);
  --total: #008300;
  --showdown: #3987e5;
  --nonshowdown: #e66767;
  --ev: #898781;
  --s1: #3987e5;
  --s2: #d95926;
  --s3: #199e70;
  --pos: #3987e5;
  --neg: #e66767;
  --seq: #3987e5;
}

* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 28px 20px 80px;
  background: var(--page);
  color: var(--ink);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
}
.wrap { max-width: 1040px; margin: 0 auto; }
h1 { font-size: 26px; margin: 0 0 4px; letter-spacing: -0.01em; }
h2 { font-size: 19px; margin: 40px 0 6px; letter-spacing: -0.01em; }
h3 { font-size: 15px; margin: 22px 0 6px; color: var(--ink-2); }
p.sub { color: var(--ink-2); margin: 0 0 20px; }
p.note { color: var(--ink-2); font-size: 13.5px; margin: 6px 0 14px; max-width: 74ch; }
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 18px;
  margin: 14px 0;
  overflow-x: auto;
}
.tiles { display: grid; grid-template-columns: repeat(auto-fit,minmax(160px,1fr)); gap: 12px; }
.tile { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }
.tile .k { font-size: 12px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); }
.tile .v { font-size: 27px; font-weight: 600; margin-top: 4px; letter-spacing: -0.02em; }
.tile .m { font-size: 12.5px; color: var(--ink-2); margin-top: 2px; }
svg.chart { width: 100%; height: auto; display: block; overflow: visible; }
svg.grid13 { max-width: 500px; margin: 6px auto; }
.grid { stroke: var(--grid); stroke-width: 1; }
.grid.zero { stroke: var(--axis); }
.axis { stroke: var(--axis); stroke-width: 1; }
.tick { fill: var(--muted); font-size: 11.5px; font-variant-numeric: tabular-nums; }
.tick.small { font-size: 10px; }
.axis-label { fill: var(--ink-2); font-size: 12px; }
.line { fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.line.total { stroke: var(--total); stroke-width: 2.5; }
.line.showdown { stroke: var(--showdown); }
.line.nonshowdown { stroke: var(--nonshowdown); }
.line.ev { stroke: var(--ev); stroke-width: 1.5; }
.line.s1 { stroke: var(--s1); } .line.s2 { stroke: var(--s2); } .line.s3 { stroke: var(--s3); }
.direct-label { font-size: 11.5px; fill: var(--ink-2); }
.target { stroke-width: 1.5; stroke-dasharray: 2 4; opacity: .7; }
.target.s1 { stroke: var(--s1); } .target.s2 { stroke: var(--s2); }
.target.s3 { stroke: var(--s3); } .target.ref { stroke: var(--muted); }
.bar.pos { fill: var(--pos); } .bar.neg { fill: var(--neg); }
.dot.pos { fill: var(--pos); } .dot.neg { fill: var(--neg); }
.dot { stroke: var(--surface); stroke-width: 2; }
.ci { stroke: var(--ink-2); stroke-width: 1.5; opacity: .75; }
.funnel { fill: var(--seq); }
.funnel.step0 { opacity: 1; } .funnel.step1 { opacity: .85; }
.funnel.step2 { opacity: .7; } .funnel.step3 { opacity: .55; }
.funnel.step4 { opacity: .42; }
.funnel-label { fill: var(--ink); font-size: 13px; }
.funnel-value { fill: var(--ink-2); font-size: 12px; font-variant-numeric: tabular-nums; }
.funnel-money { font-size: 11.5px; font-variant-numeric: tabular-nums; }
.funnel-money.pos { fill: var(--pos); } .funnel-money.neg { fill: var(--neg); }
.hover-target { fill: transparent; }
.crosshair { stroke: var(--muted); stroke-width: 1; stroke-dasharray: 3 3; }
.chart-wrap, .heatmap { position: relative; }
.tooltip {
  position: absolute; pointer-events: none; z-index: 5;
  background: var(--surface); color: var(--ink);
  border: 1px solid var(--border); border-radius: 8px;
  padding: 8px 10px; font-size: 12.5px; line-height: 1.45;
  box-shadow: 0 6px 20px rgba(0,0,0,.18); white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
.tooltip b { font-weight: 600; }
.tooltip .sw { display:inline-block; width:9px; height:9px; border-radius:2px; margin-right:6px; }
.legend { display: flex; flex-wrap: wrap; gap: 16px; margin: 10px 0 2px; font-size: 13px; color: var(--ink-2); }
.legend span { display: inline-flex; align-items: center; gap: 7px; }
.legend i { width: 22px; height: 0; border-top-width: 2.5px; border-top-style: solid; display: inline-block; }
table { border-collapse: collapse; width: 100%; font-size: 13.5px; }
th, td { text-align: right; padding: 7px 10px; border-bottom: 1px solid var(--border); font-variant-numeric: tabular-nums; }
th:first-child, td:first-child { text-align: left; font-variant-numeric: normal; }
th { color: var(--muted); font-weight: 500; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
tbody tr:last-child td { border-bottom: none; }
td.ci { color: var(--muted); font-size: 12.5px; }
td.thin { color: var(--muted); }
.pill { display:inline-block; padding: 1px 8px; border-radius: 999px; font-size: 11.5px; font-weight: 500; white-space: nowrap; }
.pill.good { background: color-mix(in srgb, var(--good) 16%, transparent); color: var(--good); }
.pill.warn { background: color-mix(in srgb, var(--warn) 20%, transparent); color: var(--ink); }
.pill.bad { background: color-mix(in srgb, var(--bad) 16%, transparent); color: var(--bad); }
.pill.none { background: color-mix(in srgb, var(--muted) 16%, transparent); color: var(--ink-2); }
/* "n too small" is not a soft failure and must not borrow the failure colour. */
.pill.thin { background: transparent; color: var(--muted); border: 1px dashed var(--border); }
.chip-row { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-bottom: 8px; }
.chip-label { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; margin-right: 4px; }
.chip {
  font: inherit; font-size: 12.5px; padding: 3px 11px; border-radius: 999px; cursor: pointer;
  background: transparent; color: var(--ink-2); border: 1px solid var(--border);
}
.chip:hover { background: color-mix(in srgb, var(--ink) 6%, transparent); }
.chip[aria-pressed="true"] { background: var(--ink); color: var(--surface); border-color: var(--ink); }
.cell { fill: var(--surface); stroke: var(--border); stroke-width: 1; }
.cell-label { font-size: 9.5px; fill: var(--ink-2); pointer-events: none; }
.cellg { cursor: default; }
.scale-legend { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--muted); justify-content: center; margin-top: 6px; }
.scale-legend .ramp { display: flex; }
.scale-legend .ramp i { width: 16px; height: 10px; display: block; }
.panels { display: flex; flex-wrap: wrap; gap: 20px; justify-content: center; }
.panels .panel { flex: 1 1 320px; max-width: 500px; }
.panel-head { font-size: 12px; text-transform: uppercase; letter-spacing: .05em;
  color: var(--muted); text-align: center; margin-bottom: 2px; }
/* The shaded period on the cumulative charts. Neutral ink rather than an
   accent colour: it marks an extent, it is not a fifth data series. */
.period-band { fill: var(--ink); opacity: .07; }
/* A delta is never coloured by sign. Donk flop falling six points is not good
   news or bad news until you know what you were trying to do, and a red number
   would answer that question for the reader before they had asked it. */
td.delta { color: var(--ink); font-weight: 500; white-space: nowrap; }
td.delta.none { color: var(--muted); font-weight: 400; }
td.prior { color: var(--ink-2); }
td small.per100 { display: block; color: var(--muted); font-size: 11.5px; white-space: nowrap; }
.period-bar .chip-count { opacity: .6; }
.period-bar { margin: 0 0 18px; }
.period-bar .chip[aria-pressed="true"] { background: var(--ink); color: var(--surface); }
.period-note { font-size: 12.5px; color: var(--muted); margin: -10px 0 18px; }
details { margin-top: 10px; }
summary { cursor: pointer; font-size: 13px; color: var(--ink-2); }
.empty { color: var(--muted); font-style: italic; padding: 20px 0; }
.banner {
  border-left: 3px solid var(--warn); background: color-mix(in srgb, var(--warn) 9%, transparent);
  padding: 10px 14px; border-radius: 0 8px 8px 0; font-size: 13.5px; margin: 14px 0;
}
footer { margin-top: 46px; color: var(--muted); font-size: 12.5px; }
.toolbar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin: 0 0 20px; }
.status { font-size: 13px; color: var(--ink-2); }
.status.busy::after {
  content: ''; display: inline-block; width: 9px; height: 9px; margin-left: 7px;
  border: 2px solid var(--muted); border-top-color: transparent; border-radius: 50%;
  vertical-align: -1px; animation: spin .7s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.hands th.sortable-col { cursor: pointer; user-select: none; white-space: nowrap; }
.hands th.sortable-col:hover { color: var(--ink-2); }
.hands th.sortable-col::after { content: ' \\2195'; opacity: .25; }
.hands th.sortable-col[aria-sort="ascending"]::after { content: ' \\25B2'; opacity: .9; }
.hands th.sortable-col[aria-sort="descending"]::after { content: ' \\25BC'; opacity: .9; }
.hands td.cards, .hands td.board { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 12.5px; letter-spacing: .02em; }
.hands td.board { color: var(--ink-2); }
.hands td.money.pos { color: var(--pos); } .hands td.money.neg { color: var(--neg); }
.hands td.when { white-space: nowrap; }
.hands td.when small { display: block; color: var(--muted); font-size: 11.5px; }
.hands td.opp { color: var(--ink-2); font-size: 12.5px; }
.hand-btn {
  font: inherit; font-size: 12.5px; padding: 2px 8px; border-radius: 6px; cursor: pointer;
  background: transparent; color: var(--ink-2); border: 1px solid var(--border);
  font-variant-numeric: tabular-nums;
}
.hand-btn:hover { background: color-mix(in srgb, var(--ink) 6%, transparent); color: var(--ink); }
.hand-btn[aria-expanded="true"] { background: var(--ink); color: var(--surface); border-color: var(--ink); }
.raw-panel { margin-top: 14px; border-top: 1px solid var(--border); padding-top: 12px; }
.raw-panel .raw-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 8px; font-size: 12.5px; color: var(--muted); }
.raw-panel pre {
  margin: 0; padding: 12px 14px; border-radius: 8px; overflow-x: auto;
  background: color-mix(in srgb, var(--ink) 5%, transparent);
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: 12px; line-height: 1.45; white-space: pre;
}
.tag { display:inline-block; padding: 1px 7px; border-radius: 999px; font-size: 11.5px; white-space: nowrap;
  background: color-mix(in srgb, var(--muted) 14%, transparent); color: var(--ink-2); }
.tag.win { background: color-mix(in srgb, var(--pos) 16%, transparent); color: var(--pos); }
.tag.loss { background: color-mix(in srgb, var(--neg) 16%, transparent); color: var(--neg); }
@media (max-width: 640px) { body { padding: 18px 12px 60px; } .tile .v { font-size: 23px; } }
"""

JS = """
(function () {
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

  // The selected period, and everything that needs telling when it changes.
  // Sections that re-render server-side are swapped wholesale; the charts and
  // the heatmap instead redraw from data they already hold, because they show
  // the selected period in the context of the whole history rather than alone.
  let PERIOD = 'all';
  let BANDS = {};
  let RAWS = {};
  const listeners = [];

  function isDark() {
    const t = document.documentElement.getAttribute('data-theme');
    if (t) return t === 'dark';
    return window.matchMedia('(prefers-color-scheme: dark)').matches;
  }
  const SEQ_LIGHT = ['#cde2fb','#b7d3f6','#9ec5f4','#86b6ef','#6da7ec','#5598e7',
                     '#3987e5','#2a78d6','#256abf','#1c5cab','#184f95','#104281','#0d366b'];
  function hex2rgb(h){return [parseInt(h.slice(1,3),16),parseInt(h.slice(3,5),16),parseInt(h.slice(5,7),16)];}
  function rgb2hex(c){return '#'+c.map(v=>Math.max(0,Math.min(255,Math.round(v))).toString(16).padStart(2,'0')).join('');}
  function lerp(a,b,t){return a.map((v,i)=>v+(b[i]-v)*t);}
  function lum(h){const c=hex2rgb(h).map(v=>{v/=255;return v<=0.03928?v/12.92:Math.pow((v+0.055)/1.055,2.4);});
    return 0.2126*c[0]+0.7152*c[1]+0.0722*c[2];}

  // Sequential: one hue, light->dark on light surfaces and dark->light on dark.
  function seqColor(t) {
    const ramp = isDark() ? SEQ_LIGHT.slice().reverse() : SEQ_LIGHT;
    const i = Math.max(0, Math.min(ramp.length - 1, Math.round(t * (ramp.length - 1))));
    return ramp[i];
  }
  // Diverging: two poles either side of a neutral grey midpoint.
  function divColor(t) {
    const mid = hex2rgb(isDark() ? '#383835' : '#f0efec');
    const pos = hex2rgb(isDark() ? '#3987e5' : '#2a78d6');
    const neg = hex2rgb(isDark() ? '#e66767' : '#e34948');
    return t >= 0 ? rgb2hex(lerp(mid, pos, Math.min(1, t)))
                  : rgb2hex(lerp(mid, neg, Math.min(1, -t)));
  }

  function place(tip, wrap, ev) {
    const r = wrap.getBoundingClientRect();
    let x = ev.clientX - r.left + 14, y = ev.clientY - r.top + 14;
    if (x + tip.offsetWidth > r.width) x = ev.clientX - r.left - tip.offsetWidth - 14;
    if (y + tip.offsetHeight > r.height) y = ev.clientY - r.top - tip.offsetHeight - 14;
    tip.style.left = Math.max(0, x) + 'px';
    tip.style.top = Math.max(0, y) + 'px';
  }

  // ---- cumulative winnings: crosshair + shared tooltip
  $$('[data-chart="winnings"]').forEach(function (wrap) {
    const d = JSON.parse(wrap.dataset.payload);
    const svg = $('svg', wrap), tip = $('.tooltip', wrap);
    const hair = $('.crosshair', wrap), target = $('.hover-target', wrap);
    const n = d.series[0].values.length;
    if (!n) return;
    function toUser(ev) {
      const pt = svg.createSVGPoint();
      pt.x = ev.clientX; pt.y = ev.clientY;
      return pt.matrixTransform(svg.getScreenCTM().inverse());
    }
    target.addEventListener('mousemove', function (ev) {
      const u = toUser(ev);
      const frac = (u.x - d.left) / (d.right - d.left);
      const i = Math.max(0, Math.min(n - 1, Math.round(frac * (n - 1))));
      const x = d.left + (i / Math.max(1, n - 1)) * (d.right - d.left);
      hair.setAttribute('x1', x); hair.setAttribute('x2', x);
      hair.style.display = '';
      tip.hidden = false;
      tip.innerHTML = '<b>Hand ' + (i + 1) + '</b><br>' + d.series.map(function (s) {
        return '<span class="sw" style="background:var(--' + s.key + ')"></span>' +
               s.name + ' <b>' + s.values[i].toFixed(1) + ' bb</b>';
      }).join('<br>');
      place(tip, wrap, ev);
    });
    target.addEventListener('mouseleave', function () {
      hair.style.display = 'none'; tip.hidden = true;
    });

    const band = $('.period-band', wrap);
    listeners.push(function () {
      const b = BANDS[PERIOD];
      if (!band) return;
      if (!b) { band.setAttribute('width', 0); return; }
      const at = i => d.left + ((i - d.x0) / Math.max(1, d.x1 - d.x0)) * (d.right - d.left);
      const x0 = at(b[0]), x1 = at(b[1]);
      band.setAttribute('x', x0.toFixed(1));
      band.setAttribute('width', Math.max(1.5, x1 - x0).toFixed(1));
    });
  });

  // ---- rolling discipline: the same band, on a hand-number axis
  $$('[data-chart="rolling"]').forEach(function (wrap) {
    const d = JSON.parse(wrap.dataset.payload);
    const band = $('.period-band', wrap);
    if (!band) return;
    listeners.push(function () {
      const b = BANDS[PERIOD];
      if (!b) { band.setAttribute('width', 0); return; }
      const at = v => d.left + ((v - d.x0) / Math.max(1, d.x1 - d.x0)) * (d.right - d.left);
      const x0 = Math.max(d.left, Math.min(d.right, at(b[0])));
      const x1 = Math.max(d.left, Math.min(d.right, at(b[1])));
      band.setAttribute('x', x0.toFixed(1));
      band.setAttribute('width', Math.max(0, x1 - x0).toFixed(1));
    });
  });

  // ---- range heatmap: position filter, metric switch, selected-vs-prior
  $$('.heatmap').forEach(function (wrap) {
    const data = JSON.parse(wrap.dataset.payload);
    const tip = $('.tooltip', wrap), legend = $('.scale-legend', wrap);
    const priPanel = $('.panel[data-side="pri"]', wrap);
    const selHead = $('.panel[data-side="sel"] .panel-head', wrap);
    let pos = 'All', metric = 'freq';

    // Cells arrive as [n, vpip, bb100]; the field names cost more than the
    // numbers, and every period carries a grid for both panels.
    function cellOf(side, combo) {
      const p = data[PERIOD] || data.all || {};
      const g = (p[side] || {})[pos] || {};
      const v = g[combo];
      if (!v || !v[0]) return null;
      return { n: v[0], vpip: v[1], freq: 100 * v[1] / v[0], bb100: v[2] };
    }

    function paint() {
      const p = data[PERIOD] || data.all || {};
      const two = PERIOD !== 'all' && !!p.pri;
      priPanel.hidden = !two;
      selHead.textContent = two ? 'Selected period' : 'All hands';
      const sides = two ? ['sel', 'pri'] : ['sel'];

      // One scale across both panels. Two grids scaled independently would
      // show a difference in colour that is not a difference in the data.
      let maxAbs = 1;
      sides.forEach(function (side) {
        const g = (p[side] || {})[pos] || {};
        Object.keys(g).forEach(function (k) {
          const v = g[k][2];
          if (v !== null && v !== undefined) maxAbs = Math.max(maxAbs, Math.abs(v));
        });
      });

      $$('.cellg', wrap).forEach(function (cell) {
        const d = cellOf(cell.dataset.side, cell.dataset.combo);
        const rect = $('rect', cell), label = $('text', cell);
        const value = d ? d[metric] : null;
        if (value === null || value === undefined) {
          rect.setAttribute('fill', 'none');
          rect.setAttribute('stroke-dasharray', '2 2');
          label.setAttribute('fill', 'var(--muted)');
          label.setAttribute('opacity', '0.5');
          return;
        }
        rect.removeAttribute('stroke-dasharray');
        label.removeAttribute('opacity');
        // Floor the sequential ramp so a combo that was dealt but never played
        // still reads as a filled cell rather than vanishing into the surface.
        const fill = metric === 'freq' ? seqColor(0.12 + 0.88 * (d.freq / 100))
                                       : divColor(d.bb100 / maxAbs);
        rect.setAttribute('fill', fill);
        label.setAttribute('fill', lum(fill) > 0.42 ? '#0b0b0b' : '#ffffff');
      });

      const steps = [];
      for (let i = 0; i <= 8; i++) {
        const t = i / 8;
        steps.push('<i style="background:' +
          (metric === 'freq' ? seqColor(t) : divColor(t * 2 - 1)) + '"></i>');
      }
      legend.innerHTML = (metric === 'freq'
        ? '<span>0%</span><span class="ramp">' + steps.join('') + '</span><span>100% played</span>'
        : '<span>-' + maxAbs.toFixed(0) + '</span><span class="ramp">' + steps.join('') +
          '</span><span>+' + maxAbs.toFixed(0) + ' bb/100</span>');
      $$('.chip[data-pos]', wrap).forEach(b =>
        b.setAttribute('aria-pressed', String(b.dataset.pos === pos)));
      $$('.chip.metric', wrap).forEach(b =>
        b.setAttribute('aria-pressed', String(b.dataset.metric === metric)));
    }

    $$('.chip[data-pos]', wrap).forEach(b =>
      b.addEventListener('click', () => { pos = b.dataset.pos; paint(); }));
    $$('.chip.metric', wrap).forEach(b =>
      b.addEventListener('click', () => { metric = b.dataset.metric; paint(); }));

    $$('.cellg', wrap).forEach(function (cell) {
      cell.addEventListener('mousemove', function (ev) {
        const d = cellOf(cell.dataset.side, cell.dataset.combo);
        const which = cell.dataset.side === 'pri' ? 'prior' : 'selected';
        tip.hidden = false;
        tip.innerHTML = '<b>' + cell.dataset.combo + '</b> &middot; ' + pos +
          (PERIOD === 'all' ? '' : ' &middot; ' + which) + '<br>' +
          (d ? ('dealt ' + d.n + ' time' + (d.n === 1 ? '' : 's') + '<br>played ' +
            d.freq.toFixed(0) + '%' +
            (d.bb100 === null ? '' : '<br>' + d.bb100.toFixed(0) + ' bb/100'))
             : 'never dealt');
        place(tip, wrap, ev);
      });
      cell.addEventListener('mouseleave', () => { tip.hidden = true; });
    });
    listeners.push(paint);
    paint();
  });


  // ---- biggest pots: table switch, column sort, raw-text panel
  // Bound through a function rather than inline, because the period filter
  // replaces this section's markup wholesale and the handlers go with it.
  function initDrilldown(root) {
    $$('[data-drilldown]', root).forEach(function (wrap) {
      const panel = $('.raw-panel', wrap);
      const pre = $('pre', panel), head = $('.raw-head span', panel);

      $$('.chip[data-tbl]', wrap).forEach(function (b) {
        b.addEventListener('click', function () {
          $$('.chip[data-tbl]', wrap).forEach(o =>
            o.setAttribute('aria-pressed', String(o === b)));
          $$('table.hands', wrap).forEach(t => { t.hidden = t.dataset.tbl !== b.dataset.tbl; });
          // The open hand belongs to the table being switched away from.
          $$('.hand-btn', wrap).forEach(o => o.setAttribute('aria-expanded', 'false'));
          panel.hidden = true;
        });
      });

      // Sort on any column. Numeric columns carry data-v so "-12.4" and a blank
      // cell order correctly regardless of how they are formatted for display.
      $$('table.hands', wrap).forEach(function (table) {
        const body = $('tbody', table);
        $$('th.sortable-col', table).forEach(function (th, idx) {
          th.addEventListener('click', function () {
            const num = th.dataset.type === 'num';
            const desc = th.getAttribute('aria-sort') !== 'descending';
            const rows = $$('tr', body);
            rows.sort(function (a, b) {
              const ca = a.children[idx], cb = b.children[idx];
              let x, y;
              if (num) {
                x = parseFloat(ca.dataset.v); y = parseFloat(cb.dataset.v);
                if (isNaN(x)) x = -Infinity;
                if (isNaN(y)) y = -Infinity;
              } else {
                x = (ca.dataset.v || ca.textContent).trim().toLowerCase();
                y = (cb.dataset.v || cb.textContent).trim().toLowerCase();
              }
              if (x < y) return desc ? 1 : -1;
              if (x > y) return desc ? -1 : 1;
              return 0;
            });
            rows.forEach(r => body.appendChild(r));
            $$('th.sortable-col', table).forEach(o => o.removeAttribute('aria-sort'));
            th.setAttribute('aria-sort', desc ? 'descending' : 'ascending');
          });
        });
      });

      $$('.hand-btn', wrap).forEach(function (btn) {
        btn.addEventListener('click', function () {
          const open = btn.getAttribute('aria-expanded') === 'true';
          $$('.hand-btn', wrap).forEach(o => o.setAttribute('aria-expanded', 'false'));
          if (open) { panel.hidden = true; return; }
          btn.setAttribute('aria-expanded', 'true');
          // One pool of hand text for every period: the biggest-pot lists
          // overlap heavily, and re-reading the same hand per period would
          // inline it five times over.
          const txt = RAWS[btn.dataset.hand];
          head.textContent = txt ? ('Hand #' + btn.dataset.no)
            : ('Hand #' + btn.dataset.no + ' — source file no longer readable');
          pre.textContent = txt || '';
          panel.hidden = false;
          panel.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        });
      });
    });
  }
  initDrilldown(document);


  // ---- period filter
  const periodData = document.getElementById('period-data');
  if (periodData) {
    const D = JSON.parse(periodData.textContent);
    RAWS = D.raws || {};
    D.periods.forEach(function (p) { if (p.band) BANDS[p.key] = p.band; });

    function applyPeriod(key) {
      PERIOD = key;
      const regions = D.regions[key] || {};
      Object.keys(regions).forEach(function (id) {
        const el = document.getElementById(id);
        if (!el) return;
        el.innerHTML = regions[id];
        initDrilldown(el);
      });
      $$('.period-bar .chip').forEach(b =>
        b.setAttribute('aria-pressed', String(b.dataset.period === key)));
      listeners.forEach(function (fn) { fn(); });
    }

    $$('.period-bar .chip').forEach(function (b) {
      b.addEventListener('click', function () { applyPeriod(b.dataset.period); });
    });
    // Deliberately not restored from storage and never defaulted to anything
    // but "All": the pull with a period filter is to open the report after
    // every session and read today's win rate, which is a number with a
    // +/-150 bb/100 interval on it.
    listeners.forEach(function (fn) { fn(); });
  }

  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function () {
    listeners.forEach(function (fn) { fn(); });
  });
})();
"""


LIVE_JS = """
(function () {
  const status = document.getElementById('refresh-status');
  if (!status) return;
  const SCROLL = 'pt-scroll';
  const EVERY = 60000;

  // A reload would otherwise throw the reader back to the top of the page.
  const saved = sessionStorage.getItem(SCROLL);
  if (saved !== null) {
    window.scrollTo(0, parseInt(saved, 10));
    sessionStorage.removeItem(SCROLL);
  }

  // The page was rendered by the server on this request, so it is as fresh as
  // a poll that just returned. Every later poll moves the stamp, whether or
  // not it found anything, because "we looked and there was nothing" is the
  // reassurance the line exists to give.
  let stamp = new Date();
  let busy = false;

  function clock(d) {
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }
  function idle(text) {
    status.textContent = text;
    status.classList.remove('busy');
  }
  function showStamp() {
    if (busy) return;
    const secs = Math.round((Date.now() - stamp.getTime()) / 1000);
    const ago = secs < 90 ? secs + 's ago'
              : Math.round(secs / 60) + 'm ago';
    idle('Last refreshed ' + clock(stamp) + ' · ' + ago);
  }

  async function refresh() {
    if (busy) return;
    busy = true;
    status.textContent = 'Checking for new hands';
    status.classList.add('busy');
    try {
      const res = await fetch('/api/refresh', { method: 'POST' });
      const d = await res.json();
      if (!d.ok) {
        busy = false;
        idle('Refresh failed at ' + clock(new Date()) + ': ' + (d.error || 'unknown error'));
        return;
      }
      stamp = new Date();
      if (!d.new_hands) {
        busy = false;
        showStamp();
        return;
      }
      status.textContent = '+' + d.new_hands + ' hands, reloading';
      sessionStorage.setItem(SCROLL, String(window.scrollY));
      location.reload();
    } catch (e) {
      busy = false;
      idle('Refresh failed at ' + clock(new Date()) + ': ' + e.message);
    }
  }

  showStamp();
  setInterval(showStamp, 5000);
  setInterval(refresh, EVERY);
})();
"""


_PILLS = {
    "ok": '<span class="pill good">on plan</span>',
    "off": '<span class="pill bad">off plan</span>',
    "watch": '<span class="pill none">watch</span>',
    # Not a soft failure. "0 opens out of 2 opportunities" is not a player who
    # never opens the button; it is a window that cannot answer the question,
    # and saying "off plan" there teaches the reader to distrust the pills.
    "thin": '<span class="pill thin">n too small</span>',
}


def _pill(status) -> str:
    return _PILLS.get(status, _PILLS["watch"])


def _delta_cell(new, old, unit: str, enough: bool = True) -> str:
    """One difference, unsigned by colour and blank when it would be noise."""
    if not enough or new is None or old is None:
        return ('<td class="delta none" title="Too few opportunities on one '
                'side for the difference to mean anything">&ndash;</td>')
    d = new - old
    return f'<td class="delta">{d:+.1f}{unit}</td>'


def _rate_table(rows, prior=None) -> str:
    """Rates with their intervals, and against the prior period when filtered.

    The delta column is the point of the period filter: "is the thing I am
    working on moving" is a different question from "what is my donk-flop
    frequency", and only the first one has an answer on a Tuesday. It is left
    blank whenever either side has fewer than MIN_OPPS opportunities, because
    the difference between two noisy numbers is noisier than either of them.
    """
    if prior is None:
        body = []
        for r in rows:
            pct = "-" if r["pct"] is None else f"{r['pct']:.1f}%"
            ci = ("-" if r["ci_lo"] is None or not r["den"]
                  else f"{r['ci_lo']:.1f} - {r['ci_hi']:.1f}")
            body.append(
                f"<tr><td>{esc(r['name'])}</td><td>{pct}</td>"
                f"<td class='ci'>{ci}</td>"
                f"<td class='thin'>{int(r['num'])} / {int(r['den'])}</td></tr>"
            )
        return (
            "<table><thead><tr><th>Stat</th><th>Rate</th>"
            "<th>95% CI</th><th>n</th></tr></thead><tbody>"
            + "".join(body) + "</tbody></table>"
        )

    by_name = {r["name"]: r for r in prior}
    body = []
    for r in rows:
        o = by_name.get(r["name"], {"pct": None, "den": 0, "num": 0})
        pct = "-" if r["pct"] is None else f"{r['pct']:.1f}%"
        opct = "-" if o["pct"] is None else f"{o['pct']:.1f}%"
        ci = ("-" if r["ci_lo"] is None or not r["den"]
              else f"{r['ci_lo']:.1f} - {r['ci_hi']:.1f}")
        enough = r["den"] >= stats.MIN_OPPS and o["den"] >= stats.MIN_OPPS
        body.append(
            f"<tr><td>{esc(r['name'])}</td><td>{pct}</td>"
            f"<td class='prior'>{opct}</td>"
            + _delta_cell(r["pct"], o["pct"], "", enough)
            + f"<td class='ci'>{ci}</td>"
            f"<td class='thin'>{int(r['num'])} / {int(r['den'])}</td></tr>"
        )
    return (
        "<table><thead><tr><th>Stat</th><th>Selected</th><th>Prior</th>"
        "<th>&Delta;</th><th>95% CI (selected)</th><th>n</th></tr></thead><tbody>"
        + "".join(body) + "</tbody></table>"
    )


def _compliance_table(rows, prior=None) -> str:
    """Compliance checks as rates, not as counters that only go up.

    A cumulative count stops carrying information after the first week: once
    "SB cold calls" reads 11 it reads 11 forever, and no amount of good play
    moves it. Both the per-100-hand rate and the comparison against the prior
    period exist so the number can come down.
    """
    def value_cell(c, cls=""):
        if c["kind"] == "count":
            per = ("" if c["per100"] is None else
                   f"<small class='per100'>{c['per100']:.2f} per 100</small>")
            return f"<td class='{cls}'>{int(c['num'])}{per}</td>"
        pct = "-" if c["value"] is None else f"{c['value']:.1f}%"
        return (f"<td class='{cls}'>{pct}"
                f"<small class='per100'>{int(c['num'])} / {int(c['den'])}</small></td>")

    by_name = {c["name"]: c for c in (prior or [])}
    body = []
    for c in rows:
        cells = [f"<td>{esc(c['name'])}</td>", value_cell(c)]
        if prior is not None:
            o = by_name.get(c["name"])
            if o is None:
                cells.append("<td class='prior'>-</td>")
                cells.append(_delta_cell(None, None, "", False))
            else:
                cells.append(value_cell(o, "prior"))
                if c["kind"] == "count":
                    # Raw counts across windows of different sizes are not
                    # comparable; the per-100 rate is what "more or less than
                    # before" actually means.
                    cells.append(_delta_cell(c["per100"], o["per100"], " /100",
                                             bool(c["den"] and o["den"])))
                else:
                    enough = (c["den"] >= stats.MIN_OPPS
                              and o["den"] >= stats.MIN_OPPS)
                    cells.append(_delta_cell(c["value"], o["value"], " pp", enough))
        cells.append(f"<td class='thin'>{esc(c['target'])}</td>")
        cells.append(f"<td>{_pill(c['status'])}</td>")
        cells.append(f"<td class='thin'>{esc(c['note'])}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")

    extra = "<th>Prior</th><th>&Delta;</th>" if prior is not None else ""
    head = (f"<tr><th>Check</th><th>Selected</th>{extra}"
            f"<th>Target</th><th></th><th>Note</th></tr>")
    return f"<table><thead>{head}</thead><tbody>{''.join(body)}</tbody></table>"


HAND_COLUMNS = [
    ("Hand", "text"), ("Time, table", "text"), ("Pos", "text"), ("Cards", "text"),
    ("Board", "text"), ("Pot (bb)", "num"), ("Net (bb)", "num"),
    ("Exit", "num"), ("Result", "text"), ("Shown", "text"),
]

# The order the streets actually happen in, so sorting the Exit column reads as
# a progression through the hand rather than alphabetically as flop/preflop/river.
_EXIT_RANK = {"preflop": 0, "flop": 1, "turn": 2, "river": 3, "showdown": 4}


def _hand_rows(rows) -> str:
    out = []
    for r in rows:
        tag = "win" if r["net_bb"] > 0 else "loss"
        shown = ", ".join(f"{x['player']} {x['cards']}" for x in r["shown"])
        out.append(
            "<tr>"
            f"<td><button type=\"button\" class=\"hand-btn\" data-hand=\"{r['hand_id']}\" "
            f"data-no=\"{esc(r['site_hand_no'])}\" aria-expanded=\"false\" "
            f"title=\"Show the raw hand history\">#{esc(r['site_hand_no'])}</button></td>"
            f"<td class=\"when\" data-v=\"{esc(r['played_at'])}\">{esc(r['played_at'][:16])}"
            f"<small>{esc(r['table_name'])}</small></td>"
            f"<td>{esc(r['position'])}</td>"
            f"<td class=\"cards\">{esc(r['hole_cards'])}</td>"
            f"<td class=\"board\">{esc(r['board'])}</td>"
            f"<td data-v=\"{r['pot_bb']:.4f}\">{r['pot_bb']:.1f}</td>"
            f"<td class=\"money {'pos' if r['net_bb'] > 0 else 'neg'}\" "
            f"data-v=\"{r['net_bb']:.4f}\">{r['net_bb']:+.1f}</td>"
            f"<td data-v=\"{_EXIT_RANK.get(r['exit_street'], 9)}\">{esc(r['exit_street'])}</td>"
            f"<td><span class=\"tag {tag}\">{esc(r['outcome'])}</span></td>"
            f"<td class=\"opp\">{esc(shown)}</td>"
            "</tr>"
        )
    return "".join(out)


def _hand_table(rows, key: str, hidden: bool) -> str:
    if not rows:
        return (f'<table class="hands" data-tbl="{key}"{" hidden" if hidden else ""}>'
                f'<tbody><tr><td class="empty">No hands in this direction yet.</td>'
                f'</tr></tbody></table>')
    head = "".join(
        f'<th class="sortable-col" data-type="{typ}">{esc(name)}</th>'
        for name, typ in HAND_COLUMNS
    )
    return (f'<table class="hands" data-tbl="{key}"{" hidden" if hidden else ""}>'
            f'<thead><tr>{head}</tr></thead><tbody>{_hand_rows(rows)}</tbody></table>')


def _drilldown(conn, hero: str, window, limit: int = 15):
    """Top losses and top wins, each row expandable to the original text.

    Returns the markup and the hand ids it references. The text itself is
    pooled across every period by the caller rather than inlined here: the
    biggest-pot lists overlap heavily, and a per-period copy would put the same
    hand in the file five times.
    """
    pots = stats.big_pots(conn, hero, limit, window)
    ids = [r["hand_id"] for r in pots["losses"] + pots["wins"]]
    n_loss, n_win = len(pots["losses"]), len(pots["wins"])
    html = f"""<div class="card" data-drilldown>
<div class="chip-row"><span class="chip-label">Show</span>
<button type="button" class="chip" data-tbl="losses" aria-pressed="true">
Top {n_loss} losses</button>
<button type="button" class="chip" data-tbl="wins" aria-pressed="false">
Top {n_win} wins</button></div>
{_hand_table(pots['losses'], 'losses', False)}
{_hand_table(pots['wins'], 'wins', True)}
<p class="note">Click any column heading to re-sort; click a hand number to read
the original PokerStars text.</p>
<div class="raw-panel" hidden><div class="raw-head"><span></span></div><pre></pre></div>
</div>"""
    return html, ids


def _compact_grid(grid: dict) -> dict:
    """[n, vpip, bb100] per combo. See the note in charts.range_heatmap."""
    return {c: [v["n"], v["vpip"],
                None if v["bb100"] is None else round(v["bb100"], 1)]
            for c, v in grid.items()}


def _rate_of(rows, name: str):
    r = next((x for x in rows if x["name"] == name), None)
    return None if r is None or r["pct"] is None else r["pct"]


def _fragments(conn, hero: str, period, positions, generated: str, stake_txt: str):
    """Every part of the page that a period filter changes.

    Each fragment is rendered here, once per period, and swapped into place in
    the browser. The report has to open from file:// with no server behind it,
    so the alternative -- asking the pipeline to recompute on selection -- is
    not available.
    """
    w = period.selected
    prior_w = None if period.is_all else period.prior
    filtered = not period.is_all

    m = stats.money_summary(conn, hero, w)
    pre = stats.preflop_stats(conn, hero, w)
    post = stats.postflop_stats(conn, hero, w)
    ag = stats.aggression(conn, hero, w)
    comp = stats.compliance(conn, hero, w)
    pre_prior = stats.preflop_stats(conn, hero, prior_w) if filtered else None
    post_prior = stats.postflop_stats(conn, hero, prior_w) if filtered else None
    comp_prior = stats.compliance(conn, hero, prior_w) if filtered else None

    pos_rows = stats.by_position(conn, hero, w)
    funnel_rows = stats.street_funnel(conn, hero, w)
    buckets = stats.pot_buckets(conn, hero, w)
    stacks = stats.stack_histogram(conn, hero, 10, w)
    tables = stats.by_table_count(conn, hero, w)
    hours = stats.by_time(conn, hero, "%H", w)
    days = stats.by_time(conn, hero, "%w", w)
    n_sessions = len(stats.sessions(conn, hero, w))
    drill_html, drill_ids = _drilldown(conn, hero, w, 15 if period.is_all else 10)

    # ---- tiles. Win rate is deliberately absent from a filtered view: one
    # session is 100-300 hands and its interval is wider than any result it
    # could show, so a tile there would only invite the reader to try to read
    # it. Hand count and the frequencies are what the window can support.
    if filtered:
        tiles = f"""
    <div class="tiles">
      <div class="tile"><div class="k">Hands</div>
        <div class="v">{m['hands']:,}</div>
        <div class="m">{n_sessions} session{'' if n_sessions == 1 else 's'}
        &middot; {m['hands'] * 100 // max(1, period.selected.hands + period.prior.hands)}%
        of the sample</div></div>
      <div class="tile"><div class="k">VPIP</div>
        <div class="v">{fmt(_rate_of(pre, 'VPIP'), 1)}%</div>
        <div class="m">voluntarily put in pot</div></div>
      <div class="tile"><div class="k">PFR</div>
        <div class="v">{fmt(_rate_of(pre, 'PFR'), 1)}%</div>
        <div class="m">preflop raise</div></div>
      <div class="tile"><div class="k">3-Bet</div>
        <div class="v">{fmt(_rate_of(pre, '3-Bet'), 1)}%</div>
        <div class="m">of opportunities</div></div>
      <div class="tile"><div class="k">Rake paid</div>
        <div class="v">{m['rake_bb100']:.1f}</div>
        <div class="m">bb/100</div></div>
    </div>"""
        banner = (
            '<div class="banner"><b>Win rate is not shown for a filtered '
            f'period, on purpose.</b> Over {m["hands"]:,} hands the 95% interval '
            f'on bb/100 is roughly &plusmn;{m["ci95"]:.0f}, which is wider than '
            'any result it could report, so the number would be pure noise '
            'wearing a decimal point. Frequencies converge in thousands of '
            'hands rather than hundreds &mdash; those are what this view is '
            'for. Switch back to <b>All</b> for the win rate.</div>'
        )
        sub = (f"{esc(period.label)} &middot; {m['hands']:,} hands &middot; "
               f"{esc(m['first_hand'][:16])} to {esc(m['last_hand'][:16])} "
               f"&middot; compared against the {period.prior.hands:,} hands before it")
    else:
        ev_tile = ""
        if m["has_ev"]:
            ev_tile = (
                f'<div class="tile"><div class="k">All-in adjusted</div>'
                f'<div class="v">{m["ev_bb100"]:+.1f}</div>'
                f'<div class="m">bb/100 &middot; luck {m["ev_diff_bb100"]:+.1f}</div></div>'
            )
        tiles = f"""
    <div class="tiles">
      <div class="tile"><div class="k">Win rate</div>
        <div class="v">{m['bb100']:+.1f}</div>
        <div class="m">bb/100 &plusmn; {m['ci95']:.0f} (95% CI)</div></div>
      {ev_tile}
      <div class="tile"><div class="k">Hands</div>
        <div class="v">{m['hands']:,}</div>
        <div class="m">{n_sessions} session{'' if n_sessions == 1 else 's'}</div></div>
      <div class="tile"><div class="k">Rake paid</div>
        <div class="v">{m['rake_bb100']:.1f}</div>
        <div class="m">bb/100 &middot; gross {m['gross_bb100']:+.1f}</div></div>
      <div class="tile"><div class="k">Std deviation</div>
        <div class="v">{m['sd_bb100']:.0f}</div>
        <div class="m">bb/100</div></div>
    </div>"""
        ci_lo, ci_hi = m["bb100"] - m["ci95"], m["bb100"] + m["ci95"]
        banner = (
            f'<div class="banner"><b>Read the interval, not the number.</b> '
            f'At {m["hands"]:,} hands your measured {m["bb100"]:+.1f} bb/100 is '
            f'statistically indistinguishable from anything between '
            f'{ci_lo:+.1f} and {ci_hi:+.1f}. Win rate is the last thing that '
            f'becomes reliable, not the first &mdash; judge yourself on the '
            f'frequencies and the compliance checks until the sample catches up.</div>'
        )
        sub = (f"{m['hands']:,} hands {esc(stake_txt)} &middot; "
               f"{esc(m['first_hand'][:16])} to {esc(m['last_hand'][:16])} &middot; "
               f"generated {esc(generated)}")

    pos_table = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}%</td><td>{}%</td><td>{}</td>"
        "<td>{:+.1f}</td><td class='ci'>&plusmn;{:.0f}</td></tr>".format(
            esc(r["position"]), r["hands"], fmt(r["vpip"], 1), fmt(r["pfr"], 1),
            "-" if r["threebet"] is None else f"{r['threebet']:.1f}%",
            r["bb100"], r["ci95"])
        for r in pos_rows
    )
    table_rows = "".join(
        "<tr><td>{} table{}</td><td>{:,}</td><td>{}</td></tr>".format(
            r["tables"], "" if r["tables"] == 1 else "s", r["hands"],
            "-" if r["bb100"] is None else f"{r['bb100']:+.1f}")
        for r in tables
    )
    DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
    day_rows = "".join(
        "<tr><td>{}</td><td>{:,}</td><td>{}</td></tr>".format(
            DAYS[int(r["key"])], r["hands"],
            "-" if r["bb100"] is None else f"{r['bb100']:+.1f}")
        for r in days
    )
    hour_rows = "".join(
        "<tr><td>{}:00</td><td>{:,}</td><td>{}</td></tr>".format(
            r["key"], r["hands"],
            "-" if r["bb100"] is None else f"{r['bb100']:+.1f}")
        for r in hours
    )
    rel_rows = "".join(
        "<tr><td>{}</td><td>{:,}</td><td class='thin'>{:,}</td><td>{}</td></tr>".format(
            esc(r["stat"]), r["have"], r["needed"],
            '<span class="pill good">usable</span>' if r["ready"]
            else f'<span class="pill none">{r["pct"]:.0f}%</span>')
        for r in stats.reliability(m["hands"])
    )

    wwsf = next((r for r in post if r["name"] == "WWSF"), None)
    wtsd = next((r for r in post if r["name"] == "WTSD"), None)
    wsd = next((r for r in post if r["name"] == "W$SD"), None)

    grids = {"sel": {k: _compact_grid(stats.range_grid(
        conn, hero, None if k == "All" else k, w)) for k in positions}}
    if filtered:
        grids["pri"] = {k: _compact_grid(stats.range_grid(
            conn, hero, None if k == "All" else k, prior_w)) for k in positions}

    regions = {
        "r-sub": sub,
        "r-tiles": tiles,
        "r-banner": banner,
        "r-position": (
            f'<div class="card">{charts.position_bars(pos_rows)}'
            f'<details><summary>Table view</summary>'
            f'<table><thead><tr><th>Position</th><th>Hands</th><th>VPIP</th>'
            f'<th>PFR</th><th>3-Bet</th><th>bb/100</th><th>95% CI</th></tr></thead>'
            f'<tbody>{pos_table}</tbody></table></details></div>'
        ),
        "r-preflop": f'<div class="card">{_rate_table(pre, pre_prior)}</div>',
        "r-postflop": (
            f'<div class="card">{_rate_table(post, post_prior)}'
            f'<table style="margin-top:14px"><thead><tr><th>Aggression</th>'
            f'<th>Flop</th><th>Turn</th><th>River</th><th>All streets</th></tr>'
            f'</thead><tbody>'
            f"<tr><td>Frequency</td>{_street_cells(ag, 'afq', '%')}"
            f"<td>{fmt(ag['afq'], 1)}%</td></tr>"
            f"<tr><td>Factor</td>{_street_cells(ag, 'af', '')}"
            f"<td>{fmt(ag['af'], 2)}</td></tr>"
            f'</tbody></table></div>{_triangle_note(wwsf, wtsd, wsd)}'
        ),
        "r-compliance":
            f'<div class="card">{_compliance_table(comp, comp_prior)}</div>',
        "r-funnel": f'<div class="card">{charts.funnel(funnel_rows)}</div>',
        "r-buckets": f'<div class="card">{charts.pot_buckets(buckets)}</div>',
        "r-drilldown": drill_html,
        "r-stacks": f'<div class="card">{charts.stack_histogram(stacks)}</div>',
        "r-load": (
            '<div class="card"><h3>Concurrent tables</h3>'
            '<table><thead><tr><th>Load</th><th>Hands</th><th>bb/100</th></tr>'
            f'</thead><tbody>{table_rows}</tbody></table>'
            '<h3>By day of week</h3>'
            '<table><thead><tr><th>Day</th><th>Hands</th><th>bb/100</th></tr>'
            f'</thead><tbody>{day_rows}</tbody></table>'
            '<h3>By hour of day</h3>'
            '<table><thead><tr><th>Hour</th><th>Hands</th><th>bb/100</th></tr>'
            f'</thead><tbody>{hour_rows}</tbody></table>'
            f'<h3>Timeouts</h3><p class="note">{stats.timeouts(conn, hero, w)} '
            'hand(s) where you timed out &mdash; a proxy for attention '
            'overload.</p></div>'
        ),
        "r-sample": (
            '<div class="card"><table><thead><tr><th>Stat</th><th>Hands</th>'
            f'<th>Needed</th><th></th></tr></thead><tbody>{rel_rows}'
            '</tbody></table></div>'
        ),
    }
    return regions, grids, drill_ids


def _json_script(payload: dict, element_id: str) -> str:
    """Inline JSON that cannot terminate its own script element."""
    text = json.dumps(payload, separators=(",", ":")).replace("<", "\\u003c")
    return f'<script type="application/json" id="{element_id}">{text}</script>'


def build(conn: sqlite3.Connection, hero: str, live: bool = False) -> str:
    m = stats.money_summary(conn, hero)
    if not m.get("hands"):
        return "<h1>No hands imported</h1>"

    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    tourn = stats.excluded_tournaments(conn, hero)
    tourney_note = (
        f" Cash games only: {tourn['hands']:,} tournament "
        f"{'hand' if tourn['hands'] == 1 else 'hands'} across "
        f"{tourn['events']:,} "
        f"{'event' if tourn['events'] == 1 else 'events'} are imported but "
        "excluded from every figure above." if tourn["hands"] else "")
    # The most recent *cash* hand: the report is a cash report, and a night that
    # happened to end on a sit-and-go would otherwise put "$20.00/$40.00" in the
    # header. The predicate is stats.CASH_ONLY so there is one definition of
    # which hands this report is about.
    stake = conn.execute(
        f"SELECT sb, bb, currency FROM hands h WHERE {stats.CASH_ONLY}"
        " ORDER BY played_at DESC LIMIT 1").fetchone()
    stake_txt = f"${stake['sb'] / 100:.2f}/${stake['bb'] / 100:.2f}" if stake else ""

    # These three are drawn once over the whole history no matter what is
    # selected. A period is shaded on the two cumulative charts rather than
    # plotted alone, because the only useful thing a single session's line can
    # tell you is where it sits relative to everything before it.
    rows = stats.bb_series(conn, hero)
    roll_window = 1000 if m["hands"] >= 1000 else max(25, m["hands"] // 3)
    roll = stats.rolling(conn, hero, roll_window)
    sess = stats.sessions(conn, hero)
    n_problems = stats.problem_count(conn)

    positions = ["All"] + [p for p in POSITION_ORDER
                           if stats.range_grid(conn, hero, p)]

    periods = stats.periods(conn, hero)
    regions, grids, hand_ids = {}, {}, []
    meta = []
    for period in periods:
        r, g, ids = _fragments(conn, hero, period, positions, generated, stake_txt)
        regions[period.key] = r
        grids[period.key] = g
        hand_ids.extend(ids)
        band = (None if period.is_all
                else stats.hand_index_range(conn, hero, period.selected))
        meta.append({"key": period.key, "label": period.label,
                     "hands": period.selected.hands, "band": band})

    raws = db.hand_texts(conn, sorted(set(hand_ids)))
    payload = _json_script(
        {"periods": meta, "regions": regions,
         "raws": {str(k): v for k, v in raws.items()}},
        "period-data")

    period_bar = ""
    if len(periods) > 1:
        chips = "".join(
            f'<button type="button" class="chip" data-period="{esc(p["key"])}" '
            f'aria-pressed="{"true" if p["key"] == "all" else "false"}">'
            f'{esc(p["label"])}'
            + ("" if p["key"] == "all" else
               f' <span class="chip-count">({p["hands"]:,})</span>')
            + '</button>'
            for p in meta
        )
        period_bar = (
            '<div class="chip-row period-bar" role="group" '
            'aria-label="Filter by period">'
            '<span class="chip-label">Period</span>' + chips + '</div>'
            '<p class="period-note">Filters the tables, the frequencies and the '
            'compliance checks, and adds a comparison against everything before '
            'the selected window. The cumulative charts and the session list '
            'always show the whole history, with the selected period shaded.</p>'
        )

    legend = (
        '<div class="legend">'
        '<span><i style="border-color:var(--total)"></i>Total (net)</span>'
        '<span><i style="border-color:var(--showdown);border-top-style:dashed"></i>'
        'Showdown &mdash; blue line</span>'
        '<span><i style="border-color:var(--nonshowdown);border-top-style:dotted"></i>'
        'Non-showdown &mdash; red line</span>'
        + ('<span><i style="border-color:var(--ev);border-top-style:dashed"></i>'
           'All-in adjusted EV</span>' if m["has_ev"] else "")
        + '</div>'
    )

    sess_rows = "".join(
        "<tr><td>{}</td><td>{:.0f} min</td><td>{:,}</td><td>{}</td>"
        "<td>{:+.1f}</td></tr>".format(
            esc(s["start"][:16]), s["duration_min"], s["hands"],
            s["n_tables"], s["bb100"])
        for s in sess
    )

    problems_block = ""
    if n_problems:
        items = "".join(
            f"<li><code>{esc(p['site_hand_no'])}</code> &mdash; {esc(p['problem'])}</li>"
            for p in stats.problems(conn, 40))
        problems_block = (
            f'<h2>Parse problems</h2><div class="banner">{n_problems} problem(s) '
            f'recorded. Every one is a hand whose arithmetic did not balance or '
            f'whose text was not understood &mdash; treat them as parser bugs, not '
            f'noise.</div><div class="card"><ul>{items}</ul></div>')

    roll_caveat = ("" if roll_window >= 1000 else
                   " The window is narrowed from the usual 1,000 because the "
                   "sample is smaller than that, so the line is correspondingly "
                   "noisier.")
    toolbar = ("""
<div class="toolbar">
  <span id="refresh-status" class="status">Checking every 60s</span>
</div>""" if live else "")
    live_js = LIVE_JS if live else ""
    a = regions["all"]

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Poker tracker &mdash; {esc(hero)}</title>
<style>{CSS}</style></head>
<body><div class="wrap">

<h1>{esc(hero)}</h1>
<p class="sub" id="r-sub">{a['r-sub']}</p>
{toolbar}
{period_bar}
<div id="r-tiles">{a['r-tiles']}</div>
<div id="r-banner">{a['r-banner']}</div>

<h2>1. Cumulative winnings</h2>
<p class="note">This single chart is the reason trackers exist; everything else
supports it. The red line is not "profit from bluffing": it is every pot taken down uncontested minus every chip lost in a
hand that never reached showdown. A slightly negative red line is normal at
micro stakes; a sharply negative one usually means you are opening and
c-betting into people who never fold.</p>
<div class="card">{legend}{charts.cumulative_winnings(rows, m['has_ev'])}</div>

<h2>2. bb/100 by position</h2>
<p class="note">Error bars are 95% confidence intervals. They will be
embarrassingly wide, and that is the point &mdash; the aggregate number hides
almost everything interesting, and this is where leaks become visible.</p>
<div id="r-position">{a['r-position']}</div>

<h2>3. Preflop</h2>
<p class="note">Every denominator is opportunities, not hands. A big blind check
is never VPIP; a big blind call of a raise is. Completing the small blind counts
as VPIP but not PFR.</p>
<div id="r-preflop">{a['r-preflop']}</div>

<h2>4. Postflop</h2>
<p class="note">"Won money" means collected any part of the pot, even if the hand
was net negative after your own contribution.</p>
<div id="r-postflop">{a['r-postflop']}</div>

<h2>5. Compliance checks</h2>
<p class="note">These answer whether you executed the plan, which is a different
question from whether the plan is good &mdash; and a much more useful one early
on, because they converge in a weekend rather than a year. Counts carry a rate
per 100 hands beside them: a cumulative tally only goes up, so it stops telling
you anything about the last week once it has a few entries in it.</p>
<div id="r-compliance">{a['r-compliance']}</div>

<h2>6. Starting-hand ranges</h2>
<p class="note">How often each combo was voluntarily played, filtered by
position &mdash; a single aggregated grid is meaningless, because button and
UTG ranges differ by design. With a period selected the grid splits in two, so
range drift shows up directly. The bb/100 view is noisy below 100k hands and
mostly decorative, but it does surface the "I keep losing money with A-rag
offsuit" pattern.</p>
<div class="card">{charts.range_heatmap(grids, positions)}</div>

<h2>7. Rolling preflop discipline</h2>
<p class="note">Trailing {roll_window:,}-hand window against target reference
lines. This is how you verify the discipline is holding rather than decaying
over weeks.{roll_caveat}</p>
<div class="card">{charts.rolling_trend(roll, TARGETS)}</div>

<h2>8. Street funnel</h2>
<p class="note">Where in the hand your money actually moves.</p>
<div id="r-funnel">{a['r-funnel']}</div>

<h2>9. Pot-size buckets</h2>
<p class="note">Most micro-stakes losing players are fine in small pots and
hemorrhage in big ones. If the losses concentrate in the top bucket, the problem
is stack-off decisions, not preflop ranges.</p>
<div id="r-buckets">{a['r-buckets']}</div>

<h2>10. Biggest pots</h2>
<p class="note">Every other section ends in "go look at those hands"; this is
where you look. The largest losses and largest wins by net big blinds, with the
board, the exit street, and whatever the opponent showed. A pattern here &mdash;
stacking off with one pair, folding rivers in the biggest pots, the same
position over and over &mdash; carries more information than any aggregate on
this page, because these are the hands the win rate is actually made of.</p>
<div id="r-drilldown">{a['r-drilldown']}</div>

<h2>11. Sessions</h2>
<p class="note">Each point is one session, split on a gap of more than
{db.SESSION_GAP_MINUTES} minutes. The boundaries are assigned once when the
hands are imported and stored on the hand, so "last session" means the same set
of hands however the report is filtered. This list always covers the whole
history.</p>
<div class="card">{charts.session_scatter(sess)}
<details><summary>Table view</summary>
<table><thead><tr><th>Start</th><th>Duration</th><th>Hands</th><th>Tables</th>
<th>bb/100</th></tr></thead><tbody>{sess_rows}</tbody></table></details></div>

<h2>12. Effective stacks</h2>
<p class="note">A one-off diagnostic. Once the distribution sits at 100bb you can
retire this chart. Bars below 80bb are the ones to worry about.</p>
<div id="r-stacks">{a['r-stacks']}</div>

<h2>13. Table load, timing and attention</h2>
<div id="r-load">{a['r-load']}</div>

<h2>14. Sample size</h2>
<p class="note">Frequency stats converge far faster than results because they
are bounded proportions. This is the argument for building a tracker around
frequencies and compliance checks rather than around win rate &mdash; and the
reason this table recomputes with the filter rather than staying put.</p>
<div id="r-sample">{a['r-sample']}</div>

{problems_block}

<footer>Generated by pokertracker from PokerStars hand histories.
Rake is attributed to players in proportion to contribution, which is an
estimate: PokerStars reports rake per pot, not per player.{tourney_note}</footer>
</div>
{payload}
<script>{JS}{live_js}</script>
</body></html>"""


def _street_cells(ag, key: str, suffix: str) -> str:
    out = []
    for street in ("flop", "turn", "river"):
        v = ag["per_street"][street][key]
        out.append(f"<td>{'-' if v is None else f'{v:.1f}{suffix}'}</td>")
    return "".join(out)


def _triangle_note(wwsf, wtsd, wsd) -> str:
    """WWSF, WTSD and W$SD read together as a diagnostic triangle."""
    if not (wwsf and wtsd and wwsf["den"]):
        return ""
    a, b = wwsf["pct"], wtsd["pct"]
    c = wsd["pct"] if wsd and wsd["den"] else None
    msg = []
    if a is not None and b is not None:
        if a < 45 and b > 32:
            msg.append("Low WWSF with high WTSD: too passive, calling down and "
                       "giving up the initiative.")
        if b > 32 and c is not None and c < 48:
            msg.append("High WTSD with low W$SD: calling too wide on later streets.")
        if a > 50 and b < 25:
            msg.append("High WWSF with low WTSD: winning uncontested pots, which "
                       "is fine, but check the red line supports it.")
    if not msg:
        msg.append("Nothing stands out yet &mdash; these three need 5,000 to "
                   "10,000 hands before they mean much.")
    return ('<div class="banner"><b>Diagnostic triangle.</b> '
            + " ".join(msg) + "</div>")
