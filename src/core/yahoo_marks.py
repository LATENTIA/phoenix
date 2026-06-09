"""
Yahoo Finance closing-price fetcher used to populate the `year_end_marks` table
for the Belgian CGT 2026+ basis reset (article: KPMG, July 2025).

Uses the public chart endpoint (`query1.finance.yahoo.com/v8/finance/chart/<symbol>`).
No API key required, no extra dependency beyond `requests` (already a dep).

Public API:
    fetch_close(symbol, date_iso)        -> float | None
    fetch_many(symbols, date_iso)        -> {"hits": [...], "misses": [...]}

Both functions silently return None / record a "miss" for unknown tickers
(delisted, OTC pinks, options notation, forex pairs, etc.). Callers should
fall back to manual entry for those.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Iterable, Optional

import requests


log = logging.getLogger("ibkr.yahoo_marks")

# Yahoo's public chart endpoint. We hit it as a browser would.
_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
}
_REQUEST_TIMEOUT = 15  # seconds
_INTER_REQUEST_DELAY = 0.20  # be polite — ~5 req/s upper bound


def _to_unix(date_iso: str) -> int:
    """ISO date 'YYYY-MM-DD' → Unix timestamp at 00:00 UTC."""
    return int(datetime.strptime(date_iso, "%Y-%m-%d").timestamp())


def fetch_close(symbol: str, date_iso: str) -> Optional[float]:
    """Fetch the closing price for `symbol` on `date_iso` (or the closest prior
    trading day if the date itself wasn't a trading day — common for 12-31 when
    markets close mid-day or the date falls on a weekend).

    Returns None if the symbol is unknown to Yahoo, the request fails, or no
    price is available in the queried window.
    """
    if not symbol or not date_iso:
        return None

    # Pull a 14-day window ending on date_iso, take the last close in range.
    # 14 days handles long weekends, holidays, and weekend year-ends.
    end_ts = _to_unix(date_iso) + 86400  # include the target day fully
    start_ts = end_ts - 14 * 86400

    try:
        resp = requests.get(
            _CHART_URL.format(symbol=symbol),
            params={
                "period1": start_ts,
                "period2": end_ts,
                "interval": "1d",
                "events": "history",
            },
            headers=_HEADERS,
            timeout=_REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        log.warning(f"yahoo: {symbol} on {date_iso}: network error: {e}")
        return None

    if resp.status_code == 404:
        return None  # symbol not on Yahoo (delisted, OTC pink, etc.)
    if resp.status_code >= 400:
        log.warning(f"yahoo: {symbol} on {date_iso}: HTTP {resp.status_code}")
        return None

    try:
        payload = resp.json()
    except ValueError:
        log.warning(f"yahoo: {symbol} on {date_iso}: response is not JSON")
        return None

    chart = (payload.get("chart") or {})
    err = chart.get("error")
    if err:
        # e.g. {'code': 'Not Found', 'description': 'No data found, ...'}
        return None
    results = chart.get("result") or []
    if not results:
        return None

    block = results[0]
    timestamps = block.get("timestamp") or []
    quote = ((block.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    if not timestamps or not closes:
        return None

    # Find the last close on or before the target date.
    target = _to_unix(date_iso)
    best = None
    for ts, cl in zip(timestamps, closes):
        if cl is None:
            continue
        if ts <= target + 86400:  # include the target day
            best = cl
    if best is None:
        return None
    try:
        return float(best)
    except (TypeError, ValueError):
        return None


def fetch_many(
    symbols: Iterable[str], date_iso: str, *, log_progress=None,
) -> dict:
    """Fetch closes for a batch of symbols. Returns
        {"hits": [{"symbol", "close_price", "currency", "source": "yahoo"}, ...],
         "misses": ["SYM1", "SYM2", ...]}
    Missed symbols typically need manual entry (delistings, options, forex).
    Polite delay between requests so we don't hammer Yahoo.
    """
    hits: list[dict] = []
    misses: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        symbol = (raw or "").strip()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)

        price = fetch_close(symbol, date_iso)
        if price is not None:
            hits.append({
                "symbol": symbol,
                "date": date_iso,
                "close_price": price,
                "currency": "USD",  # Yahoo returns native ccy; assume USD for our scope
                "source": "yahoo",
            })
            if log_progress:
                log_progress(f"  [ok]   {symbol}: {price:.4f}")
        else:
            misses.append(symbol)
            if log_progress:
                log_progress(f"  [miss] {symbol}: no quote")
        time.sleep(_INTER_REQUEST_DELAY)

    return {"hits": hits, "misses": misses}


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("usage: python -m core.yahoo_marks YYYY-MM-DD SYM1 SYM2 ...")
        sys.exit(1)
    date_iso = sys.argv[1]
    syms = sys.argv[2:]
    result = fetch_many(syms, date_iso, log_progress=print)
    print(f"\nhits: {len(result['hits'])}, misses: {len(result['misses'])}")
    for h in result["hits"]:
        print(f"  {h['symbol']:<8} {h['close_price']:>10,.4f} {h['currency']}")
    if result["misses"]:
        print("misses:", ", ".join(result["misses"]))
