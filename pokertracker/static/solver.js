(function () {
  const root = document.getElementById('solver');
  const dataEl = document.getElementById('solver-data');
  if (!root || !dataEl) return;
  const D = JSON.parse(dataEl.textContent);
  const $ = (s, r) => (r || root).querySelector(s);
  const $$ = (s, r) => Array.from((r || root).querySelectorAll(s));
  const SUIT = { s: '♠', h: '♥', d: '♦', c: '♣' };
  const SLOTS = ['h0', 'h1', 'b0', 'b1', 'b2', 'b3', 'b4'];
  const state = { cards: {}, active: 'h0', cells: new Set(),
                  mode: 'facing_bet', continues: null };
  const out = $('#sv-out'), status = $('#sv-status'), note = $('#sv-hand');
  const rangeBox = $('#sv-range'), count = $('#sv-count');

  function esc(s) {
    return String(s).replace(/[&<>"]/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }
  function face(card) { return card[0] + SUIT[card[1]]; }
  function pct(x, d) { return (x * 100).toFixed(d === undefined ? 1 : d) + '%'; }
  function bb(x) { return (x >= 0 ? '+' : '−') + Math.abs(x).toFixed(2) + ' bb'; }
  function say(text, bad) {
    status.textContent = text;
    status.classList.remove('busy');
    status.classList.toggle('err', !!bad);
  }

  async function api(path, body) {
    const res = await fetch(path, body === undefined ? {} : {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body) });
    const d = await res.json();
    if (!res.ok) throw new Error(d.error || ('HTTP ' + res.status));
    return d;
  }

  // ---- cards
  function paintCards() {
    const used = new Set(Object.values(state.cards));
    $$('.sv-slot').forEach(function (b) {
      const c = state.cards[b.dataset.slot];
      b.textContent = c ? face(c) : '';
      b.dataset.suit = c ? c[1] : '';
      b.classList.toggle('filled', !!c);
      b.setAttribute('aria-pressed', String(b.dataset.slot === state.active));
    });
    $$('.sv-card').forEach(function (b) { b.disabled = used.has(b.dataset.card); });
  }
  function firstEmpty() { return SLOTS.find(k => !state.cards[k]) || null; }
  $$('.sv-slot').forEach(function (b) {
    b.addEventListener('click', function () {
      delete state.cards[b.dataset.slot];
      state.active = b.dataset.slot;
      paintCards();
    });
  });
  $$('.sv-card').forEach(function (b) {
    b.addEventListener('click', function () {
      const slot = state.active && !state.cards[state.active] ? state.active : firstEmpty();
      if (!slot) return;
      state.cards[slot] = b.dataset.card;
      state.active = firstEmpty();
      paintCards();
    });
  });

  // ---- range: the grid, the text and the presets are three views of one set
  function paintGrid() {
    $$('.sv-cell').forEach(function (b) {
      b.setAttribute('aria-pressed', String(state.cells.has(b.dataset.cell)));
    });
  }
  let rangeSeq = 0;
  async function syncRange(text) {
    const mine = ++rangeSeq;
    try {
      const d = await api('/api/range', { range: text });
      if (mine !== rangeSeq) return false;
      state.cells = new Set(d.cells);
      rangeBox.value = d.range;
      count.textContent = d.combos + ' holdings · ' + pct(d.combos / 1326) + ' of hands';
      paintGrid();
      $$('.chip[data-preset]').forEach(o => o.setAttribute('aria-pressed', 'false'));
      say('');
      return true;
    } catch (e) { say(e.message, true); return false; }
  }
  let painting = null, timer = null;
  function touch(b) {
    if (painting) state.cells.add(b.dataset.cell); else state.cells.delete(b.dataset.cell);
    b.setAttribute('aria-pressed', String(painting));
    clearTimeout(timer);
    timer = setTimeout(function () { syncRange(Array.from(state.cells).join(' ')); }, 250);
  }
  $$('.sv-cell').forEach(function (b) {
    // Press to toggle one cell, drag to paint the rest the same way.
    b.addEventListener('mousedown', function (ev) {
      ev.preventDefault();
      painting = !state.cells.has(b.dataset.cell);
      touch(b);
    });
    b.addEventListener('mouseenter', function () { if (painting !== null) touch(b); });
    b.addEventListener('keydown', function (ev) {
      if (ev.key !== 'Enter' && ev.key !== ' ') return;
      ev.preventDefault();
      painting = !state.cells.has(b.dataset.cell);
      touch(b);
      painting = null;
    });
  });
  document.addEventListener('mouseup', function () { painting = null; });
  rangeBox.addEventListener('change', function () { syncRange(rangeBox.value); });
  rangeBox.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') { ev.preventDefault(); syncRange(rangeBox.value).then(ok => ok && solve()); }
  });
  $$('.chip[data-preset]').forEach(function (b) {
    b.addEventListener('click', async function () {
      const p = D.presets.find(x => x.key === b.dataset.preset);
      if (await syncRange(p.range)) b.setAttribute('aria-pressed', 'true');
    });
  });

  // ---- the bet
  function paintMode() {
    $$('.chip[data-mode]').forEach(o =>
      o.setAttribute('aria-pressed', String(o.dataset.mode === state.mode)));
    const facing = state.mode === 'facing_bet';
    $('#sv-pot-label').textContent = facing ? 'Pot, with their bet in it (bb)' : 'Pot (bb)';
    $('#sv-bet-label').textContent = facing ? 'To call (bb)' : 'Your bet (bb)';
  }
  $$('.chip[data-mode]').forEach(function (b) {
    b.addEventListener('click', function () { state.mode = b.dataset.mode; paintMode(); });
  });

  // ---- the answer
  function spec() {
    const s = {
      hero: ['h0', 'h1'].map(k => state.cards[k]).filter(Boolean).join(' '),
      board: ['b0', 'b1', 'b2', 'b3', 'b4'].map(k => state.cards[k]).filter(Boolean).join(' '),
      range: Array.from(state.cells).join(' '),
      pot: $('#sv-pot').value, bet: $('#sv-bet').value, mode: state.mode,
    };
    if (state.continues) s.continues = Array.from(state.continues);
    return s;
  }
  let solveSeq = 0;
  async function solve() {
    const mine = ++solveSeq;
    status.textContent = 'Calculating';
    status.classList.remove('err');
    status.classList.add('busy');
    try {
      const d = await api('/api/solve', spec());
      if (mine !== solveSeq) return;
      // Kept only once there are buckets to keep: a preflop answer has none,
      // and remembering "nothing continues" from it would fold every hand on
      // the next flop.
      if (d.buckets.length) {
        state.continues = new Set(d.buckets.filter(b => b.continues).map(b => b.bucket));
      }
      render(d);
      say('');
    } catch (e) {
      if (mine === solveSeq) say(e.message, true);
    }
  }
  $('#sv-go').addEventListener('click', solve);
  ['#sv-pot', '#sv-bet'].forEach(function (id) {
    $(id).addEventListener('keydown', function (ev) { if (ev.key === 'Enter') solve(); });
  });

  function tile(k, v, m) {
    return '<div class="tile"><div class="k">' + esc(k) + '</div><div class="v">'
      + esc(v) + '</div><div class="m">' + esc(m) + '</div></div>';
  }
  function render(d) {
    const how = d.method === 'exact'
      ? 'exact · ' + d.evaluated.toLocaleString() + ' runouts × holdings'
      : 'estimated from ' + d.evaluated.toLocaleString() + ' sampled boards, ± '
        + pct(1.96 * d.se) + ' — preflop against a range is too many boards to enumerate';
    let html = '<div class="tiles">'
      + tile('Your equity', pct(d.equity), how)
      + tile('You hold', d.hero_class || d.hero,
             d.board ? d.hero + ' on ' + d.board : 'preflop')
      + tile('Their range', d.combos.toLocaleString(),
             'holdings once your cards and the board are removed');
    if (d.facing) {
      const f = d.facing;
      html += tile('Calling ' + d.bet + ' bb to win ' + d.pot + ' bb', bb(f.ev_call),
                   'needs ' + pct(f.required_equity) + ' · you have ' + pct(d.equity));
    } else {
      const b = d.betting;
      const called = b.equity_when_called === null ? 'nothing calls'
                   : pct(b.equity_when_called) + ' equity when called';
      html += tile('Betting ' + d.bet + ' bb into ' + d.pot + ' bb', bb(b.ev_bet),
                   (b.fold_derived
                      ? 'they fold ' + pct(b.fold_freq) + ' (the unticked rows below)'
                      : 'no fold frequency before the flop: priced as called by the whole range')
                   + ' · ' + called)
            + tile('Checking instead', bb(b.ev_check),
                   'a bet with no equity needs ' + pct(b.breakeven_fold) + ' folds');
    }
    html += '</div>';

    if (d.buckets.length) {
      const pick = !!d.betting;
      html += '<table><thead><tr><th>Their hand</th><th>Holdings</th>'
            + '<th>Share of range</th><th>Your equity against it</th>'
            + (pick ? '<th>Continues against a bet</th>' : '') + '</tr></thead><tbody>';
      d.buckets.forEach(function (r) {
        const none = r.combos === 0;
        html += '<tr><td' + (none ? ' class="none"' : '') + '>' + esc(r.label) + '</td>'
              + '<td' + (none ? ' class="none"' : '') + '>' + r.combos + '</td>'
              + '<td' + (none ? ' class="none"' : '') + '>' + (none ? '—' : pct(r.share)) + '</td>'
              + '<td' + (none ? ' class="none"' : '') + '>' + (none ? '—' : pct(r.equity)) + '</td>'
              + (pick ? '<td><input type="checkbox" data-bucket="' + esc(r.bucket) + '"'
                        + (r.continues ? ' checked' : '')
                        + ' aria-label="' + esc(r.label) + ' continues"></td>' : '')
              + '</tr>';
      });
      html += '</tbody></table>';
    }
    html += '<p class="note">' + esc(d.caveat) + '</p>';
    out.innerHTML = html;
    $$('input[data-bucket]', out).forEach(function (box) {
      box.addEventListener('change', function () {
        if (box.checked) state.continues.add(box.dataset.bucket);
        else state.continues.delete(box.dataset.bucket);
        solve();
      });
    });
  }

  // ---- a real hand, sent over from the Stats tab
  async function loadHand(id, street) {
    const tab = document.querySelector('.tab[data-tab="solver"]');
    if (tab && tab.getAttribute('aria-selected') !== 'true') tab.click();
    say('Loading hand');
    let d;
    try {
      d = await api('/api/hand?id=' + encodeURIComponent(id)
                    + (street ? '&street=' + encodeURIComponent(street) : ''));
    } catch (e) { say(e.message, true); return; }

    state.cards = {};
    d.hero.split(' ').filter(Boolean).forEach((c, i) => { state.cards['h' + i] = c; });
    d.board.split(' ').filter(Boolean).forEach((c, i) => { state.cards['b' + i] = c; });
    state.active = firstEmpty();
    state.mode = d.mode;
    state.continues = null;
    $('#sv-pot').value = d.pot;
    $('#sv-bet').value = d.bet;
    paintCards();
    paintMode();

    const verb = { fold: 'folded', call: 'called', check: 'checked',
                   bet: 'bet', raise: 'raised' }[d.did] || d.did;
    let html = '<b>Hand #' + esc(d.site_hand_no) + '</b> · '
      + d.streets.map(s => '<button type="button" class="chip" data-street="' + esc(s)
          + '" aria-pressed="' + (s === d.street) + '">' + esc(s) + '</button>').join(' ')
      + ' · you ' + esc(verb) + ' here'
      + (d.hero_position ? ' from ' + esc(d.hero_position) : '') + '. ';
    const opp = d.opponents.map(o => esc(o.player) + (o.position ? ' (' + esc(o.position) + ')' : '')
      + (o.shown ? ', who showed ' + esc(o.shown) : ''));
    if (d.opponents.length === 1) {
      html += 'Against ' + opp[0] + '. ';
      const p = D.presets.find(x => x.key === d.preset);
      html += p ? 'Range suggested: <b>' + esc(p.label) + '</b>, because ' + esc(d.preset_why)
                  + '. That is your own chart read onto an opponent — change it if they do not play like it.'
                : 'Nothing in your chart describes what they did preflop, so the range is yours to pick.';
    } else {
      html += d.opponents.length + ' opponents still in: ' + opp.join('; ')
            + '. Pick the range of the one you want to test against.';
    }
    note.innerHTML = html;
    note.hidden = false;
    $$('[data-street]', note).forEach(function (b) {
      b.addEventListener('click', function () { loadHand(id, b.dataset.street); });
    });

    out.innerHTML = '';
    if (d.range) {
      if (await syncRange(d.range)) solve();
    } else {
      say('Pick a range, then Calculate');
    }
  }
  // Delegated, because the period filter replaces the drill-down's markup
  // wholesale and a handler bound to a button would go with it.
  document.addEventListener('click', function (ev) {
    const b = ev.target.closest && ev.target.closest('[data-analyse]');
    if (b) loadHand(b.dataset.analyse, '');
  });

  $('#sv-clear').addEventListener('click', function () {
    state.cards = {}; state.active = 'h0'; state.continues = null;
    note.hidden = true; note.innerHTML = ''; out.innerHTML = '';
    paintCards();
    syncRange('');
  });

  paintCards();
  paintMode();
})();
