// Sortable tables — shared between the main dashboard and the share view.
//
// Any <table> inside #viewer becomes sortable per-column. Clicking a
// <th> flips the rows in the tbody, adds a ▼ / ▲ arrow to the current
// sort column, and clears the arrow from siblings.
//
// The first click on a column sorts DESCENDING because most numeric /
// date columns are more interesting newest / largest first (matches
// the server-side default in tob.py / pnl.py / cgt.py / dividends.py).
// A second click flips to ascending.
//
// Exposed globals (both dashboards call them):
//   makeAllTablesSortable() — walk every <table> in #viewer and bind
//                             click handlers to its <th>s.

function makeTableSortable(table) {
  const thead = table.querySelector('thead');
  const tbody = table.querySelector('tbody');
  if (!thead || !tbody) return;
  const ths = Array.from(thead.querySelectorAll('th'));
  ths.forEach((th, colIndex) => {
    if (th.classList.contains('check-col')) return;   // checkbox column, no data
    if (th.dataset.sortableBound === '1') return;     // already wired
    th.dataset.sortableBound = '1';
    th.classList.add('sortable');
    th.addEventListener('click', () => sortTableByColumn(table, colIndex, th));
  });
}

function sortTableByColumn(table, colIndex, clickedTh) {
  const tbody = table.querySelector('tbody');
  if (!tbody) return;
  // First click on this column → DESC. Second click on the same
  // column → ASC. Clicking any other column resets to DESC.
  const current = clickedTh.dataset.order || '';
  const next = current === 'desc' ? 'asc' : 'desc';

  // Clear direction + arrow from every sibling <th>, then stamp this one.
  Array.from(clickedTh.parentElement.children).forEach(t => {
    t.removeAttribute('data-order');
    const old = t.querySelector('.sort-arrow');
    if (old) old.remove();
  });
  clickedTh.dataset.order = next;
  const arrow = document.createElement('span');
  arrow.className = 'sort-arrow';
  arrow.textContent = next === 'asc' ? ' ▲' : ' ▼';
  clickedTh.appendChild(arrow);

  // Extract a sort key from each row's Nth cell. Numeric wins over
  // string when the cell parses cleanly — comma / currency / whitespace
  // are stripped so "€23,094.05" and "1,234" sort as numbers. ISO dates
  // "YYYY-MM-DD" sort correctly as strings, so we don't need a special
  // date parser.
  const parseKey = txt => {
    const cleaned = txt.replace(/[,€$\s]/g, '');
    const n = parseFloat(cleaned);
    return isNaN(n) ? txt : n;
  };
  const rows = Array.from(tbody.querySelectorAll('tr'));
  rows.sort((a, b) => {
    const ca = a.cells[colIndex];
    const cb = b.cells[colIndex];
    if (!ca || !cb) return 0;
    const va = parseKey(ca.textContent.trim());
    const vb = parseKey(cb.textContent.trim());
    if (va === vb) return 0;
    const cmp = va < vb ? -1 : 1;
    return next === 'asc' ? cmp : -cmp;
  });
  rows.forEach(r => tbody.appendChild(r));
}

function makeAllTablesSortable() {
  document.querySelectorAll('#viewer table').forEach(makeTableSortable);
}
