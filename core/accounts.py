"""
Account-related helpers used by the Flask app.

Single source of truth: the `accounts` table in `data.db`.
On first run, seeds default `personal`/`business` rows from `ibkr_flex.ACCOUNTS`.
On every call, backfills any missing `flex_token` from the corresponding env var
(IBKR_FLEX_TOKEN / IBKR_FLEX_TOKEN_BUSINESS) so the DB remains the single source.
"""

import logging
import os
from datetime import datetime
from pathlib import Path

from core import db


log = logging.getLogger("ibkr.accounts")


def get_accounts() -> dict[str, dict]:
    """
    Return {code: account_dict} from the DB.

    Side effects:
      - Creates seeded rows on first run (when DB is empty).
      - Migrates env-var tokens into the DB on every call (one-shot per account).
    """
    conn = db.connect()
    db.init_schema(conn)
    rows = db.list_accounts(conn)
    try:
        import ibkr_flex
    except Exception:
        ibkr_flex = None

    if not rows and ibkr_flex is not None:
        for code, name in [("P", "personal"), ("B", "business")]:
            cfg = ibkr_flex.ACCOUNTS.get(name, {})
            db.create_account(
                conn, name=name, code=code, type=name,
                flex_token=None,
                queries=cfg.get("queries", {}),
            )
        log.info("seeded default accounts (personal, business)")
        rows = db.list_accounts(conn)

    if ibkr_flex is not None:
        migrated = 0
        for r in rows:
            if r.get("flex_token"):
                continue
            cfg = ibkr_flex.ACCOUNTS.get(r["name"], {})
            env_var = cfg.get("token_env")
            if env_var and os.environ.get(env_var):
                db.update_account(conn, r["id"], flex_token=os.environ[env_var])
                migrated += 1
                log.info(f"migrated token for account '{r['name']}' from env var {env_var}")
        if migrated:
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
    counts = {
        "trades": conn.execute(
            "SELECT COUNT(*) FROM trades WHERE account_code = ?", (code,)
        ).fetchone()[0],
        "ca": conn.execute(
            "SELECT COUNT(*) FROM corporate_actions WHERE account_code = ?", (code,)
        ).fetchone()[0],
        "transfers": conn.execute(
            "SELECT COUNT(*) FROM transfers WHERE account_code = ?", (code,)
        ).fetchone()[0],
        "open_positions": conn.execute(
            "SELECT COUNT(*) FROM open_positions_snapshots WHERE account_code = ?", (code,)
        ).fetchone()[0],
    }
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
