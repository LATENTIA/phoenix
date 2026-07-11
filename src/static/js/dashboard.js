// Dashboard logic: report iframe loading, "Load data", account CRUD, toasts.
// CURRENT_ACCOUNT is set inline by the template before this script loads.

const viewer = document.getElementById('viewer');

// ---------- CSRF wrapper ----------
// Every POST / PUT / PATCH / DELETE goes through fetchCsrf() which auto-injects
// the X-CSRFToken header read from the <meta name="csrf-token"> tag rendered
// by Flask-WTF. GET requests pass through unchanged (they're CSRF-exempt).
const CSRF_TOKEN = (
  document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || ''
);
function fetchCsrf(url, opts = {}) {
  const method = (opts.method || 'GET').toUpperCase();
  if (method === 'GET' || method === 'HEAD') return fetch(url, opts);
  const headers = new Headers(opts.headers || {});
  if (CSRF_TOKEN && !headers.has('X-CSRFToken')) {
    headers.set('X-CSRFToken', CSRF_TOKEN);
  }
  return fetch(url, { ...opts, headers });
}

// Sortable tables — the makeAllTablesSortable() function lives in
// sortable.js (loaded by dashboard.html BEFORE this file). Same file is
// loaded by share_dashboard.html so the read-only view gets the same
// header-click behaviour.


// ---------- Report tabs ----------
//
// Phase 2 removes the iframe. Reports are now fetched as partial HTML
// fragments (?partial=1) and injected directly into #viewer. The shell
// owns: typography, palette, fonts, scrolling, privacy toggle. Reports
// own: their own body content, tables, and per-report JS (re-executed
// after each injection because innerHTML doesn't run <script> tags).
function showReport(kind) {
  // Toggle the active sidebar pill. `.tab` is the legacy horizontal-strip
  // class still used by share_dashboard.html — keep both selectors live so
  // this file works in both layouts.
  document.querySelectorAll('.nav-item, .tab').forEach(t => {
    t.classList.toggle('active', t.dataset.report === kind);
  });

  // Reflect the active tab in the URL so a browser refresh (or a copied
  // link) lands the user on the same tab instead of the default TOB.
  // `replaceState` — not `pushState` — because a per-click history entry
  // would make the back button walk through every tab visited before
  // leaving the page, which is disorienting for a dashboard.
  try {
    const u = new URL(window.location);
    u.searchParams.set('report', kind);
    history.replaceState({report: kind}, '', u.toString());
  } catch (e) { /* older browsers — no-op */ }

  // Year-end marks are only meaningful for the personal CGT 2026+ basis
  // reset. Reveal the sidebar button only when the user is on that tab.
  const marksBtn = document.getElementById('btn-fetch-marks');
  if (marksBtn) {
    marksBtn.style.display = (kind === 'cgt') ? '' : 'none';
  }

  // Point the topbar CSV button at the current report. Kinds without a
  // CSV export (methodology, performance rolls up into pnl) hide it.
  updateCsvDownload(kind);

  // Each report kind has its own route — including `performance`, which
  // the server now routes through the P&L builder with the Performance
  // sub-tab pre-activated via data-initial-tab. No more tunneling
  // through `pnl?tab=performance` (which silently fell back to plain
  // P&L because the route handler doesn't read the query string).
  const url = `/report/${kind}/${CURRENT_ACCOUNT}?partial=1`;

  // Optimistic "loading" state — the fetch usually returns in ~200-500ms
  // for these reports, but a placeholder beats a blank #viewer between
  // tab clicks.
  viewer.innerHTML = '<div class="placeholder">Loading…</div>';

  fetch(url, {credentials: 'same-origin'})
    .then(r => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.text();
    })
    .then(html => {
      // Inject the partial. innerHTML drops the new DOM in but does NOT
      // execute <script> tags, so we re-execute them by cloning each one
      // into a new <script> element. Per-report JS (TOB filters, P&L
      // sub-tabs, etc.) re-binds its handlers to the freshly-injected DOM.
      viewer.innerHTML = html;
      viewer.querySelectorAll('script').forEach(orig => {
        const fresh = document.createElement('script');
        for (const a of orig.attributes) fresh.setAttribute(a.name, a.value);
        fresh.textContent = orig.textContent;
        orig.replaceWith(fresh);
      });
      // Performance activation is now handled server-side: the P&L
      // builder renders the partial with `data-initial-tab="performance"`
      // on the .report-shell wrap, and pnl.js's IIFE reads that attribute
      // and activates the right panel + sets body.isolated-tab. All we
      // need to do here is CLEAR isolated-tab when switching AWAY from
      // Performance — pnl.js sets it but never removes it on its own.
      if (kind !== 'performance') {
        document.body.classList.remove('isolated-tab');
      }
      // Make every table in the freshly injected partial click-sortable.
      makeAllTablesSortable();
    })
    .catch(err => {
      console.error('showReport failed:', err);
      viewer.innerHTML =
        '<div class="placeholder" style="color:var(--loss)">' +
        'Failed to load report: ' + (err && err.message ? err.message : err) +
        '</div>';
    });
}

function refreshActiveReport() {
  // Cover both layouts: .nav-item is the new sidebar pill (Phase 1
  // dashboard), .tab is the legacy horizontal tab strip still used by
  // share_dashboard.html.
  const active = document.querySelector('.nav-item.active, .tab.active');
  if (active && !active.classList.contains('empty')) showReport(active.dataset.report);
}

// Maps report kinds to the CSV route the server exposes. Methodology has
// no data to export; performance shares its data with pnl so we route
// its CSV to the pnl endpoint (closed lots, which is what the tab shows).
const CSV_EXPORT_KIND = {
  tob: 'tob',
  pnl: 'pnl',
  performance: 'pnl',
  cgt: 'cgt',
  corporate_tax: 'corporate_tax',
  dividends: 'dividends',
};
function updateCsvDownload(kind) {
  const btn = document.getElementById('csv-download');
  if (!btn) return;
  const csvKind = CSV_EXPORT_KIND[kind];
  if (!csvKind) {
    btn.style.display = 'none';
    return;
  }
  btn.style.display = '';
  btn.setAttribute('href', `/report/${csvKind}/${CURRENT_ACCOUNT}/csv`);
}

// ---------- Toasts ----------
function toast(message, options = {}) {
  const { type = 'info', duration = 3000, spin = false } = options;
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  el.innerHTML = (spin ? '<span class="spin"></span>' : '<span class="dot"></span>')
                 + '<span class="msg"></span>';
  el.querySelector('.msg').textContent = message;
  document.getElementById('toasts').appendChild(el);
  requestAnimationFrame(() => el.classList.add('show'));
  if (duration > 0) {
    setTimeout(() => {
      el.classList.remove('show');
      setTimeout(() => el.remove(), 300);
    }, duration);
  }
  return el;
}
function dismissToast(el) {
  if (!el) return;
  el.classList.remove('show');
  setTimeout(() => el.remove(), 300);
}

// ---------- Load data ----------
async function loadData(code) {
  const log = document.getElementById('log');
  log.className = 'log show';
  log.textContent = '> Download Flex + ingest into DB\n';
  const t = toast('Downloading from IBKR + ingesting into DB...', {spin: true, duration: 0});
  try {
    const res = await fetchCsrf(`/run/download/${code}`, { method: 'POST' });
    const data = await res.json();
    renderLog(log, data, true);
    dismissToast(t);
    if (data.returncode === 0) {
      toast(`Loaded successfully (${data.elapsed_s.toFixed(1)}s)`, {type: 'success', duration: 2500});
      refreshActiveReport();
      setTimeout(() => location.reload(), 1000);
    } else {
      // Prefer the human-readable message extracted on the server side.
      // Falls back to the generic "exit N" line when no friendly message is available.
      const message = data.friendly_message || `Failed (exit ${data.returncode}). See log below.`;
      toast(message, {type: 'error', duration: 6000});
    }
  } catch (e) {
    dismissToast(t);
    toast('Network error: ' + e.message, {type: 'error', duration: 4500});
  }
}

// ---------- Refresh year-end marks (Belgian CGT basis reset) ----------
async function fetchMarks(code) {
  const log = document.getElementById('log');
  log.className = 'log show';
  log.textContent = '> Refresh 2025-12-31 closing prices from Yahoo\n';
  const t = toast('Fetching year-end marks from Yahoo...', {spin: true, duration: 0});
  try {
    const res = await fetchCsrf(`/run/fetch_marks/${code}`, { method: 'POST' });
    const data = await res.json();
    renderLog(log, data, true);
    dismissToast(t);
    if (data.returncode === 0) {
      const msg = data.friendly_message || 'Year-end marks refreshed.';
      toast(msg, {type: 'success', duration: 4000});
      // If the CGT tab is active, reload it to pick up the new marks.
      const active = document.querySelector('.nav-item.active, .tab.active');
      if (active && active.dataset.report === 'cgt') refreshActiveReport();
    } else {
      const message = data.friendly_message || `Failed (exit ${data.returncode}). See log below.`;
      toast(message, {type: 'error', duration: 6000});
    }
  } catch (e) {
    dismissToast(t);
    toast('Network error: ' + e.message, {type: 'error', duration: 4500});
  }
}

function renderLog(log, data, append=false) {
  const prefix = append ? log.innerHTML : '';
  const out = data.stdout || '';
  const err = data.stderr || '';
  const status = data.returncode === 0
    ? `<span class="ok">OK in ${data.elapsed_s.toFixed(1)}s</span>`
    : `<span class="err">FAIL exit ${data.returncode}</span>`;
  log.innerHTML = prefix + out + (err ? `\n<span class="err">${err}</span>` : '') + `\n${status}\n`;
  log.scrollTop = log.scrollHeight;
}

// ---------- Add account ----------
function openAddAccount() {
  const o = document.getElementById('add-account-overlay');
  document.getElementById('add-account-form').reset();
  o.classList.add('show');
  o.querySelector('input[name="name"]').focus();
}
function closeAddAccount() {
  document.getElementById('add-account-overlay').classList.remove('show');
}
async function submitAddAccount(ev) {
  ev.preventDefault();
  const form = document.getElementById('add-account-form');
  const data = Object.fromEntries(new FormData(form).entries());
  const t = toast('Creating account...', {spin: true, duration: 0});
  try {
    const res = await fetchCsrf('/accounts/add', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data),
    });
    const result = await res.json();
    dismissToast(t);
    if (result.ok) {
      closeAddAccount();
      toast(`Account "${data.name}" created — folder ${result.folder}`, {type: 'success', duration: 3500});
      setTimeout(() => location.href = '/?account=' + data.name, 800);
    } else {
      toast('Error: ' + (result.errors || ['unknown']).join('; '), {type: 'error', duration: 5000});
    }
  } catch (e) {
    dismissToast(t);
    toast('Network error: ' + e.message, {type: 'error', duration: 4000});
  }
}

// ---------- Add manual trade ----------
function openAddManualTrade(accountCode) {
  const overlay = document.getElementById('add-manual-trade-overlay');
  const form = document.getElementById('add-manual-trade-form');
  form.reset();
  // Pre-fill account code (hidden field) and today's date.
  form.querySelector('[name="account_code"]').value = accountCode;
  form.querySelector('[name="trade_date"]').value = new Date().toISOString().slice(0, 10);
  form.querySelector('[name="currency"]').value = 'USD';
  overlay.classList.add('show');
  form.querySelector('[name="symbol"]').focus();
}

function closeAddManualTrade() {
  document.getElementById('add-manual-trade-overlay').classList.remove('show');
}

async function submitManualTrade(ev) {
  ev.preventDefault();
  const form = document.getElementById('add-manual-trade-form');
  const data = Object.fromEntries(new FormData(form).entries());
  // Coerce numerics — FormData gives us strings only.
  data.quantity = parseFloat(data.quantity);
  data.price = parseFloat(data.price);
  data.commission = parseFloat(data.commission || '0');
  const t = toast('Adding trade...', {spin: true, duration: 0});
  try {
    const res = await fetchCsrf('/trades/manual', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data),
    });
    const result = await res.json();
    dismissToast(t);
    if (result.ok) {
      closeAddManualTrade();
      toast(`Added ${data.side} ${data.quantity} ${data.symbol} (manual, id=${result.trade_id})`,
            {type: 'success', duration: 3000});
      // Refresh the active report so the new trade appears.
      setTimeout(() => refreshActiveReport(), 600);
    } else {
      const msg = (result.errors || [result.error || 'unknown']).join('; ');
      toast('Error: ' + msg, {type: 'error', duration: 5000});
    }
  } catch (e) {
    dismissToast(t);
    toast('Network error: ' + e.message, {type: 'error', duration: 4000});
  }
}

// Destructive operations (delete account, empty DB) have moved to /settings
// to put them one navigation step away from accidental clicks. The settings
// page handles its own typed-confirmation flow with the helpers below
// (fetchCsrf, toast).

// ---------- Privacy toggle ----------
//
// Phase 2C moved this out of every report's JS. The shell now owns the
// single #privacy-toggle button in the topbar and binds the handler once.
// Reports rely on body.privacy being toggled — that class still drives
// their CSS rules (body.privacy td.num { filter: blur(...) } etc.), so
// nothing changes on the report side. Persisted via localStorage so the
// preference survives reloads / tab switches.
(function setupPrivacyToggle() {
  const btn = document.getElementById('privacy-toggle');
  if (!btn) return;     // share dashboard doesn't render this button
  function apply(on) {
    document.body.classList.toggle('privacy', on);
    btn.textContent = on ? '🙈' : '👁';
    btn.title = on ? 'Show values' : 'Hide values';
  }
  btn.addEventListener('click', () => {
    const on = !document.body.classList.contains('privacy');
    localStorage.setItem('ibkr_privacy', on ? '1' : '0');
    apply(on);
  });
  apply(localStorage.getItem('ibkr_privacy') === '1');
})();


// ---------- Boot ----------
//
// Phase 2: the dashboard's `/` route already inlines the active report's
// partial into #viewer when the account has data, so the first paint is
// real content (no Loading flash). Detect that via the marker element and
// just re-run any embedded <script> tags for the inlined partial.
//
// When there's no initial paint (empty account, or the JS-only share
// dashboard), we fall back to the legacy "auto-open the first tab" path.
document.addEventListener('DOMContentLoaded', () => {
  const marker = document.getElementById('initial-paint-marker');
  if (marker) {
    // The server-rendered partial is already in #viewer AND its inline
    // <script> already executed at parse time (the browser runs inline
    // scripts as it parses, so the report's IIFE bound its handlers
    // before this DOMContentLoaded fired). We do NOT need to re-clone
    // and re-execute the script — doing that would bind every handler a
    // second time, which adds latency and weird double-firing behaviour.
    // Just sync the marks-button visibility for the initial tab.
    const kind = marker.dataset.report;
    const marksBtn = document.getElementById('btn-fetch-marks');
    if (marksBtn) marksBtn.style.display = (kind === 'cgt') ? '' : 'none';
    updateCsvDownload(kind);
    // Deep-link to Performance (?report=performance) is now handled
    // server-side: the partial ships with `data-initial-tab="performance"`
    // on its .report-shell wrap, and pnl.js reads it and activates the
    // right panel at parse time. Nothing to do here.
    // Make every table in the server-rendered partial click-sortable.
    makeAllTablesSortable();
    return;
  }
  // No initial paint — fall back to "open the first non-empty tab".
  const first = document.querySelector('.nav-item:not(.empty), .tab:not(.empty)');
  if (first) showReport(first.dataset.report);
});
