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

  // Privacy toggle (shared pattern with the other reports).
  const privacyBtn = document.getElementById('privacy-toggle');
  function applyPrivacy(on) {
    document.body.classList.toggle('privacy', on);
    privacyBtn.textContent = on ? '🙈' : '👁';
    privacyBtn.title = on ? 'Show values' : 'Hide values';
  }
  privacyBtn.addEventListener('click', () => {
    const on = !document.body.classList.contains('privacy');
    localStorage.setItem('ibkr_privacy', on ? '1' : '0');
    applyPrivacy(on);
  });
  applyPrivacy(localStorage.getItem('ibkr_privacy') === '1');
})();
