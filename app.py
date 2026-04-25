"""
Local web UI for the IBKR TOB + P&L parser.

Runs on http://127.0.0.1:5000 (localhost only — not exposed to the network).
Wraps the existing CLI scripts (ibkr_flex.py / parser.py / pnl.py) and serves
the generated HTML reports in the browser.

Start:
    python app.py
"""

import logging
import sys
import traceback
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, abort, jsonify, render_template, request

from core import accounts as account_service
from core import db
from core import processing


ROOT = Path(__file__).resolve().parent
PARSED_DIR = ROOT / "parsed"
DOWNLOADED_DIR = ROOT / "downloaded"
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "app.log"

# ---------- Logging ----------
LOG_DIR.mkdir(exist_ok=True)
_root_logger = logging.getLogger()
_root_logger.setLevel(logging.INFO)
# Avoid duplicate handlers when Flask reloads
for h in list(_root_logger.handlers):
    _root_logger.removeHandler(h)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setFormatter(_fmt)
_sh = logging.StreamHandler(sys.stderr)
_sh.setFormatter(_fmt)
_root_logger.addHandler(_fh)
_root_logger.addHandler(_sh)
log = logging.getLogger("ibkr.app")


app = Flask(__name__)


@app.before_request
def _log_request():
    log.info(f"REQ {request.method} {request.path} args={dict(request.args)}")


@app.errorhandler(Exception)
def _handle_exception(e):
    log.error(f"UNCAUGHT {type(e).__name__}: {e}")
    log.error(traceback.format_exc())
    raise e


# Ensure the DB schema exists (creates an empty data.db on first run).
# Population happens only when the user clicks "Load data".
def _bootstrap_db():
    conn = db.connect()
    db.init_schema(conn)
    conn.close()
    log.info(f"startup: DB ready at {db.DB_PATH} (empty until 'Load data' is clicked)")
    log.info(f"startup: log file = {LOG_FILE}")


_bootstrap_db()


# ---------- helpers ----------

@app.route("/")
def index():
    # Pick the active account from ?account=personal|business, default to personal
    requested = request.args.get("account", "personal").lower()
    accs = account_service.get_accounts()
    name_to_code = {a["name"]: c for c, a in accs.items()}
    if requested not in name_to_code:
        requested = next(iter(name_to_code), "personal")
    current_code = name_to_code.get(requested)
    current_type = accs[current_code]["type"] if current_code in accs else "personal"
    statuses = {name: account_service.report_status(name, downloaded_dir=DOWNLOADED_DIR) for name in name_to_code.keys()}
    accounts_simple = {c: a["name"] for c, a in accs.items()}
    conn = db.connect()
    db.init_schema(conn)
    db_stat = db.status(conn)
    conn.close()
    return render_template(
        "dashboard.html",
        accounts=accounts_simple,
        accounts_full=accs,
        current_type=current_type,
        statuses=statuses,
        current_name=requested,
        current_code=current_code,
        db_status=db_stat,
    )


@app.route("/run/<action>/<code>", methods=["POST"])
def run_action(action: str, code: str):
    log.info(f"action: {action} -a {code}")
    accs = account_service.get_accounts()
    if code not in accs:
        log.warning(f"action: unknown account code {code!r}")
        abort(400, f"Unknown account code: {code}")
    account = accs[code]
    if action == "download":
        acc_dir = DOWNLOADED_DIR / account["name"]
        acc_dir.mkdir(parents=True, exist_ok=True)
        token = account.get("flex_token")
        queries = account.get("queries") or {}
        query_id = queries.get("ytd")

        if not token or not query_id:
            msg = (f"Account '{account['name']}' has no Flex token / query ID in the DB. "
                   f"Re-create the account from the dashboard (×, then ＋) with full credentials.")
            log.warning(msg)
            return jsonify({"cmd": f"download -a {code}", "returncode": 2,
                            "stdout": "", "stderr": msg, "elapsed_s": 0.0,
                            "friendly_message": msg})

        # Save first as <name>_ytd.xml, then rename to <name>_<year>.xml after extract.
        tmp_out = acc_dir / f"{account['name']}_ytd.xml"
        cmd = ["ibkr_flex.py",
               "--token", token,
               "--query-id", query_id,
               "--out", str(tmp_out)]
        log.info(f"download: account={account['name']} code={code} (DB credentials)")
        result = processing.run_subprocess(cmd, cwd=ROOT)

        if result["returncode"] == 0 and tmp_out.exists():
            year = processing.year_from_xml(tmp_out)
            if year:
                final_out = acc_dir / f"{account['name']}_{year}.xml"
                if final_out != tmp_out:
                    if final_out.exists():
                        final_out.unlink()
                    tmp_out.rename(final_out)
                    log.info(f"download: renamed {tmp_out.name} → {final_out.name}")
        if result["returncode"] == 0:
            log.info(f"action: download OK, running ingest")
            ingest_log = processing.run_ingest(code)
            result["stdout"] = (result["stdout"] or "") + "\n" + ingest_log
        else:
            friendly = result.get("friendly_message") or f"exit {result['returncode']}"
            log.warning(f"action: download FAILED ({friendly}); skipping ingest")
    elif action == "ingest":
        log.info("action: manual ingest")
        result = {"cmd": f"ingest -a {code}", "returncode": 0,
                  "stdout": processing.run_ingest(code), "stderr": "", "elapsed_s": 0}
    elif action == "fetch_marks":
        result = _run_fetch_marks(code)
    else:
        log.warning(f"action: unknown action {action!r}")
        abort(400, f"Unknown action: {action}")
    return jsonify(result)


def _run_fetch_marks(code: str) -> dict:
    """Populate the year-end marks needed for the Belgian CGT 2026+ basis reset.

    Determines which symbols this account requires (from closed/open trades),
    fetches them from Yahoo, and upserts into `year_end_marks`. Returns a
    dashboard-style result dict so the toast/log UI can display it.
    """
    from core import yahoo_marks
    from reports import cgt as _cgt
    from reports import pnl as _pnl

    log.info(f"fetch_marks: starting for account code={code}")
    start = datetime.now()

    conn = db.connect()
    db.init_schema(conn)
    df = db.get_trades(conn, code)
    if df.empty:
        conn.close()
        msg = f"No trades for account code={code}; nothing to fetch."
        log.warning(f"fetch_marks: {msg}")
        return {"cmd": f"fetch_marks -a {code}", "returncode": 0,
                "stdout": msg, "stderr": "", "elapsed_s": 0.0,
                "friendly_message": msg}

    df = _pnl.dedupe(df)
    ca = _pnl._group_ca_actions(db.get_corporate_actions(conn, code))
    xf_df = db.get_transfers(conn, code)
    known = db.get_known_accounts(conn)
    transfers = [
        x for x in xf_df.to_dict("records")
        if not (x.get("direction") == "IN" and x.get("xfer_account") in known)
    ]
    snaps = db.get_open_positions_snapshots(conn, code)
    snaps.sort(key=lambda t: t[0])
    closed, open_df = _pnl.match_lots(
        df, ca_actions=ca, transfers=transfers,
        reconcile_snapshots=snaps, method="FIFO",
    )
    needed = _cgt.symbols_needing_marks(closed, open_df)
    existing = db.get_year_end_marks(conn, _cgt.RESET_DATE)
    todo = [s for s in needed if s not in existing]

    lines: list[str] = []

    def _log(msg=""):
        lines.append(str(msg))
        log.info(f"fetch_marks: {msg}")

    _log(f"account={code} symbols_needed={len(needed)} already_have={len(existing)} "
         f"to_fetch={len(todo)}")

    if not todo:
        conn.close()
        msg = f"All {len(needed)} marks already present — nothing to fetch."
        return {"cmd": f"fetch_marks -a {code}", "returncode": 0,
                "stdout": "\n".join(lines + [msg]), "stderr": "",
                "elapsed_s": (datetime.now() - start).total_seconds(),
                "friendly_message": msg}

    result = yahoo_marks.fetch_many(todo, _cgt.RESET_DATE, log_progress=_log)
    n = db.upsert_year_end_marks(conn, result["hits"])
    conn.commit()
    conn.close()

    elapsed = (datetime.now() - start).total_seconds()
    summary = (f"Fetched {len(result['hits'])} marks, "
               f"{len(result['misses'])} symbol{'s' if len(result['misses']) != 1 else ''} "
               f"missing (manual entry needed)")
    _log(summary)
    return {
        "cmd": f"fetch_marks -a {code}",
        "returncode": 0,
        "stdout": "\n".join(lines),
        "stderr": "",
        "elapsed_s": elapsed,
        "friendly_message": summary,
    }


@app.route("/db/status")
def db_status():
    """JSON snapshot of DB counts for the dashboard sidebar."""
    conn = db.connect()
    db.init_schema(conn)
    s = db.status(conn)
    conn.close()
    return jsonify(s)


@app.route("/accounts", methods=["GET"])
def accounts_list():
    """JSON list of all accounts (tokens redacted)."""
    accs = account_service.get_accounts()
    safe = []
    for code, a in accs.items():
        safe.append({
            "id": a["id"], "code": a["code"], "name": a["name"], "type": a["type"],
            "queries": a["queries"],
            "has_token": bool(a.get("flex_token")),
        })
    return jsonify(safe)


@app.route("/accounts/add", methods=["POST"])
def accounts_add():
    """Create a new account from form/JSON data."""
    data = request.get_json(silent=True) or request.form.to_dict()
    name = (data.get("name") or "").strip().lower()
    code = (data.get("code") or "").strip().upper()
    type_ = (data.get("type") or "").strip().lower()
    token = (data.get("token") or "").strip() or None
    query_id_ytd = (data.get("query_id") or "").strip() or None

    errors = []
    if not name or not name.replace("_", "").isalnum():
        errors.append("name must be lowercase letters/numbers/underscore")
    if not code or len(code) > 4 or not code.isalnum():
        errors.append("code must be 1-4 alphanumeric characters")
    if type_ not in ("personal", "business"):
        errors.append("type must be 'personal' or 'business'")
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400

    queries = {"ytd": query_id_ytd} if query_id_ytd else {}
    conn = db.connect()
    db.init_schema(conn)
    try:
        new_id = db.create_account(
            conn, name=name, code=code, type=type_,
            flex_token=token, queries=queries,
        )
    except Exception as e:
        conn.close()
        log.warning(f"accounts/add: failed — {e}")
        return jsonify({"ok": False, "errors": [str(e)]}), 400
    conn.close()

    # Create the per-account input folder so the user can drop CSVs there.
    folder = DOWNLOADED_DIR / name
    folder.mkdir(parents=True, exist_ok=True)
    log.info(f"accounts/add: created {name} (code={code}, type={type_}) id={new_id}")
    log.info(f"accounts/add: folder ready at {folder}")
    return jsonify({"ok": True, "id": new_id, "folder": str(folder.relative_to(ROOT))})


@app.route("/accounts/<int:account_id>", methods=["DELETE"])
def accounts_delete(account_id: int):
    conn = db.connect()
    db.init_schema(conn)
    result = db.delete_account(conn, account_id)
    conn.close()
    log.info(f"accounts/delete: id={account_id} result={result}")
    if not result.get("deleted"):
        return jsonify({"ok": False, "error": "account not found"}), 404
    folder = DOWNLOADED_DIR / result["name"]
    return jsonify({
        "ok": True,
        "name": result["name"],
        "code": result["code"],
        "counts": result["counts"],
        "folder_kept": str(folder.relative_to(ROOT)) if folder.exists() else None,
    })


@app.route("/db/reset", methods=["POST"])
def db_reset():
    """Wipe every data table. Destructive — caller must already have confirmed."""
    log.warning("db: RESET requested — wiping all tables")
    conn = db.connect()
    db.init_schema(conn)
    counts = db.reset_database(conn)
    conn.close()
    log.warning(f"db: reset complete, deleted {counts}")
    return jsonify({"deleted": counts, "ok": True})


@app.route("/report/<kind>/<account>")
def report(kind: str, account: str):
    """Render the requested report directly from the DB — no static files."""
    log.info(f"report: kind={kind} account={account}")
    accs = account_service.get_accounts()
    name_to_code = {a["name"]: c for c, a in accs.items()}
    if account not in name_to_code:
        log.warning(f"report: unknown account {account!r}")
        abort(404)
    code = name_to_code[account]

    start = datetime.now()
    try:
        if kind == "tob":
            from reports import tob as _tob
            html = _tob.build_tob_html(code)
        elif kind == "pnl":
            from reports import pnl as _pnl
            html = _pnl.build_pnl_html(code)
        elif kind == "cgt":
            from reports import cgt as _cgt
            html = _cgt.build_cgt_html(code)
        else:
            log.warning(f"report: unknown kind {kind!r}")
            abort(404, f"Unknown report kind: {kind}")
    except Exception as e:
        log.error(f"report: FAILED kind={kind} account={account}: {e}")
        log.error(traceback.format_exc())
        raise
    log.info(f"report: rendered kind={kind} account={account} bytes={len(html)} in {(datetime.now()-start).total_seconds():.2f}s")
    return Response(html, mimetype="text/html; charset=utf-8")


if __name__ == "__main__":
    # Bind to localhost only — never expose to the network.
    app.run(host="127.0.0.1", port=5000, debug=False)
