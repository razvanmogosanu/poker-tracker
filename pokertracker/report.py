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
.pill { display:inline-block; padding: 1px 8px; border-radius: 999px; font-size: 11.5px; font-weight: 500; }
.pill.good { background: color-mix(in srgb, var(--good) 16%, transparent); color: var(--good); }
.pill.warn { background: color-mix(in srgb, var(--warn) 20%, transparent); color: var(--ink); }
.pill.bad { background: color-mix(in srgb, var(--bad) 16%, transparent); color: var(--bad); }
.pill.none { background: color-mix(in srgb, var(--muted) 16%, transparent); color: var(--ink-2); }
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
details { margin-top: 10px; }
summary { cursor: pointer; font-size: 13px; color: var(--ink-2); }
.empty { color: var(--muted); font-style: italic; padding: 20px 0; }
.banner {
  border-left: 3px solid var(--warn); background: color-mix(in srgb, var(--warn) 9%, transparent);
  padding: 10px 14px; border-radius: 0 8px 8px 0; font-size: 13.5px; margin: 14px 0;
}
footer { margin-top: 46px; color: var(--muted); font-size: 12.5px; }
.toolbar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin: 0 0 20px; }
.btn {
  font: inherit; font-size: 13.5px; font-weight: 500; padding: 7px 16px;
  border-radius: 8px; cursor: pointer; border: 1px solid var(--border);
  background: var(--ink); color: var(--surface);
}
.btn:hover:not(:disabled) { opacity: .88; }
.btn:disabled { opacity: .5; cursor: progress; }
.status { font-size: 13px; color: var(--ink-2); }
.status.busy::after {
  content: ''; display: inline-block; width: 9px; height: 9px; margin-left: 7px;
  border: 2px solid var(--muted); border-top-color: transparent; border-radius: 50%;
  vertical-align: -1px; animation: spin .7s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.auto { font-size: 13px; color: var(--muted); display: inline-flex; align-items: center; gap: 6px; cursor: pointer; }
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
  });

  // ---- range heatmap: position filter + metric switch
  $$('.heatmap').forEach(function (wrap) {
    const grids = JSON.parse(wrap.dataset.payload);
    const tip = $('.tooltip', wrap), legend = $('.scale-legend', wrap);
    let pos = Object.keys(grids)[0], metric = 'freq';

    function paint() {
      const g = grids[pos] || {};
      const vals = Object.values(g).map(v => v[metric]).filter(v => v !== null && v !== undefined);
      const maxAbs = Math.max(1, ...vals.map(Math.abs));
      $$('.cellg', wrap).forEach(function (cell) {
        const combo = cell.dataset.combo, d = g[combo];
        const rect = $('rect', cell), label = $('text', cell);
        if (!d || d.n === 0 || d[metric] === null || d[metric] === undefined) {
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
        const d = (grids[pos] || {})[cell.dataset.combo];
        tip.hidden = false;
        tip.innerHTML = '<b>' + cell.dataset.combo + '</b> &middot; ' + pos + '<br>' +
          (d && d.n ? ('dealt ' + d.n + ' time' + (d.n === 1 ? '' : 's') + '<br>played ' +
            (d.freq === null ? '-' : d.freq.toFixed(0) + '%') + '<br>' +
            (d.bb100 === null ? '' : d.bb100.toFixed(0) + ' bb/100'))
                     : 'never dealt');
        place(tip, wrap, ev);
      });
      cell.addEventListener('mouseleave', () => { tip.hidden = true; });
    });
    paint();
  });


  // ---- biggest pots: table switch, column sort, raw-text panel
  $$('[data-drilldown]').forEach(function (wrap) {
    const raws = JSON.parse(wrap.dataset.payload);
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
        const txt = raws[btn.dataset.hand];
        head.textContent = txt ? ('Hand #' + btn.dataset.no)
          : ('Hand #' + btn.dataset.no + ' — source file no longer readable');
        pre.textContent = txt || '';
        panel.hidden = false;
        panel.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      });
    });
  });

  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function () {
    $$('.heatmap .chip[aria-pressed="true"]').forEach(b => b.click());
  });
})();
"""


LIVE_JS = """
(function () {
  const btn = document.getElementById('refresh');
  if (!btn) return;
  const status = document.getElementById('refresh-status');
  const auto = document.getElementById('auto');
  const SCROLL = 'pt-scroll', AUTOKEY = 'pt-auto';

  // A reload would otherwise throw the reader back to the top of the page.
  const saved = sessionStorage.getItem(SCROLL);
  if (saved !== null) {
    window.scrollTo(0, parseInt(saved, 10));
    sessionStorage.removeItem(SCROLL);
  }

  let busy = false;
  function setStatus(text, spinning) {
    status.textContent = text;
    status.classList.toggle('busy', !!spinning);
  }
  async function refresh(manual) {
    if (busy) return;
    busy = true; btn.disabled = true;
    setStatus('Parsing new hands', true);
    try {
      const res = await fetch('/api/refresh', { method: 'POST' });
      const d = await res.json();
      if (!d.ok) {
        setStatus('Failed: ' + (d.error || 'unknown error'), false);
        btn.disabled = false; busy = false; return;
      }
      // An automatic poll that found nothing must not steal the page from under
      // someone who is reading it.
      if (!d.new_hands && !manual) {
        setStatus('No new hands at ' + new Date().toLocaleTimeString(), false);
        btn.disabled = false; busy = false; return;
      }
      setStatus(d.new_hands ? ('+' + d.new_hands + ' hands, reloading') : 'Reloading', true);
      sessionStorage.setItem(SCROLL, String(window.scrollY));
      location.reload();
    } catch (e) {
      setStatus('Failed: ' + e.message, false);
      btn.disabled = false; busy = false;
    }
  }
  btn.addEventListener('click', function () { refresh(true); });

  let timer = null;
  function applyAuto() {
    localStorage.setItem(AUTOKEY, auto.checked ? '1' : '0');
    if (timer) { clearInterval(timer); timer = null; }
    if (auto.checked) timer = setInterval(function () { refresh(false); }, 60000);
  }
  auto.checked = localStorage.getItem(AUTOKEY) === '1';
  auto.addEventListener('change', applyAuto);
  applyAuto();
})();
"""


def _pill(ok) -> str:
    if ok is True:
        return '<span class="pill good">on plan</span>'
    if ok is False:
        return '<span class="pill bad">off plan</span>'
    return '<span class="pill none">watch</span>'


def _rate_table(rows, caption: str = "") -> str:
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
    cap = f"<caption>{esc(caption)}</caption>" if caption else ""
    return (
        f"<table>{cap}<thead><tr><th>Stat</th><th>Rate</th>"
        f"<th>95% CI</th><th>n</th></tr></thead><tbody>"
        + "".join(body) + "</tbody></table>"
    )


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


def _drilldown(conn, hero: str, limit: int = 15) -> str:
    """Top losses and top wins, each row expandable to the original text.

    The raw text is inlined rather than linked because the report is a single
    self-contained file: a file:// link into the history folder would break the
    moment the report is copied anywhere.
    """
    pots = stats.big_pots(conn, hero, limit)
    ids = [r["hand_id"] for r in pots["losses"] + pots["wins"]]
    raws = db.hand_texts(conn, ids)
    payload = json.dumps({str(k): v for k, v in raws.items()})
    n_loss, n_win = len(pots["losses"]), len(pots["wins"])
    return f"""<div class="card" data-drilldown data-payload='{esc(payload)}'>
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


def build(conn: sqlite3.Connection, hero: str, live: bool = False) -> str:
    m = stats.money_summary(conn, hero)
    if not m.get("hands"):
        return "<h1>No hands imported</h1>"

    rows = stats.bb_series(conn, hero)
    pos_rows = stats.by_position(conn, hero)
    pre = stats.preflop_stats(conn, hero)
    post = stats.postflop_stats(conn, hero)
    ag = stats.aggression(conn, hero)
    comp = stats.compliance(conn, hero)
    sess = stats.sessions(conn, hero)
    funnel_rows = stats.street_funnel(conn, hero)
    buckets = stats.pot_buckets(conn, hero)
    stacks = stats.stack_histogram(conn, hero)
    roll_window = 1000 if m["hands"] >= 1000 else max(25, m["hands"] // 3)
    roll = stats.rolling(conn, hero, roll_window)
    tables = stats.by_table_count(conn, hero)
    hours = stats.by_time(conn, hero, "%H")
    days = stats.by_time(conn, hero, "%w")
    drilldown = _drilldown(conn, hero)
    n_problems = stats.problem_count(conn)

    grids = {"All": stats.range_grid(conn, hero)}
    for p in POSITION_ORDER:
        g = stats.range_grid(conn, hero, p)
        if g:
            grids[p] = g

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
        <div class="m">{len(sess)} session{'' if len(sess) == 1 else 's'}</div></div>
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

    pos_table = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}%</td><td>{}%</td><td>{}</td>"
        "<td>{:+.1f}</td><td class='ci'>&plusmn;{:.0f}</td></tr>".format(
            esc(r["position"]), r["hands"], fmt(r["vpip"], 1), fmt(r["pfr"], 1),
            "-" if r["threebet"] is None else f"{r['threebet']:.1f}%",
            r["bb100"], r["ci95"])
        for r in pos_rows
    )

    comp_rows = "".join(
        "<tr><td>{}</td><td>{}</td><td class='thin'>{}</td><td>{}</td>"
        "<td class='thin'>{}</td></tr>".format(
            esc(c["name"]),
            "-" if c["value"] is None else
            (f"{c['value']:.1f}%" if c["unit"] == "pct" else f"{c['value']}"),
            esc(c["target"]), _pill(c["ok"]), esc(c["note"]))
        for c in comp
    )

    rel_rows = "".join(
        "<tr><td>{}</td><td>{:,}</td><td class='thin'>{:,}</td><td>{}</td></tr>".format(
            esc(r["stat"]), r["have"], r["needed"],
            '<span class="pill good">usable</span>' if r["ready"]
            else f'<span class="pill none">{r["pct"]:.0f}%</span>')
        for r in stats.reliability(m["hands"])
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

    sess_rows = "".join(
        "<tr><td>{}</td><td>{:.0f} min</td><td>{:,}</td><td>{}</td>"
        "<td>{:+.1f}</td></tr>".format(
            esc(s["start"][:16]), s["duration_min"], s["hands"],
            s["n_tables"], s["bb100"])
        for s in sess
    )

    wwsf = next((r for r in post if r["name"] == "WWSF"), None)
    wtsd = next((r for r in post if r["name"] == "WTSD"), None)
    wsd = next((r for r in post if r["name"] == "W$SD"), None)
    triangle = _triangle_note(wwsf, wtsd, wsd)

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
  <button type="button" id="refresh" class="btn">Refresh hands</button>
  <span id="refresh-status" class="status">Showing every hand imported so far</span>
  <label class="auto"><input type="checkbox" id="auto"> auto every 60s</label>
</div>""" if live else "")
    live_js = LIVE_JS if live else ""
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    stake = conn.execute(
        "SELECT sb, bb, currency FROM hands ORDER BY played_at DESC LIMIT 1").fetchone()
    stake_txt = f"${stake['sb'] / 100:.2f}/${stake['bb'] / 100:.2f}" if stake else ""

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Poker tracker &mdash; {esc(hero)}</title>
<style>{CSS}</style></head>
<body><div class="wrap">

<h1>{esc(hero)}</h1>
<p class="sub">{m['hands']:,} hands {esc(stake_txt)} &middot;
{esc(m['first_hand'][:16])} to {esc(m['last_hand'][:16])} &middot;
generated {esc(generated)}</p>
{toolbar}
{tiles}
{banner}

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
<div class="card">{charts.position_bars(pos_rows)}
<details><summary>Table view</summary>
<table><thead><tr><th>Position</th><th>Hands</th><th>VPIP</th><th>PFR</th>
<th>3-Bet</th><th>bb/100</th><th>95% CI</th></tr></thead>
<tbody>{pos_table}</tbody></table></details></div>

<h2>3. Preflop</h2>
<p class="note">Every denominator is opportunities, not hands. A big blind check
is never VPIP; a big blind call of a raise is. Completing the small blind counts
as VPIP but not PFR.</p>
<div class="card">{_rate_table(pre)}</div>

<h2>4. Postflop</h2>
<p class="note">"Won money" means collected any part of the pot, even if the hand
was net negative after your own contribution.</p>
<div class="card">{_rate_table(post)}
<table style="margin-top:14px"><thead><tr><th>Aggression</th><th>Flop</th>
<th>Turn</th><th>River</th><th>All streets</th></tr></thead><tbody>
<tr><td>Frequency</td>{_street_cells(ag, 'afq', '%')}
<td>{fmt(ag['afq'], 1)}%</td></tr>
<tr><td>Factor</td>{_street_cells(ag, 'af', '')}
<td>{fmt(ag['af'], 2)}</td></tr>
</tbody></table></div>
{triangle}

<h2>5. Compliance checks</h2>
<p class="note">These answer whether you executed the plan, which is a different
question from whether the plan is good &mdash; and a much more useful one early
on, because they converge in a weekend rather than a year.</p>
<div class="card"><table><thead><tr><th>Check</th><th>Value</th><th>Target</th>
<th></th><th>Note</th></tr></thead><tbody>{comp_rows}</tbody></table></div>

<h2>6. Starting-hand ranges</h2>
<p class="note">How often each combo was voluntarily played, filtered by
position &mdash; a single aggregated grid is meaningless, because button and
UTG ranges differ by design. The bb/100 view is noisy below 100k hands and
mostly decorative, but it does surface the "I keep losing money with A-rag
offsuit" pattern.</p>
<div class="card">{charts.range_heatmap(grids)}</div>

<h2>7. Rolling preflop discipline</h2>
<p class="note">Trailing {roll_window:,}-hand window against target reference
lines. This is how you verify the discipline is holding rather than decaying
over weeks.{roll_caveat}</p>
<div class="card">{charts.rolling_trend(roll, TARGETS)}</div>

<h2>8. Street funnel</h2>
<p class="note">Where in the hand your money actually moves.</p>
<div class="card">{charts.funnel(funnel_rows)}</div>

<h2>9. Pot-size buckets</h2>
<p class="note">Most micro-stakes losing players are fine in small pots and
hemorrhage in big ones. If the losses concentrate in the top bucket, the problem
is stack-off decisions, not preflop ranges.</p>
<div class="card">{charts.pot_buckets(buckets)}</div>

<h2>10. Biggest pots</h2>
<p class="note">Every other section ends in "go look at those hands"; this is
where you look. Fifteen largest losses and fifteen largest wins by net big
blinds, with the board, the exit street, and whatever the opponent showed. A
pattern here &mdash; stacking off with one pair, folding rivers in the biggest
pots, the same position over and over &mdash; carries more information than any
aggregate on this page, because these are the hands the win rate is actually
made of.</p>
{drilldown}

<h2>11. Sessions</h2>
<p class="note">Each point is one session, split on a gap of more than 30
minutes. Two or three months of data will tell you your real table limit.</p>
<div class="card">{charts.session_scatter(sess)}
<details><summary>Table view</summary>
<table><thead><tr><th>Start</th><th>Duration</th><th>Hands</th><th>Tables</th>
<th>bb/100</th></tr></thead><tbody>{sess_rows}</tbody></table></details></div>

<h2>12. Effective stacks</h2>
<p class="note">A one-off diagnostic. Once the distribution sits at 100bb you can
retire this chart. Bars below 80bb are the ones to worry about.</p>
<div class="card">{charts.stack_histogram(stacks)}</div>

<h2>13. Table load, timing and attention</h2>
<div class="card">
<h3>Concurrent tables</h3>
<table><thead><tr><th>Load</th><th>Hands</th><th>bb/100</th></tr></thead>
<tbody>{table_rows}</tbody></table>
<h3>By day of week</h3>
<table><thead><tr><th>Day</th><th>Hands</th><th>bb/100</th></tr></thead>
<tbody>{day_rows}</tbody></table>
<h3>By hour of day</h3>
<table><thead><tr><th>Hour</th><th>Hands</th><th>bb/100</th></tr></thead>
<tbody>{hour_rows}</tbody></table>
<h3>Timeouts</h3>
<p class="note">{stats.timeouts(conn, hero)} hand(s) where you timed out &mdash;
a proxy for attention overload.</p>
</div>

<h2>14. Sample size</h2>
<p class="note">Frequency stats converge far faster than results because they
are bounded proportions. This is the argument for building a tracker around
frequencies and compliance checks rather than around win rate.</p>
<div class="card"><table><thead><tr><th>Stat</th><th>Hands</th>
<th>Needed</th><th></th></tr></thead><tbody>{rel_rows}</tbody></table></div>

{problems_block}

<footer>Generated by pokertracker from PokerStars hand histories.
Rake is attributed to players in proportion to contribution, which is an
estimate: PokerStars reports rake per pot, not per player.</footer>
</div>
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
