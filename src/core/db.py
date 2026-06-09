"""
SQLite layer for the IBKR parser.

One file: data.db (project root). Stdlib only — no extra deps.

Tables:
  source_files                — every CSV/XML we've ingested (mtime+size for change detection)
  trades                      — normalized buy/sell rows from XML+CSV
  corporate_actions           — splits, delistings, mergers
  transfers                   — share movements between accounts
  open_positions_snapshots    — IBKR's reported open positions per statement date
  fx_rates                    — ECB EUR/USD daily
"""

import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd


# DB location.
#   Default: <project root>/data.db (backwards compatible with every install
#            that ran the app outside Docker).
#   Override via PHOENIX_DB_PATH for containerised / EC2 deploys, where the
#   DB should live on a mounted volume outside the container, not inside the
#   project tree. The Dockerfile sets this to /app/data/data.db.
#
# We never auto-create the parent directory here. That's app.py's job at
# startup (see _ensure_data_dirs) so a typo'd env var fails loud rather than
# silently creating a wrong directory somewhere.
DB_PATH = Path(
    os.environ.get("PHOENIX_DB_PATH")
    # src/core/db.py → parent.parent.parent = project root.
    # data.db lives at the project root for legacy non-Docker installs.
    # Docker always overrides this via the PHOENIX_DB_PATH env var.
    or (Path(__file__).resolve().parent.parent.parent / "data.db")
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT UNIQUE NOT NULL,                -- e.g. 'personal', 'business', 'work'
    code          TEXT UNIQUE NOT NULL,                -- short code: P, B, W, etc. (1-4 chars)
    type          TEXT NOT NULL CHECK(type IN ('personal','business')),
    flex_token    TEXT,                                -- IBKR Flex Web Service token
    queries_json  TEXT NOT NULL DEFAULT '{}',          -- {"ytd":"<query-id>","mtd":"..."}
    created_at    TEXT NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS source_files (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    path          TEXT UNIQUE NOT NULL,
    account_code  TEXT NOT NULL,
    kind          TEXT NOT NULL,
    ibkr_account  TEXT,
    size          INTEGER NOT NULL,
    mtime         REAL NOT NULL,
    ingested_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       INTEGER REFERENCES source_files(id) ON DELETE CASCADE,
    account_code    TEXT NOT NULL,
    trade_id        TEXT,
    datetime        TEXT,
    trade_date      TEXT,
    symbol          TEXT,
    description     TEXT,
    asset_category  TEXT,
    currency        TEXT,
    quantity        REAL,
    trade_price     REAL,
    proceeds_usd    REAL,
    commission_usd  REAL,
    -- Manual entry support. is_manual=1 means a row the user added through
    -- the dashboard (not from an IBKR statement). Manual rows have source_id
    -- NULL and survive re-ingest (no source file deletes them).
    is_manual       INTEGER NOT NULL DEFAULT 0,
    -- Normalised asset class for tax-rule branching downstream.
    -- IBKR's `asset_category` carries STK/OPT/FUT/CRYPTO/CASH; we collapse
    -- those to 'stock' or 'crypto' here for simpler downstream logic.
    asset_class     TEXT NOT NULL DEFAULT 'stock'
                    CHECK (asset_class IN ('stock', 'crypto'))
);
CREATE INDEX IF NOT EXISTS idx_trades_account_date ON trades(account_code, trade_date);
CREATE INDEX IF NOT EXISTS idx_trades_symbol       ON trades(symbol);
-- Row-level dedup: same trade across multiple sources (e.g. YTD XML + yearly CSV)
-- is identified by an exact match on date + time + all numeric values.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_trades ON trades (
    account_code,
    COALESCE(datetime, ''),
    COALESCE(symbol, ''),
    COALESCE(quantity, 0),
    COALESCE(trade_price, 0),
    COALESCE(proceeds_usd, 0),
    COALESCE(commission_usd, 0)
);

CREATE TABLE IF NOT EXISTS corporate_actions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id            INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
    account_code         TEXT NOT NULL,
    date                 TEXT,
    type                 TEXT,
    symbol               TEXT,
    description          TEXT,
    ratio_old            REAL,
    ratio_new            REAL,
    per_share            REAL,
    quantity             REAL,
    proceeds_usd         REAL,
    realized_pnl_usd_ibkr REAL
);
CREATE INDEX IF NOT EXISTS idx_ca_account_date ON corporate_actions(account_code, date);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_corporate_actions ON corporate_actions (
    account_code,
    COALESCE(date, ''),
    COALESCE(type, ''),
    COALESCE(symbol, ''),
    COALESCE(description, ''),
    COALESCE(quantity, 0),
    COALESCE(proceeds_usd, 0)
);

CREATE TABLE IF NOT EXISTS transfers (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id         INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
    account_code      TEXT NOT NULL,
    date              TEXT,
    symbol            TEXT,
    direction         TEXT,
    quantity          REAL,
    market_value_usd  REAL,
    per_share_usd     REAL,
    asset_category    TEXT,
    xfer_account      TEXT
);
CREATE INDEX IF NOT EXISTS idx_transfers_account_date ON transfers(account_code, date);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_transfers ON transfers (
    account_code,
    COALESCE(date, ''),
    COALESCE(symbol, ''),
    COALESCE(direction, ''),
    COALESCE(quantity, 0),
    COALESCE(market_value_usd, 0),
    COALESCE(xfer_account, '')
);

CREATE TABLE IF NOT EXISTS open_positions_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
    account_code  TEXT NOT NULL,
    as_of         TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    quantity      REAL NOT NULL,
    currency      TEXT
);
CREATE INDEX IF NOT EXISTS idx_op_account_asof ON open_positions_snapshots(account_code, as_of);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_open_positions ON open_positions_snapshots (
    account_code,
    as_of,
    symbol,
    quantity
);

CREATE TABLE IF NOT EXISTS fx_rates (
    date     TEXT PRIMARY KEY,
    eur_usd  REAL NOT NULL
);

-- Read-only share links. Each row hands out a long random token that grants
-- view-only access to ONE account's reports, scoped to a chosen tab list.
-- The link IS the credential: anyone with the URL gets in (no basic auth).
-- `revoked=1` disables a link immediately; DELETE wipes it without history.
CREATE TABLE IF NOT EXISTS share_links (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    token            TEXT UNIQUE NOT NULL,        -- 32 bytes URL-safe (~44 chars)
    account_code     TEXT NOT NULL,
    allowed_tabs     TEXT NOT NULL,               -- CSV: 'tob,pnl,dividends'
    label            TEXT,
    created_at       TEXT NOT NULL,
    expires_at       TEXT,                        -- ISO 8601, NULL = never
    revoked          INTEGER NOT NULL DEFAULT 0,
    last_accessed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_share_links_token ON share_links(token);

-- Year-end marks for the Belgian CGT 2026+ basis reset.
-- One row per (symbol, date). Source records where the price came from
-- so the user can audit / override stale or manually entered values.
CREATE TABLE IF NOT EXISTS year_end_marks (
    symbol      TEXT NOT NULL,
    date        TEXT NOT NULL,           -- ISO 'YYYY-MM-DD' (typically 2025-12-31)
    close_price REAL NOT NULL,           -- closing price in `currency`
    currency    TEXT NOT NULL DEFAULT 'USD',
    source      TEXT NOT NULL,           -- 'ibkr', 'yahoo', 'manual'
    fetched_at  TEXT NOT NULL,           -- ISO timestamp when this row was last written
    note        TEXT,                    -- optional free-form (e.g. "delisted, no quote")
    PRIMARY KEY (symbol, date)
);

-- Cash dividends received. CSV "Dividends" section and (when present)
-- XML <CashTransactions type="Dividends">. Belgian individuals must declare
-- foreign dividends and pay 30% precompte mobilier; this table is the input.
CREATE TABLE IF NOT EXISTS dividends (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
    account_code    TEXT NOT NULL,
    pay_date        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    isin            TEXT,
    description     TEXT,
    currency        TEXT NOT NULL DEFAULT 'USD',
    amount          REAL NOT NULL,        -- gross dividend in `currency`
    per_share       REAL,
    dividend_type   TEXT                  -- "Ordinary", "Qualified", "PIL", etc.
);
CREATE INDEX IF NOT EXISTS idx_div_account_date ON dividends(account_code, pay_date);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_dividends ON dividends (
    account_code,
    pay_date,
    symbol,
    COALESCE(amount, 0),
    COALESCE(description, '')
);

-- Foreign withholding tax taken at source (typically -15% for US dividends
-- under the Belgium-US treaty when W-8BEN is filed; -30% otherwise).
CREATE TABLE IF NOT EXISTS withholding_tax (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
    account_code    TEXT NOT NULL,
    pay_date        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    isin            TEXT,
    description     TEXT,
    currency        TEXT NOT NULL DEFAULT 'USD',
    amount          REAL NOT NULL,        -- typically negative (tax taken)
    per_share       REAL,                 -- per-share dividend this WHT pairs with
    source_country  TEXT,                 -- e.g. "US", "NL" (parsed from description)
    code            TEXT
);
CREATE INDEX IF NOT EXISTS idx_wht_account_date ON withholding_tax(account_code, pay_date);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_withholding_tax ON withholding_tax (
    account_code,
    pay_date,
    symbol,
    COALESCE(amount, 0),
    COALESCE(description, '')
);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON;")
    # WAL gives readers + writers concurrency without blocking. Safe and fast;
    # creates `data.db-wal` and `data.db-shm` siblings (already gitignored).
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")  # WAL-safe, faster than FULL
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate_add_manual_columns(conn)
    _migrate_canonicalise_source_paths(conn)
    conn.commit()


def _migrate_canonicalise_source_paths(conn: sqlite3.Connection) -> None:
    """One-time backfill: convert absolute source paths to the canonical
    (host-portable) form. Also collapses any duplicates created before this
    rule existed (e.g. the same file ingested once via Windows path, once
    via Linux path).

    Idempotent — safe to run on every startup. The expensive group/dedup
    step is gated on whether anything actually needs converting.
    """
    rows = conn.execute(
        "SELECT id, path, account_code, kind FROM source_files"
    ).fetchall()
    if not rows:
        return

    fk_tables = ["trades", "corporate_actions", "transfers",
                 "open_positions_snapshots", "dividends", "withholding_tax"]

    # Bucket every row by its canonical form so we can detect duplicates.
    # We DON'T early-return when no conversions are needed — the recovery
    # step further down still has work to do on already-canonical-but-broken
    # paths (over-stripped basenames).
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        canon = _canonical_source_path(r["path"])
        groups.setdefault((r["account_code"], r["kind"], canon), []).append(dict(r))

    converted = 0
    consolidated = 0
    for (acct, kind, canon), bucket in groups.items():
        # Pick the canonical row to keep. Prefer a row whose path is ALREADY
        # canonical; otherwise the one with the highest id (most recent).
        bucket.sort(key=lambda r: (r["path"] == canon, r["id"]), reverse=True)
        keeper = bucket[0]
        # Repoint the keeper's path to canonical if it isn't already.
        if keeper["path"] != canon:
            try:
                conn.execute(
                    "UPDATE source_files SET path = ? WHERE id = ?",
                    (canon, keeper["id"]),
                )
                converted += 1
            except sqlite3.IntegrityError:
                # A row with this canonical path already exists. Skip; it
                # gets handled as a duplicate in the next loop.
                pass

        # Anything else in the bucket is a duplicate of the keeper.
        for dup in bucket[1:]:
            for tbl in fk_tables:
                conn.execute(
                    f"UPDATE OR IGNORE {tbl} SET source_id = ? WHERE source_id = ?",
                    (keeper["id"], dup["id"]),
                )
            conn.execute("DELETE FROM source_files WHERE id = ?", (dup["id"],))
            consolidated += 1

    # Recovery step: an earlier (non-idempotent) version of this migration
    # over-stripped some paths down to just the basename. Restore the
    # subdir by prepending the account name (`business/`, `personal/`, ...)
    # to any source row whose path is a bare filename. Idempotent — paths
    # that already have a `/` or are synthetic (`__manual:*`) are skipped.
    try:
        account_names = {
            row[0]: row[1]
            for row in conn.execute("SELECT code, name FROM accounts")
        }
    except sqlite3.OperationalError:
        # accounts table doesn't exist yet (very fresh DB) — nothing to do.
        account_names = {}

    restored = 0
    if account_names:
        # Find rows whose path is just a bare basename (no `/`) and isn't a
        # synthetic `__manual:...` marker. Note: SQL LIKE treats `_` as a
        # single-char wildcard, so we use substr() instead of LIKE for the
        # `__` prefix check.
        bare = conn.execute(
            "SELECT id, account_code, path FROM source_files "
            "WHERE path NOT LIKE '%/%' AND substr(path, 1, 2) != '__'"
        ).fetchall()
        for r in bare:
            subdir = account_names.get(r["account_code"])
            if not subdir:
                continue
            new_path = f"{subdir}/{r['path']}"
            try:
                conn.execute(
                    "UPDATE source_files SET path = ? WHERE id = ?",
                    (new_path, r["id"]),
                )
                restored += 1
            except sqlite3.IntegrityError:
                # A row with the restored path already exists (unlikely).
                pass

    if converted or consolidated or restored:
        import logging
        logging.getLogger("ibkr.db").info(
            f"source_files migration: converted {converted}, "
            f"consolidated {consolidated}, restored-subdir {restored}"
        )


def _migrate_add_manual_columns(conn: sqlite3.Connection) -> None:
    """Add is_manual + asset_class columns to trades for DBs created before
    those columns existed. Idempotent — does nothing on a fresh schema."""
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(trades)")}

    if "is_manual" not in existing_cols:
        conn.execute(
            "ALTER TABLE trades ADD COLUMN is_manual INTEGER NOT NULL DEFAULT 0"
        )

    if "asset_class" not in existing_cols:
        # SQLite can't add a NOT NULL column with a CHECK constraint via
        # ALTER TABLE, so add it permissive, backfill, then we live with the
        # constraint only being enforced on new inserts. The CHECK on the
        # CREATE TABLE above applies to brand-new tables; for old DBs the
        # application layer (insert_manual_trade) does the validation.
        conn.execute(
            "ALTER TABLE trades ADD COLUMN asset_class TEXT NOT NULL DEFAULT 'stock'"
        )
        # Backfill: IBKR's "CRYPTO" asset_category → our 'crypto'. Everything
        # else (STK, OPT, FUT, CASH) stays at the 'stock' default. The user
        # can recategorise manually if needed.
        conn.execute(
            "UPDATE trades SET asset_class='crypto' "
            "WHERE UPPER(COALESCE(asset_category, ''))='CRYPTO'"
        )


def reset_database(conn: sqlite3.Connection) -> dict:
    """
    Wipe every data table (keeps the schema). Returns row counts deleted.
    Source-file deletions CASCADE to trades/CA/transfers/open positions.
    """
    counts = {
        "source_files": conn.execute("SELECT COUNT(*) FROM source_files").fetchone()[0],
        "trades": conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0],
        "corporate_actions": conn.execute("SELECT COUNT(*) FROM corporate_actions").fetchone()[0],
        "transfers": conn.execute("SELECT COUNT(*) FROM transfers").fetchone()[0],
        "open_positions": conn.execute("SELECT COUNT(*) FROM open_positions_snapshots").fetchone()[0],
        "fx_rates": conn.execute("SELECT COUNT(*) FROM fx_rates").fetchone()[0],
    }
    # CASCADE handles trades/CA/transfers/op when source_files rows go away
    conn.execute("DELETE FROM source_files")
    conn.execute("DELETE FROM fx_rates")
    # Make sure children are clean even if a foreign-key was missing
    for table in ("trades", "corporate_actions", "transfers", "open_positions_snapshots"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    return counts


# ---------- Accounts CRUD ----------

import json as _json

def list_accounts(conn: sqlite3.Connection, only_active: bool = True) -> list[dict]:
    """All accounts, sorted by id."""
    sql = "SELECT * FROM accounts"
    if only_active:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY id"
    rows = []
    for r in conn.execute(sql):
        d = dict(r)
        d["queries"] = _json.loads(d.pop("queries_json") or "{}")
        rows.append(d)
    return rows


def get_account(conn: sqlite3.Connection, *, name: str | None = None,
                code: str | None = None, account_id: int | None = None) -> dict | None:
    """Look up an account by name OR code OR id. Returns dict with queries decoded, or None."""
    if account_id is not None:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    elif code is not None:
        row = conn.execute("SELECT * FROM accounts WHERE code = ?", (code,)).fetchone()
    elif name is not None:
        row = conn.execute("SELECT * FROM accounts WHERE name = ?", (name,)).fetchone()
    else:
        return None
    if row is None:
        return None
    d = dict(row)
    d["queries"] = _json.loads(d.pop("queries_json") or "{}")
    return d


def create_account(conn: sqlite3.Connection, *, name: str, code: str, type: str,
                   flex_token: str | None = None, queries: dict | None = None) -> int:
    """Create a new account. Raises sqlite3.IntegrityError on UNIQUE conflict."""
    if type not in ("personal", "business"):
        raise ValueError(f"type must be 'personal' or 'business', got {type!r}")
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO accounts (name, code, type, flex_token, queries_json, created_at)
           VALUES (?,?,?,?,?,?)""",
        (name, code, type, flex_token or None,
         _json.dumps(queries or {}),
         datetime.utcnow().isoformat()),
    )
    conn.commit()
    return cur.lastrowid


def update_account(conn: sqlite3.Connection, account_id: int, **fields) -> None:
    """Update an account's fields. Allowed: name, code, type, flex_token, queries (dict), is_active."""
    allowed = {"name", "code", "type", "flex_token", "is_active"}
    sets = []
    params: list = []
    for k, v in fields.items():
        if k == "queries":
            sets.append("queries_json = ?")
            params.append(_json.dumps(v or {}))
        elif k in allowed:
            sets.append(f"{k} = ?")
            params.append(v)
    if not sets:
        return
    params.append(account_id)
    conn.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id = ?", params)
    conn.commit()


def delete_account(conn: sqlite3.Connection, account_id: int) -> dict:
    """
    Delete an account AND all of its data rows from the DB
    (trades, CA, transfers, open positions, source-file records).
    Files in `downloaded/<account>/` are kept on disk untouched.
    Returns a dict of what was deleted (for the toast/log).
    """
    row = conn.execute("SELECT code, name FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if not row:
        return {"deleted": False}
    code = row["code"]
    counts = {
        "trades": conn.execute("SELECT COUNT(*) FROM trades WHERE account_code = ?", (code,)).fetchone()[0],
        "corporate_actions": conn.execute("SELECT COUNT(*) FROM corporate_actions WHERE account_code = ?", (code,)).fetchone()[0],
        "transfers": conn.execute("SELECT COUNT(*) FROM transfers WHERE account_code = ?", (code,)).fetchone()[0],
        "open_positions": conn.execute("SELECT COUNT(*) FROM open_positions_snapshots WHERE account_code = ?", (code,)).fetchone()[0],
        "source_files": conn.execute("SELECT COUNT(*) FROM source_files WHERE account_code = ?", (code,)).fetchone()[0],
    }
    # Source-file CASCADE handles most child rows; explicit deletes mop up any orphans.
    conn.execute("DELETE FROM source_files WHERE account_code = ?", (code,))
    for tbl in ("trades", "corporate_actions", "transfers", "open_positions_snapshots"):
        conn.execute(f"DELETE FROM {tbl} WHERE account_code = ?", (code,))
    conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    conn.commit()
    return {"deleted": True, "name": row["name"], "code": code, "counts": counts}


# ---------- Source-file tracking ----------
#
# `source_files.path` used to be the absolute path (`/app/data/downloaded/...`
# or `C:\Users\...\downloaded\...`). That made the UNIQUE constraint useless
# across host moves: the same file under a new mount point looked like a
# brand-new source, so re-ingesting produced duplicate rows.
#
# Fix: store paths CANONICAL — relative to the downloaded-dir root, with `/`
# separators. `business/file.csv` is identical whether the file lives at
# `C:\Users\...\downloaded\business\file.csv` or `/app/data/downloaded/...`
# so the UNIQUE constraint catches the duplicate at insert time.
#
# Synthetic paths (`__manual:B` for manual entries) are left untouched.
# Anything that can't be resolved under the downloaded dir falls back to
# its basename — which still gives the UNIQUE constraint something useful.

def _canonical_source_path(path) -> str:
    """Return a canonical, host-portable identifier for a source file.

    IDEMPOTENT — once a path has been canonicalised, calling this on the
    result returns it unchanged. (An earlier version stripped further on
    each pass, which was a bug.)

    Examples:

      Path('C:/.../downloaded/business/file.csv')  -> 'business/file.csv'
      Path('/app/data/downloaded/personal/x.xml')  -> 'personal/x.xml'
      Path('business/file.csv')                    -> 'business/file.csv'  (no-op)
      Path('__manual:B')                           -> '__manual:B'         (no-op)
      Path('/some/random/place/file.csv')          -> 'file.csv'           (last-resort)
    """
    s = str(path).replace("\\", "/")          # normalise Windows backslashes

    # Synthetic / non-filesystem paths pass through unchanged.
    if s.startswith("__"):
        return s

    # Already canonical (relative)? Detect by the absence of any absolute
    # path marker. Both `business/file.csv` (canonical) and `file.csv`
    # (basename-only fallback) pass this check, so re-running the migration
    # never strips further.
    is_unix_abs    = s.startswith("/")
    is_windows_abs = len(s) >= 2 and s[1] == ":"
    if not (is_unix_abs or is_windows_abs):
        return s

    # Absolute path. Strip everything up to and including the LAST occurrence
    # of `downloaded/`. Works for both host paths and container paths without
    # us having to know the actual downloaded-dir absolute path at this layer.
    marker = "downloaded/"
    idx = s.rfind(marker)
    if idx >= 0:
        return s[idx + len(marker):]

    # Last resort: basename. Survives host moves but loses subdir info, so
    # in-account uniqueness depends on filenames not colliding.
    return s.rsplit("/", 1)[-1]


def needs_ingest(conn: sqlite3.Connection, path: Path) -> bool:
    """True if file is new or has changed since last ingest."""
    if not path.exists():
        return False
    canon = _canonical_source_path(path)
    row = conn.execute(
        "SELECT size, mtime FROM source_files WHERE path = ?",
        (canon,),
    ).fetchone()
    if row is None:
        return True
    stat = path.stat()
    return row["size"] != stat.st_size or row["mtime"] != stat.st_mtime


def upsert_source(conn: sqlite3.Connection, path: Path,
                  account_code: str, kind: str,
                  ibkr_account: Optional[str] = None) -> int:
    """
    Insert or replace a source_files row. The stored path is a CANONICAL
    (host-portable) form, so the same file under different mounts/hosts
    never duplicates. See _canonical_source_path for the rules.
    """
    stat = path.stat()
    canon = _canonical_source_path(path)
    cur = conn.cursor()
    # Delete prior row for this canonical path (CASCADE removes child rows).
    cur.execute("DELETE FROM source_files WHERE path = ?", (canon,))
    cur.execute(
        """INSERT INTO source_files (path, account_code, kind, ibkr_account, size, mtime, ingested_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (canon, account_code, kind, ibkr_account, stat.st_size, stat.st_mtime,
         datetime.utcnow().isoformat()),
    )
    return cur.lastrowid


# ---------- Inserters (DataFrame → table) ----------

def _df_records(df: pd.DataFrame, columns: list[str], extra: dict) -> list[tuple]:
    """Convert a DataFrame's selected columns to (col1, col2, ..., extra...) tuples.

    Vectorized: re-indexes the DataFrame to the requested column order (filling
    missing columns with NaN), converts NaN→None in one shot, then materializes
    the rows via `to_records`. ~10× faster than the iterrows-per-row form.
    """
    if df.empty:
        return []
    # Re-index to the requested column subset (creates missing cols as NaN).
    sub = df.reindex(columns=columns)
    # NaN → None in a single pass; the result is an object-dtype frame.
    sub = sub.astype(object).where(sub.notna(), None)
    extra_tuple = tuple(extra.values())
    return [tuple(r) + extra_tuple for r in sub.itertuples(index=False, name=None)]


def insert_trades(conn: sqlite3.Connection, source_id: int, account_code: str,
                  df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    cols = ["tradeID", "dateTime", "tradeDate", "symbol", "description",
            "assetCategory", "currency", "quantity", "tradePrice",
            "proceeds_usd", "commission_usd"]
    rows = _df_records(df, cols, {"source_id": source_id, "account_code": account_code})

    # Derive asset_class from IBKR's assetCategory (positional index 5 in
    # cols above). CRYPTO → 'crypto'; STK / OPT / FUT / CASH all collapse
    # to 'stock'. Phoenix doesn't currently model options/futures separately
    # at the tax layer; if you need that, branch here on assetCategory.
    ASSET_CATEGORY_IDX = 5
    enriched = []
    for r in rows:
        ac = (r[ASSET_CATEGORY_IDX] or "").upper()
        klass = "crypto" if ac == "CRYPTO" else "stock"
        enriched.append(r + (klass,))

    conn.executemany(
        """INSERT OR IGNORE INTO trades
           (trade_id, datetime, trade_date, symbol, description, asset_category,
            currency, quantity, trade_price, proceeds_usd, commission_usd,
            source_id, account_code, asset_class)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        enriched,
    )
    return len(enriched)


def insert_corporate_actions(conn: sqlite3.Connection, source_id: int,
                              account_code: str, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    cols = ["date", "type", "symbol", "description", "ratio_old", "ratio_new",
            "per_share", "quantity", "proceeds_usd", "realized_pnl_usd_ibkr"]
    rows = _df_records(df, cols, {"source_id": source_id, "account_code": account_code})
    conn.executemany(
        """INSERT OR IGNORE INTO corporate_actions
           (date, type, symbol, description, ratio_old, ratio_new, per_share,
            quantity, proceeds_usd, realized_pnl_usd_ibkr,
            source_id, account_code)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    return len(rows)


def insert_transfers(conn: sqlite3.Connection, source_id: int,
                     account_code: str, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    cols = ["date", "symbol", "direction", "quantity", "market_value_usd",
            "per_share_usd", "asset_category", "xfer_account"]
    rows = _df_records(df, cols, {"source_id": source_id, "account_code": account_code})
    conn.executemany(
        """INSERT OR IGNORE INTO transfers
           (date, symbol, direction, quantity, market_value_usd, per_share_usd,
            asset_category, xfer_account, source_id, account_code)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    return len(rows)


def insert_dividends(conn: sqlite3.Connection, source_id: int,
                      account_code: str, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    cols = ["pay_date", "symbol", "isin", "description", "currency",
            "amount", "per_share", "dividend_type"]
    rows = _df_records(df, cols, {"source_id": source_id, "account_code": account_code})
    conn.executemany(
        """INSERT OR IGNORE INTO dividends
           (pay_date, symbol, isin, description, currency, amount,
            per_share, dividend_type, source_id, account_code)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    return len(rows)


def insert_withholding(conn: sqlite3.Connection, source_id: int,
                        account_code: str, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    cols = ["pay_date", "symbol", "isin", "description", "currency",
            "amount", "per_share", "source_country", "code"]
    rows = _df_records(df, cols, {"source_id": source_id, "account_code": account_code})
    conn.executemany(
        """INSERT OR IGNORE INTO withholding_tax
           (pay_date, symbol, isin, description, currency, amount,
            per_share, source_country, code, source_id, account_code)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    return len(rows)


def insert_open_positions(conn: sqlite3.Connection, source_id: int,
                          account_code: str, as_of: str, df: pd.DataFrame) -> int:
    if df.empty or not as_of:
        return 0
    rows = []
    for r in df.to_dict("records"):
        sym = r.get("symbol")
        qty = r.get("quantity")
        if sym is None or qty is None or pd.isna(qty):
            continue
        rows.append((source_id, account_code, as_of, sym, float(qty), r.get("currency")))
    conn.executemany(
        """INSERT OR IGNORE INTO open_positions_snapshots
           (source_id, account_code, as_of, symbol, quantity, currency)
           VALUES (?,?,?,?,?,?)""",
        rows,
    )
    return len(rows)


def upsert_fx_rates(conn: sqlite3.Connection, rates: dict[str, float]) -> int:
    if not rates:
        return 0
    rows = [(d, float(r)) for d, r in rates.items() if r is not None]
    conn.executemany(
        "INSERT OR REPLACE INTO fx_rates (date, eur_usd) VALUES (?, ?)",
        rows,
    )
    return len(rows)


# ---------- Status ----------

def status(conn: sqlite3.Connection) -> dict:
    def n(table):
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    last_ingest_row = conn.execute(
        "SELECT path, ingested_at FROM source_files ORDER BY ingested_at DESC LIMIT 1"
    ).fetchone()
    return {
        "source_files": n("source_files"),
        "trades": n("trades"),
        "corporate_actions": n("corporate_actions"),
        "transfers": n("transfers"),
        "open_positions": n("open_positions_snapshots"),
        "dividends": n("dividends"),
        "withholding_tax": n("withholding_tax"),
        "fx_rates": n("fx_rates"),
        "last_ingest": dict(last_ingest_row) if last_ingest_row else None,
    }


# ---------- Query helpers (DataFrames matching the loaders' schema) ----------

def get_trades(conn: sqlite3.Connection, account_code: str) -> pd.DataFrame:
    """Return trades for an account in the same column schema the loaders produce.

    Includes manual entries (source_id NULL) via a LEFT JOIN so they aren't
    silently dropped. is_manual and asset_class come through as extra columns
    for the reports / tables to display.
    """
    df = pd.read_sql_query(
        """SELECT
              t.id            AS db_id,
              t.source_id,
              COALESCE('db:' || sf.path, 'manual')  AS source,
              t.trade_id      AS tradeID,
              t.datetime      AS dateTime,
              t.trade_date    AS tradeDate,
              t.symbol        AS symbol,
              t.description   AS description,
              t.asset_category AS assetCategory,
              t.currency      AS currency,
              t.quantity      AS quantity,
              t.trade_price   AS tradePrice,
              t.proceeds_usd  AS proceeds_usd,
              t.commission_usd AS commission_usd,
              t.is_manual     AS is_manual,
              t.asset_class   AS asset_class
           FROM trades t
           LEFT JOIN source_files sf ON sf.id = t.source_id
           WHERE t.account_code = ?
           ORDER BY t.datetime, t.id""",
        conn, params=(account_code,),
    )
    return df


def _ensure_manual_source(conn: sqlite3.Connection, account_code: str) -> int:
    """Return the source_files.id of the synthetic 'manual' row for this
    account, creating it on first use.

    Why: the trades table was created with source_id NOT NULL (legacy DBs).
    Manual trades can't have a real source file, so we keep one synthetic
    source_files row per account with path = '__manual:<account_code>'.
    This row's `mtime` and `size` are zeroed; it can't collide with a real
    file path. Re-ingests don't touch it (they only delete rows that match
    a real path that vanished from disk, and `__manual:*` paths never appear
    on disk)."""
    from datetime import datetime as _dt
    pseudo_path = f"__manual:{account_code}"
    row = conn.execute(
        "SELECT id FROM source_files WHERE path = ?", (pseudo_path,)
    ).fetchone()
    if row:
        return row[0]
    cur = conn.execute(
        """INSERT INTO source_files
           (path, account_code, kind, ibkr_account, size, mtime, ingested_at)
           VALUES (?, ?, 'manual', NULL, 0, 0, ?)""",
        (pseudo_path, account_code, _dt.now().isoformat(timespec="seconds")),
    )
    return cur.lastrowid


def insert_manual_trade(
    conn: sqlite3.Connection,
    *,
    account_code: str,
    symbol: str,
    asset_class: str,           # 'stock' or 'crypto'
    trade_date: str,            # ISO 'YYYY-MM-DD'
    side: str,                  # 'buy' or 'sell'
    quantity: float,
    price: float,
    currency: str,
    commission: float = 0.0,
    description: str = "",
) -> int:
    """Insert a user-entered trade. is_manual=1. Uses a synthetic
    source_files row per account so the legacy NOT NULL constraint on
    trades.source_id is satisfied. Returns the new row id.

    Convention follows the IBKR import path:
      buy  → quantity > 0, proceeds < 0 (money flowing out)
      sell → quantity < 0, proceeds > 0 (money flowing in)
    The form / API normalises whatever the user typed before calling this.
    """
    if asset_class not in ("stock", "crypto"):
        raise ValueError(f"asset_class must be 'stock' or 'crypto', got {asset_class!r}")
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")
    if quantity <= 0:
        raise ValueError("quantity must be a positive number")
    if price < 0:
        raise ValueError("price cannot be negative")

    signed_qty = quantity if side == "buy" else -quantity
    # Proceeds sign mirrors the IBKR convention: negative on buy, positive on sell.
    proceeds = -(signed_qty * price)
    # Commission is always a cost → negative in the trades.commission_usd col.
    signed_commission = -abs(commission) if commission else 0.0

    # Map "manual" naming: the column is `commission_usd` but the value is in
    # whatever currency the user entered. Same with `proceeds_usd`. We don't
    # auto-convert here — the report layer does FX conversion based on the
    # `currency` column. Field is named "_usd" for historic reasons; the
    # actual currency lives in the currency column.
    manual_source_id = _ensure_manual_source(conn, account_code)

    cur = conn.execute(
        """INSERT INTO trades
           (account_code, datetime, trade_date, symbol, description,
            asset_category, currency, quantity, trade_price,
            proceeds_usd, commission_usd,
            source_id, is_manual, asset_class)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
        (
            account_code,
            f"{trade_date} 00:00:00",
            trade_date,
            symbol.upper().strip(),
            description.strip() or "Manual entry",
            "STK" if asset_class == "stock" else "CRYPTO",
            currency.upper().strip(),
            signed_qty,
            float(price),
            proceeds,
            signed_commission,
            manual_source_id,
            asset_class,
        ),
    )
    conn.commit()
    return cur.lastrowid


def delete_manual_trade(conn: sqlite3.Connection, *,
                        trade_id: int, account_code: str) -> bool:
    """Delete a manual trade by id. Refuses to touch is_manual=0 rows so an
    IBKR-imported row can never be deleted through this path. Returns True
    if a row was deleted, False if not found / not manual / wrong account."""
    cur = conn.execute(
        "DELETE FROM trades WHERE id = ? AND account_code = ? AND is_manual = 1",
        (trade_id, account_code),
    )
    conn.commit()
    return cur.rowcount > 0


def list_manual_trades(conn: sqlite3.Connection, account_code: str) -> pd.DataFrame:
    """All user-entered trades for one account, newest first."""
    return pd.read_sql_query(
        """SELECT id, trade_date, symbol, asset_class, currency,
                  quantity, trade_price, proceeds_usd, commission_usd,
                  description
           FROM trades
           WHERE account_code = ? AND is_manual = 1
           ORDER BY trade_date DESC, id DESC""",
        conn, params=(account_code,),
    )


# ---------- Share links (view-only access tokens) ----------

import secrets as _stdlib_secrets


def create_share_link(conn: sqlite3.Connection, *,
                      account_code: str,
                      allowed_tabs: list[str],
                      label: str = "",
                      expires_at: Optional[str] = None) -> dict:
    """Generate a new view-only share link.

    Returns a dict with `id`, `token` (use to build the URL), and other
    fields the settings UI displays. The token is a 32-byte URL-safe
    random string (~43 ASCII chars, ~256 bits of entropy)."""
    if not allowed_tabs:
        raise ValueError("allowed_tabs must contain at least one tab name")

    token = _stdlib_secrets.token_urlsafe(32)
    created_at = datetime.utcnow().isoformat(timespec="seconds")
    tabs_csv = ",".join(sorted(set(t.strip().lower() for t in allowed_tabs if t.strip())))

    cur = conn.execute(
        """INSERT INTO share_links
           (token, account_code, allowed_tabs, label, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (token, account_code, tabs_csv, label.strip()[:200], created_at, expires_at),
    )
    conn.commit()
    return {
        "id": cur.lastrowid,
        "token": token,
        "account_code": account_code,
        "allowed_tabs": tabs_csv,
        "label": label.strip()[:200],
        "created_at": created_at,
        "expires_at": expires_at,
        "revoked": 0,
    }


def validate_share_token(conn: sqlite3.Connection, token: str) -> Optional[dict]:
    """Return the share-link row if `token` is valid (exists, not revoked,
    not expired). None otherwise. Don't leak any other reason to the caller;
    we want 404 on every failed validation, never 401/403."""
    if not token:
        return None
    row = conn.execute(
        """SELECT id, token, account_code, allowed_tabs, label,
                  created_at, expires_at, revoked, last_accessed_at
           FROM share_links WHERE token = ?""",
        (token,),
    ).fetchone()
    if row is None:
        return None
    if row["revoked"]:
        return None
    if row["expires_at"]:
        if row["expires_at"] < datetime.utcnow().isoformat(timespec="seconds"):
            return None
    return dict(row)


def touch_share_link_access(conn: sqlite3.Connection, share_id: int) -> None:
    """Stamp last_accessed_at so admins can see when a link was last used."""
    conn.execute(
        "UPDATE share_links SET last_accessed_at = ? WHERE id = ?",
        (datetime.utcnow().isoformat(timespec="seconds"), share_id),
    )
    conn.commit()


def list_share_links(conn: sqlite3.Connection) -> pd.DataFrame:
    """All share links, newest first. Includes revoked ones (settings UI
    shows them greyed-out so the admin sees the history)."""
    return pd.read_sql_query(
        """SELECT id, token, account_code, allowed_tabs, label,
                  created_at, expires_at, revoked, last_accessed_at
           FROM share_links ORDER BY id DESC""",
        conn,
    )


def revoke_share_link(conn: sqlite3.Connection, share_id: int) -> bool:
    """Set revoked=1. Idempotent. Returns True if the row was found and
    updated (regardless of whether it was already revoked)."""
    cur = conn.execute(
        "UPDATE share_links SET revoked = 1 WHERE id = ?", (share_id,)
    )
    conn.commit()
    return cur.rowcount > 0


def delete_share_link(conn: sqlite3.Connection, share_id: int) -> bool:
    """Hard-delete a share link row (no audit trail). Returns True if a
    row was deleted."""
    cur = conn.execute(
        "DELETE FROM share_links WHERE id = ?", (share_id,)
    )
    conn.commit()
    return cur.rowcount > 0


def get_corporate_actions(conn: sqlite3.Connection, account_code: str) -> pd.DataFrame:
    df = pd.read_sql_query(
        """SELECT
              ('db:' || sf.path) AS source,
              ca.date          AS date,
              ca.date          AS dateTime,
              ca.type          AS type,
              ca.symbol        AS symbol,
              ca.description   AS description,
              ca.ratio_old     AS ratio_old,
              ca.ratio_new     AS ratio_new,
              ca.per_share     AS per_share,
              ca.quantity      AS quantity,
              ca.proceeds_usd  AS proceeds_usd,
              ca.realized_pnl_usd_ibkr AS realized_pnl_usd_ibkr
           FROM corporate_actions ca
           JOIN source_files sf ON sf.id = ca.source_id
           WHERE ca.account_code = ?
           ORDER BY ca.date""",
        conn, params=(account_code,),
    )
    return df


def get_transfers(conn: sqlite3.Connection, account_code: str) -> pd.DataFrame:
    df = pd.read_sql_query(
        """SELECT
              ('db:' || sf.path) AS source,
              tr.date              AS date,
              tr.symbol            AS symbol,
              tr.direction         AS direction,
              tr.quantity          AS quantity,
              tr.market_value_usd  AS market_value_usd,
              tr.per_share_usd     AS per_share_usd,
              tr.asset_category    AS asset_category,
              tr.xfer_account      AS xfer_account
           FROM transfers tr
           JOIN source_files sf ON sf.id = tr.source_id
           WHERE tr.account_code = ?
           ORDER BY tr.date""",
        conn, params=(account_code,),
    )
    return df


def get_open_positions_snapshots(conn: sqlite3.Connection, account_code: str) -> list[tuple[str, pd.DataFrame]]:
    """Return list of (as_of_date, positions_df) tuples in chronological order.
       Each positions_df has columns: symbol, quantity, currency."""
    dates_df = pd.read_sql_query(
        "SELECT DISTINCT as_of FROM open_positions_snapshots WHERE account_code = ? ORDER BY as_of",
        conn, params=(account_code,),
    )
    out: list[tuple[str, pd.DataFrame]] = []
    for as_of in dates_df["as_of"]:
        op = pd.read_sql_query(
            """SELECT symbol, quantity, currency
               FROM open_positions_snapshots
               WHERE account_code = ? AND as_of = ?""",
            conn, params=(account_code, as_of),
        )
        out.append((as_of, op))
    return out


def get_dividends(conn: sqlite3.Connection, account_code: str) -> pd.DataFrame:
    """Return cash dividends for an account (joined to source_files for traceability)."""
    return pd.read_sql_query(
        """SELECT
              ('db:' || sf.path) AS source,
              d.pay_date         AS pay_date,
              d.symbol           AS symbol,
              d.isin             AS isin,
              d.description      AS description,
              d.currency         AS currency,
              d.amount           AS amount,
              d.per_share        AS per_share,
              d.dividend_type    AS dividend_type
           FROM dividends d
           JOIN source_files sf ON sf.id = d.source_id
           WHERE d.account_code = ?
           ORDER BY d.pay_date, d.symbol""",
        conn, params=(account_code,),
    )


def get_withholding(conn: sqlite3.Connection, account_code: str) -> pd.DataFrame:
    """Return foreign withholding-tax entries for an account."""
    return pd.read_sql_query(
        """SELECT
              ('db:' || sf.path)  AS source,
              w.pay_date          AS pay_date,
              w.symbol            AS symbol,
              w.isin              AS isin,
              w.description       AS description,
              w.currency          AS currency,
              w.amount            AS amount,
              w.per_share         AS per_share,
              w.source_country    AS source_country,
              w.code              AS code
           FROM withholding_tax w
           JOIN source_files sf ON sf.id = w.source_id
           WHERE w.account_code = ?
           ORDER BY w.pay_date, w.symbol""",
        conn, params=(account_code,),
    )


def get_known_accounts(conn: sqlite3.Connection) -> set[str]:
    """All IBKR account numbers we've ingested any source for."""
    rows = conn.execute(
        "SELECT DISTINCT ibkr_account FROM source_files WHERE ibkr_account IS NOT NULL"
    ).fetchall()
    return {r[0] for r in rows if r[0]}


# ---------- Year-end marks (Belgian CGT 2026+ basis reset) ----------

def upsert_year_end_marks(
    conn: sqlite3.Connection,
    rows: list[dict],
) -> int:
    """Insert or update marks. Each row dict needs: symbol, date, close_price,
    currency, source. Optional: note. `fetched_at` is auto-stamped."""
    if not rows:
        return 0
    now_iso = datetime.now().isoformat(timespec="seconds")
    payload = [
        (
            r["symbol"],
            r["date"],
            float(r["close_price"]),
            r.get("currency") or "USD",
            r["source"],
            now_iso,
            r.get("note"),
        )
        for r in rows
        if r.get("symbol") and r.get("date") and r.get("close_price") is not None
    ]
    conn.executemany(
        """INSERT INTO year_end_marks
              (symbol, date, close_price, currency, source, fetched_at, note)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(symbol, date) DO UPDATE SET
              close_price = excluded.close_price,
              currency    = excluded.currency,
              source      = excluded.source,
              fetched_at  = excluded.fetched_at,
              note        = excluded.note""",
        payload,
    )
    return len(payload)


def get_year_end_marks(conn: sqlite3.Connection, date: str) -> dict[str, dict]:
    """Return {symbol: {close_price, currency, source, fetched_at, note}} for one date."""
    rows = conn.execute(
        "SELECT symbol, close_price, currency, source, fetched_at, note "
        "FROM year_end_marks WHERE date = ?",
        (date,),
    ).fetchall()
    return {
        r["symbol"]: {
            "close_price": r["close_price"],
            "currency": r["currency"],
            "source": r["source"],
            "fetched_at": r["fetched_at"],
            "note": r["note"],
        }
        for r in rows
    }


def delete_year_end_mark(conn: sqlite3.Connection, symbol: str, date: str) -> int:
    cur = conn.execute(
        "DELETE FROM year_end_marks WHERE symbol = ? AND date = ?",
        (symbol, date),
    )
    return cur.rowcount


if __name__ == "__main__":
    conn = connect()
    init_schema(conn)
    s = status(conn)
    print(f"Database: {DB_PATH}")
    for k, v in s.items():
        print(f"  {k}: {v}")
    conn.close()
