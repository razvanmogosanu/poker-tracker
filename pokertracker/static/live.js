(function () {
  const status = document.getElementById('refresh-status');
  if (!status) return;
  const SCROLL = 'pt-scroll';
  const RELOADED = 'pt-reloaded';
  const EVERY = 60000;
  // What this page was rendered from. Staleness is a fact about the page, not
  // about whether our own refresh call happened to be the one that imported.
  const RENDERED = parseInt(status.dataset.hands, 10);

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
  let waiting = false;

  function clock(d) {
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }
  function idle(text) {
    status.textContent = text;
    status.classList.remove('busy');
  }
  function showStamp() {
    if (busy || waiting) return;
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
      // d.new_hands is what this call imported; the total is what the server
      // holds. Only the second one can say whether the page is behind.
      const behind = Number.isNaN(RENDERED) ? d.new_hands
                                            : d.total_hands - RENDERED;
      if (!behind) {
        busy = false;
        showStamp();
        return;
      }
      const what = behind > 0 ? '+' + behind + ' hands' : 'history changed';
      // A reload rebuilds the page, and a half-built scenario on the Solver
      // tab is not something the server can give back. New hands arriving is
      // no reason to throw it away, so the reload waits for the reader to go
      // back to Stats -- which is the tab the new hands are for anyway.
      const solver = document.getElementById('tab-solver');
      if (solver && !solver.hidden) {
        busy = false;
        idle(what + ' — will reload when you go back to Stats');
        if (!waiting) {
          waiting = true;
          document.addEventListener('pt-tab', function (ev) {
            if (ev.detail === 'stats') { waiting = false; refresh(); }
          }, { once: true });
        }
        return;
      }
      status.textContent = what + ', reloading';
      // Both are read once on the way back and cleared, so only the reload
      // this line is about restores anything.
      sessionStorage.setItem(SCROLL, String(window.scrollY));
      sessionStorage.setItem(RELOADED, '1');
      location.reload();
    } catch (e) {
      busy = false;
      idle('Refresh failed at ' + clock(new Date()) + ': ' + e.message);
    }
  }

  // The desktop launcher leaves no window behind, so this is the off switch.
  // Two clicks rather than a confirm(): a modal dialog would block the page.
  const stop = document.getElementById('stop-tracker');
  let timers = [];
  if (stop) {
    let armed = null;
    stop.addEventListener('click', async function () {
      if (!armed) {
        stop.textContent = 'Click again to stop';
        stop.classList.add('armed');
        armed = setTimeout(function () {
          armed = null;
          stop.textContent = 'Stop tracker';
          stop.classList.remove('armed');
        }, 4000);
        return;
      }
      clearTimeout(armed);
      try {
        await fetch('/api/stop', { method: 'POST' });
        timers.forEach(clearInterval);
        stop.remove();
        idle('Tracker stopped. Start it again from the desktop shortcut.');
      } catch (e) {
        idle('Could not stop the tracker: ' + e.message);
      }
    });
  }

  showStamp();
  timers = [setInterval(showStamp, 5000), setInterval(refresh, EVERY)];
})();
