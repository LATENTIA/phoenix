"""
Corporate (CIT) tax report for Belgian business-type accounts.

Audience: a Belgian company (BV/SRL/NV/SA, subject to *Impôt des Sociétés* /
*Vennootschapsbelasting*) that holds an IBKR portfolio of stocks, ETFs,
bonds, and/or crypto. The tab is meant for the company's accountant: it
aggregates every realised income stream the broker statement carries and
produces the indicative CIT contribution at the 25% flat corporate rate.

How each line of an IBKR statement feeds the corporate tax base:

    + realised gains  on stocks  (FIFO, EUR-converted at trade date)
    + realised gains  on crypto  (treated as ordinary income, no special regime)
    - realised losses on stocks  (fully deductible, unlike for individuals)
    - realised losses on crypto  (fully deductible)
    + dividends received (GROSS, before foreign WHT)
    + bond / cash interest credited to the broker account (when present)
    -----------------------------------------------------------------
    = CIT base contribution from investment activity

Then:

    CIT due on that base   = base * 25%
    Foreign WHT credit     = sum of foreign WHT paid on those dividends
                              (creditable as FTC, capped per dividend at
                               25% of the gross of that dividend)
    Net CIT payable        = max(0, CIT due - FTC)

What this report DOESN'T do:

  * It is NOT the company's tax return. Operating profits, salaries,
    depreciations, deductions, prior-year losses carried forward, the
    notional-interest deduction, etc. all live outside Phoenix.
  * It does NOT apply the participation exemption (100% exempt dividends
    and capital gains on shares). A typical IBKR portfolio doesn't
    qualify: the conditions are 10% stake in the issuer OR EUR 2.5M
    acquisition value, plus 1-year holding, plus the issuer must be
    properly taxed (anti-tax-haven). If you do hold a strategic stake
    that may qualify, flag it manually with your accountant; this report
    will OVER-state your CIT bill for that line.
  * It does NOT track interest income unless the source ingestion has
    written rows for it. Many setups don't (IBKR's Flex query needs the
    correct sections enabled).

Source for the framework:
  PwC Worldwide Tax Summaries, Belgium / Corporate / Income determination
  (https://taxsummaries.pwc.com/belgium/corporate/income-determination)
"""

from __future__ import annotations

import pandas as pd

from core import db as _db
from core.templating import render, render_report
from reports import dividends as _div
from reports import pnl as _pnl
from reports._helpers import ACCOUNT_ALIASES


# Standard Belgian corporate income tax rate (2025/2026). The reduced 20%
# SME rate (first EUR 100k of taxable profit when several conditions are
# met) is NOT applied here because Phoenix doesn't see the rest of the
# company's P&L. Showing 25% is the conservative, defensible default.
CIT_RATE = 0.25


# ---------------------------------------------------------------------------
# Yearly aggregations.
# ---------------------------------------------------------------------------

def _compute_realized_by_year(closed: pd.DataFrame) -> pd.DataFrame:
    """Per-year split of FIFO-closed lots. Both gains AND losses matter for
    a corporate: losses are fully deductible against any other corporate
    income, so we keep them as a negative number and let the sum flow."""
    if closed is None or closed.empty or "close_year" not in closed.columns:
        return pd.DataFrame()
    df = closed.dropna(subset=["close_year"]).copy()
    if df.empty:
        return pd.DataFrame()
    rows = []
    for year, g in df.groupby("close_year"):
        pnl = g["realized_pnl_eur"]
        rows.append({
            "year": int(year),
            "n_closed": int(len(g)),
            "gains_eur": float(pnl[pnl > 0].sum() or 0.0),
            "losses_eur": float(pnl[pnl < 0].sum() or 0.0),    # negative
            "realized_net_eur": float(pnl.sum() or 0.0),
        })
    return pd.DataFrame(rows).sort_values("year", ascending=False).reset_index(drop=True)


def _compute_dividends_by_year(paired: pd.DataFrame) -> pd.DataFrame:
    """Per-year split of dividend payments (paired with their WHT in EUR).

    Returns three columns: gross_eur (always positive), wht_eur (always
    negative or zero, IBKR convention), and n_div (count of payments).
    The corporate regime does NOT apply the personal EUR 833 exemption,
    so we deliberately do NOT carry any of the personal-side fields here.
    """
    if paired is None or paired.empty:
        return pd.DataFrame()
    df = paired.copy()
    df["year"] = df["pay_date"].str[:4]
    df = df[df["year"].str.isdigit()]
    if df.empty:
        return pd.DataFrame()
    rows = []
    for year, sub in df.groupby("year"):
        rows.append({
            "year": int(year),
            "n_div": int(len(sub)),
            "gross_eur": float(sub["amount_eur"].sum() or 0.0),
            "wht_eur": float(sub["wht_amount_eur"].sum() or 0.0),
        })
    return pd.DataFrame(rows).sort_values("year", ascending=False).reset_index(drop=True)


def _merge_annual(realized: pd.DataFrame, divs: pd.DataFrame) -> pd.DataFrame:
    """Outer-join the two annual frames on `year`, then compute the CIT base
    and the indicative tax / FTC / net payable for each year."""
    if realized.empty and divs.empty:
        return pd.DataFrame()
    if realized.empty:
        merged = divs.copy()
        merged["n_closed"]         = 0
        merged["gains_eur"]        = 0.0
        merged["losses_eur"]       = 0.0
        merged["realized_net_eur"] = 0.0
    elif divs.empty:
        merged = realized.copy()
        merged["n_div"]            = 0
        merged["gross_eur"]        = 0.0
        merged["wht_eur"]          = 0.0
    else:
        merged = realized.merge(divs, on="year", how="outer").fillna(0)

    merged["year"] = merged["year"].astype(int)
    merged = merged.sort_values("year", ascending=False).reset_index(drop=True)

    # CIT base contribution from investment activity. WHT is creditable
    # (not deductible) so dividends enter at the GROSS amount.
    merged["cit_base_eur"] = merged["realized_net_eur"] + merged["gross_eur"]
    merged["cit_due_eur"]  = merged["cit_base_eur"].clip(lower=0) * CIT_RATE

    # If the year had a NET LOSS (cit_base_eur < 0), no CIT is due on
    # investment activity and the loss flows into the company's overall
    # taxable result (often offsetting operating profit). We surface the
    # loss separately rather than try to net it against future years
    # (loss-carry-forward is the accountant's job, not ours).
    merged["cit_due_eur"] = merged["cit_due_eur"].astype(float)

    # FTC: capped per year at the Belgian CIT that would otherwise be
    # due on the dividend gross (you can't recover more in FTC than you
    # were going to owe in Belgium on that same income).
    def _ftc(row):
        return min(abs(row["wht_eur"]),
                   max(0.0, row["gross_eur"] * CIT_RATE))
    merged["ftc_eur"] = merged.apply(_ftc, axis=1)

    merged["cit_payable_eur"] = (merged["cit_due_eur"] - merged["ftc_eur"]).clip(lower=0)
    return merged


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------

def _empty_report_html(account_code: str) -> str:
    name = ACCOUNT_ALIASES.get(account_code, account_code)
    return render("empty_report.html", kind="Corporate Tax (CIT)", account=name)


def build_corporate_tax_html(account_code: str, method: str = "FIFO",
                             as_partial: bool = False) -> str:
    """Compute and render the corporate-tax view.

    Reuses pnl.match_lots() for realised gains/losses and the
    dividends-pipeline pairing for the dividend / WHT side. Both engines
    are already battle-tested by their own tabs, so this report is a thin
    aggregation layer on top.

    `as_partial=True` returns the body fragment for the dashboard shell;
    default returns a standalone document.
    """
    method = method.upper()
    conn = _db.connect()
    _db.init_schema(conn)

    trades = _db.get_trades(conn, account_code)
    div_raw = _db.get_dividends(conn, account_code)

    # Nothing at all? Show the standard empty placeholder.
    if trades.empty and div_raw.empty:
        conn.close()
        return _empty_report_html(account_code)

    # --- Realised gains / losses (reuses pnl.match_lots) ---
    closed = pd.DataFrame()
    if not trades.empty:
        trades = _pnl.dedupe(trades)
        ca_actions = _pnl._group_ca_actions(_db.get_corporate_actions(conn, account_code))
        xf_df = _db.get_transfers(conn, account_code)
        known_accounts = _db.get_known_accounts(conn)
        transfers = []
        for xf in xf_df.to_dict("records"):
            if xf.get("direction") == "IN" and xf.get("xfer_account") in known_accounts:
                continue
            transfers.append(xf)
        snaps = _db.get_open_positions_snapshots(conn, account_code)
        snaps.sort(key=lambda t: t[0])
        closed, _open_df = _pnl.match_lots(
            trades, ca_actions=ca_actions, transfers=transfers,
            reconcile_snapshots=snaps, method=method,
        )

    # --- Dividends paired with WHT (reuses dividends pipeline) ---
    wht_raw = _db.get_withholding(conn, account_code)
    conn.close()

    paired = pd.DataFrame()
    if not div_raw.empty:
        fx_cache: dict = {}
        div = div_raw.copy()
        div["amount_eur"] = [
            _div._eur_for(a, d, fx_cache) for a, d in zip(div["amount"], div["pay_date"])
        ]
        if not wht_raw.empty:
            wht = wht_raw.copy()
            wht["amount_eur"] = [
                _div._eur_for(a, d, fx_cache) for a, d in zip(wht["amount"], wht["pay_date"])
            ]
        else:
            wht = pd.DataFrame(columns=[
                "pay_date", "symbol", "per_share",
                "amount", "amount_eur", "source_country",
            ])
        paired = _div._pair_dividends_with_wht(div, wht)
        paired["wht_amount_eur"] = [
            _div._eur_for(a, d, fx_cache) if a else 0.0
            for a, d in zip(paired["wht_amount"], paired["pay_date"])
        ]

    # --- Roll up by year, then compute CIT + FTC ---
    realized_by_year = _compute_realized_by_year(closed)
    divs_by_year     = _compute_dividends_by_year(paired)
    annual           = _merge_annual(realized_by_year, divs_by_year)

    # --- Lifetime totals (footer / KPI cards) ---
    totals = {
        "gains_eur":        float(annual["gains_eur"].sum())        if not annual.empty else 0.0,
        "losses_eur":       float(annual["losses_eur"].sum())       if not annual.empty else 0.0,
        "realized_net_eur": float(annual["realized_net_eur"].sum()) if not annual.empty else 0.0,
        "gross_eur":        float(annual["gross_eur"].sum())        if not annual.empty else 0.0,
        "wht_eur":          float(annual["wht_eur"].sum())          if not annual.empty else 0.0,
        "cit_base_eur":     float(annual["cit_base_eur"].sum())     if not annual.empty else 0.0,
        "cit_due_eur":      float(annual["cit_due_eur"].sum())      if not annual.empty else 0.0,
        "ftc_eur":          float(annual["ftc_eur"].sum())          if not annual.empty else 0.0,
        "cit_payable_eur":  float(annual["cit_payable_eur"].sum())  if not annual.empty else 0.0,
    }

    # No per-trade / per-dividend tables here on purpose: the P&L tab is
    # the canonical place to inspect closed lots and the Dividends tab is
    # the canonical place to inspect individual dividend payments. The CIT
    # tab's job is the unique part: the per-year CIT roll-up.

    return render_report(
        "corporate_tax.html",
        css_files=["css/dividends.css"],
        js_files=["js/dividends.js"],
        as_partial=as_partial,
        account=ACCOUNT_ALIASES.get(account_code, account_code),
        cit_rate_pct=int(CIT_RATE * 100),
        annual=annual.to_dict("records") if not annual.empty else [],
        totals=totals,
    )
