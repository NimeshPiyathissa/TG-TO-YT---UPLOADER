/* Live job progress. Subscribes to /events (Server-Sent Events) and animates the bars that
   the server already rendered, so nothing is replaced and nothing jumps:
   - each frame eases the shown value toward the latest reported one;
   - between events it keeps moving at the reported rate (for at most 2.5s of silence).
   If the stream is unavailable, htmx polling takes over (window.__sseLive is false).
   When the set of active jobs changes, the region is re-fetched once from the server. */
(function () {
  'use strict';
  window.__sseLive = false;
  if (!('EventSource' in window)) return;

  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var pill = document.getElementById('live-pill');
  var ACTIVE = { QUEUED: 1, DOWNLOADING: 1, AWAITING_CHANNEL: 1, AWAITING_CAPTION: 1, READY: 1, UPLOADING: 1, VERIFYING: 1 };
  var jobs = {};          // id -> model
  var refreshing = false;

  function num(v) { return parseFloat(v) || 0; }
  function legOf(state) {
    if (state === 'QUEUED' || state === 'DOWNLOADING') return 'pull';
    if (state === 'UPLOADING' || state === 'VERIFYING') return 'push';
    return null;
  }

  function apply(v) {
    var j = jobs[v.id];
    if (!j) { j = jobs[v.id] = { sPull: v.pull, sPush: v.push, els: {} }; }
    j.state = v.state; j.pull = v.pull; j.push = v.push;
    j.cur = v.cur; j.total = v.total; j.rate = v.rate; j.t = performance.now();
  }

  // Read what the server rendered (first paint, and after each htmx swap).
  function collect() {
    var seen = {};
    document.querySelectorAll('[data-job]').forEach(function (el) {
      var id = el.dataset.job;
      seen[id] = true;
      // Re-rendered elements carry server truth; apply() keeps the value currently shown,
      // so a swap doesn't make the bar jump.
      apply({ id: id, state: el.dataset.state, pull: num(el.dataset.pull), push: num(el.dataset.push),
              cur: num(el.dataset.cur), total: num(el.dataset.total), rate: num(el.dataset.rate) });
    });
    Object.keys(jobs).forEach(function (id) {
      if (!seen[id]) { delete jobs[id]; return; }
      var els = { monitor: null, row: null };
      document.querySelectorAll('[data-job="' + id + '"]').forEach(function (el) {
        if (el.classList.contains('monitor')) els.monitor = el; else els.row = el;
      });
      jobs[id].els = els;
    });
  }

  // ---- formatting (mirrors bot/ui.py) ----
  function hbytes(n) {
    var u = ['B', 'KB', 'MB', 'GB'], i = 0;
    while (n >= 1024 && i < 3) { n /= 1024; i++; }
    return i === 0 ? Math.round(n) + ' B' : n.toFixed(1) + ' ' + u[i];
  }
  function heta(s) {
    s = Math.round(s);
    if (!isFinite(s) || s < 0) return '—';
    if (s < 60) return s + 's';
    var m = Math.floor(s / 60); s = s % 60;
    if (m < 60) return m + 'm' + (s < 10 ? '0' : '') + s + 's';
    return Math.floor(m / 60) + 'h' + ('0' + (m % 60)).slice(-2) + 'm';
  }
  function pct(f) { return Math.floor(Math.min(1, Math.max(0, f)) * 100); }
  function setText(el, t) { if (el && el.textContent !== t) el.textContent = t; }

  // Ease toward the target, but land on it exactly once it's close (else 100% never shows).
  function settle(shown, target, k) {
    return Math.abs(target - shown) < 0.0005 ? target : shown + (target - shown) * k;
  }

  function paint(j, now) {
    var leg = legOf(j.state);
    var dt = Math.min((now - j.t) / 1000, 2.5);
    var pull = j.pull, push = j.push, cur = j.cur;
    if (j.rate > 0 && j.total > 0 && leg) {
      var d = j.rate * dt / j.total;
      if (leg === 'pull') pull = Math.min(1, pull + d); else if (j.state === 'UPLOADING') push = Math.min(1, push + d);
      cur = Math.min(j.total, cur + j.rate * dt);
    }
    var k = reduce ? 1 : 0.14;
    j.sPull = settle(j.sPull, pull, k);
    j.sPush = settle(j.sPush, push, k);

    var m = j.els.monitor;
    if (m) {
      var a = m.querySelector('[data-f=pull]'), b = m.querySelector('[data-f=push]');
      a.style.setProperty('--p', j.sPull.toFixed(4)); a.style.setProperty('--h', j.sPull > 0.002 && j.sPull < 0.998 ? 1 : 0);
      b.style.setProperty('--p', j.sPush.toFixed(4)); b.style.setProperty('--h', j.sPush > 0.002 && j.sPush < 0.998 ? 1 : 0);
      setText(m.querySelector('[data-f=pull-pct]'), pct(j.sPull) + '%');
      setText(m.querySelector('[data-f=push-pct]'), pct(j.sPush) + '%');
      m.querySelector('[data-f=n2]').classList.toggle('on', j.sPull > 0.995);
      m.querySelector('[data-f=n3]').classList.toggle('on', j.sPush > 0.002);
      setText(m.querySelector('[data-f=rate]'), j.rate > 0 ? hbytes(j.rate) + '/s' : '—');
      setText(m.querySelector('[data-f=eta]'), j.rate > 0 && j.total > cur ? heta((j.total - cur) / j.rate) : '—');
      setText(m.querySelector('[data-f=amt]'), hbytes(cur) + ' / ' + hbytes(j.total));
    }
    var r = j.els.row;
    if (r) {
      var bar = r.querySelector('.pbar i');
      if (bar) {
        var f = j.state === 'DONE' ? 1 : (leg === 'pull' ? j.sPull : (leg === 'push' ? j.sPush : null));
        if (f !== null) {
          bar.style.setProperty('--p', f.toFixed(4));
          setText(r.querySelector('[data-f=pct]'), pct(f) + '%');
          var holder = r.querySelector('.pbar'); if (holder) holder.setAttribute('aria-valuenow', pct(f));
        }
      }
    }
  }

  function frame(now) {
    for (var id in jobs) if (jobs.hasOwnProperty(id)) paint(jobs[id], now);
    window.requestAnimationFrame(frame);
  }

  // ---- structure changes: ask the server to re-render the region once ----
  function regionNeedsRefresh(list) {
    var ids = {};
    list.forEach(function (v) { ids[v.id] = v; });
    var changed = false;
    document.querySelectorAll('tr[data-job], .monitor[data-job]').forEach(function (el) {
      var id = el.dataset.job, state = el.dataset.state;
      if (!ACTIVE[state]) return;                       // finished rows don't come back
      if (!ids[id] || ids[id].state !== state) changed = true;
    });
    document.querySelectorAll('[data-live-all]').forEach(function () {
      list.forEach(function (v) { if (!document.querySelector('[data-job="' + v.id + '"]')) changed = true; });
    });
    return changed;
  }
  function refresh() {
    var region = document.querySelector('[data-live-region]') || document.getElementById('dashboard-live') || document.getElementById('jobs-list');
    if (!region || refreshing || !window.htmx) return;
    refreshing = true;
    window.htmx.ajax('GET', region.getAttribute('hx-get'), { target: region, swap: 'innerHTML' });
    setTimeout(function () { refreshing = false; }, 1500);
  }
  document.body.addEventListener('htmx:afterSwap', function (e) {
    collect();
    if (e.target && e.target.classList) e.target.classList.add('swapped');   // later refreshes shouldn't replay entrance animations
    var now = performance.now();
    for (var id in jobs) if (jobs.hasOwnProperty(id)) paint(jobs[id], now);   // repaint before the next frame so the swap is invisible
  });

  function setLive(on) {
    window.__sseLive = on;
    if (!pill) return;
    pill.classList.toggle('is-off', false);
    pill.classList.toggle('is-warn', !on);
    setText(pill.querySelector('span'), on ? 'Live' : 'Reconnecting…');
  }

  var es = new window.EventSource('/events');
  es.addEventListener('open', function () { setLive(true); });
  es.addEventListener('error', function () { setLive(false); });
  es.addEventListener('jobs', function (e) {
    setLive(true);
    var list = JSON.parse(e.data).jobs || [];
    list.forEach(apply);
    if (regionNeedsRefresh(list)) refresh();
  });
  window.addEventListener('pagehide', function () { es.close(); });

  // ---- uptime in the header ticks locally ----
  var up = document.querySelector('[data-uptime]');
  if (up) {
    var base = num(up.dataset.uptime), t0 = Date.now();
    setInterval(function () {
      var s = Math.floor(base + (Date.now() - t0) / 1000), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
      setText(up, (h ? h + 'h ' : '') + m + 'm ' + (s % 60) + 's');
    }, 1000);
  }

  collect();
  window.requestAnimationFrame(frame);
})();
