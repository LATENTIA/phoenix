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

// ---------- Report tabs ----------
function showReport(kind) {
  document.querySelectorAll('.tab').forEach(t => {
    t.classList.toggle('active', t.dataset.report === kind);
  });
  const ts = Date.now();
  if (kind === 'performance') {
    viewer.innerHTML = `<iframe src="/report/pnl/${CURRENT_ACCOUNT}?t=${ts}&tab=performance"></iframe>`;
  } else {
    viewer.innerHTML = `<iframe src="/report/${kind}/${CURRENT_ACCOUNT}?t=${ts}"></iframe>`;
  }
}

function refreshActiveReport() {
  const active = document.querySelector('.tab.active');
  if (active && !active.classList.contains('empty')) showReport(active.dataset.report);
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
      const active = document.querySelector('.tab.active');
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

// ---------- Boot ----------
document.addEventListener('DOMContentLoaded', () => {
  const first = document.querySelector('.tab:not(.empty)');
  if (first) showReport(first.dataset.report);
});
