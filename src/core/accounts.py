"""
Account-related helpers used by the Flask app.

Single source of truth: the `accounts` table in `data.db`.
On first run, seeds default `personal`/`business` rows so the dashboard has
something to render; the user adds their own token + query ID via the UI.
"""

import logging
from datetime import datetime
from pathlib import Path

from core import db


log = logging.getLogger("ibkr.accounts")


def get_accounts() -> dict[str, dict]:
    """
    Return {code: account_dict} from the DB.

    Side effect: on a fresh install (no rows in `accounts` yet) we seed two
    empty placeholder rows — `personal` (P) and `business` (B) — so the
    dashboard renders something. The user fills in token + query ID via
    the "Add account" UI; nothing else is auto-populated.
    """
    conn = db.connect()
    db.init_schema(conn)
    rows = db.list_accounts(conn)

    if not rows:
        for code, name in [("P", "personal"), ("B", "business")]:
            db.create_account(
                conn, name=name, code=code, type=name,
                flex_token=None, queries={},
            )
        log.info("seeded default accounts (personal, business)")
        rows = db.list_accounts(conn)

    conn.close()
    return {r["code"]: r for r in rows}


def get_accounts_simple() -> dict[str, str]:
    """{code: name} convenience for templates that don't need the full dict."""
    return {code: a["name"] for code, a in get_accounts().items()}


def report_status(account_name: str, *, downloaded_dir: Path) -> dict:
    """Per-account dashboard status: row counts, last-ingest, list of XML files on disk."""
    accs = get_accounts()
    code = next((c for c, a in accs.items() if a["name"] == account_name), None)
    if code is None:
        return {
            "has_data": False,
            "counts": {"trades": 0, "ca": 0, "transfers": 0, "open_positions": 0},
            "last_ingest": None,
            "xmls": [],
        }

    conn = db.connect()
    db.init_schema(conn)
    # One query, four counts via a UNION-of-counts pattern. Keyed by an alias
    # so we can dict-lookup the result; fewer round-trips than four separate
    # COUNT(*) queries.
    rows = conn.execute(
        """SELECT 'trades' AS k, COUNT(*) AS n FROM trades             WHERE account_code = ?
        UNION ALL
           SELECT 'ca',          COUNT(*)      FROM corporate_actions  WHERE account_code = ?
        UNION ALL
           SELECT 'transfers',   COUNT(*)      FROM transfers          WHERE account_code = ?
        UNION ALL
           SELECT 'open_positions', COUNT(*)   FROM open_positions_snapshots WHERE account_code = ?""",
        (code, code, code, code),
    ).fetchall()
    counts = {r["k"]: r["n"] for r in rows}
    last_ingest_row = conn.execute(
        "SELECT path, ingested_at FROM source_files WHERE account_code = ? "
        "ORDER BY ingested_at DESC LIMIT 1", (code,),
    ).fetchone()
    last_ingest = None
    if last_ingest_row:
        try:
            ts = datetime.fromisoformat(last_ingest_row["ingested_at"]).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            ts = last_ingest_row["ingested_at"]
        last_ingest = {"path": Path(last_ingest_row["path"]).name, "mtime": ts}
    conn.close()

    xml_files = sorted((downloaded_dir / account_name).glob("*.xml")) if (downloaded_dir / account_name).exists() else []
    return {
        "has_data": counts["trades"] > 0,
        "counts": counts,
        "last_ingest": last_ingest,
        "xmls": [
            {"name": p.name,
             "mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")}
            for p in xml_files
        ],
    }
