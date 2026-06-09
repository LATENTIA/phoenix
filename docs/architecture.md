# Phoenix — Structural Analysis

This document is a tour of the codebase: how the modules fit together, what each
one is responsible for, and how data flows from an IBKR statement to the rendered
tax report. For optimization opportunities and refactoring suggestions, see
[`optimization-notes.md`](optimization-notes.md).

---

## 1. Big-picture architecture

A **single-binary local web app** for Belgian retail traders on IBKR. ~7,500 LOC.
Stack: Flask + SQLite + pandas + Jinja2. Three external dependencies total
(`flask`, `pandas`, `requests` — Jinja arrives via Flask).

```
                          ┌──────────────┐
                  click   │   Browser    │   iframe → /report/<kind>
                  ───────▶│  dashboard   │◀───────────────────────────┐
                          └──────┬───────┘                            │
                                 │ POST /run/<action>/<code>          │
                                 ▼                                    │
       ┌───────────── Flask app (app.py, 379 LOC) ──────────────┐    │
       │                                                          │   │
       │    /run/download ──► subprocess(ibkr_flex.py) ──► XML   │   │
       │              │                                           │   │
       │              ▼                                           │   │
       │    /run/ingest  ──► ingest.py (in-process)              │   │
       │                       │                                  │   │
       │                       ▼ loaders.py (XML/CSV → DF)        │   │
       │                       ▼                                  │   │
       │                   ┌───────┐                              │   │
       │                   │data.db│ ◀─── ECB cache, year-end ───┘   │
       │                   │  (8   │      marks, accounts            │
       │                   │tables)│                                  │
       │                   └───┬───┘                                  │
       │                       │                                      │
       │                       ▼                                      │
       │   /report/<k>  ──► reports/{tob,pnl,cgt}.py                 │
       │                       │                                      │
       │                       ▼ Jinja + inlined CSS/JS               │
       │                       ▼                                      │
       │                   self-contained HTML ──────────────────────┘
       └──────────────────────────────────────────────────────────────┘
```

**Key principles:**

- **Local-first.** Everything binds to `127.0.0.1`. No cloud, no telemetry.
- **DB is the source of truth.** Tokens, query IDs, trades, FX rates, year-end
  marks all live in `data.db`. The on-disk `downloaded/` folder is a *cache*
  of raw IBKR exports; deleting it loses no logical state.
- **Idempotent ETL.** Source files are tracked by `(path, size, mtime)`; the
  ingest pipeline only re-processes files that have actually changed.
- **Self-contained reports.** Each rendered HTML report inlines its CSS and
  JS, so it works equally well served by Flask or saved to disk.

---

## 2. Module map and responsibilities

| Layer | Module | LOC | Purpose |
|---|---|---|---|
| **Entry** | `app.py` | 379 | Flask routes + subprocess orchestration + structured logging |
| | `ibkr_flex.py` | 305 | IBKR Flex Web Service client (2-step request/poll), invoked as subprocess |
| | `ingest.py` | 185 | Idempotent ETL: scans `downloaded/<account>/`, calls loaders, upserts into DB |
| **core/** (cross-cutting) | `db.py` | 629 | SQLite schema + helpers: 8 tables, CRUD, status, query helpers returning DataFrames |
| | `loaders.py` | 245 | Read XML/CSV statements → normalized DataFrames; CA-description regex parser |
| | `accounts.py` | 122 | Account CRUD wrapper; seeds defaults + migrates env-var tokens to DB |
| | `ecb_fx_parser.py` | 139 | Local-cached daily ECB EUR/USD rates (downloads ZIP if cache missing) |
| | `processing.py` | 123 | Subprocess runner with friendly-error extraction; in-process ingest invoker |
| | `templating.py` | 43 | Jinja2 environment + `render_report()` that inlines CSS/JS |
| | `yahoo_marks.py` | 173 | Yahoo Finance closing-price fetcher (for the 2025-12-31 basis reset) |
| **reports/** | `pnl.py` | 1,515 | The matching engine — FIFO/LIFO walker + symbol-change detector + P&L analytics + HTML renderer |
| | `cgt.py` | 764 | Belgian CGT 2026+ calculator on top of `pnl.match_lots()`, basis-reset logic, exemption rollover bank, HTML renderer |
| | `tob.py` | 457 | TOB (0.35%) computation from Flex XML/CSV with ECB rate enrichment, HTML renderer |
| | `_helpers.py` | 120 | Shared utilities: account aliases, ticker canonicalization, date/number formatting |
| **Front end** | `templates/*.html` | 738 | Jinja2 templates (5 files: dashboard, tob, pnl, cgt, empty_report) |
| | `static/css/*.css` | 502 | Per-report stylesheets, all share the `base.css` palette |
| | `static/js/*.js` | 427 | Tab switching, privacy toggle, account modals, toast/log UI |

---

## 3. Functional features (what the user gets)

### 3.1 Multi-account dashboard
- Add/delete IBKR accounts via UI (account name, code, type, Flex token, query ID).
- Tokens stored in `accounts.flex_token` column — never in env vars, never in logs.
- One-click **⚡ Load data** orchestrates: download → rename to year-stamped file → ingest → regenerate reports.
- Per-account folder under `downloaded/<account>/` for CSV drops.

### 3.2 Idempotent ETL
- Source files tracked in `source_files` (size + mtime); re-ingest only if changed.
- Row-level dedup via UNIQUE indexes using `COALESCE` for NULL-safety.
- Cascade delete: removing a `source_files` row drops its trades / corporate
  actions / transfers / open-positions rows.
- Re-ingestion replaces all rows from that file atomically.

### 3.3 Statement parsing
Five loaders covering both XML and CSV formats:

- `load_flex_xml` — Trades section, EXECUTION-level
- `load_statement_csv` — Trades / Order rows
- `load_corporate_actions_csv` — splits, delistings, cash mergers, stock mergers
- `load_transfers_csv` — IN/OUT share movements
- `load_open_positions_xml` / `load_open_positions_csv` — IBKR's reported
  positions per statement date

### 3.4 The matching engine (the heart of the system)
`pnl.match_lots()` is a chronological event-stream walker:

- Sorts trades + corporate actions + transfers + reconcile-snapshots into one
  timeline (with priority for end-of-day events).
- Maintains `open_lots[symbol] -> list[dict]`, FIFO or LIFO indexing.
- BUY → push lot; SELL → match-and-pop; emit `closed_trade` rows with both legs.
- Handles 6 event types: trade, split, delist, cash_merger, stock_merger, transfer, reconcile.
- **FX-accurate Method 2**: basis at buy-date EUR/USD rate, proceeds at sell-date rate.
- Output: `(closed_df, open_df)`.
- Accepts an optional pre-computed `auto_changes` list, so callers that already invoked
  `_detect_symbol_changes()` (e.g. to surface detections in the UI) avoid duplicate work.

### 3.5 Reconciliation (silent-bankruptcy detection)
- Compare matcher's `open_lots` vs IBKR's open-position snapshot at year-end.
- If IBKR reports 0 for symbol X but we still have lots → write off as a forced
  close on the snapshot date.
- If quantities mismatch but both > 0 → log a warning, leave lots untouched
  (likely missing trade history rather than a write-off).

### 3.6 Symbol-change auto-detection (`_detect_symbol_changes`)
Catches IBKR's silent ticker renames during bankruptcy, FDIC takeovers, CUSIP/ISIN
swaps, etc.:

- Walks consecutive snapshot pairs.
- Computes `expected_qty = prior + buys − sells − CA_effects + transfers + split_qty_change`.
- Identifies "missing" (phantom disappearance) vs "appeared" (phantom appearance) symbols.
- Greedy match with three safety rails: 200-share minimum, 5% tolerance, 95% capacity rule.
- Emits synthetic `stock_merger` events injected into the event stream **before**
  reconcile fires, so the basis rolls forward instead of being written off.
- Output: chains like A → AQ → XYZ are caught automatically with no hardcoded ticker rules.

### 3.7 TOB report (Belgian transaction tax)
- Per-execution: `Total_EUR = |proceeds_USD| / ECB_rate(trade_date)`,
  `TOB = Total_EUR × 0.35%`.
- Year picker, date-range filter, symbol filter, asset-class filter.
- Exports CSV for direct import into the tax form.

### 3.8 P&L Performance analysis
- **Headline KPIs**: realized P&L, ROI on basis, win rate, profit factor,
  expectancy, avg win / avg loss, best / worst trade.
- **Equity curve**: inline SVG with max-drawdown peak/trough markers.
- **FX vs price decomposition**: per-year split into price-driven and
  FX-driven components (`Total = Price + FX`, reconciles exactly).
- **Distribution of trade outcomes**: bucketed by % return with horizontal bars.
- **Annual heatmap**: year × month grid for spotting seasonal patterns.
- **Top winners / losers by symbol**: aggregated across all FIFO-matched lots.

### 3.9 Belgian CGT 2026+ report (the flagship)
Implements the new 10% capital-gains regime that takes effect 1 January 2026:

- 10% flat rate on net realized gains per calendar year.
- €10,000 annual exemption per taxpayer.
- Up to €1,000/year of unused exemption rolls forward, capped at €15,000 in the bank.
- Cost-basis reset to the 2025-12-31 mark for any pre-2026 lot — *or the original
  buy basis if it is higher* (favorable to the taxpayer for 5 years).
- Same-year loss offset before the exemption applies; no carry-forward of losses.
- All gains realized on or before 2025-12-31 stay tax-free under the
  transitional rule.

The report has four tabs:

- **Annual summary** — per-year computation table (gains, losses, net, exemption used, taxable, tax due).
- **Loss-offset detail** — per-year breakdown grouping gains/losses by symbol, with a
  forced-close vs trade split.
- **Per-trade detail** — lot-level audit view with basis-source tags
  ("reset_2025_12_31", "original (higher)", "original (mark missing)").
- **Year-end marks** — table of 2025-12-31 closing prices fetched from Yahoo
  (with status per symbol).
- **Detected renames** — the auto-detected symbol-change list with rationale, for
  the accountant's audit.

### 3.10 UX touches
- **Privacy toggle**: a single 👁 button blurs all numeric values via CSS
  `filter: blur(6px)` for screen-sharing.
- **Friendly error messages**: subprocess errors are mapped to single-line
  user-facing strings (no Python tracebacks leak into the UI).
- **Toast notifications** for async ops, with spinner state during long runs.
- **Self-contained HTML reports**: CSS + JS inlined so each report is a portable
  single file you can email to your accountant.

---

## 4. Data model (8 SQLite tables)

| Table | Role |
|---|---|
| `accounts` | id, name, code, type, flex_token, queries_json — multi-account config |
| `source_files` | path, account_code, kind, size, mtime, ingested_at — ETL state |
| `trades` | normalized trade rows (joined to `source_files` via FK + CASCADE) |
| `corporate_actions` | parsed CA rows (splits, delistings, mergers) |
| `transfers` | IN/OUT share movements between accounts |
| `open_positions_snapshots` | IBKR's reported positions per statement date |
| `fx_rates` | ECB EUR/USD daily |
| `year_end_marks` | 2025-12-31 closing prices for the CGT basis reset (source: 'yahoo' / 'manual' / 'ibkr') |

Foreign keys are enforced (`PRAGMA foreign_keys = ON`). Journal mode is **WAL**
with `synchronous = NORMAL` — readers don't block writers, and the dashboard
can render a report while an ingest is in flight. Row-level UNIQUE indexes
prevent duplicates across re-ingestions. The schema file is in `core/db.py`.

---

## 5. Request lifecycles

### 5.1 "Load data" click

```
POST /run/download/<code>
  └─► app.py:run_action()
       ├─► look up account in DB (token + query_id)
       ├─► subprocess ibkr_flex.py --token … --query-id … --out tmp.xml
       │    └─► ibkr_flex.py: SendRequest → poll GetStatement → save XML
       │       └─► refresh ECB cache (core.ecb_fx_parser.refresh_from_ecb)
       ├─► rename tmp.xml → <account>_<year>.xml (year extracted from XML)
       └─► processing.run_ingest(code)
            └─► ingest.ingest_all(account=code)
                 ├─► scan downloaded/<account>/*.{xml,csv}
                 ├─► for each changed file:
                 │    ├─► loaders.load_flex_xml or load_statement_csv
                 │    ├─► db.upsert_source(...)
                 │    ├─► db.insert_trades / _ca / _transfers / _open_positions
                 │    └─► commit
                 └─► sync ECB rates from local cache → fx_rates table
```

### 5.2 Report render

```
GET /report/cgt/<account>
  └─► app.py:report()
       └─► reports.cgt.build_cgt_html(code)
            ├─► db.get_trades / _corporate_actions / _transfers / _open_positions / _year_end_marks
            ├─► pnl.dedupe()  (collapse duplicate trades from XML+CSV)
            ├─► pnl._group_ca_actions()  (collapse paired old/new CA legs)
            ├─► pnl._detect_symbol_changes()  (heuristic ticker renames)
            ├─► pnl.match_lots(method="FIFO")
            │    ├─► build event stream (trades + CAs + auto-changes + reconciles)
            │    ├─► sort by (datetime, priority)
            │    └─► walk events, FIFO-match sells against open lots
            ├─► cgt.annotate_tax_basis()  (per-trade basis decision: max of original/reset)
            ├─► cgt.compute_annual_tax()  (yearly netting + exemption rollover bank)
            └─► cgt.render_html()  (Jinja + inlined CSS/JS)
                 └─► returns one self-contained HTML string
```

---

## 6. Code organization invariants

- **`core/` is leaf-level.** It must not import from `reports/`.
- **`reports/` may use `core/`.** It must not import from `app.py` or
  `ibkr_flex.py`.
- **`app.py` is the only Flask integration point.** Reports and core are
  Flask-agnostic — they can be invoked from a CLI or a notebook with the same
  function signatures.
- **Loaders return DataFrames with stable column names**, regardless of source
  format (XML vs CSV). Downstream consumers don't have to care which loader was
  used.
- **The DB returns DataFrames with the same schema the loaders produce**
  (`db.get_trades` etc.), so the report layer is source-agnostic.

---

## 7. External integrations

| Service | Direction | Endpoint / file | Purpose |
|---|---|---|---|
| **IBKR Flex Web Service** | outbound | `https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.{Send,Get}Request` | Download Activity Flex Query XML |
| **ECB** | outbound | `https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip` | Daily EUR/USD reference rates (full historical archive). Cached locally; a 12 h freshness gate prevents redundant re-downloads. |
| **Yahoo Finance** | outbound | `https://query1.finance.yahoo.com/v8/finance/chart/<symbol>` | 2025-12-31 closing prices for the CGT basis reset |

All three are unauthenticated GET requests. Tokens for IBKR are stored locally;
ECB and Yahoo require no auth.

---

## 8. Where to look when you want to…

| Task | File(s) |
|---|---|
| Add a new tax report | `reports/<name>.py` + `templates/<name>.html` + `static/{css,js}/<name>.{css,js}` + add a route branch in `app.py:report()` and a tab in `templates/dashboard.html` |
| Change the FIFO/LIFO matching logic | `reports/pnl.py:match_lots()` and the `_apply_*` helpers |
| Tweak the symbol-change detector | `reports/pnl.py:_detect_symbol_changes()` |
| Add a new column to ingested trades | `core/db.py:SCHEMA` + `core/loaders.py:load_flex_xml/load_statement_csv` + `core/db.py:insert_trades / get_trades` |
| Adjust Belgian CGT rules | `reports/cgt.py` constants block + `compute_annual_tax()` |
| Modify the dashboard chrome | `templates/dashboard.html` + `static/css/dashboard.css` + `static/js/dashboard.js` |
| Tune the matcher's logging | All `[warn]` / `[reconcile]` / `[symbol-change]` messages go through `logging.getLogger("phoenix.match")`. Set its level / handler to silence or redirect. |
| Bypass the ECB freshness gate | `core.ecb_fx_parser.refresh_from_ecb(force=True)` — useful right after the ECB publishes a new daily rate. |
| Add a new IBKR Flex section | New loader in `core/loaders.py` + new DB table in `core/db.py` + new ingest call in `ingest.py:ingest_file()` |
