// P&L report: tab switching + isolated-mode + privacy toggle.
(function () {
  const buttons = document.querySelectorAll('.tab');
  const panels = document.querySelectorAll('.tab-panel');

  function activate(target) {
    buttons.forEach(b => b.classList.toggle('active', b.dataset.target === target));
    panels.forEach(p => p.classList.toggle('active', p.id === target));
  }
  buttons.forEach(btn => btn.addEventListener('click', () => activate(btn.dataset.target)));

  // Allow external nav to a specific sub-tab. The old design read this
  // from `window.location.search` (worked only when the partial ran in
  // its own iframe — the iframe URL carried `?tab=performance`). After
  // the Phase 2 iframe removal, the browser URL is the dashboard's, not
  // the partial fetch URL, so the query string trick stops working.
  //
  // We now read `data-initial-tab` off the closest `.report-shell` wrap
  // instead. The Python builder sets this attribute when called via
  // /report/performance/<account>?partial=1 — same activation result,
  // no dependency on the browser URL.
  const shell = document.querySelector('.report-shell[data-report="pnl"]');
  const requested = shell ? (shell.dataset.initialTab || '') : '';
  if (requested) {
    const targetId = 'tab-' + requested;
    if (document.getElementById(targetId)) {
      activate(targetId);
      document.body.classList.add('isolated-tab');
    }
  }

  // Privacy toggle owned by the dashboard shell (Phase 2C). See tob.js
  // for the rationale; same applies here — this IIFE re-runs on every
  // tab switch so it must not re-bind shell-level handlers.
})();
