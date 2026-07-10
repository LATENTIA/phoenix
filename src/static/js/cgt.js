// Belgian CGT report: tab switching + isolated-mode + privacy toggle.
(function () {
  const buttons = document.querySelectorAll('.tab');
  const panels = document.querySelectorAll('.tab-panel');

  function activate(target) {
    buttons.forEach(b => b.classList.toggle('active', b.dataset.target === target));
    panels.forEach(p => p.classList.toggle('active', p.id === target));
  }
  buttons.forEach(btn => btn.addEventListener('click', () => activate(btn.dataset.target)));

  // External nav via ?tab=summary|offset|trades|marks
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
