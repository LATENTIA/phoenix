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
    source_id       INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
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
    commission_usd  REAL
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
    conn.commit()


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

def needs_ingest(conn: sqlite3.Connection, path: Path) -> bool:
    """True if file is new or has changed since last ingest."""
    if not path.exists():
        return False
    row = conn.execute(
        "SELECT size, mtime FROM source_files WHERE path = ?",
        (str(path),),
    ).fetchone()
    if row is None:
        return True
    stat = path.stat()
    return row["size"] != stat.st_size or row["mtime"] != stat.st_mtime


def upsert_source(conn: sqlite3.Connection, path: Path,
                  account_code: str, kind: str,
                  ibkr_account: Optional[str] = None) -> int:
    """
    Insert or replace a source_files row. If a previous row exists for this path,
    deletes its dependent rows first (CASCADE) and replaces it.
    Returns the new source_id.
    """
    stat = path.stat()
    cur = conn.cursor()
    # Delete prior row for this path (CASCADE removes child rows)
    cur.execute("DELETE FROM source_files WHERE path = ?", (str(path),))
    cur.execute(
        """INSERT INTO source_files (path, account_code, kind, ibkr_account, size, mtime, ingested_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (str(path), account_code, kind, ibkr_account, stat.st_size, stat.st_mtime,
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
    conn.executemany(
        """INSERT OR IGNORE INTO trades
           (trade_id, datetime, trade_date, symbol, description, asset_category,
            currency, quantity, trade_price, proceeds_usd, commission_usd,
            source_id, account_code)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    return len(rows)


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
    """Return trades for an account in the same column schema the loaders produce."""
    df = pd.read_sql_query(
        """SELECT
              source_id,
              ('db:' || sf.path) AS source,
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
              t.commission_usd AS commission_usd
           FROM trades t
           JOIN source_files sf ON sf.id = t.source_id
           WHERE t.account_code = ?
           ORDER BY t.datetime, t.id""",
        conn, params=(account_code,),
    )
    return df


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
