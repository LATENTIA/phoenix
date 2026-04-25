"""
Annual P&L calculator for IBKR accounts with lot-based FIFO/LIFO matching.

Loads every trade available:
  - Year-stamped Flex XML in `downloaded/<account>/<account>_<year>.xml`
  - IBKR activity statement CSVs in `downloaded/<account>/*.csv`
  - Legacy fallback: `downloaded/<account>_ytd.xml`

Walks the full trade history chronologically per symbol:
  - BUY  -> opens a lot
  - SELL -> matches against open lots using FIFO (default) or LIFO
  - Remaining lots = open positions
  - Each matched sell produces a "closed trade" row with both legs

EUR conversion (FX-accurate, a.k.a. Method 2):
  basis_eur    = basis_usd    / EUR/USD rate on BUY  date
  proceeds_eur = proceeds_usd / EUR/USD rate on SELL date
  realized_pnl_eur = proceeds_eur - basis_eur (captures FX movement in P&L)

This file is the rebuilt source after the 2026-04-25 truncation incident.
Verified output-identical to the original Python 3.12 bytecode (`_pnl_legacy.pyc`,
kept beside as a historical audit trail).
"""

import argparse
import html
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd

from core import db as _db
from core.ecb_fx_parser import get_eur_usd_rate_for_day
from core.templating import render, render_report
from reports._helpers import (
    ACCOUNT_ALIASES, ACCOUNT_LETTER,
    fmt_num, fmt_qty,
    resolve_account as _resolve_account,
)


DOWNLOAD_DIR = Path("downloaded")
PARSED_DIR = Path("parsed")


# ---------- Trade dedup ----------

def dedupe(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse duplicate trades that may appear in both XML and CSV sources."""
    if df.empty:
        return df
    id_key = df["tradeID"].fillna("").astype(str)
    fallback_key = (
        df["tradeDate"].fillna("").astype(str)
        + "|" + df["symbol"].fillna("").astype(str)
        + "|" + df["quantity"].astype(str)
        + "|" + df["tradePrice"].astype(str)
    )
    df = df.assign(_dedup_key=id_key.where(id_key != "", fallback_key))
    df = df.sort_values("source").drop_duplicates(subset="_dedup_key", keep="first")
    return df.drop(columns="_dedup_key").reset_index(drop=True)


# ---------- FX rate lookup ----------

def _rate_for(date_iso: str, cache: dict) -> float | None:
    """Look up the EUR/USD rate, caching ECB calls per date."""
    if not date_iso:
        return None
    if date_iso not in cache:
        try:
            cache[date_iso] = get_eur_usd_rate_for_day(date_iso)
        except Exception:
            cache[date_iso] = None
    return cache[date_iso]


# ---------- Corporate-action event handlers ----------

def _apply_split(event: dict, open_lots: dict[str, list[dict]]) -> None:
    """Rescale all open lots of a symbol by a split ratio."""
    symbol = event["symbol"]
    ratio = event.get("ratio")
    if not symbol or not ratio or ratio <= 0:
        return
    for lot in open_lots.get(symbol, []):
        # 1-for-N reverse split: ratio = old/new (>1) → qty shrinks, price grows
        # N-for-1 forward split: ratio = old/new (<1) → qty grows, price shrinks
        lot["qty_original"] = lot["qty_original"] / ratio
        lot["qty_remaining"] = lot["qty_remaining"] / ratio
        if lot.get("buy_price_usd"):
            lot["buy_price_usd"] = lot["buy_price_usd"] * ratio
        # basis_usd_total is unchanged (same dollars, fewer shares)


def _apply_close_event(event: dict, open_lots: dict[str, list[dict]],
                        closed: list[dict], fx_cache: dict, method: str) -> None:
    """Emit a synthetic SELL matching the open lots of a symbol at given proceeds."""
    symbol = event["symbol"]
    lots = open_lots.get(symbol, [])
    total_qty = sum(l["qty_remaining"] for l in lots)
    if total_qty <= 0:
        return
    proceeds_total = event.get("proceeds_usd") or 0.0
    qty_removed = event.get("qty_removed") or total_qty
    close_date = event.get("date")
    close_rate = _rate_for(close_date, fx_cache)

    remaining = min(total_qty, qty_removed) if qty_removed else total_qty
    total_to_match = remaining
    while remaining > 1e-9 and open_lots[symbol]:
        idx = 0 if method == "FIFO" else -1
        lot = open_lots[symbol][idx]
        matched = min(remaining, lot["qty_remaining"])
        lot_frac = matched / lot["qty_original"]
        basis_usd = lot["basis_usd_total"] * lot_frac
        buy_commission_matched = (lot.get("buy_commission_usd") or 0) * lot_frac

        sell_frac = matched / total_to_match if total_to_match else 0
        proceeds_matched = proceeds_total * sell_frac

        buy_rate = lot.get("buy_fx")
        basis_eur = (basis_usd / buy_rate) if buy_rate else None
        proceeds_eur = (proceeds_matched / close_rate) if close_rate else None
        realized_usd = proceeds_matched - basis_usd + buy_commission_matched
        realized_eur = (proceeds_eur - basis_eur) if (proceeds_eur is not None and basis_eur is not None) else None

        closed.append({
            "symbol": symbol,
            "asset_category": lot.get("asset_category", ""),
            "buy_date": lot.get("buy_date"),
            "sell_date": close_date,
            "close_year": int(close_date[:4]) if close_date else None,
            "quantity": matched,
            "buy_price_usd": lot.get("buy_price_usd"),
            "sell_price_usd": (proceeds_total / qty_removed) if qty_removed else 0,
            "basis_usd": basis_usd,
            "proceeds_usd": proceeds_matched,
            "commission_buy_usd": buy_commission_matched,
            "commission_sell_usd": 0.0,
            "realized_pnl_usd": realized_usd,
            "buy_fx": buy_rate,
            "sell_fx": close_rate,
            "basis_eur": basis_eur,
            "proceeds_eur": proceeds_eur,
            "realized_pnl_eur": realized_eur,
            "buy_source": lot.get("buy_source"),
            "sell_source": f"ca:{event.get('type')}",
            "close_type": event.get("type"),
        })

        lot["qty_remaining"] -= matched
        remaining -= matched
        if lot["qty_remaining"] <= 1e-9:
            open_lots[symbol].pop(idx)


def _apply_transfer(event: dict, open_lots: dict[str, list[dict]], fx_cache: dict) -> None:
    """
    Share movement between accounts.
      IN  → add a new lot (basis approximated from market value at transfer date)
      OUT → remove qty (no P&L; basis just leaves the account)
    """
    direction = event.get("direction", "").upper()
    symbol = event.get("symbol")
    qty = event.get("quantity") or 0
    date = event.get("date")
    if not symbol or qty <= 0 or not direction:
        return

    if direction == "IN":
        market_value = event.get("market_value_usd") or 0.0
        per_share = event.get("per_share_usd")
        rate = _rate_for(date, fx_cache)
        lot = {
            "buy_date": date,
            "buy_source": event.get("source", "transfer"),
            "qty_original": qty,
            "qty_remaining": qty,
            "buy_price_usd": per_share,
            "basis_usd_total": market_value,
            "buy_commission_usd": 0.0,
            "buy_fx": rate,
            "asset_category": event.get("asset_category", ""),
            "description": f"Transferred IN from {event.get('xfer_account','')}",
        }
        open_lots[symbol].append(lot)
        return

    if direction == "OUT":
        remaining = qty
        while remaining > 1e-9 and open_lots.get(symbol):
            lot = open_lots[symbol][0]
            matched = min(remaining, lot["qty_remaining"])
            lot["qty_remaining"] -= matched
            remaining -= matched
            if lot["qty_remaining"] <= 1e-9:
                open_lots[symbol].pop(0)
        if remaining > 1e-9:
            print(f"[warn] Transfer OUT {symbol} on {date}: {remaining:g} shares beyond open position")


def _apply_stock_merger(event: dict, open_lots: dict[str, list[dict]]) -> None:
    """Share-for-share exchange. Close old lots and open new ones with same basis (non-taxable rollover)."""
    old_sym = event.get("old_symbol")
    new_sym = event.get("new_symbol")
    if not old_sym or not new_sym:
        return
    old_lots = open_lots.get(old_sym, [])
    if not old_lots:
        return
    old_total = sum(l["qty_remaining"] for l in old_lots)
    new_total = event.get("new_qty") or 0
    if old_total <= 0 or new_total <= 0:
        return
    scale = new_total / old_total
    for lot in old_lots:
        frac = lot["qty_remaining"] / lot["qty_original"] if lot["qty_original"] else 1
        new_lot = {
            "buy_date": lot.get("buy_date"),
            "buy_source": lot.get("buy_source"),
            "qty_original": lot["qty_remaining"] * scale,
            "qty_remaining": lot["qty_remaining"] * scale,
            "buy_price_usd": ((lot.get("buy_price_usd") or 0) / scale) if scale else None,
            "basis_usd_total": lot["basis_usd_total"] * frac,
            "buy_commission_usd": (lot.get("buy_commission_usd") or 0) * frac,
            "buy_fx": lot.get("buy_fx"),
            "asset_category": lot.get("asset_category", ""),
            "description": f"Rolled over from {old_sym} via stock merger",
        }
        open_lots[new_sym].append(new_lot)
    open_lots[old_sym] = []


def _group_ca_actions(ca_df: pd.DataFrame) -> list[dict]:
    """Collapse raw CA rows (paired old/new legs) into logical actions for the event stream."""
    if ca_df.empty:
        return []
    actions = []
    for (date, type_), g in ca_df.groupby(["date", "type"]):
        sym_net = g.groupby("symbol")["quantity"].sum().to_dict()
        total_proceeds = g["proceeds_usd"].fillna(0).sum()
        desc = g.iloc[0]["description"]

        if type_ == "split":
            removed = -g.loc[g["quantity"] < 0, "quantity"].sum()
            added = g.loc[g["quantity"] > 0, "quantity"].sum()
            symbol = next(iter(sym_net))
            ratio = (removed / added) if added else None
            actions.append({"date": date, "type": "split", "symbol": symbol,
                            "ratio": ratio,
                            "qty_change": float(added - removed),
                            "desc": desc})
        elif type_ == "delist":
            symbol, qty = next(iter(sym_net.items()))
            actions.append({"date": date, "type": "delist", "symbol": symbol,
                            "qty_removed": abs(qty) if qty else 0,
                            "proceeds_usd": total_proceeds, "desc": desc})
        elif type_ == "cash_merger":
            symbol, qty = next(iter(sym_net.items()))
            actions.append({"date": date, "type": "cash_merger", "symbol": symbol,
                            "qty_removed": abs(qty) if qty else 0,
                            "proceeds_usd": total_proceeds, "desc": desc})
        elif type_ == "stock_merger":
            neg = {s: q for s, q in sym_net.items() if q < 0}
            pos = {s: q for s, q in sym_net.items() if q > 0}
            if neg and pos:
                old_sym = max(neg, key=lambda s: abs(neg[s]))
                new_sym = max(pos, key=lambda s: pos[s])
                actions.append({"date": date, "type": "stock_merger",
                                "old_symbol": old_sym, "new_symbol": new_sym,
                                "old_qty": abs(neg[old_sym]), "new_qty": pos[new_sym],
                                "desc": desc})
            elif neg and not pos:
                symbol = next(iter(neg))
                actions.append({"date": date, "type": "delist", "symbol": symbol,
                                "qty_removed": abs(neg[symbol]),
                                "proceeds_usd": total_proceeds, "desc": desc})
        else:
            print(f"[ca] unhandled action type on {date}: {desc[:80]}")
    return actions


def _snapshot_to_dict(snap_df: pd.DataFrame) -> dict[str, float]:
    """Collapse a snapshot DataFrame into {symbol: total_qty}, ignoring forex pairs."""
    if snap_df is None or snap_df.empty:
        return {}
    out: dict[str, float] = {}
    for _, r in snap_df.iterrows():
        sym = r.get("symbol")
        if not sym or re.fullmatch(r"[A-Z]{3}\.[A-Z]{3}", str(sym)):
            continue
        try:
            out[sym] = out.get(sym, 0.0) + float(r.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
    return out


def _detect_symbol_changes(
    reconcile_snapshots: list[tuple[str, pd.DataFrame]],
    trades_df: pd.DataFrame,
    ca_actions: list[dict],
    transfers: list[dict] | None = None,
    *,
    min_qty: float = 200.0,
) -> list[dict]:
    """Heuristically detect un-recorded symbol changes (renames during bankruptcy
    proceedings, ticker changes, CUSIP/ISIN swaps that IBKR didn't record as a CA).

    For each consecutive pair of open-position snapshots:
      expected_qty[S]  = prev_snap_qty[S]  +  trade_net[S]  +  ca_net[S]
      actual_qty[S]    = next_snap_qty[S]
      missing[S]   = expected − actual    (positive = phantom disappearance)
      appeared[S]  = actual − expected    (positive = phantom appearance)

    If sum(missing) ≈ sum(appeared) within tolerance, treat as a rollover.
    Pick the largest appeared symbol as the destination, emit one
    `stock_merger` event per missing symbol routing the basis there.

    Conservative: if quantities don't sum-match within ~1 share we emit
    nothing — better to miss a rename than invent a fake one.
    """
    if not reconcile_snapshots or len(reconcile_snapshots) < 2:
        return []

    transfers = transfers or []
    snaps = sorted(reconcile_snapshots, key=lambda t: t[0])
    trades_df = trades_df.copy()
    trades_df["tradeDate"] = trades_df["tradeDate"].fillna("")

    detected: list[dict] = []
    prev_date, prev_snap = snaps[0]
    prev_qty = _snapshot_to_dict(prev_snap)

    for curr_date, curr_snap in snaps[1:]:
        curr_qty = _snapshot_to_dict(curr_snap)

        # Net trade qty between (prev_date, curr_date].
        sel = (trades_df["tradeDate"] > prev_date) & (trades_df["tradeDate"] <= curr_date)
        period = trades_df[sel]
        net_trade: dict[str, float] = (
            period.groupby("symbol")["quantity"].sum().to_dict()
            if not period.empty else {}
        )

        # Net transfer qty in the same window (IN = +, OUT = -).
        net_transfer: dict[str, float] = defaultdict(float)
        for xf in transfers:
            d = xf.get("date")
            if not d or not (prev_date < d <= curr_date):
                continue
            sym = xf.get("symbol")
            qty = float(xf.get("quantity") or 0)
            direction = (xf.get("direction") or "").upper()
            if not sym or qty <= 0:
                continue
            net_transfer[sym] += qty if direction == "IN" else -qty

        # Net CA qty effects in the same window.
        net_ca: dict[str, float] = defaultdict(float)
        for ca in ca_actions:
            d = ca.get("date")
            if not d or not (prev_date < d <= curr_date):
                continue
            t = ca.get("type")
            if t in ("delist", "cash_merger"):
                sym = ca.get("symbol")
                if sym:
                    net_ca[sym] -= float(ca.get("qty_removed") or 0)
            elif t == "stock_merger":
                old, new = ca.get("old_symbol"), ca.get("new_symbol")
                if old:
                    net_ca[old] -= float(ca.get("old_qty") or 0)
                if new:
                    net_ca[new] += float(ca.get("new_qty") or 0)
            elif t == "split":
                # Split rescales qty in place (e.g. 1-for-25 reverse: 1500 → 60).
                # The qty_change captures the snapshot-visible delta.
                sym = ca.get("symbol")
                if sym:
                    net_ca[sym] += float(ca.get("qty_change") or 0)

        all_syms = (set(prev_qty) | set(curr_qty)
                    | set(net_trade) | set(net_ca) | set(net_transfer))
        # forex / known non-equity symbols
        all_syms = {s for s in all_syms
                    if s and not re.fullmatch(r"[A-Z]{3}\.[A-Z]{3}", str(s))}

        missing: dict[str, float] = {}
        appeared: dict[str, float] = {}
        for sym in all_syms:
            expected = (prev_qty.get(sym, 0.0)
                        + net_trade.get(sym, 0.0)
                        + net_ca.get(sym, 0.0)
                        + net_transfer.get(sym, 0.0))
            actual = curr_qty.get(sym, 0.0)
            diff = expected - actual
            if diff > 0.5:
                missing[sym] = diff
            elif diff < -0.5:
                appeared[sym] = -diff

        # Greedy per-symbol matching: for each disappeared (largest-first), find the
        # largest still-unattributed appeared symbol whose remaining capacity is
        # ≥ this disappearance. This naturally handles many-to-one rollovers
        # (e.g. ABC + ABCQ → XYZ during bankruptcy proceedings) and one-to-one
        # renames (e.g. ticker → ticker-with-Q-suffix) in the same period.
        # `min_qty` filters out small coincidental matches (e.g. two unrelated
        # bankrupt stocks both with ~100 shares — likely a false positive).
        appeared_remaining = dict(appeared)
        for old_sym, miss_qty in sorted(
            missing.items(), key=lambda kv: -kv[1]
        ):
            if miss_qty < min_qty:
                continue
            # Find best target: largest appeared with capacity ≥ miss_qty * 0.95
            # (allow 5% slack for IBKR fractional fudge / 1-share adjustments).
            candidates = [
                (s, q) for s, q in appeared_remaining.items()
                if q >= miss_qty * 0.95
            ]
            if not candidates:
                continue
            target, target_total = max(candidates, key=lambda kv: kv[1])
            assigned = min(miss_qty, target_total)
            appeared_remaining[target] = target_total - assigned
            detected.append({
                "date": curr_date,
                "type": "stock_merger",
                "old_symbol": old_sym,
                "new_symbol": target,
                "old_qty": assigned,
                "new_qty": assigned,
                "desc": (f"AUTO-DETECTED symbol change "
                         f"{old_sym} → {target} on {curr_date}: "
                         f"{assigned:g} shares disappeared from {old_sym}, "
                         f"matched against unexplained appearance in {target} "
                         f"(snapshot reconciliation). Basis rolls forward "
                         f"(non-taxable, like a stock merger)."),
                "auto_detected": True,
            })

        prev_date, prev_qty = curr_date, curr_qty

    return detected


def match_lots(
    df: pd.DataFrame,
    ca_actions: list[dict] | None = None,
    transfers: list[dict] | None = None,
    reconcile_snapshots: list[tuple[str, pd.DataFrame]] | None = None,
    method: str = "FIFO",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Walk trades + corporate actions + transfers chronologically per symbol, matching
    sells to open lots. After processing, optionally reconcile against IBKR's reported
    open positions (force-closing any phantom-open lots at $0 on the snapshot date).
    Returns (closed_df, open_df).
    """
    method = method.upper()
    if method not in ("FIFO", "LIFO"):
        raise ValueError(f"method must be FIFO or LIFO, got {method}")
    ca_actions = ca_actions or []
    transfers = transfers or []
    reconcile_snapshots = reconcile_snapshots or []

    df = df.sort_values(["dateTime", "source"], kind="stable", na_position="last").reset_index(drop=True)

    # Heuristically detect un-recorded symbol changes (Chapter-11 ticker renames
    # with a "Q" suffix, FDIC-takeover renames, CUSIP/ISIN swaps IBKR forgot to
    # flag as a CA, etc.) by reconciling consecutive snapshots against the
    # trades/CAs/transfers in between. These become extra stock_merger events
    # so the basis rolls forward instead of being written off.
    auto_changes = _detect_symbol_changes(
        reconcile_snapshots, df, ca_actions, transfers=transfers,
    )
    for ch in auto_changes:
        print(f"[symbol-change] {ch['date']}: {ch['old_symbol']} → {ch['new_symbol']} "
              f"(qty {ch['old_qty']:g}; basis rolled forward, no taxable event)")

    # Unified event stream: trades (kind=0, earliest), transfers (kind=1, mid-day),
    # CAs and reconcile (kind=2, end-of-day). Auto-detected symbol changes share the
    # mid-day priority slot so they fire BEFORE reconcile on the same date.
    events: list[tuple[str, int, dict]] = []
    for _, row in df.iterrows():
        key = (row.get("dateTime") or row.get("tradeDate") or "", 0)
        events.append((*key, {"kind": "trade", **row.to_dict()}))
    for act in ca_actions:
        key = ((act["date"] or "") + " 23:59:59", 1)
        events.append((*key, {"kind": act["type"], **act}))
    for xfer in transfers:
        key = ((xfer.get("date") or "") + " 12:00:00", 1)
        events.append((*key, {"kind": "transfer", **xfer}))
    for ch in auto_changes:
        # Mid-day so it precedes the reconcile event at the same date.
        events.append((ch["date"] + " 12:00:00", 1,
                       {"kind": "stock_merger", **ch}))
    for snap_date, snap_df in reconcile_snapshots:
        if snap_date and snap_df is not None and not snap_df.empty:
            events.append((snap_date + " 23:59:59", 2,
                           {"kind": "reconcile", "snapshot": snap_df, "date": snap_date}))
    events.sort(key=lambda e: (e[0], e[1]))

    open_lots: dict[str, list[dict]] = defaultdict(list)
    closed: list[dict] = []
    fx_cache: dict = {}

    for _, _, event in events:
        kind = event.get("kind")

        if kind == "split":
            _apply_split(event, open_lots)
            continue
        if kind in ("delist", "cash_merger"):
            _apply_close_event(event, open_lots, closed, fx_cache, method)
            continue
        if kind == "stock_merger":
            _apply_stock_merger(event, open_lots)
            continue
        if kind == "transfer":
            _apply_transfer(event, open_lots, fx_cache)
            continue
        if kind == "reconcile":
            _apply_reconcile(open_lots, closed, fx_cache, method,
                             event.get("snapshot"), event.get("date"))
            continue

        # Regular trade
        row = event
        symbol = row.get("symbol")
        qty = row.get("quantity")
        if not symbol or qty is None or qty == 0:
            continue

        proceeds = row.get("proceeds_usd") or 0.0
        commission = row.get("commission_usd") or 0.0
        trade_date = row.get("tradeDate")
        rate = _rate_for(trade_date, fx_cache)

        if qty > 0:
            # BUY → open a new lot
            lot = {
                "buy_date": trade_date,
                "buy_source": row.get("source", ""),
                "qty_original": qty,
                "qty_remaining": qty,
                "buy_price_usd": row.get("tradePrice"),
                "basis_usd_total": abs(proceeds) + abs(commission),
                "buy_commission_usd": commission,
                "buy_fx": rate,
                "asset_category": row.get("assetCategory", ""),
                "description": row.get("description", ""),
            }
            open_lots[symbol].append(lot)
            continue

        # SELL → match against open lots
        sell_qty_remaining = -qty
        sell_qty_original = sell_qty_remaining
        sell_proceeds = proceeds
        sell_commission = commission

        while sell_qty_remaining > 0 and open_lots.get(symbol):
            idx = 0 if method == "FIFO" else -1
            lot = open_lots[symbol][idx]

            matched = min(sell_qty_remaining, lot["qty_remaining"])
            lot_frac = matched / lot["qty_original"]
            basis_usd = lot["basis_usd_total"] * lot_frac
            buy_commission_matched = (lot["buy_commission_usd"] or 0) * lot_frac

            sell_frac = matched / sell_qty_original
            proceeds_matched = sell_proceeds * sell_frac
            sell_commission_matched = sell_commission * sell_frac

            buy_rate = lot["buy_fx"]
            sell_rate = rate
            basis_eur = (basis_usd / buy_rate) if buy_rate else None
            proceeds_eur = (proceeds_matched / sell_rate) if sell_rate else None
            realized_usd = proceeds_matched - basis_usd + sell_commission_matched
            realized_eur = (proceeds_eur - basis_eur) if (proceeds_eur is not None and basis_eur is not None) else None

            closed.append({
                "symbol": symbol,
                "asset_category": lot.get("asset_category", ""),
                "buy_date": lot["buy_date"],
                "sell_date": trade_date,
                "close_year": int(trade_date[:4]) if trade_date else None,
                "quantity": matched,
                "buy_price_usd": lot["buy_price_usd"],
                "sell_price_usd": row.get("tradePrice"),
                "basis_usd": basis_usd,
                "proceeds_usd": proceeds_matched,
                "commission_buy_usd": buy_commission_matched,
                "commission_sell_usd": sell_commission_matched,
                "realized_pnl_usd": realized_usd,
                "buy_fx": buy_rate,
                "sell_fx": sell_rate,
                "basis_eur": basis_eur,
                "proceeds_eur": proceeds_eur,
                "realized_pnl_eur": realized_eur,
                "buy_source": lot.get("buy_source"),
                "sell_source": row.get("source", ""),
                "close_type": "trade",
            })

            lot["qty_remaining"] -= matched
            sell_qty_remaining -= matched
            if lot["qty_remaining"] <= 1e-9:
                open_lots[symbol].pop(idx)

        if sell_qty_remaining > 0:
            print(f"[warn] {symbol} on {trade_date}: sold {sell_qty_remaining:g} "
                  f"shares beyond any open lot (short sale or missing prior history)")

    # Flatten remaining open lots for the output DataFrame
    open_rows = []
    for symbol, lots in open_lots.items():
        for lot in lots:
            frac = lot["qty_remaining"] / lot["qty_original"]
            basis_usd_rem = lot["basis_usd_total"] * frac
            open_rows.append({
                "symbol": symbol,
                "asset_category": lot.get("asset_category", ""),
                "buy_date": lot["buy_date"],
                "qty_original": lot["qty_original"],
                "qty_remaining": lot["qty_remaining"],
                "buy_price_usd": lot["buy_price_usd"],
                "basis_usd_remaining": basis_usd_rem,
                "buy_fx": lot["buy_fx"],
                "basis_eur_remaining": (basis_usd_rem / lot["buy_fx"]) if lot["buy_fx"] else None,
                "buy_source": lot["buy_source"],
            })

    closed_df = pd.DataFrame(closed)
    open_df = pd.DataFrame(open_rows)
    if not closed_df.empty:
        closed_df = closed_df.sort_values(["close_year", "sell_date", "symbol"]).reset_index(drop=True)
    if not open_df.empty:
        open_df = open_df.sort_values(["symbol", "buy_date"]).reset_index(drop=True)
    return closed_df, open_df


def _apply_reconcile(open_lots: dict[str, list[dict]], closed: list[dict],
                     fx_cache: dict, method: str,
                     snapshot: pd.DataFrame | None, reconcile_date: str | None) -> None:
    """
    Force-close any open lots that IBKR's snapshot says are no longer there.
    Treats the disposal as a $0 close on `reconcile_date` (full loss). For symbols
    where IBKR reports a partial position, we just warn and leave the lots untouched.
    """
    if snapshot is None or snapshot.empty or not reconcile_date:
        return
    ibkr_qty: dict[str, float] = {}
    for _, r in snapshot.iterrows():
        sym = r.get("symbol")
        qty = r.get("quantity")
        if sym and qty is not None:
            ibkr_qty[sym] = ibkr_qty.get(sym, 0) + qty

    rate = _rate_for(reconcile_date, fx_cache)
    for symbol, lots in list(open_lots.items()):
        if not symbol:
            continue
        if re.fullmatch(r"[A-Z]{3}\.[A-Z]{3}", symbol):
            continue  # forex, not a stock position
        our_qty = sum(l["qty_remaining"] for l in lots)
        their_qty = ibkr_qty.get(symbol, 0)
        if our_qty <= 1e-6:
            continue
        if their_qty > 1e-6:
            if abs(our_qty - their_qty) > 1e-6:
                print(f"[reconcile] {symbol}: we have {our_qty:g}, IBKR has {their_qty:g} "
                      f"(likely missing trades — left OPEN, please verify)")
            continue

        # IBKR reports zero — write off all our lots at $0 on reconcile_date.
        print(f"[reconcile] {symbol}: IBKR reports 0, writing off {our_qty:g} shares "
              f"at $0 (bankruptcy/delisting) on {reconcile_date}")
        excess = our_qty
        while excess > 1e-9 and open_lots[symbol]:
            idx = 0 if method == "FIFO" else -1
            lot = open_lots[symbol][idx]
            matched = min(excess, lot["qty_remaining"])
            lot_frac = matched / lot["qty_original"]
            basis_usd = lot["basis_usd_total"] * lot_frac
            buy_commission_matched = (lot.get("buy_commission_usd") or 0) * lot_frac
            buy_rate = lot.get("buy_fx")
            basis_eur = (basis_usd / buy_rate) if buy_rate else None
            realized_usd = -basis_usd + buy_commission_matched
            realized_eur = -basis_eur if basis_eur is not None else None
            closed.append({
                "symbol": symbol,
                "asset_category": lot.get("asset_category", ""),
                "buy_date": lot.get("buy_date"),
                "sell_date": reconcile_date,
                "close_year": int(reconcile_date[:4]) if reconcile_date else None,
                "quantity": matched,
                "buy_price_usd": lot.get("buy_price_usd"),
                "sell_price_usd": 0.0,
                "basis_usd": basis_usd,
                "proceeds_usd": 0.0,
                "commission_buy_usd": buy_commission_matched,
                "commission_sell_usd": 0.0,
                "realized_pnl_usd": realized_usd,
                "buy_fx": buy_rate,
                "sell_fx": rate,
                "basis_eur": basis_eur,
                "proceeds_eur": 0.0,
                "realized_pnl_eur": realized_eur,
                "buy_source": lot.get("buy_source"),
                "sell_source": "reconcile:IBKR-open-positions",
                "close_type": "reconcile",
            })
            lot["qty_remaining"] -= matched
            excess -= matched
            if lot["qty_remaining"] <= 1e-9:
                open_lots[symbol].pop(idx)


# ---------- Summaries ----------

def annual_summary(closed: pd.DataFrame) -> pd.DataFrame:
    """Aggregate closed trades by close-year. Includes wins/losses split."""
    if closed.empty:
        return pd.DataFrame()
    rows = []
    for year, g in closed.dropna(subset=["close_year"]).groupby("close_year"):
        pnl_eur = g["realized_pnl_eur"]
        gains_eur = pnl_eur[pnl_eur > 0].sum()
        losses_eur = pnl_eur[pnl_eur < 0].sum()
        rows.append({
            "year": int(year),
            "closed_trades": len(g),
            "wins": int((pnl_eur > 0).sum()),
            "losses_count": int((pnl_eur < 0).sum()),
            "symbols": g["symbol"].nunique(),
            "gross_proceeds_usd": g["proceeds_usd"].abs().sum(),
            "total_basis_usd": g["basis_usd"].sum(),
            "commissions_usd": (g["commission_buy_usd"].sum() + g["commission_sell_usd"].sum()),
            "realized_pnl_usd": g["realized_pnl_usd"].sum(),
            "gains_eur": gains_eur,
            "losses_eur": losses_eur,
            "realized_pnl_eur": pnl_eur.sum(skipna=True),
        })
    return pd.DataFrame(rows).sort_values("year").reset_index(drop=True)


def _holdings_summary(open_df: pd.DataFrame, ibkr_positions: pd.DataFrame) -> pd.DataFrame:
    """Aggregate open lots by symbol, joined with IBKR's reported quantity for side-by-side compare."""
    if open_df.empty:
        return pd.DataFrame()
    stocks_only = open_df[~open_df["symbol"].astype(str).str.fullmatch(r"[A-Z]{3}\.[A-Z]{3}", na=False)]
    if stocks_only.empty:
        return pd.DataFrame()
    agg = stocks_only.groupby("symbol", as_index=False).agg(
        lots=("buy_date", "count"),
        qty=("qty_remaining", "sum"),
        basis_usd=("basis_usd_remaining", "sum"),
        basis_eur=("basis_eur_remaining", "sum"),
    )
    if ibkr_positions is not None and not ibkr_positions.empty and "symbol" in ibkr_positions.columns:
        ibkr_sum = ibkr_positions.groupby("symbol", as_index=False)["quantity"].sum().rename(
            columns={"quantity": "ibkr_qty"}
        )
        agg = agg.merge(ibkr_sum, on="symbol", how="left")
    else:
        agg["ibkr_qty"] = None
    agg["diff"] = agg["qty"] - agg["ibkr_qty"].fillna(0)
    return agg.sort_values("symbol").reset_index(drop=True)


# ---------- Performance analytics: equity curve, drawdown, FX decomposition, distribution ----------

def _equity_curve_points(closed: pd.DataFrame) -> list[tuple[str, float]]:
    """Cumulative realized P&L (EUR) by sell_date — the spine of the equity curve."""
    if closed.empty:
        return []
    df = closed.dropna(subset=["sell_date", "realized_pnl_eur"]).copy()
    if df.empty:
        return []
    df = df.sort_values("sell_date")
    daily = df.groupby("sell_date")["realized_pnl_eur"].sum()
    cumulative = daily.cumsum()
    return list(zip(daily.index.tolist(), cumulative.tolist()))


def _drawdown_stats(points: list[tuple[str, float]]) -> dict:
    """Walk an equity curve and return max-drawdown size, peak/trough dates, recovery date,
    and underwater status. Drawdown is measured in EUR, peak-to-trough."""
    empty = {"max_dd_eur": 0.0, "peak_date": None, "trough_date": None,
             "recovered_date": None, "days_underwater": 0, "is_underwater": False,
             "peak_value": 0.0, "trough_value": 0.0}
    if not points:
        return empty
    peak = points[0][1]
    peak_date = points[0][0]
    max_dd = 0.0
    max_peak_date = peak_date
    max_trough_date = peak_date
    max_peak_value = peak
    max_trough_value = peak
    for date, val in points:
        if val > peak:
            peak = val
            peak_date = date
        dd = peak - val
        if dd > max_dd:
            max_dd = dd
            max_peak_date = peak_date
            max_trough_date = date
            max_peak_value = peak
            max_trough_value = val
    # Recovery: the curve gets back to (or above) the pre-drawdown peak after the trough.
    recovered_date = None
    for date, val in points:
        if date > max_trough_date and val >= max_peak_value:
            recovered_date = date
            break
    last_date, last_val = points[-1]
    is_underwater = recovered_date is None and last_val < max_peak_value
    days_underwater = 0
    if is_underwater:
        try:
            d_peak = datetime.strptime(max_peak_date, "%Y-%m-%d").date()
            d_last = datetime.strptime(last_date, "%Y-%m-%d").date()
            days_underwater = (d_last - d_peak).days
        except (ValueError, TypeError):
            pass
    return {
        "max_dd_eur": max_dd,
        "peak_date": max_peak_date,
        "trough_date": max_trough_date,
        "recovered_date": recovered_date,
        "days_underwater": days_underwater,
        "is_underwater": is_underwater,
        "peak_value": max_peak_value,
        "trough_value": max_trough_value,
    }


def _fx_decomposition(closed: pd.DataFrame) -> pd.DataFrame:
    """Per-year split of realized P&L into price-driven vs FX-driven components.

    With FX-accurate Method 2 (basis_eur = basis_usd / buy_fx; proceeds_eur = proceeds_usd / sell_fx),
    the "price-only" P&L (what we'd have made if FX hadn't moved) is:
        price_pnl_eur = (proceeds_usd - basis_usd) / buy_fx
    The FX impact is the residual:
        fx_pnl_eur    = realized_pnl_eur - price_pnl_eur
                      = proceeds_usd * (1/sell_fx - 1/buy_fx)
    """
    if closed.empty:
        return pd.DataFrame()
    c = closed.copy()
    for col in ("proceeds_usd", "basis_usd", "buy_fx", "realized_pnl_eur"):
        c[col] = pd.to_numeric(c[col], errors="coerce")
    valid = c.dropna(subset=["proceeds_usd", "basis_usd", "buy_fx",
                              "realized_pnl_eur", "close_year"])
    valid = valid[valid["buy_fx"] > 0]
    if valid.empty:
        return pd.DataFrame()
    price_eur = (valid["proceeds_usd"] - valid["basis_usd"]) / valid["buy_fx"]
    fx_eur = valid["realized_pnl_eur"] - price_eur
    out = pd.DataFrame({
        "close_year": valid["close_year"].astype(int),
        "total_eur": valid["realized_pnl_eur"],
        "price_eur": price_eur,
        "fx_eur": fx_eur,
    })
    return out.groupby("close_year", as_index=False).sum().sort_values("close_year").reset_index(drop=True)


_DIST_BINS: list[tuple[str, float | None, float | None]] = [
    ("< -50%",     None,  -50.0),
    ("-50…-25%",  -50.0,  -25.0),
    ("-25…-10%",  -25.0,  -10.0),
    ("-10…0%",    -10.0,    0.0),
    ("0…10%",       0.0,   10.0),
    ("10…25%",     10.0,   25.0),
    ("25…50%",     25.0,   50.0),
    ("50…100%",    50.0,  100.0),
    ("> 100%",    100.0,    None),
]


def _outcome_distribution(closed: pd.DataFrame) -> list[dict]:
    """Bin closed trades by % return (realized_pnl_eur / basis_eur). Same bins for everyone."""
    if closed.empty:
        return [{"label": l, "trades": 0, "total_eur": 0.0,
                 "is_win_bin": (lo is not None and lo >= 0)}
                for l, lo, _ in _DIST_BINS]
    c = closed.copy()
    c["pnl_eur"] = pd.to_numeric(c["realized_pnl_eur"], errors="coerce")
    c["basis_eur"] = pd.to_numeric(c["basis_eur"], errors="coerce")
    valid = c.dropna(subset=["pnl_eur", "basis_eur"])
    valid = valid[valid["basis_eur"] > 0]
    if valid.empty:
        return [{"label": l, "trades": 0, "total_eur": 0.0,
                 "is_win_bin": (lo is not None and lo >= 0)}
                for l, lo, _ in _DIST_BINS]
    pct = valid["pnl_eur"] / valid["basis_eur"] * 100
    out = []
    for label, lo, hi in _DIST_BINS:
        mask = pd.Series(True, index=pct.index)
        if lo is not None:
            mask &= pct >= lo
        if hi is not None:
            mask &= pct < hi
        out.append({
            "label": label,
            "trades": int(mask.sum()),
            "total_eur": float(valid.loc[mask, "pnl_eur"].sum()),
            "is_win_bin": (lo is not None and lo >= 0),
        })
    return out


# ---------- Inline-SVG / table renderers for the new performance widgets ----------

def _render_equity_curve_svg(points: list[tuple[str, float]], dd: dict,
                             width: int = 720, height: int = 220) -> str:
    """Inline-SVG line+area chart of cumulative realized P&L over time. No JS deps."""
    if len(points) < 2:
        return ('<div class="muted" style="padding:32px;text-align:center">'
                'Not enough closed trades to draw an equity curve.</div>')
    margin_l, margin_r, margin_t, margin_b = 56, 16, 14, 28
    plot_w = width - margin_l - margin_r
    plot_h = height - margin_t - margin_b
    values = [v for _, v in points]
    v_min = min(0.0, min(values))
    v_max = max(0.0, max(values))
    v_span = (v_max - v_min) or 1.0
    n = len(points)

    def x_of(i: int) -> float:
        return margin_l + plot_w * i / max(1, n - 1)

    def y_of(v: float) -> float:
        return margin_t + plot_h - (v - v_min) / v_span * plot_h

    last_v = values[-1]
    line_color = "var(--green)" if last_v >= 0 else "var(--red)"
    fill_color = ("rgba(74, 222, 128, .18)" if last_v >= 0
                  else "rgba(248, 113, 113, .18)")

    line_d = "M " + " L ".join(f"{x_of(i):.1f},{y_of(v):.1f}"
                               for i, (_, v) in enumerate(points))
    y_zero = y_of(0)
    area_d = (f"M {x_of(0):.1f},{y_zero:.1f} "
              + " ".join(f"L {x_of(i):.1f},{y_of(v):.1f}"
                         for i, (_, v) in enumerate(points))
              + f" L {x_of(n - 1):.1f},{y_zero:.1f} Z")

    # Horizontal grid lines + y-axis labels at v_min, 0, v_max.
    grid = []
    seen_y: set[int] = set()
    for tv in (v_min, 0.0, v_max):
        y = y_of(tv)
        key = round(y)
        if key in seen_y:
            continue
        seen_y.add(key)
        grid.append(
            f'<line x1="{margin_l}" y1="{y:.1f}" x2="{width - margin_r}" y2="{y:.1f}" '
            f'class="ec-grid"/>'
            f'<text x="{margin_l - 6}" y="{y + 3:.1f}" class="ec-axis" text-anchor="end">'
            f'{tv:,.0f}</text>'
        )

    # Drawdown markers: peak (grey dot), trough (red dot), connectors.
    markers = ""
    if dd.get("max_dd_eur", 0) > 0:
        peak_d = dd.get("peak_date")
        trough_d = dd.get("trough_date")
        peak_idx = next((i for i, (d, _) in enumerate(points) if d == peak_d), None)
        trough_idx = next((i for i, (d, _) in enumerate(points) if d == trough_d), None)
        if peak_idx is not None and trough_idx is not None:
            px, py = x_of(peak_idx), y_of(points[peak_idx][1])
            tx, ty = x_of(trough_idx), y_of(points[trough_idx][1])
            markers = (
                f'<line x1="{px:.1f}" y1="{py:.1f}" x2="{tx:.1f}" y2="{py:.1f}" class="ec-dd-h"/>'
                f'<line x1="{tx:.1f}" y1="{py:.1f}" x2="{tx:.1f}" y2="{ty:.1f}" class="ec-dd-v"/>'
                f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3.5" class="ec-peak"/>'
                f'<circle cx="{tx:.1f}" cy="{ty:.1f}" r="3.5" class="ec-trough"/>'
                f'<title>Max drawdown {peak_d} → {trough_d}: '
                f'{dd["max_dd_eur"]:,.0f} EUR</title>'
            )

    first_date = points[0][0]
    last_date = points[-1][0]
    return (
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
        f'class="ec-svg" preserveAspectRatio="none">'
        f'{"".join(grid)}'
        f'<path d="{area_d}" fill="{fill_color}" stroke="none"/>'
        f'<path d="{line_d}" fill="none" stroke="{line_color}" stroke-width="2" '
        f'stroke-linejoin="round"/>'
        f'{markers}'
        f'<text x="{margin_l}" y="{height - 6}" class="ec-axis">'
        f'{html.escape(first_date)}</text>'
        f'<text x="{width - margin_r}" y="{height - 6}" class="ec-axis" '
        f'text-anchor="end">{html.escape(last_date)}</text>'
        f'</svg>'
    )


def _render_drawdown_cards(dd: dict) -> str:
    """Three-card row pairing the equity curve."""
    if not dd or dd.get("max_dd_eur", 0) == 0:
        return ('<div class="card"><div class="label">Max drawdown</div>'
                '<div class="value muted">—</div></div>')
    cards = [
        f'<div class="card"><div class="label">Max drawdown (EUR)</div>'
        f'<div class="value neg">−{dd["max_dd_eur"]:,.0f}</div></div>',
        f'<div class="card"><div class="label">Peak → trough</div>'
        f'<div class="value" style="font-size:1rem">'
        f'{html.escape(str(dd["peak_date"]))} → {html.escape(str(dd["trough_date"]))}</div></div>',
    ]
    if dd.get("is_underwater"):
        cards.append(
            f'<div class="card"><div class="label">Currently underwater</div>'
            f'<div class="value neg">{dd["days_underwater"]} days since peak</div></div>'
        )
    elif dd.get("recovered_date"):
        cards.append(
            f'<div class="card"><div class="label">Recovered</div>'
            f'<div class="value pos">{html.escape(str(dd["recovered_date"]))}</div></div>'
        )
    return "\n".join(cards)


def _render_fx_decomp_rows(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return "<tr><td colspan='5' class='muted'>No closed trades.</td></tr>"
    rows = []
    for _, r in df.iterrows():
        total = float(r["total_eur"])
        price = float(r["price_eur"])
        fx = float(r["fx_eur"])
        fx_share = (fx / total * 100) if abs(total) > 1e-6 else 0.0
        cls_total = "pos" if total >= 0 else "neg"
        cls_price = "pos" if price >= 0 else "neg"
        cls_fx = "pos" if fx >= 0 else "neg"
        rows.append(
            f"<tr><td>{int(r['close_year'])}</td>"
            f"<td class='num {cls_total}'><strong>{fmt_num(total)}</strong></td>"
            f"<td class='num {cls_price}'>{fmt_num(price)}</td>"
            f"<td class='num {cls_fx}'>{fmt_num(fx)}</td>"
            f"<td class='num muted'>{fx_share:+.1f}%</td></tr>"
        )
    return "\n".join(rows)


def _render_distribution_rows(bins: list[dict]) -> str:
    if not bins or all(b["trades"] == 0 for b in bins):
        return "<tr><td colspan='4' class='muted'>No closed trades.</td></tr>"
    max_count = max(b["trades"] for b in bins) or 1
    rows = []
    for b in bins:
        bar_pct = b["trades"] / max_count * 100 if b["trades"] else 0
        bar_class = "dist-bar-win" if b["is_win_bin"] else "dist-bar-loss"
        bar_html = (f'<div class="dist-bar {bar_class}" '
                    f'style="width:{bar_pct:.1f}%"></div>')
        cls = "pos" if b["total_eur"] >= 0 else "neg"
        rows.append(
            f"<tr><td>{html.escape(b['label'])}</td>"
            f"<td class='num'>{b['trades']:,}</td>"
            f"<td class='num {cls}'>{fmt_num(b['total_eur'])}</td>"
            f"<td class='dist-bar-cell'>{bar_html}</td></tr>"
        )
    return "\n".join(rows)


# ---------- HTML render (Jinja-driven) ----------

def render_html(annual: pd.DataFrame, closed: pd.DataFrame, open_df: pd.DataFrame,
                all_trades: pd.DataFrame, ibkr_positions: pd.DataFrame,
                reconcile_date: str | None,
                account: str, method: str, sources: list[str]) -> str:
    """Build all the row-HTML fragments and hand them to the Jinja `pnl.html` template."""

    # Annual table rows
    annual_rows_html = []
    for _, y in annual.iterrows():
        pnl_eur = y["realized_pnl_eur"]
        cls = "pos" if (pnl_eur or 0) >= 0 else "neg"
        annual_rows_html.append(
            "<tr>"
            f"<td>{int(y['year'])}</td>"
            f"<td class='num'>{int(y['closed_trades']):,}</td>"
            f"<td class='num'>{int(y['wins']):,}</td>"
            f"<td class='num'>{int(y['losses_count']):,}</td>"
            f"<td class='num pos'>{fmt_num(y['gains_eur'])}</td>"
            f"<td class='num neg'>{fmt_num(y['losses_eur'])}</td>"
            f"<td class='num {cls}'><strong>{fmt_num(pnl_eur)}</strong></td>"
            "</tr>"
        )
    annual_html = "\n".join(annual_rows_html)

    # Closed trades — full detail
    closed_rows_html = []
    cs = closed.sort_values(["sell_date", "symbol"]) if not closed.empty else closed
    for _, t in cs.iterrows():
        v = t.get("realized_pnl_eur")
        cls = "pos" if (v or 0) >= 0 else "neg"
        closed_rows_html.append(
            "<tr>"
            f"<td>{html.escape(str(t.get('symbol','')))}</td>"
            f"<td>{html.escape(str(t.get('buy_date','')))}</td>"
            f"<td>{html.escape(str(t.get('sell_date','')))}</td>"
            f"<td class='num'>{fmt_qty(t.get('quantity'))}</td>"
            f"<td class='num'>{fmt_num(t.get('buy_price_usd'), 4)}</td>"
            f"<td class='num'>{fmt_num(t.get('sell_price_usd'), 4)}</td>"
            f"<td class='num'>{fmt_num(t.get('basis_usd'))}</td>"
            f"<td class='num'>{fmt_num(t.get('proceeds_usd'))}</td>"
            f"<td class='num'>{fmt_num(t.get('buy_fx'), 4)}</td>"
            f"<td class='num'>{fmt_num(t.get('sell_fx'), 4)}</td>"
            f"<td class='num {cls}'><strong>{fmt_num(v)}</strong></td>"
            "</tr>"
        )
    closed_html = "\n".join(closed_rows_html) or "<tr><td colspan='11' class='muted'>No closed trades.</td></tr>"

    # Open positions
    open_rows_html = []
    for _, t in open_df.iterrows():
        filled = t["qty_original"] - t["qty_remaining"]
        status = "OPEN" if filled == 0 else "PARTIAL"
        status_cls = "open" if filled == 0 else "partial"
        open_rows_html.append(
            "<tr>"
            f"<td>{html.escape(str(t.get('symbol','')))}</td>"
            f"<td>{html.escape(str(t.get('buy_date','')))}</td>"
            f"<td><span class='status-{status_cls}'>{status}</span></td>"
            f"<td class='num'>{fmt_qty(t.get('qty_original'))}</td>"
            f"<td class='num'>{fmt_qty(t.get('qty_remaining'))}</td>"
            f"<td class='num'>{fmt_num(t.get('buy_price_usd'), 4)}</td>"
            f"<td class='num'>{fmt_num(t.get('basis_usd_remaining'))}</td>"
            f"<td class='num'>{fmt_num(t.get('buy_fx'), 4)}</td>"
            f"<td class='num'>{fmt_num(t.get('basis_eur_remaining'))}</td>"
            "</tr>"
        )
    open_html_str = "\n".join(open_rows_html) or "<tr><td colspan='9' class='muted'>No open positions.</td></tr>"

    # All transactions (raw trade log)
    all_rows = []
    if not all_trades.empty:
        at = all_trades.sort_values(["tradeDate", "symbol"], kind="stable")
        for _, t in at.iterrows():
            qty = t.get("quantity") or 0
            side = "BUY" if qty > 0 else ("SELL" if qty < 0 else "")
            side_cls = "buy" if side == "BUY" else ("sell" if side == "SELL" else "")
            all_rows.append(
                "<tr>"
                f"<td>{html.escape(str(t.get('tradeDate','') or ''))}</td>"
                f"<td>{html.escape(str(t.get('symbol','')))}</td>"
                f"<td class='{side_cls}'>{side}</td>"
                f"<td class='num'>{fmt_qty(abs(qty) if qty else None)}</td>"
                f"<td class='num'>{fmt_num(t.get('tradePrice'), 4)}</td>"
                f"<td class='num'>{fmt_num(t.get('proceeds_usd'))}</td>"
                f"<td class='num'>{fmt_num(t.get('commission_usd'))}</td>"
                f"<td class='muted'>{html.escape(str(t.get('source','')))}</td>"
                "</tr>"
            )
    all_trades_html = "\n".join(all_rows) or "<tr><td colspan='8' class='muted'>No trades.</td></tr>"

    # Performance analysis
    perf: dict = {}
    perf_annual_html = ""
    perf_monthly_html = ""
    top_symbols_html = "<tr><td colspan='4' class='muted'>No closed trades.</td></tr>"
    bottom_symbols_html = top_symbols_html
    close_type_html = "<tr><td colspan='3' class='muted'>No closed trades.</td></tr>"
    category_html = "<tr><td colspan='4' class='muted'>No closed trades.</td></tr>"
    hold_period_html = "<tr><td colspan='3' class='muted'>No closed trades.</td></tr>"

    if not closed.empty:
        c = closed.copy()
        c["pnl_eur"] = pd.to_numeric(c["realized_pnl_eur"], errors="coerce")
        c["buy_date_dt"] = pd.to_datetime(c["buy_date"], errors="coerce")
        c["sell_date_dt"] = pd.to_datetime(c["sell_date"], errors="coerce")
        c["hold_days"] = (c["sell_date_dt"] - c["buy_date_dt"]).dt.days
        c["sell_month"] = c["sell_date_dt"].dt.strftime("%Y-%m")

        pnl_eur = c["pnl_eur"].dropna()
        wins = pnl_eur[pnl_eur > 0]
        losses = pnl_eur[pnl_eur < 0]
        total_basis = c["basis_eur"].dropna().sum()

        perf = {
            "trades": len(pnl_eur),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / len(pnl_eur) * 100) if len(pnl_eur) else 0,
            "total_pnl": pnl_eur.sum(),
            "avg_win": wins.mean() if len(wins) else 0,
            "avg_loss": losses.mean() if len(losses) else 0,
            "best_trade": pnl_eur.max() if len(pnl_eur) else 0,
            "worst_trade": pnl_eur.min() if len(pnl_eur) else 0,
            "profit_factor": (wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() != 0 else None,
            "expectancy": pnl_eur.mean() if len(pnl_eur) else 0,
            "total_basis": total_basis,
            "roi_pct": (pnl_eur.sum() / total_basis * 100) if total_basis else 0,
            "avg_hold_winners": c.loc[wins.index, "hold_days"].dropna().mean() if len(wins) else 0,
            "avg_hold_losers": c.loc[losses.index, "hold_days"].dropna().mean() if len(losses) else 0,
        }

        # Annual breakdown for the Performance panel
        ann_rows = []
        for year, g in c.dropna(subset=["close_year"]).groupby("close_year"):
            gp = g["pnl_eur"]
            w = (gp > 0).sum()
            l = (gp < 0).sum()
            total = w + l
            wr = (w / total * 100) if total else 0
            net = gp.sum()
            cls = "pos" if net >= 0 else "neg"
            ann_rows.append(
                f"<tr><td>{int(year)}</td>"
                f"<td class='num'>{total}</td>"
                f"<td class='num'>{wr:.1f}%</td>"
                f"<td class='num pos'>{fmt_num(gp[gp > 0].sum())}</td>"
                f"<td class='num neg'>{fmt_num(gp[gp < 0].sum())}</td>"
                f"<td class='num {cls}'><strong>{fmt_num(net)}</strong></td></tr>"
            )
        perf_annual_html = "\n".join(ann_rows)

        # Monthly heatmap
        if c["sell_month"].notna().any():
            monthly = c.dropna(subset=["sell_month"]).groupby("sell_month")["pnl_eur"].sum()
            years = sorted({m[:4] for m in monthly.index})
            max_abs = float(monthly.abs().max()) if len(monthly) else 1.0
            max_abs = max(max_abs, 1.0)
            rows_html = []
            for yr in years:
                row = [f"<td><strong>{yr}</strong></td>"]
                total = 0.0
                for mi in range(1, 13):
                    key = f"{yr}-{mi:02d}"
                    val = monthly.get(key)
                    if pd.isna(val) or val is None:
                        row.append('<td class="num muted">—</td>')
                    else:
                        intensity = min(abs(val) / max_abs, 1.0)
                        alpha = 0.15 + 0.75 * intensity
                        color = (f"rgba(74, 222, 128, {alpha})" if val >= 0
                                 else f"rgba(248, 113, 113, {alpha})")
                        row.append(f'<td class="num" style="background:{color}">{val:,.0f}</td>')
                        total += val
                tot_cls = "pos" if total >= 0 else "neg"
                row.append(f'<td class="num {tot_cls}"><strong>{total:,.0f}</strong></td>')
                rows_html.append("<tr>" + "".join(row) + "</tr>")
            perf_monthly_html = "\n".join(rows_html)

        # Top / bottom 10 symbols
        by_symbol = c.groupby("symbol").agg(
            trades=("symbol", "count"),
            pnl_eur=("pnl_eur", "sum"),
            win_rate=("pnl_eur", lambda s: (s > 0).mean() * 100),
        ).reset_index().sort_values("pnl_eur", ascending=False)

        def render_symbol_rows(df_sym):
            rows = []
            for _, r in df_sym.iterrows():
                v = r["pnl_eur"]
                clsn = "pos" if v >= 0 else "neg"
                rows.append(
                    f"<tr><td>{html.escape(str(r['symbol']))}</td>"
                    f"<td class='num'>{int(r['trades'])}</td>"
                    f"<td class='num'>{r['win_rate']:.0f}%</td>"
                    f"<td class='num {clsn}'>{fmt_num(v)}</td></tr>"
                )
            return "\n".join(rows)

        top_symbols_html = render_symbol_rows(by_symbol.head(10))
        bottom_symbols_html = render_symbol_rows(by_symbol.tail(10).iloc[::-1])

        # Asset category breakdown
        def classify(row):
            cat = (row.get("asset_category") or "").lower()
            sym = row.get("symbol", "")
            if "option" in cat:
                return "Options"
            if "crypto" in cat:
                return "Crypto"
            if re.search(r"\d{2}[A-Z]{3}\d{2}", sym):
                return "Options"
            return "Stocks"

        c["_cat"] = c.apply(classify, axis=1)
        by_cat = c.groupby("_cat").agg(
            trades=("_cat", "count"),
            win_rate=("pnl_eur", lambda s: (s > 0).mean() * 100),
            pnl_eur=("pnl_eur", "sum"),
        ).reset_index().sort_values("pnl_eur", ascending=False)
        cat_rows = []
        for _, r in by_cat.iterrows():
            v = r["pnl_eur"]
            clsn = "pos" if v >= 0 else "neg"
            cat_rows.append(
                f"<tr><td>{html.escape(str(r['_cat']))}</td>"
                f"<td class='num'>{int(r['trades'])}</td>"
                f"<td class='num'>{r['win_rate']:.0f}%</td>"
                f"<td class='num {clsn}'><strong>{fmt_num(v)}</strong></td></tr>"
            )
        category_html = "\n".join(cat_rows)

        # Close-type breakdown
        by_close = c.groupby("close_type").agg(
            trades=("symbol", "count"),
            pnl_eur=("pnl_eur", "sum"),
        ).reset_index()
        close_type_html = "\n".join(
            f"<tr><td>{html.escape(str(r['close_type']))}</td>"
            f"<td class='num'>{int(r['trades'])}</td>"
            f"<td class='num'>{fmt_num(r['pnl_eur'])}</td></tr>"
            for _, r in by_close.iterrows()
        )

        # Holding-period buckets
        def bucket(days):
            if pd.isna(days):
                return "unknown"
            if days < 1:
                return "intraday"
            if days < 8:
                return "< 1 week"
            if days < 31:
                return "1w–1m"
            if days < 91:
                return "1m–3m"
            if days < 366:
                return "3m–1y"
            return "> 1 year"

        c["_bucket"] = c["hold_days"].apply(bucket)
        order = ["intraday", "< 1 week", "1w–1m", "1m–3m", "3m–1y", "> 1 year", "unknown"]
        by_hold = c.groupby("_bucket").agg(
            trades=("symbol", "count"),
            pnl_eur=("pnl_eur", "sum"),
        ).reindex(order).dropna().reset_index()
        hold_rows = []
        for _, r in by_hold.iterrows():
            v = r["pnl_eur"]
            clsn = "pos" if v >= 0 else "neg"
            hold_rows.append(
                f"<tr><td>{html.escape(str(r['_bucket']))}</td>"
                f"<td class='num'>{int(r['trades'])}</td>"
                f"<td class='num {clsn}'>{fmt_num(v)}</td></tr>"
            )
        hold_period_html = "\n".join(hold_rows)

    # ---------- Equity curve, drawdown, FX decomposition, outcome distribution ----------
    equity_points = _equity_curve_points(closed)
    drawdown = _drawdown_stats(equity_points)
    fx_decomp_df = _fx_decomposition(closed)
    distribution = _outcome_distribution(closed)

    equity_curve_svg = _render_equity_curve_svg(equity_points, drawdown)
    drawdown_cards_html = _render_drawdown_cards(drawdown)
    fx_decomp_html = _render_fx_decomp_rows(fx_decomp_df)
    distribution_html = _render_distribution_rows(distribution)
    has_equity_curve = len(equity_points) >= 2

    # Holdings table (current snapshot vs IBKR)
    holdings = _holdings_summary(open_df, ibkr_positions)
    has_ibkr = ibkr_positions is not None and not ibkr_positions.empty
    holdings_rows = []
    for _, h in holdings.iterrows():
        diff = h.get("diff")
        ibkr_qty = h.get("ibkr_qty")
        if pd.isna(ibkr_qty):
            diff_cell = "<td class='num muted'>—</td>"
            ibkr_cell = "<td class='num muted'>—</td>"
        elif abs(diff) < 1e-6:
            diff_cell = "<td class='num' style='color:var(--green)'>✓</td>"
            ibkr_cell = f"<td class='num'>{fmt_qty(ibkr_qty)}</td>"
        else:
            sign = "+" if diff > 0 else ""
            diff_cell = f"<td class='num' style='color:var(--orange)'>{sign}{fmt_qty(diff)}</td>"
            ibkr_cell = f"<td class='num'>{fmt_qty(ibkr_qty)}</td>"
        holdings_rows.append(
            "<tr>"
            f"<td>{html.escape(str(h.get('symbol','')))}</td>"
            f"<td class='num'><strong>{fmt_qty(h.get('qty'))}</strong></td>"
            f"{ibkr_cell}"
            f"{diff_cell}"
            f"<td class='num'>{fmt_num(h.get('basis_usd'))}</td>"
            f"<td class='num'>{fmt_num(h.get('basis_eur'))}</td>"
            f"<td class='num muted'>{int(h.get('lots', 0))}</td>"
            "</tr>"
        )
    holdings_html = "\n".join(holdings_rows) or "<tr><td colspan='7' class='muted'>No current holdings.</td></tr>"

    total_pnl_eur = annual["realized_pnl_eur"].sum() if not annual.empty else 0.0
    total_closed = int(annual["closed_trades"].sum()) if not annual.empty else 0
    total_open_symbols = open_df["symbol"].nunique() if not open_df.empty else 0
    open_basis_eur = open_df["basis_eur_remaining"].sum(skipna=True) if not open_df.empty else 0.0
    sources_html = "".join(f"<li><code>{html.escape(s)}</code></li>" for s in sources)

    return render_report(
        "pnl.html",
        css_files=["css/pnl.css"],
        js_files=["js/pnl.js"],
        account=account,
        method=method,
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        annual_count=len(annual),
        total_closed=total_closed,
        total_pnl_eur=total_pnl_eur,
        total_open_symbols=total_open_symbols,
        open_basis_eur=open_basis_eur,
        annual_rows=annual_html,
        holdings_rows=holdings_html,
        reconcile_date=reconcile_date,
        has_ibkr=has_ibkr,
        perf=perf,
        perf_annual_rows=perf_annual_html,
        perf_monthly_rows=perf_monthly_html,
        top_symbols_rows=top_symbols_html,
        bottom_symbols_rows=bottom_symbols_html,
        category_rows=category_html,
        hold_period_rows=hold_period_html,
        close_type_rows=close_type_html,
        open_rows=open_html_str,
        closed_rows=closed_html,
        all_trades_rows=all_trades_html,
        sources_list=sources_html,
        equity_curve_svg=equity_curve_svg,
        drawdown_cards=drawdown_cards_html,
        fx_decomp_rows=fx_decomp_html,
        distribution_rows=distribution_html,
        has_equity_curve=has_equity_curve,
    )


# ---------- Public API for the web UI ----------

def _empty_report_html(account_code: str, kind: str) -> str:
    """Friendly placeholder HTML when the DB has no data for the account yet."""
    name = ACCOUNT_ALIASES.get(account_code, account_code)
    return render("empty_report.html", kind=kind, account=name)


def build_pnl_html(account_code: str, method: str = "FIFO") -> str:
    """
    Compute the full P&L pipeline directly from the SQLite DB and render the report.
    Returns the HTML string, or a 'no data' placeholder if the DB is empty for this account.
    """
    method = method.upper()
    conn = _db.connect()
    _db.init_schema(conn)
    df = _db.get_trades(conn, account_code)
    if df.empty:
        conn.close()
        return _empty_report_html(account_code, "P&L")

    df = dedupe(df)
    ca_actions = _group_ca_actions(_db.get_corporate_actions(conn, account_code))

    xf_df = _db.get_transfers(conn, account_code)
    known_accounts = _db.get_known_accounts(conn)
    transfers = []
    for xf in xf_df.to_dict("records"):
        if xf.get("direction") == "IN" and xf.get("xfer_account") in known_accounts:
            continue
        transfers.append(xf)

    reconcile_snapshots = _db.get_open_positions_snapshots(conn, account_code)
    reconcile_snapshots.sort(key=lambda t: t[0])

    sources_rows = conn.execute(
        "SELECT kind, path FROM source_files WHERE account_code = ? ORDER BY ingested_at",
        (account_code,),
    ).fetchall()
    sources = [f"[{r['kind']}] {r['path']}" for r in sources_rows]
    conn.close()

    closed, open_df = match_lots(
        df, ca_actions=ca_actions, transfers=transfers,
        reconcile_snapshots=reconcile_snapshots, method=method,
    )
    open_positions_latest = reconcile_snapshots[-1][1] if reconcile_snapshots else pd.DataFrame()
    reconcile_date = reconcile_snapshots[-1][0] if reconcile_snapshots else None
    annual = annual_summary(closed)

    account_name = ACCOUNT_ALIASES.get(account_code, account_code)
    return render_html(
        annual, closed, open_df, df,
        open_positions_latest, reconcile_date,
        account_name, method, sources,
    )


# ---------- CLI ----------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Annual P&L with lot matching (FIFO/LIFO).")
    ap.add_argument("-a", "--account", required=True, type=_resolve_account,
                    help="Account: P|personal or B|business")
    ap.add_argument("--method", default="FIFO", choices=["FIFO", "LIFO", "fifo", "lifo"],
                    help="Lot matching method (default FIFO)")
    args = ap.parse_args(argv)
    code = ACCOUNT_LETTER[args.account]
    html = build_pnl_html(code, method=args.method)
    sys.stdout.write(html)
    return 0


if __name__ == "__main__":
    sys.exit(main())
