(function () {
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

  // The selected period, and everything that needs telling when it changes.
  // Sections that re-render server-side are swapped wholesale, the winnings
  // chart among them; the rolling chart and the heatmap instead redraw from
  // data they already hold, the one shading the selected period in the context
  // of the whole history and the other painting it beside the prior one.
  let PERIOD = 'all';
  // Keys shared with LIVE_JS, which runs in its own scope in the same script.
  const PKEY = 'pt-period', SKEY = 'pt-jumpy-starts', RELOADED = 'pt-reloaded';
  // The period keys whose boundary jumps rather than slides; see the restore.
  const JUMPY = ['session', 'today'];
  let BANDS = {};
  let RAWS = {};
  const listeners = [];

  // ---- Stats | Solver
  // Announced on the document because two other scripts care which tab is
  // showing: the live poll holds its reload while a scenario is being built,
  // and the Solver switches here itself when a hand is sent to it.
  $$('.tab[data-tab]').forEach(function (b) {
    b.addEventListener('click', function () {
      $$('.tab[data-tab]').forEach(function (o) {
        o.setAttribute('aria-selected', String(o === b));
        const pane = document.getElementById('tab-' + o.dataset.tab);
        if (pane) pane.hidden = o !== b;
      });
      document.dispatchEvent(new CustomEvent('pt-tab', { detail: b.dataset.tab }));
    });
  });

  function isDark() {
    const t = document.documentElement.getAttribute('data-theme');
    if (t) return t === 'dark';
    return window.matchMedia('(prefers-color-scheme: dark)').matches;
  }
  const SEQ_LIGHT = ['#cde2fb','#b7d3f6','#9ec5f4','#86b6ef','#6da7ec','#5598e7',
                     '#3987e5','#2a78d6','#256abf','#1c5cab','#184f95','#104281','#0d366b'];
  // Below this share played, a starting-hand cell is painted blank on both
  // metrics. bb/100 is over every deal, so a hand that is always folded shows
  // only the blinds lost folding it -- noise in red, not a result.
  const RARELY_PLAYED = 2;
  // The bb/100 colour scale ends at this share of the played cells.
  const SCALE_PCTILE = 0.9;
  // Blank is white on a light page and the surface on a dark one, where the
  // ramp runs the other way and white would be the loudest cell on it.
  function blankColor() { return isDark() ? '#1a1a19' : '#ffffff'; }
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
  // Bound through a function for the same reason as initDrilldown: the chart
  // is redrawn per period server-side and swapped in, handlers and all.
  function initWinnings(root) {
    $$('[data-chart="winnings"]', root).forEach(function (wrap) {
      const d = JSON.parse(wrap.dataset.payload);
      const svg = $('svg', wrap), tip = $('.tooltip', wrap);
      const hair = $('.crosshair', wrap), target = $('.hover-target', wrap);
      const m = d.series[0].values.length;
      if (!m) return;
      function toUser(ev) {
        const pt = svg.createSVGPoint();
        pt.x = ev.clientX; pt.y = ev.clientY;
        return pt.matrixTransform(svg.getScreenCTM().inverse());
      }
      // Hand h is drawn at h on an axis padded past the last hand, so the
      // mouse maps through the domain, not across the full plot width.
      const span = Math.max(1, d.x1 - d.x0), width = d.right - d.left;
      target.addEventListener('mousemove', function (ev) {
        const u = toUser(ev);
        const hand = d.x0 + ((u.x - d.left) / width) * span;
        // values[j] is hand j * step + 1, the last one clamped to hand n.
        const j = Math.max(0, Math.min(m - 1, Math.round((hand - 1) / d.step)));
        const i = Math.min(j * d.step, d.n - 1);
        const x = d.left + ((i + 1 - d.x0) / span) * width;
        hair.setAttribute('x1', x); hair.setAttribute('x2', x);
        hair.style.display = '';
        tip.hidden = false;
        tip.innerHTML = '<b>Hand ' + (i + 1) + '</b><br>' + d.series.map(function (s) {
          return '<span class="sw" style="background:var(--' + s.key + ')"></span>' +
                 s.name + ' <b>' + s.values[j].toFixed(1) + ' bb</b>';
        }).join('<br>');
        place(tip, wrap, ev);
      });
      target.addEventListener('mouseleave', function () {
        hair.style.display = 'none'; tip.hidden = true;
      });
    });
  }
  initWinnings(document);

  // ---- rolling discipline: the selected period shaded on a hand-number axis
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

    // Cells arrive as [n, vpip, bb100] in grid order, indexed by data-i; the
    // field names and combo keys cost more than the numbers, and every period
    // carries a grid for both panels.
    function cellOf(side, i) {
      const p = data[PERIOD] || data.all || {};
      const g = (p[side] || {})[pos] || [];
      const v = g[i];
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
      // The ends sit at the SCALE_PCTILE of |bb/100|, not the maximum: AA alone
      // runs to four figures, and scaling to it washed every other cell into
      // the grey midpoint. Cells past the end saturate, and the legend says so.
      const mags = [];
      sides.forEach(function (side) {
        ((p[side] || {})[pos] || []).forEach(function (c) {
          if (!c || !c[0] || 100 * c[1] / c[0] < RARELY_PLAYED) return;
          if (c[2] !== null && c[2] !== undefined) mags.push(Math.abs(c[2]));
        });
      });
      mags.sort((a, b) => a - b);
      const maxAbs = Math.max(1, mags.length
        ? mags[Math.min(mags.length - 1, Math.floor(SCALE_PCTILE * mags.length))] : 1);

      $$('.cellg', wrap).forEach(function (cell) {
        const d = cellOf(cell.dataset.side, +cell.dataset.i);
        const rect = $('rect', cell), label = $('text', cell);
        const value = d ? d[metric] : null;
        // Inline style, not the fill attribute: a presentation attribute loses
        // to any stylesheet rule, and .cell / .cell-label both set a fill, so
        // setAttribute('fill') left every cell the colour of the surface.
        if (value === null || value === undefined) {
          rect.style.fill = 'none';
          rect.style.strokeDasharray = '2 2';
          label.style.fill = 'var(--muted)';
          label.style.opacity = '0.5';
          return;
        }
        rect.style.strokeDasharray = '';
        label.style.opacity = '';
        // A combo played under RARELY_PLAYED % of the time is blank, not the
        // palest blue: most of the grid is folded hands, and tinting them all
        // buries the range that was actually played. Never-dealt cells keep
        // their dashed outline, so blank still means "dealt and folded".
        const fill = d.freq < RARELY_PLAYED ? blankColor()
          : metric === 'freq' ? seqColor(0.12 + 0.88 * (d.freq / 100))
          : divColor(d.bb100 / maxAbs);
        rect.style.fill = fill;
        label.style.fill = lum(fill) > 0.42 ? '#0b0b0b' : '#ffffff';
      });

      const steps = [];
      for (let i = 0; i <= 8; i++) {
        const t = i / 8;
        steps.push('<i style="background:' +
          (metric === 'freq' ? seqColor(t) : divColor(t * 2 - 1)) + '"></i>');
      }
      const blank = '<span class="ramp"><i style="background:' + blankColor() +
        ';outline:1px solid var(--border);outline-offset:-1px"></i></span><span>played under ' +
        RARELY_PLAYED + '%</span>';
      legend.innerHTML = (metric === 'freq'
        ? blank + '<span class="ramp">' + steps.join('') + '</span><span>100% played</span>'
        : blank + '<span>&le; -' + maxAbs.toFixed(0) + '</span><span class="ramp">' +
          steps.join('') + '</span><span>&ge; +' + maxAbs.toFixed(0) + ' bb/100</span>');
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
        const d = cellOf(cell.dataset.side, +cell.dataset.i);
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
    // "All" is the markup the page opened with, so it is read back from the
    // page rather than shipped a second time. Nothing has been swapped yet.
    D.regions.all = {};
    D.ids.forEach(function (id) {
      const el = document.getElementById(id);
      if (el) D.regions.all[id] = el.innerHTML;
    });
    RAWS = D.raws || {};
    D.periods.forEach(function (p) { if (p.band) BANDS[p.key] = p.band; });

    function applyPeriod(key) {
      PERIOD = key;
      // Remembered only so an auto-reload can put it back; see the restore
      // below for why that is not the same thing as defaulting to it.
      try { sessionStorage.setItem(PKEY, key); } catch (e) {}
      const regions = D.regions[key] || {};
      Object.keys(regions).forEach(function (id) {
        const el = document.getElementById(id);
        if (!el) return;
        el.innerHTML = regions[id];
        initDrilldown(el);
        initWinnings(el);
      });
      $$('.period-bar .chip').forEach(b =>
        b.setAttribute('aria-pressed', String(b.dataset.period === key)));
      listeners.forEach(function (fn) { fn(); });
    }

    $$('.period-bar .chip').forEach(function (b) {
      b.addEventListener('click', function () { applyPeriod(b.dataset.period); });
    });

    // Restored across the live page's own reload, and across nothing else.
    // Opening report.html, or reloading it by hand, still lands on "All": the
    // pull with a period filter is to open the report after every session and
    // read today's win rate, which is a number with a +/-150 bb/100 interval
    // on it. Carrying an explicit click through a reload the reader did not
    // ask for is the opposite of that -- the filter was chosen, and new hands
    // arriving is not a reason to throw the choice away.
    //
    // The reload flag is what separates the two, and LIVE_JS sets it only on
    // the path that calls location.reload().
    let want = null;
    try {
      if (sessionStorage.getItem(RELOADED) !== null) want = sessionStorage.getItem(PKEY);
      sessionStorage.removeItem(RELOADED);
    } catch (e) {}

    // An option can vanish between renders: periods() drops the ones that are
    // empty, that cover the whole history, or that start where an option
    // already offered starts, so "Last session" appears and disappears as the
    // history grows. A key with no fragments behind it would swap in nothing.
    if (want && !D.regions[want]) want = null;

    // Two keys can survive a reload and mean something else. Most of them
    // slide -- "last 7 days" is still the last 7 days after two more hands --
    // but a 30-minute gap makes "Last session" a different session, and
    // midnight does the same to "Today" in the middle of a session that is
    // still going. Either would quietly swap the 400 hands being read for the
    // 3 just dealt, with the same button lit.
    //
    // The band's start index is where the window begins in the hand sequence,
    // so it is the discriminator: new hands extend the end, and the start
    // moves only when the boundary itself jumped. The sliding keys get no such
    // check, because their start drifts on every import by design and checking
    // it would reject them always.
    const starts = {};
    JUMPY.forEach(function (k) { if (BANDS[k]) starts[k] = BANDS[k][0]; });
    if (want && JUMPY.indexOf(want) !== -1) {
      let was = {};
      try { was = JSON.parse(sessionStorage.getItem(SKEY) || '{}'); } catch (e) {}
      if (!(want in starts) || was[want] !== starts[want]) want = null;
    }
    try { sessionStorage.setItem(SKEY, JSON.stringify(starts)); } catch (e) {}

    if (want && want !== 'all') {
      applyPeriod(want);
    } else {
      // A rejected key is forgotten, not kept: having fallen back to "All",
      // the next reload must not jump back to a filter the reader is no
      // longer looking at.
      try { sessionStorage.removeItem(PKEY); } catch (e) {}
      listeners.forEach(function (fn) { fn(); });
    }
  }

  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function () {
    listeners.forEach(function (fn) { fn(); });
  });
})();
