// TOB report interactivity: row checkboxes, date-range filter, year quick-select,
// summary recalc, privacy toggle.
(function () {
  const rows = Array.from(document.querySelectorAll("table.trades tbody tr"));
  const checks = rows.map(r => r.querySelector("input.row-check"));
  const checkAll = document.getElementById("check-all");
  const statCount = document.getElementById("stat-count");
  const statSymbols = document.getElementById("stat-symbols");
  const statTotal = document.getElementById("stat-total");
  const statCommission = document.getElementById("stat-commission");
  const statTob = document.getElementById("stat-tob");
  const statSelected = document.getElementById("stat-selected");
  const statTotalCount = document.getElementById("stat-total-count");
  const dateFrom = document.getElementById("date-from");
  const dateTo = document.getElementById("date-to");

  const fmt = n => n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  function recalc() {
    let count = 0, total = 0, tob = 0, commission = 0;
    const symbols = new Set();
    rows.forEach((r, i) => {
      if (checks[i].checked) {
        count++;
        total      += parseFloat(r.dataset.totalEur)   || 0;
        tob        += parseFloat(r.dataset.tob)        || 0;
        commission += parseFloat(r.dataset.commission) || 0;
        if (r.dataset.symbol) symbols.add(r.dataset.symbol);
        r.classList.remove("excluded");
      } else {
        r.classList.add("excluded");
      }
    });
    statCount.textContent = count.toLocaleString();
    statSymbols.textContent = symbols.size.toLocaleString();
    statTotal.textContent = fmt(total);
    statCommission.textContent = fmt(commission);
    statTob.textContent = fmt(tob);
    statSelected.textContent = count.toLocaleString();
    statTotalCount.textContent = rows.length.toLocaleString();
    checkAll.checked = (count === rows.length);
    checkAll.indeterminate = (count > 0 && count < rows.length);
  }

  checks.forEach(c => c.addEventListener("change", recalc));
  checkAll.addEventListener("change", () => {
    checks.forEach(c => c.checked = checkAll.checked);
    recalc();
  });

  document.getElementById("select-all").addEventListener("click", () => {
    checks.forEach(c => c.checked = true);
    recalc();
  });
  document.getElementById("select-none").addEventListener("click", () => {
    checks.forEach(c => c.checked = false);
    recalc();
  });

  function applyRange(from, to) {
    rows.forEach((r, i) => {
      const d = r.dataset.date;
      const show = (!from || d >= from) && (!to || d <= to);
      checks[i].checked = show;
      r.style.display = show ? "" : "none";
    });
    recalc();
  }

  document.getElementById("apply-range").addEventListener("click", () => {
    applyRange(dateFrom.value, dateTo.value);
    document.querySelectorAll(".year-btn").forEach(b => b.classList.remove("active"));
  });

  function iso(d) { return d.toISOString().slice(0, 10); }

  document.querySelectorAll(".year-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const year = btn.dataset.year;
      document.querySelectorAll(".year-btn").forEach(b => b.classList.toggle("active", b === btn));
      if (year === "all") {
        dateFrom.value = dateFrom.min;
        dateTo.value = dateTo.max;
      } else if (year === "last2m") {
        const now = new Date();
        const endOfPrev = new Date(now.getFullYear(), now.getMonth(), 0);
        const startOfPrevPrev = new Date(endOfPrev.getFullYear(), endOfPrev.getMonth() - 1, 1);
        dateFrom.value = iso(startOfPrevPrev);
        dateTo.value = iso(endOfPrev);
      } else {
        dateFrom.value = `${year}-01-01`;
        const today = dateTo.max;
        dateTo.value = today.startsWith(year) ? today : `${year}-12-31`;
      }
      applyRange(dateFrom.value, dateTo.value);
    });
  });

  applyRange(dateFrom.value, dateTo.value);

  // Privacy toggle
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
