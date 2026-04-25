// Toggles a "privacy" mode that blurs all numeric values in the page.
// Persists across reports via localStorage.
(function () {
  function applyPrivacy(on) {
    document.body.classList.toggle('privacy', on);
    const btn = document.getElementById('privacy-toggle');
    if (!btn) return;
    btn.textContent = on ? '\uD83D\uDE48' : '\uD83D\uDC41';
    btn.title = on ? 'Show values' : 'Hide values';
  }
  document.addEventListener('DOMContentLoaded', () => {
    const btn = document.getElementById('privacy-toggle');
    if (!btn) return;
    btn.addEventListener('click', () => {
      const on = !document.body.classList.contains('privacy');
      localStorage.setItem('ibkr_privacy', on ? '1' : '0');
      applyPrivacy(on);
    });
    applyPrivacy(localStorage.getItem('ibkr_privacy') === '1');
  });
})();
