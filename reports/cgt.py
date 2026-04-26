"""
Belgian Capital Gains Tax (CGT) on financial assets — 2026+ regime.

Implements the new tax that takes effect on 1 January 2026:
- Flat **10%** rate on net realized gains from financial assets.
- **€10,000 annual exemption** per taxpayer.
- Up to **€1,000/year** of unused exemption may be carried forward,
  capped at a cumulative bank balance of **€15,000**.
- All gains realized **on or before 31 December 2025 are exempt** under
  the transitional rule.
- For lots opened before 1 January 2026 and sold afterward, the cost
  basis is **reset to the 31 Dec 2025 market price** — but the original
  historical basis may be used instead if it is *higher* (favorable to
  the taxpayer), available for 5 years post-enactment.
- Losses offset gains within the **same year only**; no carry-forward.

Source: KPMG Belgium "The new capital gains tax on financial assets",
July 2025 (parliamentary text not yet final at that time).

This calculator deliberately does *not* implement:
- The 33% rate on internal capital gains (sale of shares to a controlled
  company) — separate special regime.
- The progressive 0% / 1.25% / 2.5% / 5% / 10% scheme for substantial
  shareholdings (≥20%) — also separate.
- The 16.5% rate on transfers to non-EEA entities.

These additional regimes require explicit per-trade flags that retail
IBKR activity will never trigger; if the user needs them, they should
consult their accountant.
"""

import argparse
import html
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from core import db as _db
from core.loaders import (
    load_corporate_actions_csv,  # noqa: F401  (re-exported for parity with pnl)
)
from core.templating import render, render_report
from reports import pnl as _pnl
from reports._helpers import (
    ACCOUNT_ALIASES, ACCOUNT_LETTER,
    fmt_num, fmt_qty,
    resolve_account as _resolve_account,
)


# ---------- Constants from the law ----------

REGIME_START = "2026-01-01"   # Gains from sales on or after this date are taxable.
RESET_DATE = "2025-12-31"     # Pre-2026 lots get a basis reset to this date's mark.
RESET_DATE_YEAR = 2025
TAX_RATE = 0.10               # 10% flat rate on net taxable gain.
ANNUAL_EXEMPTION_EUR = 10_000.0
MAX_YEARLY_CARRY_EUR = 1_000.0
MAX_CARRY_BANK_EUR = 15_000.0


# ---------- Per-trade tax calculation ----------

def annotate_tax_basis(
    closed: pd.DataFrame,
    year_end_marks: dict[str, dict],
    fx_2025_12_31: Optional[float],
) -> pd.DataFrame:
    """For each closed trade, decide which basis applies for Belgian CGT and
    compute the tax-relevant realized P&L in EUR.

    Adds these columns to a copy of `closed`:
      - `is_taxable_year`     : True if sell_date is on/after 2026-01-01
      - `is_pre_reset_lot`    : True if buy_date is before 2026-01-01
      - `tax_basis_source`    : "original" | "reset_2025_12_31" | "n/a (exempt)"
      - `tax_basis_eur`       : the basis used for tax (max of original and reset)
      - `tax_basis_per_share_usd`: per-share USD value used (for the reset path)
      - `ye_mark_usd`         : the 2025-12-31 close used (None if missing)
      - `tax_realized_eur`    : proceeds_eur - tax_basis_eur (only when taxable)
      - `mark_status`         : "ok" | "missing" | "n/a" — was a mark needed and
                                 did we have one?
    """
    if closed.empty:
        return closed.copy()

    df = closed.copy()
    df["is_taxable_year"] = df["sell_date"].fillna("") >= REGIME_START
    df["is_pre_reset_lot"] = df["buy_date"].fillna("") < REGIME_START

    tax_basis_source = []
    tax_basis_eur = []
    tax_basis_per_share = []
    ye_marks_used = []
    tax_realized = []
    mark_status = []

    for row in df.to_dict("records"):
        if not row["is_taxable_year"]:
            tax_basis_source.append("n/a (exempt)")
            tax_basis_eur.append(None)
            tax_basis_per_share.append(None)
            ye_marks_used.append(None)
            tax_realized.append(None)
            mark_status.append("n/a")
            continue

        original_basis_eur = row.get("basis_eur")
        proceeds_eur = row.get("proceeds_eur")

        if not row["is_pre_reset_lot"]:
            # Lot opened in 2026+ — original basis applies, no reset.
            tax_basis_source.append("original")
            tax_basis_eur.append(original_basis_eur)
            tax_basis_per_share.append(row.get("buy_price_usd"))
            ye_marks_used.append(None)
            mark_status.append("n/a")
            tax_realized.append(
                (proceeds_eur - original_basis_eur)
                if (proceeds_eur is not None and original_basis_eur is not None)
                else None
            )
            continue

        # Pre-2026 lot — compute reset basis and pick the max.
        symbol = row.get("symbol", "")
        qty = float(row.get("quantity") or 0)
        mark = (year_end_marks.get(symbol) or {}).get("close_price")

        if mark is None or fx_2025_12_31 is None or fx_2025_12_31 <= 0 or qty <= 0:
            # No reset basis available — fall back to original. Flag for UI.
            tax_basis_source.append("original (mark missing)")
            tax_basis_eur.append(original_basis_eur)
            tax_basis_per_share.append(row.get("buy_price_usd"))
            ye_marks_used.append(None)
            mark_status.append("missing" if mark is None else "n/a")
            tax_realized.append(
                (proceeds_eur - original_basis_eur)
                if (proceeds_eur is not None and original_basis_eur is not None)
                else None
            )
            continue

        reset_basis_usd = qty * float(mark)
        reset_basis_eur = reset_basis_usd / fx_2025_12_31
        ye_marks_used.append(float(mark))

        if original_basis_eur is None:
            # Edge case: original buy_fx was missing. Use reset.
            chosen_eur = reset_basis_eur
            chosen_src = "reset_2025_12_31"
            chosen_per_share = float(mark)
        elif reset_basis_eur >= float(original_basis_eur):
            chosen_eur = reset_basis_eur
            chosen_src = "reset_2025_12_31"
            chosen_per_share = float(mark)
        else:
            chosen_eur = float(original_basis_eur)
            chosen_src = "original (higher)"
            chosen_per_share = row.get("buy_price_usd")

        tax_basis_source.append(chosen_src)
        tax_basis_eur.append(chosen_eur)
        tax_basis_per_share.append(chosen_per_share)
        mark_status.append("ok")
        tax_realized.append(
            (proceeds_eur - chosen_eur)
            if (proceeds_eur is not None) else None
        )

    df["tax_basis_source"] = tax_basis_source
    df["tax_basis_eur"] = tax_basis_eur
    df["tax_basis_per_share_usd"] = tax_basis_per_share
    df["ye_mark_usd"] = ye_marks_used
    df["tax_realized_eur"] = tax_realized
    df["mark_status"] = mark_status
    return df


# ---------- Annual netting + exemption rollover ----------

def compute_annual_tax(
    tax_trades: pd.DataFrame,
    *,
    annual_exemption: float = ANNUAL_EXEMPTION_EUR,
    max_yearly_carry: float = MAX_YEARLY_CARRY_EUR,
    max_carry_bank: float = MAX_CARRY_BANK_EUR,
    rate: float = TAX_RATE,
) -> list[dict]:
    """Walk each taxable year (2026+), nett gains and losses, apply the
    exemption (with rollover bank), and compute tax due.

    Returns a list of per-year dicts (one per year with at least one taxable
    trade), in chronological order. Each dict contains:
      year, gains, losses, net,
      carry_in, base_exemption, base_used, bank_used, total_exemption_used,
      carry_added, carry_out,
      taxable, tax
    """
    if tax_trades.empty:
        return []
    taxable = tax_trades[tax_trades["is_taxable_year"] == True].copy()
    if taxable.empty:
        return []
    taxable["close_year_int"] = pd.to_numeric(
        taxable["close_year"], errors="coerce"
    ).astype("Int64")
    taxable = taxable.dropna(subset=["close_year_int", "tax_realized_eur"])

    rows: list[dict] = []
    carry = 0.0
    for year, g in taxable.groupby("close_year_int"):
        gains = float(g.loc[g["tax_realized_eur"] > 0, "tax_realized_eur"].sum())
        losses = float(-g.loc[g["tax_realized_eur"] < 0, "tax_realized_eur"].sum())
        net = gains - losses

        # Exemption mechanics: spend this year's base €10k first, then dip
        # into the bank if more is needed; save up to €1k of unused base for
        # next year, capped at €15k total bank.
        if net <= 0:
            base_used = 0.0
            bank_used = 0.0
            taxable_eur = 0.0
            unused_base = annual_exemption
        else:
            base_used = min(net, annual_exemption)
            remaining = net - base_used
            bank_used = min(remaining, carry)
            remaining -= bank_used
            taxable_eur = max(0.0, remaining)
            unused_base = annual_exemption - base_used

        carry_added = min(unused_base, max_yearly_carry)
        carry_out = min((carry - bank_used) + carry_added, max_carry_bank)

        rows.append({
            "year": int(year),
            "gains": gains,
            "losses": losses,
            "net": net,
            "carry_in": carry,
            "base_exemption": annual_exemption,
            "base_used": base_used,
            "bank_used": bank_used,
            "total_exemption_used": base_used + bank_used,
            "carry_added": carry_added,
            "carry_out": carry_out,
            "taxable": taxable_eur,
            "tax": taxable_eur * rate,
        })
        carry = carry_out

    return rows


# ---------- Symbols-needing-marks discovery ----------

def symbols_needing_marks(
    closed: pd.DataFrame,
    open_df: pd.DataFrame,
) -> list[str]:
    """Return the deduped set of symbols for which we'd need a 2025-12-31
    mark to compute CGT correctly. That's:
      - symbols that have at least one closed trade matched against a
        pre-2026 lot AND sold from 2026 onward
      - plus all symbols still open at year-end (so future sales work)
    """
    needed: set[str] = set()
    if not closed.empty:
        mask = (
            (closed["sell_date"].fillna("") >= REGIME_START)
            & (closed["buy_date"].fillna("") < REGIME_START)
        )
        for s in closed.loc[mask, "symbol"].dropna().unique():
            needed.add(str(s))
    if open_df is not None and not open_df.empty:
        for s in open_df["symbol"].dropna().unique():
            sd = open_df.loc[open_df["symbol"] == s, "buy_date"].dropna()
            if (sd < REGIME_START).any():
                needed.add(str(s))
    # Filter out symbols that obviously can't be priced from a market feed:
    # forex pairs (e.g. EUR.USD) and option contracts (have spaces, dates).
    out = []
    for s in sorted(needed):
        if "." in s and len(s) <= 7 and s.isupper():  # forex like EUR.USD
            continue
        if " " in s:  # option chain ("AAPL 16FEB24 11 C")
            continue
        out.append(s)
    return out


# ---------- Public API ----------

def _empty_report_html(account_code: str) -> str:
    name = ACCOUNT_ALIASES.get(account_code, account_code)
    return render("empty_report.html", kind="Belgian CGT", account=name)


def build_cgt_html(account_code: str, method: str = "FIFO") -> str:
    """Compute the Belgian CGT pipeline and render the report.

    Returns the HTML string, or an "empty" placeholder if there's no data yet.
    """
    method = method.upper()
    conn = _db.connect()
    _db.init_schema(conn)
    df = _db.get_trades(conn, account_code)
    if df.empty:
        conn.close()
        return _empty_report_html(account_code)

    df = _pnl.dedupe(df)
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

    ye_marks = _db.get_year_end_marks(conn, RESET_DATE)
    fx_row = conn.execute(
        "SELECT eur_usd FROM fx_rates WHERE date = ?", (RESET_DATE,)
    ).fetchone()
    fx_2025_12_31 = float(fx_row["eur_usd"]) if fx_row else None
    conn.close()

    # Surface auto-detected ticker changes (Chapter-11 renames, CUSIP/ISIN
    # swaps IBKR forgot to flag as a CA, etc.) — compute once and pass into
    # `match_lots` so it doesn't re-detect on its own. We need them out here
    # too so the report's "Detected renames" tab can list them with rationale.
    auto_changes = _pnl._detect_symbol_changes(
        snaps, df, ca_actions, transfers=transfers,
    )

    closed, open_df = _pnl.match_lots(
        df, ca_actions=ca_actions, transfers=transfers,
        reconcile_snapshots=snaps, method=method,
        auto_changes=auto_changes,
    )

    tax_trades = annotate_tax_basis(closed, ye_marks, fx_2025_12_31)
    annual = compute_annual_tax(tax_trades)
    needed = symbols_needing_marks(closed, open_df)
    missing = [s for s in needed if s not in ye_marks]

    return render_html(
        annual=annual,
        tax_trades=tax_trades,
        ye_marks=ye_marks,
        fx_2025_12_31=fx_2025_12_31,
        symbols_needed=needed,
        symbols_missing=missing,
        auto_changes=auto_changes,
        account=ACCOUNT_ALIASES.get(account_code, account_code),
        method=method,
    )


# ---------- HTML rendering ----------

def _render_annual_rows(annual: list[dict]) -> str:
    if not annual:
        return ('<tr><td colspan="9" class="muted">No taxable years yet — '
                'the new regime starts 1 January 2026.</td></tr>')
    rows = []
    for r in annual:
        cls_net = "pos" if r["net"] >= 0 else "neg"
        cls_tax = "neg" if r["tax"] > 0 else "muted"
        rows.append(
            f"<tr><td>{int(r['year'])}</td>"
            f"<td class='num pos'>{fmt_num(r['gains'])}</td>"
            f"<td class='num neg'>{fmt_num(-r['losses']) if r['losses'] else fmt_num(0)}</td>"
            f"<td class='num {cls_net}'><strong>{fmt_num(r['net'])}</strong></td>"
            f"<td class='num muted'>{fmt_num(r['carry_in'])}</td>"
            f"<td class='num'>{fmt_num(r['total_exemption_used'])}</td>"
            f"<td class='num muted'>{fmt_num(r['carry_out'])}</td>"
            f"<td class='num'>{fmt_num(r['taxable'])}</td>"
            f"<td class='num {cls_tax}'><strong>{fmt_num(r['tax'])}</strong></td>"
            "</tr>"
        )
    return "\n".join(rows)


_FORCED_CLOSE_TYPES = {"reconcile", "delist", "cash_merger", "stock_merger"}

_CLOSE_TYPE_LABELS = {
    "trade": ("trade", "tag-orig"),
    "reconcile": ("forced close (write-off)", "tag-missing"),
    "delist": ("delisted", "tag-missing"),
    "cash_merger": ("cash merger", "tag-yahoo"),
    "stock_merger": ("stock merger", "tag-orig"),
}


def _close_type_html(close_type: str) -> str:
    label, cls = _CLOSE_TYPE_LABELS.get(close_type, (close_type or "—", "tag-orig"))
    return f'<span class="basis-tag {cls}">{html.escape(label)}</span>'


def _aggregate_by_symbol(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse closed-lot rows into one row per symbol with totals + a label
    that reflects the dominant close type (forced vs trade vs mixed).

    Output columns: symbol, total_eur, n_lots, dominant_type, has_forced
    Sorted by total_eur ascending (most-negative first for losses) /
    descending caller (most-positive first for gains) — handled outside.
    """
    if df.empty:
        return pd.DataFrame(columns=["symbol", "total_eur", "n_lots",
                                     "dominant_type", "has_forced"])
    rows = []
    for sym, g in df.groupby("symbol"):
        total = float(g["tax_realized_eur"].sum())
        n = int(len(g))
        types = g["close_type"].fillna("trade").astype(str)
        forced_count = int(types.isin(_FORCED_CLOSE_TYPES).sum())
        has_forced = forced_count > 0
        if forced_count == n:
            dominant = types.iloc[0]
        elif forced_count == 0:
            dominant = "trade"
        else:
            dominant = "mixed"
        rows.append({
            "symbol": sym,
            "total_eur": total,
            "n_lots": n,
            "dominant_type": dominant,
            "has_forced": has_forced,
        })
    out = pd.DataFrame(rows)
    # Default ordering: most-negative first (so callers get losses sorted by
    # depth ascending; for gains they sort descending themselves).
    return out.sort_values("total_eur").reset_index(drop=True)


def _format_symbol_aggregate_rows(df: pd.DataFrame, *, sign: str) -> str:
    """Render symbol-aggregated rows for the per-year offset block.
    `sign` is "pos" (gains, sorted high→low) or "neg" (losses, sorted low→high).
    """
    if df.empty:
        kind = "gains" if sign == "pos" else "losses"
        return f"<tr><td colspan='4' class='muted'>No {kind}.</td></tr>"
    if sign == "pos":
        df = df.sort_values("total_eur", ascending=False)
    rows = []
    for t in df.to_dict("records"):
        type_html = _close_type_html(str(t["dominant_type"]))
        if str(t["dominant_type"]) == "mixed":
            type_html = '<span class="basis-tag tag-yahoo">mixed</span>'
        n_lots = int(t["n_lots"])
        lots_label = f"{n_lots} lot" if n_lots == 1 else f"{n_lots} lots"
        rows.append(
            f"<tr><td><strong>{html.escape(str(t['symbol']))}</strong></td>"
            f"<td class='muted'>{lots_label}</td>"
            f"<td>{type_html}</td>"
            f"<td class='num {sign}'><strong>{fmt_num(t['total_eur'])}</strong></td></tr>"
        )
    return "\n".join(rows)


def _render_per_trade_rows(tax_trades: pd.DataFrame) -> str:
    if tax_trades.empty:
        return '<tr><td colspan="11" class="muted">No closed trades.</td></tr>'
    taxable = tax_trades[tax_trades["is_taxable_year"] == True].copy()
    if taxable.empty:
        return ('<tr><td colspan="11" class="muted">'
                'No trades closed in 2026 or later — nothing to tax.</td></tr>')

    taxable = taxable.sort_values(["sell_date", "symbol"])
    rows = []
    for t in taxable.to_dict("records"):
        v = t.get("tax_realized_eur")
        cls = "pos" if (v or 0) >= 0 else "neg"
        close_type = str(t.get("close_type") or "trade")
        # Origin / source label
        src = str(t.get("tax_basis_source") or "")
        if src.startswith("reset_2025"):
            src_html = '<span class="basis-tag tag-reset">reset 2025-12-31</span>'
        elif src.startswith("original (higher)"):
            src_html = ('<span class="basis-tag tag-orig-higher">'
                        'original (higher)</span>')
        elif src.startswith("original (mark missing)"):
            src_html = ('<span class="basis-tag tag-missing">'
                        'original (mark missing)</span>')
        else:
            src_html = '<span class="basis-tag tag-orig">original</span>'

        original_basis_per_share = t.get("buy_price_usd")
        ye_mark = t.get("ye_mark_usd")
        ye_cell = (f"{ye_mark:,.4f}"
                   if ye_mark is not None and not pd.isna(ye_mark) else "—")

        # Highlight rows from forced closes so the accountant can spot them.
        row_attr = ' class="forced-close-row"' if close_type in _FORCED_CLOSE_TYPES else ""

        rows.append(
            f"<tr{row_attr}>"
            f"<td>{html.escape(str(t.get('symbol','')))}</td>"
            f"<td>{html.escape(str(t.get('buy_date','')))}</td>"
            f"<td>{html.escape(str(t.get('sell_date','')))}</td>"
            f"<td>{_close_type_html(close_type)}</td>"
            f"<td class='num'>{fmt_qty(t.get('quantity'))}</td>"
            f"<td class='num'>{fmt_num(original_basis_per_share, 4)}</td>"
            f"<td class='num'>{ye_cell}</td>"
            f"<td>{src_html}</td>"
            f"<td class='num'>{fmt_num(t.get('tax_basis_eur'))}</td>"
            f"<td class='num'>{fmt_num(t.get('proceeds_eur'))}</td>"
            f"<td class='num {cls}'><strong>{fmt_num(v)}</strong></td>"
            "</tr>"
        )
    return "\n".join(rows)


def _render_year_offset_rows(tax_trades: pd.DataFrame, annual: list[dict]) -> str:
    """For each taxable year, show the loss-offset breakdown explicitly:
    aggregate gains, aggregate losses split into regular vs forced-close
    (bankruptcies / delistings / mergers), top contributors per side.
    Helps the accountant audit how losses cancelled gains within the year.
    """
    if not annual:
        return ""
    if tax_trades.empty:
        return ""
    taxable = tax_trades[tax_trades["is_taxable_year"] == True].copy()
    if taxable.empty:
        return ""

    blocks = []
    for r in annual:
        year = r["year"]
        g = taxable[taxable["close_year"] == year]
        if g.empty:
            continue

        # Split losses: forced closes (bankruptcies/delistings/mergers) vs regular sells.
        # Tax law treats them identically — both reduce taxable gain — but the
        # accountant wants to see them separately for audit.
        losses_all = g[g["tax_realized_eur"] < 0]
        losses_forced = losses_all[
            losses_all["close_type"].isin(_FORCED_CLOSE_TYPES)
        ]
        losses_trade = losses_all[
            ~losses_all["close_type"].isin(_FORCED_CLOSE_TYPES)
        ]
        forced_loss_eur = float(losses_forced["tax_realized_eur"].sum())
        trade_loss_eur = float(losses_trade["tax_realized_eur"].sum())

        # Aggregate gains and losses by SYMBOL (across all FIFO-matched lots)
        # so a multi-lot forced-close shows as one summed line, not many
        # separate small lines that hide the real impact.
        wins_by_sym = _aggregate_by_symbol(g[g["tax_realized_eur"] > 0])
        losses_by_sym = _aggregate_by_symbol(losses_all)

        # Show ALL gains and ALL losses (not capped). The accountant gets the
        # full picture; per-trade-detail tab still has the lot-level view.
        win_rows = _format_symbol_aggregate_rows(wins_by_sym, sign="pos")
        loss_rows = _format_symbol_aggregate_rows(losses_by_sym, sign="neg")

        # Optional second summary line that breaks losses into the two buckets.
        forced_breakdown_html = ""
        if abs(forced_loss_eur) > 1e-6 or abs(trade_loss_eur) > 1e-6:
            parts = []
            if abs(trade_loss_eur) > 1e-6:
                parts.append(
                    f"trades <strong class='neg'>{fmt_num(trade_loss_eur)}</strong>"
                    f" ({len(losses_trade)})"
                )
            if abs(forced_loss_eur) > 1e-6:
                parts.append(
                    f"forced closes <strong class='neg'>{fmt_num(forced_loss_eur)}</strong>"
                    f" ({len(losses_forced)} bankruptcies / delistings / mergers)"
                )
            forced_breakdown_html = (
                f'<div class="offset-summary loss-breakdown">'
                f'Loss split: {" · ".join(parts)}'
                f'</div>'
            )

        cls_net = "pos" if r["net"] >= 0 else "neg"
        blocks.append(f"""
<details class="year-block" {'open' if year >= 2026 else ''}>
  <summary>
    <strong>{year}</strong>
    <span class="muted">{len(g)} trades</span>
    <span class="num pos">+{fmt_num(r['gains'])}</span>
    <span class="num neg">−{fmt_num(r['losses'])}</span>
    <span class="num {cls_net}"><strong>= {fmt_num(r['net'])}</strong></span>
  </summary>
  <div class="offset-grid">
    <div>
      <h4>All gains by symbol</h4>
      <div class="table-wrap"><table>
        <thead><tr><th>Symbol</th><th>Lots</th><th>Type</th>
                   <th class="num">P&amp;L (EUR)</th></tr></thead>
        <tbody>{win_rows}</tbody>
      </table></div>
    </div>
    <div>
      <h4>All losses by symbol</h4>
      <div class="table-wrap"><table>
        <thead><tr><th>Symbol</th><th>Lots</th><th>Type</th>
                   <th class="num">P&amp;L (EUR)</th></tr></thead>
        <tbody>{loss_rows}</tbody>
      </table></div>
    </div>
  </div>
  <div class="muted" style="padding:0 14px 8px;font-size:.82rem">
    Aggregated per symbol across all FIFO-matched lots in the year
    (a multi-lot write-off shows as one row, not many).
    See the <em>Per-trade detail</em> tab for the lot-level view.
  </div>
  {forced_breakdown_html}
  <div class="offset-summary">
    Gains <strong class="pos">+{fmt_num(r['gains'])}</strong>
    offset by losses <strong class="neg">−{fmt_num(r['losses'])}</strong>
    → net <strong class="{cls_net}">{fmt_num(r['net'])}</strong>
    minus exemption used <strong>{fmt_num(r['total_exemption_used'])}</strong>
    = taxable <strong>{fmt_num(r['taxable'])}</strong>
    ({fmt_num(r['tax'])} EUR tax @ 10%)
  </div>
</details>
""")
    return "\n".join(blocks)


def _render_auto_changes_rows(auto_changes: list[dict]) -> str:
    """Table rows for the auto-detected symbol-change list."""
    if not auto_changes:
        return ('<tr><td colspan="4" class="muted">'
                'No symbol changes auto-detected from snapshot reconciliation.</td></tr>')
    rows = []
    for ch in sorted(auto_changes, key=lambda c: c["date"]):
        rows.append(
            f"<tr><td>{html.escape(str(ch['date']))}</td>"
            f"<td><strong>{html.escape(str(ch['old_symbol']))}</strong> → "
            f"{html.escape(str(ch['new_symbol']))}</td>"
            f"<td class='num'>{fmt_qty(ch.get('old_qty'))}</td>"
            f"<td class='muted'>{html.escape(str(ch.get('desc') or ''))}</td></tr>"
        )
    return "\n".join(rows)


def _render_marks_status(
    needed: list[str],
    missing: list[str],
    ye_marks: dict[str, dict],
    fx_2025_12_31: Optional[float],
) -> str:
    rows = []
    for sym in needed:
        m = ye_marks.get(sym)
        if m:
            rows.append(
                f"<tr><td>{html.escape(sym)}</td>"
                f"<td class='num'>{m['close_price']:,.4f} {html.escape(m.get('currency') or 'USD')}</td>"
                f"<td><span class='basis-tag tag-{html.escape(m['source'])}'>"
                f"{html.escape(m['source'])}</span></td>"
                f"<td class='muted'>{html.escape(m.get('fetched_at') or '')}</td></tr>"
            )
        else:
            rows.append(
                f"<tr><td>{html.escape(sym)}</td>"
                f"<td class='num muted'>—</td>"
                f"<td><span class='basis-tag tag-missing'>missing</span></td>"
                f"<td class='muted'>fall back to original buy basis</td></tr>"
            )
    if not rows:
        rows = ["<tr><td colspan='4' class='muted'>"
                "No symbols require a year-end mark for this account.</td></tr>"]
    return "\n".join(rows)


def render_html(
    *,
    annual: list[dict],
    tax_trades: pd.DataFrame,
    ye_marks: dict[str, dict],
    fx_2025_12_31: Optional[float],
    symbols_needed: list[str],
    symbols_missing: list[str],
    auto_changes: list[dict],
    account: str,
    method: str,
) -> str:
    annual_rows_html = _render_annual_rows(annual)
    per_trade_rows_html = _render_per_trade_rows(tax_trades)
    offset_blocks_html = _render_year_offset_rows(tax_trades, annual)
    marks_rows_html = _render_marks_status(
        symbols_needed, symbols_missing, ye_marks, fx_2025_12_31
    )
    auto_changes_rows_html = _render_auto_changes_rows(auto_changes)

    # Headline KPIs across all taxable years
    total_tax = sum(r["tax"] for r in annual)
    total_taxable = sum(r["taxable"] for r in annual)
    total_net = sum(r["net"] for r in annual)
    total_exemption_used = sum(r["total_exemption_used"] for r in annual)
    last_year = annual[-1] if annual else None
    carry_balance = last_year["carry_out"] if last_year else 0.0

    return render_report(
        "cgt.html",
        css_files=["css/cgt.css"],
        js_files=["js/cgt.js"],
        account=account,
        method=method,
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        # KPIs
        total_tax_eur=total_tax,
        total_taxable_eur=total_taxable,
        total_net_eur=total_net,
        total_exemption_used_eur=total_exemption_used,
        carry_balance_eur=carry_balance,
        years_count=len(annual),
        # Marks status
        marks_total=len(symbols_needed),
        marks_have=len([s for s in symbols_needed if s in ye_marks]),
        marks_missing=len(symbols_missing),
        fx_2025_12_31=fx_2025_12_31,
        reset_date=RESET_DATE,
        regime_start=REGIME_START,
        # Tables
        annual_rows=annual_rows_html,
        per_trade_rows=per_trade_rows_html,
        offset_blocks=offset_blocks_html,
        marks_rows=marks_rows_html,
        auto_changes_rows=auto_changes_rows_html,
        auto_changes_count=len(auto_changes),
        # Constants for the help text
        annual_exemption=ANNUAL_EXEMPTION_EUR,
        max_yearly_carry=MAX_YEARLY_CARRY_EUR,
        max_carry_bank=MAX_CARRY_BANK_EUR,
        tax_rate_pct=TAX_RATE * 100,
    )


# ---------- CLI ----------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Belgian capital gains tax (10% from 2026) for an IBKR account."
    )
    ap.add_argument("-a", "--account", required=True, type=_resolve_account,
                    help="Account: P|personal or B|business")
    ap.add_argument("--method", default="FIFO", choices=["FIFO", "LIFO", "fifo", "lifo"],
                    help="Lot matching method (default FIFO)")
    args = ap.parse_args(argv)
    code = ACCOUNT_LETTER[args.account]
    out = build_cgt_html(code, method=args.method)
    sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
