/* Small, dependency-free UI glue: the phone "More" sheet and a loading state on
   submit buttons. Everything works without it (forms post normally). */
(function () {
  var btn = document.getElementById('more-btn');
  var sheet = document.getElementById('more-sheet');
  if (btn && sheet) {
    var setOpen = function (open) {
      sheet.classList.toggle('open', open);
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    };
    btn.addEventListener('click', function (e) { e.stopPropagation(); setOpen(!sheet.classList.contains('open')); });
    document.addEventListener('click', function (e) { if (!sheet.contains(e.target)) setOpen(false); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') setOpen(false); });
  }

  // Theme switch (Light / Dark / System). The head script in base.html applies the theme;
  // this only wires the buttons and keeps aria-pressed in sync.
  var syncTheme = function () {
    var pref = document.documentElement.getAttribute('data-theme-pref') || 'system';
    document.querySelectorAll('[data-theme-set]').forEach(function (b) {
      b.setAttribute('aria-pressed', b.getAttribute('data-theme-set') === pref ? 'true' : 'false');
    });
  };
  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('[data-theme-set]');
    if (b && window.__setTheme) { window.__setTheme(b.getAttribute('data-theme-set')); syncTheme(); }
  });
  document.addEventListener('themechange', syncTheme);
  syncTheme();

  // Delete-video confirmation: a dialog that still needs DELETE typed. The server re-checks it.
  var dlg = document.getElementById('delete-dialog');
  if (dlg && dlg.showModal) {
    var form = dlg.querySelector('form'), input = dlg.querySelector('input[name=confirm]'), go = dlg.querySelector('[data-go]');
    document.addEventListener('click', function (e) {
      var t = e.target.closest && e.target.closest('[data-delete-video]');
      if (!t) return;
      form.action = '/videos/' + encodeURIComponent(t.getAttribute('data-delete-video')) + '/delete';
      dlg.querySelector('.target').textContent = t.getAttribute('data-title') || '';
      input.value = ''; go.disabled = true;
      dlg.showModal(); input.focus();
    });
    input.addEventListener('input', function () { go.disabled = input.value.trim().toLowerCase() !== 'delete'; });
    dlg.addEventListener('click', function (e) { if (e.target === dlg) dlg.close(); });
    dlg.querySelector('[data-cancel]').addEventListener('click', function () { dlg.close(); });
  }

  // Show a spinner on the button that was pressed. Skipped for htmx forms (the page
  // doesn't reload, so the state would stick) and when a confirm() already said no.
  document.addEventListener('submit', function (e) {
    var form = e.target, b = e.submitter;
    if (e.defaultPrevented || !b || form.hasAttribute('hx-post') || !b.classList.contains('btn')) return;
    b.classList.add('is-loading');
  });
  window.addEventListener('pageshow', function () {
    document.querySelectorAll('.btn.is-loading').forEach(function (b) { b.classList.remove('is-loading'); });
  });
})();
