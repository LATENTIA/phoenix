// Dividends report: tab switching + privacy toggle + isolated-mode.
(function () {
  const buttons = document.querySelectorAll('.tab');
  const panels = document.querySelectorAll('.tab-panel');

  function activate(target) {
    buttons.forEach(b => b.classList.toggle('active', b.dataset.target === target));
    panels.forEach(p => p.classList.toggle('active', p.id === target));
  }
  buttons.forEach(btn => btn.addEventListener('click', () => activate(btn.dataset.target)));

  // ?tab=annual|symbol|payments|sources from the dashboard iframe.
  const params = new URLSearchParams(window.location.search);
  const requested = params.get('tab');
  if (requested) {
    const targetId = 'tab-' + requested;
    if (document.getElementById(targetId)) {
      activate(targetId);
      document.body.classList.add('isolated-tab');
    }
  }

  // Privacy toggle owned by the dashboard shell (Phase 2C). See tob.js
  // for the rationale.
})();
