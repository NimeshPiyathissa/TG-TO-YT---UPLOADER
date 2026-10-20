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
