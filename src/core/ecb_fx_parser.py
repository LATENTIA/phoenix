"""
ECB EUR/USD reference rate lookup, with persistent on-disk cache.

- First call loads from `downloaded/ecb_rates.csv` if present.
- If missing, downloads the full ECB historical archive (all daily rates since 1999)
  and saves it to the local CSV.
- `refresh_from_ecb()` force-refreshes the local CSV (call this after each Flex
  download so rates stay current).

Public API:
  get_eur_usd_rate_for_day("YYYY-MM-DD") -> float | None
  refresh_from_ecb() -> int   # returns rate count written
"""

import csv
import io
import zipfile
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional

import requests


ECB_HIST_ZIP_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"
LOCAL_CACHE_PATH = Path("downloaded/ecb_rates.csv")


def _download_ecb_history() -> Dict[str, float]:
    """Download and parse the ECB historical ZIP into {date: rate}."""
    resp = requests.get(
        ECB_HIST_ZIP_URL,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=60,
    )
    resp.raise_for_status()

    rates: Dict[str, float] = {}
    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        name = z.namelist()[0]
        with z.open(name) as f:
            text = io.TextIOWrapper(f, encoding="utf-8")
            reader = csv.reader(text)
            header = next(reader)
            try:
                usd_idx = header.index("USD")
            except ValueError:
                return rates
            for row in reader:
                if not row or len(row) <= usd_idx:
                    continue
                date = row[0].strip()
                val = row[usd_idx].strip()
                if not date or not val or val == "N/A":
                    continue
                try:
                    rates[date] = float(val)
                except ValueError:
                    continue
    return rates


def _save_local_cache(rates: Dict[str, float], path: Path = LOCAL_CACHE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "eur_usd"])
        for date in sorted(rates):
            w.writerow([date, f"{rates[date]:.6f}"])


def _load_local_cache(path: Path = LOCAL_CACHE_PATH) -> Dict[str, float]:
    rates: Dict[str, float] = {}
    if not path.exists():
        return rates
    with path.open("r", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return rates
        for row in reader:
            if len(row) < 2:
                continue
            try:
                rates[row[0].strip()] = float(row[1])
            except (ValueError, IndexError):
                continue
    return rates


def refresh_from_ecb(
    path: Path = LOCAL_CACHE_PATH,
    *,
    force: bool = False,
    max_age_hours: float = 12.0,
) -> int:
    """Refresh the local ECB rate cache. Skips the HTTP fetch entirely if the
    cache file was modified less than `max_age_hours` ago — the ECB only
    publishes a new rate ~once per business day, so refreshing more often is
    pure waste. Pass `force=True` to bypass the freshness gate.

    Returns the number of rates now in the cache (download path) or 0 when
    the gate skipped the fetch.
    """
    if not force and path.exists():
        import time as _time
        age_hours = (_time.time() - path.stat().st_mtime) / 3600.0
        if age_hours < max_age_hours:
            print(f"[ecb] cache is {age_hours:.1f}h old "
                  f"(< {max_age_hours:.0f}h threshold) — skipping refresh")
            return 0

    print("[ecb] refreshing rates from ECB...")
    rates = _download_ecb_history()
    _save_local_cache(rates, path)
    _load_ecb_history.cache_clear()  # invalidate in-process cache
    print(f"[ecb] saved {len(rates)} rates to {path}")
    return len(rates)


@lru_cache(maxsize=1)
def _load_ecb_history() -> Dict[str, float]:
    """
    Return the full {date: rate} dict. Priority:
      1) local CSV cache (downloaded/ecb_rates.csv)
      2) fresh download from ECB (and write to local CSV)
    """
    rates = _load_local_cache()
    if rates:
        print(f"[ecb] loaded {len(rates)} rates from local cache ({LOCAL_CACHE_PATH})")
        return rates
    print("[ecb] local cache missing, downloading from ECB...")
    rates = _download_ecb_history()
    _save_local_cache(rates)
    print(f"[ecb] saved {len(rates)} rates to {LOCAL_CACHE_PATH}")
    return rates


def get_eur_usd_rate_for_day(target_date: str) -> Optional[float]:
    """
    Return the ECB EUR/USD reference rate for a given date ('YYYY-MM-DD').
    Weekends / holidays return None (no rate published).
    """
    datetime.strptime(target_date, "%Y-%m-%d")  # validate format
    return _load_ecb_history().get(target_date)


def sync_to_db_incremental(conn) -> dict:
    """Fetch fresh ECB rates and insert only the dates not already in
    `fx_rates`. Idempotent and safe to run repeatedly. Returns:

        {"max_before": str|None, "max_after": str, "row_count": int,
         "new_rows": int, "fetched": int}

    Designed for the weekly scheduler + the /fx/refresh route.

    Why incremental: the ECB ZIP is small (~50 KB) but we don't want to
    rewrite 7000 rows of the fx_rates table every week.
    """
    # 1. What's the latest date we already have in DB?
    row = conn.execute("SELECT MAX(date), COUNT(*) FROM fx_rates").fetchone()
    max_before, count_before = (row[0], row[1]) if row else (None, 0)

    # 2. Fetch full ECB history (cheap — small ZIP).
    rates = _download_ecb_history()

    # 3. Filter to NEW dates only.
    if max_before:
        new_rates = {d: r for d, r in rates.items() if d > max_before}
    else:
        new_rates = rates

    # 4. Insert (INSERT OR IGNORE so a re-run is a no-op).
    if new_rates:
        conn.executemany(
            "INSERT OR IGNORE INTO fx_rates (date, eur_usd) VALUES (?, ?)",
            [(d, float(r)) for d, r in new_rates.items()],
        )
        conn.commit()

    # 5. Refresh the in-process cache so subsequent rate lookups see the new
    # data without restarting the process. We also rewrite the local CSV so
    # the file stays in sync (used as a fallback if the DB is wiped).
    _save_local_cache(rates)
    _load_ecb_history.cache_clear()

    # 6. Re-query for the up-to-date count + max.
    row = conn.execute("SELECT MAX(date), COUNT(*) FROM fx_rates").fetchone()
    max_after, count_after = (row[0], row[1]) if row else (None, 0)

    return {
        "max_before": max_before,
        "max_after": max_after,
        "row_count": count_after,
        "new_rows": count_after - count_before,
        "fetched": len(rates),
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "refresh":
        refresh_from_ecb()
    else:
        for d in ("2026-03-10", "2025-06-15", "2024-01-02"):
            rate = get_eur_usd_rate_for_day(d)
            if rate is None:
                print(f"No EUR/USD reference rate for {d} (weekend/holiday).")
            else:
                print(f"EUR/USD on {d}: {rate}")
