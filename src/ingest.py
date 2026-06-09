"""
Scan downloaded/ and feed the SQLite database.

Idempotent: a source file is only re-ingested if its size or mtime has changed
since last time. Re-ingestion replaces all rows from that file.

Usage:
    python ingest.py             # ingest both accounts
    python ingest.py -a P        # personal only
    python ingest.py --force     # re-ingest everything
"""

import argparse
import re
import sys
from pathlib import Path

from core import db
from core import ecb_fx_parser
from core import loaders


# Project root is one level above src/. The downloaded/ dir lives at the
# project root for backwards compat with non-Docker installs. Docker
# overrides this via PHOENIX_DOWNLOADED_DIR at runtime.
import os as _os
DOWNLOAD_DIR = Path(
    _os.environ.get("PHOENIX_DOWNLOADED_DIR")
    or (Path(__file__).resolve().parent.parent / "downloaded")
)


def _accounts_map() -> dict[str, str]:
    """{code: name} for every account in the DB. Falls back to the legacy P/B if DB is unreachable."""
    try:
        conn = db.connect()
        db.init_schema(conn)
        rows = db.list_accounts(conn)
        conn.close()
        if rows:
            return {r["code"]: r["name"] for r in rows}
    except Exception:
        pass
    return {"P": "personal", "B": "business"}


def _files_for_account(account_name: str) -> list[tuple[str, Path]]:
    """
    Per-account folder layout:
        downloaded/<account>/*.xml
        downloaded/<account>/*.csv
    Returns (kind, path) tuples in chronological order.
    """
    files: list[tuple[str, Path]] = []
    acc_dir = DOWNLOAD_DIR / account_name
    if not acc_dir.exists():
        return files

    for p in sorted(acc_dir.glob("*.xml")):
        files.append(("xml", p))
    for p in sorted(acc_dir.glob("*.csv")):
        if p.name.endswith("_reconciled.csv"):
            continue
        files.append(("csv", p))
    return files


# Back-compat aliases — code that still imports ACCOUNTS / ACCOUNT_LETTER continues to work
ACCOUNTS = _accounts_map()
ACCOUNT_LETTER = {n: c for c, n in ACCOUNTS.items()}


def _ibkr_account_from(path: Path) -> str | None:
    m = re.match(r"(U\d+)_\d{8}_\d{8}", path.stem)
    return m.group(1) if m else None


def ingest_file(conn, code: str, account_name: str, kind: str, path: Path) -> dict:
    """Ingest a single file. Returns counts (incl. dividends + WHT)."""
    counts = {"trades": 0, "ca": 0, "transfers": 0, "open_positions": 0,
              "dividends": 0, "withholding_tax": 0}

    ibkr_account = _ibkr_account_from(path)
    source_id = db.upsert_source(conn, path, code, kind, ibkr_account)

    if kind == "xml":
        df_trades = loaders.load_flex_xml(path)
        counts["trades"] = db.insert_trades(conn, source_id, code, df_trades)
        df_op, as_of = loaders.load_open_positions_xml(path)
        counts["open_positions"] = db.insert_open_positions(conn, source_id, code, as_of, df_op)
        # Dividends & WHT live in <CashTransactions> when the Flex Query is
        # configured to include that section. Empty DataFrames otherwise.
        df_div = loaders.load_dividends_xml(path)
        counts["dividends"] = db.insert_dividends(conn, source_id, code, df_div)
        df_wht = loaders.load_withholding_xml(path)
        counts["withholding_tax"] = db.insert_withholding(conn, source_id, code, df_wht)
    else:  # csv
        df_trades = loaders.load_statement_csv(path)
        counts["trades"] = db.insert_trades(conn, source_id, code, df_trades)
        df_ca = loaders.load_corporate_actions_csv(path)
        counts["ca"] = db.insert_corporate_actions(conn, source_id, code, df_ca)
        df_xf = loaders.load_transfers_csv(path)
        counts["transfers"] = db.insert_transfers(conn, source_id, code, df_xf)
        df_op, as_of = loaders.load_open_positions_csv(path)
        counts["open_positions"] = db.insert_open_positions(conn, source_id, code, as_of, df_op)
        df_div = loaders.load_dividends_csv(path)
        counts["dividends"] = db.insert_dividends(conn, source_id, code, df_div)
        df_wht = loaders.load_withholding_csv(path)
        counts["withholding_tax"] = db.insert_withholding(conn, source_id, code, df_wht)

    conn.commit()
    return counts


def ingest_account(conn, code: str, *, force: bool = False, log=print) -> dict:
    """Ingest all sources for one account. Returns total counts and per-file results."""
    accounts = _accounts_map()
    if code not in accounts:
        raise ValueError(f"Unknown account code {code!r}. Known: {sorted(accounts)}")
    account_name = accounts[code]
    files = _files_for_account(account_name)
    summary = {"account": account_name, "scanned": len(files), "ingested": 0,
               "skipped": 0,
               "totals": {"trades": 0, "ca": 0, "transfers": 0, "open_positions": 0,
                          "dividends": 0, "withholding_tax": 0},
               "files": []}

    for kind, path in files:
        if not force and not db.needs_ingest(conn, path):
            summary["skipped"] += 1
            summary["files"].append({"path": path.name, "status": "unchanged"})
            continue
        counts = ingest_file(conn, code, account_name, kind, path)
        summary["ingested"] += 1
        for k, v in counts.items():
            summary["totals"][k] += v
        summary["files"].append({"path": path.name, "status": "ingested", **counts})
        log(f"  [{kind}] {path.name}  trades={counts['trades']} "
            f"ca={counts['ca']} xfer={counts['transfers']} op={counts['open_positions']} "
            f"div={counts['dividends']} wht={counts['withholding_tax']}")

    return summary


def ingest_fx_rates(conn, log=print) -> int:
    """Mirror the local ECB cache CSV into the fx_rates table."""
    rates = ecb_fx_parser._load_local_cache()
    if not rates:
        log("  [fx] no local ECB cache yet — run ibkr_flex.py to refresh")
        return 0
    n = db.upsert_fx_rates(conn, rates)
    conn.commit()
    log(f"  [fx] {n} rates synced from {ecb_fx_parser.LOCAL_CACHE_PATH}")
    return n


def ingest_all(account: str | None = None, *, force: bool = False, log=print) -> dict:
    """Public entry point. Initializes DB if needed, ingests files, returns summary."""
    conn = db.connect()
    db.init_schema(conn)
    accounts = _accounts_map()

    log(f"[ingest] DB at {db.DB_PATH}")

    summaries = {}
    codes = [account] if account else list(accounts.keys())
    for code in codes:
        if code not in accounts:
            log(f"[ingest] skipping unknown account code: {code}")
            continue
        log(f"[ingest] account={accounts[code]} (-a {code})")
        summaries[accounts[code]] = ingest_account(conn, code, force=force, log=log)

    log("[ingest] FX rates...")
    fx_count = ingest_fx_rates(conn, log=log)

    s = db.status(conn)
    log(f"[ingest] DB now: trades={s['trades']} ca={s['corporate_actions']} "
        f"xfer={s['transfers']} op_snapshots={s['open_positions']} fx={s['fx_rates']}")
    conn.close()
    return {"accounts": summaries, "fx_rates": fx_count, "status": s}


def _resolve_account(value: str) -> str:
    v = value.upper()
    if v in ACCOUNTS:
        return v
    rev = {n.lower(): c for c, n in ACCOUNTS.items()}
    if value.lower() in rev:
        return rev[value.lower()]
    raise argparse.ArgumentTypeError(f"Unknown account: {value}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Ingest CSV/XML files in downloaded/ into SQLite.")
    ap.add_argument("-a", "--account", type=_resolve_account, default=None,
                    help="Restrict to one account (P|B|personal|business)")
    ap.add_argument("--force", action="store_true",
                    help="Re-ingest all files even if unchanged")
    args = ap.parse_args(argv)
    ingest_all(args.account, force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
