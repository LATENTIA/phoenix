# Phoenix

<img src="docs/logo-tile.svg" alt="" align="left" width="100" hspace="24" vspace="4">

**Your IBKR tax & P&L companion — built for Belgian traders.**

A local web dashboard that turns your Interactive Brokers statements into the tax reports your accountant actually needs: the new **Belgian capital gains tax** (10% from 2026), the existing **TOB** transaction tax (0.35%), and a multi-year **P&L analysis** that finally tells you the truth about your trading.

> 🔒 **Runs entirely on your machine.** Your trade history never leaves your computer.

<br clear="all">

---

## What it does, in one minute

| Report | What it answers |
|---|---|
| 💸 **Belgian CGT 2026+** | "How much capital gains tax do I owe under the new regime — and exactly which trades drove it?" |
| 🧾 **TOB** | "What 0.35% transaction tax do I owe this period?" |
| 📊 **P&L Performance** | "Am I actually a good trader? Where did my money come from — picks or just the dollar moving?" |
| 🪙 **Per-trade detail** | "Show me lot-by-lot what my accountant will see." |

All four are generated from a single click on **⚡ Load data**.

![Dashboard overview](docs/mockups/dashboard.svg)

> 🖱 Want a richer interactive preview? Open the live HTML mockups:
> [📋 Dashboard](docs/mockups/dashboard.html) ·
> [💸 CGT 2026+](docs/mockups/cgt-report.html) ·
> [📊 P&L Performance](docs/mockups/pnl-performance.html) ·
> [🧾 TOB](docs/mockups/tob-report.html)

---

## ✨ Key features

### 🇧🇪 Belgian Capital Gains Tax 2026+

Belgium's brand-new **10% tax on financial assets** (effective 1 January 2026) is the headline feature.
Phoenix implements every rule from the KPMG July 2025 reference text:

- **10% flat rate** on net realized gains per calendar year
- **€10,000 annual exemption**, with up to **€1,000/year** carrying forward (capped at €15,000 in the bank)
- **Cost basis reset to 31 Dec 2025** for any pre-2026 lot — *or the original buy basis if it was higher* (favorable for 5 years)
- **Same-year loss offset** — losses cancel gains 1:1 before the exemption applies
- **Transitional exemption** — everything closed by 2025-12-31 stays tax-free

![CGT 2026+ report](docs/mockups/cgt-report.svg)

> 🖱 [Open the interactive HTML version →](docs/mockups/cgt-report.html)

#### 🧬 Smart symbol-change detection

When a stock enters Chapter 11 it often gets renamed (e.g. a "Q" suffix), or merged into a successor entity.
IBKR doesn't always record this as a corporate action, which means a naive calculator would write the position
off as a bankruptcy in the wrong year — and the loss would land in an exempt period and be wasted.

Phoenix walks your IBKR snapshots and **automatically detects ticker renames** by reconciling
quantities across consecutive year-ends — accounting for trades, splits, transfers, and explicit corporate
actions in between. Detected renames roll the basis forward as a non-taxable event, so the loss lands on
the right year.

> *Fully automatic and works for any user, any tickers — no manual rules to maintain.*

Every detection is logged with a rationale, so you and your accountant can audit each one.

---

### 📊 P&L Performance analysis

A full trader scorecard with FX-accurate EUR conversion (the basis is locked at the buy-date EUR/USD rate;
proceeds at the sell-date rate — so realized P&L correctly captures FX movement during the holding period).

What you get:

- **Equity curve** — cumulative realized P&L over time, with max-drawdown peak/trough markers
- **FX vs price decomposition** — how much of each year's P&L came from picking the right stocks vs the dollar drifting
- **Distribution of trade outcomes** — bucketed by % return, with a horizontal bar visualization
- **Headline KPIs** — win rate, profit factor, avg win/loss, expectancy, ROI on basis, best/worst trade
- **Annual heatmap** — month-by-month P&L grid for spotting seasonal patterns
- **Top winners / losers by symbol** — aggregated across all FIFO-matched lots

![P&L performance](docs/mockups/pnl-performance.svg)

> 🖱 [Open the interactive HTML version →](docs/mockups/pnl-performance.html)

---

### 🧾 TOB (Belgian transaction tax)

The classic 0.35% Taxe sur les Opérations de Bourse, computed automatically:

- Reads your Flex statement, applies the **ECB EUR/USD daily reference rate** to every leg
- Computes `Total_EUR = |proceeds| / rate` and `TOB = Total_EUR × 0.35%`
- Filterable by year / date range / symbol / asset class
- Exports to CSV for direct import into your tax form

![TOB report](docs/mockups/tob-report.svg)

> 🖱 [Open the interactive HTML version →](docs/mockups/tob-report.html)

---

### 🔒 Privacy by design

- **Local-only.** Everything runs on `127.0.0.1`. No cloud, no telemetry, no analytics.
- **Your data, your machine.** Trade history lives in a SQLite file beside the app.
- **Privacy toggle** — a single 👁 button blurs all numbers in the UI when you're sharing your screen.
- **Open source.** Inspect every line. The math is auditable.

---

### 🤝 Multi-account, account-agnostic

Manage multiple IBKR accounts from one dashboard:
- Add as many accounts as you have (personal, business, family members, etc.)
- Each gets its own folder, its own data, its own reports
- Tokens stored in the local DB (never in environment variables, never in logs)

---

## 🚀 Quick start

### 1. Install

```bash
git clone <this-repo>
cd Parser
python -m venv .venv
.\.venv\Scripts\Activate.ps1     # Windows PowerShell
# or:  source .venv/bin/activate  # Linux/macOS
pip install -r requirements.txt
```

Two external dependencies: `pandas` and `requests`. That's it.

### 2. Configure your IBKR Flex query (one-time)

In your IBKR Client Portal:
1. **Performance & Reports → Flex Queries** → create an Activity Flex Query (XML format, Year-to-Date, include the *Trades* section)
2. **Flex Web Service Configuration** → Enable → copy the token (shown once)
3. Note the Query ID

Run the app once, click **＋ Add account**, paste the token + query ID. Done.

### 3. Run

```bash
python app.py
```

Open http://127.0.0.1:5000. Click **⚡ Load data** to download your statement and ingest it.
Reports regenerate automatically.

---

## 🛣️ Roadmap

The CGT 2026+ implementation is feature-complete for a typical retail trader. Things that may come next, in
order of likelihood:

- Manual override mechanism for auto-detected renames (in case a heuristic misfires)
- Year-end mark editor in the UI (for delisted tickers Yahoo doesn't carry)
- Export to a Belgian-tax-form-friendly CSV
- The two CGT special regimes (33% on internal capital gains; substantial-shareholding scheme)
  — currently out of scope; they don't apply to typical retail IBKR activity

---

## ⚠️ Disclaimer

This tool implements Belgian tax law as described in the KPMG July 2025 reference text. The legislation was
not yet final when this was written. Use the output as a starting point for your tax filing — **not as legal
advice**. Always have your accountant verify the numbers before filing. The author is a trader, not a tax
attorney.

---

## 🤝 Contributing

Issues and pull requests welcome. The codebase is intentionally compact:

- `core/` — DB, loaders, FX rates, account management
- `reports/` — TOB, P&L, CGT report builders
- `templates/` + `static/` — the UI
- `app.py` — Flask routes
- `ibkr_flex.py` — IBKR Flex Web Service client
- `ingest.py` — ETL entry point

For a tour of the math (FIFO matching, FX-accurate Method 2, basis reset, exemption rollover bank,
symbol-change detection), see the docstrings in `reports/cgt.py` and `reports/pnl.py`.

---

🦅 *Built for FIRE-minded Belgian traders who want to know exactly what they owe — and exactly why.*
