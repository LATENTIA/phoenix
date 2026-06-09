"""
Local web UI for the IBKR TOB + P&L parser.

Runs on http://127.0.0.1:5000 (localhost only — not exposed to the network).
Wraps the existing CLI scripts (ibkr_flex.py / parser.py / pnl.py) and serves
the generated HTML reports in the browser.

Start:
    python app.py
"""

import logging
import os
import secrets as _stdlib_secrets
import sys
import traceback
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, abort, jsonify, render_template, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_talisman import Talisman
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.security import check_password_hash

from core import accounts as account_service
from core import db
from core import processing
from core import secrets as phoenix_secrets


# Project root = one level above src/ on the host. Inside the Docker container
# this resolves to "/" (since the Dockerfile copies src/ to /app/), but that
# doesn't matter because the env vars below are always set in-container.
# The fallbacks here only kick in for local non-Docker dev, where they should
# point at the project root next to the .env / LICENSE / phoenix-data/ etc.
ROOT = Path(__file__).resolve().parent.parent

# All on-disk locations are env-var overridable so containerised / EC2
# deploys can keep user data outside the project tree (on a mounted volume).
# Defaults preserve the original layout for anyone running outside Docker.
#
# Dockerfile sets these to subpaths of /app/data, which docker-compose then
# bind-mounts to a stable host directory (defaults to ./phoenix-data/ or
# whatever PHOENIX_DATA_DIR points to on EC2).
PARSED_DIR = ROOT / "parsed"
DOWNLOADED_DIR = Path(
    os.environ.get("PHOENIX_DOWNLOADED_DIR") or (ROOT / "downloaded")
)
LOG_DIR = Path(os.environ.get("PHOENIX_LOG_DIR") or (ROOT / "logs"))
LOG_FILE = LOG_DIR / "app.log"


def _ensure_data_dirs() -> None:
    """Create writable directories the app needs. Called once at startup."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOADED_DIR.mkdir(parents=True, exist_ok=True)
    db_parent = db.DB_PATH.parent
    db_parent.mkdir(parents=True, exist_ok=True)


def _warn_if_unmigrated_install() -> None:
    """If the active DB path lives on a mounted volume (env-overridden) but
    is empty AND a legacy `data.db` exists at the project root, the user
    almost certainly forgot to migrate. Log a loud warning so they notice
    before they create new accounts in a fresh DB."""
    if not os.environ.get("PHOENIX_DB_PATH"):
        return       # not using the env-driven path, nothing to migrate
    if db.DB_PATH.exists() and db.DB_PATH.stat().st_size > 0:
        return       # active DB is already populated
    legacy = ROOT / "data.db"
    if not legacy.exists() or legacy.stat().st_size == 0:
        return       # no legacy DB to migrate either
    log.warning("=" * 70)
    log.warning("Looks like you have a legacy data.db at the project root that")
    log.warning(f"hasn't been migrated to the configured PHOENIX_DB_PATH ({db.DB_PATH}).")
    log.warning("Phoenix will start with an EMPTY DB. To preserve your existing")
    log.warning("trade history, stop the app and run:")
    log.warning("    python scripts/migrate-to-docker.py")
    log.warning("Then restart docker compose.")
    log.warning("=" * 70)

# ---------- Logging ----------
# Create LOG_DIR before configuring the FileHandler. The rest of the
# writable dirs are created later by _ensure_data_dirs() once logging is up.
LOG_DIR.mkdir(parents=True, exist_ok=True)
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

# ---------- Security config ----------
# SECRET_KEY: signs CSRF tokens and session cookies. Read from env so the
# value survives restarts (CSRF tokens issued before the restart stay valid).
# In local dev, fall back to a fresh random key per process — every restart
# invalidates open tabs, but that's acceptable for solo use.
app.config["SECRET_KEY"] = (
    os.environ.get("PHOENIX_SECRET_KEY") or _stdlib_secrets.token_hex(32)
)
# Cap request bodies. /accounts/add JSON is well under this; uploads aren't
# accepted on any route. A multi-GB POST can't OOM us.
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024     # 1 MB
# CSRF tokens never expire mid-session (the dashboard is a long-lived page).
app.config["WTF_CSRF_TIME_LIMIT"] = None
# Don't send the CSRF token in URLs; header-only.
app.config["WTF_CSRF_HEADERS"] = ["X-CSRFToken"]

csrf = CSRFProtect(app)

# Rate limiter. Defaults are global; specific routes tighten via @limiter.limit.
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per hour", "30 per minute"],
    storage_uri="memory://",     # fine for single-instance; swap to redis on AWS multi-instance
    strategy="fixed-window",
    headers_enabled=True,        # adds X-RateLimit-* and Retry-After response headers
)

# Security headers (CSP, X-Frame-Options, etc.). force_https=False because
# we haven't wired TLS yet; the proxy/ALB will handle it later.
# Templates use lots of inline <style> / <script> tags, so allow 'unsafe-inline'.
# Tightening to nonces would mean rewriting every template.
_csp = {
    "default-src": "'self'",
    "style-src": ["'self'", "'unsafe-inline'"],
    "script-src": ["'self'", "'unsafe-inline'"],
    "img-src": ["'self'", "data:"],
    "font-src": "'self'",
    "frame-src": "'self'",
    "frame-ancestors": "'self'",
}
talisman = Talisman(
    app,
    force_https=False,
    content_security_policy=_csp,
    content_security_policy_nonce_in=[],
    frame_options="SAMEORIGIN",
    referrer_policy="strict-origin-when-cross-origin",
    session_cookie_secure=False,            # set True once HTTPS lands
    strict_transport_security=False,        # ditto
)


@app.before_request
def _require_basic_auth():
    """Gate every route behind HTTP Basic Auth when both PHOENIX_AUTH_USER
    and PHOENIX_AUTH_PASS_HASH are set. Both unset = auth disabled (local dev).

    Set them like:
        export PHOENIX_AUTH_USER=admin
        export PHOENIX_AUTH_PASS_HASH="$(python -c 'from werkzeug.security import generate_password_hash; print(generate_password_hash("yourpassword"))')"

    Only one user is supported on purpose (this is a single-tenant app).
    """
    expected_user = os.environ.get("PHOENIX_AUTH_USER")
    expected_hash = os.environ.get("PHOENIX_AUTH_PASS_HASH")
    if not expected_user or not expected_hash:
        return None        # auth disabled

    auth = request.authorization
    if (auth and auth.username == expected_user
            and check_password_hash(expected_hash, auth.password or "")):
        return None        # creds OK

    return Response(
        "Authentication required.",
        status=401,
        headers={"WWW-Authenticate": 'Basic realm="Phoenix"'},
    )


@app.before_request
def _log_request():
    # Path + method only. Query strings are NOT logged — if a future endpoint
    # ever accepts a sensitive value via GET, it must not silently end up in
    # logs/app.log or CloudWatch.
    log.info(f"REQ {request.method} {request.path}")


@app.errorhandler(CSRFError)
def _handle_csrf_error(e):
    """Don't leak the framework's default CSRF traceback to the browser."""
    log.warning(f"CSRF rejected: {e.description} on {request.method} {request.path}")
    return Response(
        "CSRF token missing or invalid. Refresh the page and try again.",
        status=400, mimetype="text/plain",
    )


@app.errorhandler(Exception)
def _handle_exception(e):
    # Log the traceback to disk, return a sanitised error to the client.
    # Never `raise e` — that would hand the traceback to the browser, leaking
    # file paths, module names, and library versions.
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        # Let Flask handle 4xx/normal HTTP errors normally (their bodies are
        # already controlled and free of internal detail).
        return e
    log.error(f"UNCAUGHT {type(e).__name__}: {e}")
    log.error(traceback.format_exc())
    return Response(
        "Internal server error. See logs/app.log for details.",
        status=500, mimetype="text/plain",
    )


# Ensure the DB schema exists (creates an empty data.db on first run).
# Population happens only when the user clicks "Load data".
def _bootstrap_db():
    _ensure_data_dirs()
    _warn_if_unmigrated_install()
    conn = db.connect()
    db.init_schema(conn)
    conn.close()
    log.info(f"startup: DB ready at {db.DB_PATH} (empty until 'Load data' is clicked)")
    log.info(f"startup: downloaded dir = {DOWNLOADED_DIR}")
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
@limiter.limit("5 per minute")
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
        # The DB row holds either the plaintext token (local dev) or an
        # `aws-sm://...` reference (AWS deploy). resolve_token() returns the
        # actual token string in both cases. We then pass it to the subprocess
        # via env var (NOT --token CLI arg) so it never lands in /proc/*/cmdline.
        stored_token = account.get("flex_token")
        token = phoenix_secrets.resolve_token(stored_token)
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
               "--query-id", query_id,
               "--out", str(tmp_out)]
        log.info(f"download: account={account['name']} code={code} (DB credentials)")
        result = processing.run_subprocess(
            cmd, cwd=ROOT, env_extra={"IBKR_FLEX_TOKEN": token},
        )

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
    # In AWS mode this writes the token to Secrets Manager and returns an
    # opaque `aws-sm://...` reference for the DB to store. In plaintext mode
    # it's a no-op and the actual token goes straight into the DB.
    stored_token = phoenix_secrets.store_token(name, token)
    conn = db.connect()
    db.init_schema(conn)
    try:
        new_id = db.create_account(
            conn, name=name, code=code, type=type_,
            flex_token=stored_token, queries=queries,
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
    # Pull the stored token reference BEFORE deleting the row, so AWS-mode
    # can clean up the corresponding Secrets Manager entry. Plaintext mode
    # treats this as a no-op.
    row = conn.execute(
        "SELECT flex_token FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    stored_token = row["flex_token"] if row else None
    result = db.delete_account(conn, account_id)
    conn.close()
    if stored_token:
        phoenix_secrets.delete_token(stored_token)
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
@limiter.limit("60 per minute")
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
        elif kind == "dividends":
            from reports import dividends as _div
            html = _div.build_dividends_html(code)
        elif kind == "methodology":
            from reports import methodology as _meth
            html = _meth.build_methodology_html(code)
        else:
            log.warning(f"report: unknown kind {kind!r}")
            abort(404, f"Unknown report kind: {kind}")
    except Exception as e:
        log.error(f"report: FAILED kind={kind} account={account}: {e}")
        log.error(traceback.format_exc())
        raise
    log.info(f"report: rendered kind={kind} account={account} bytes={len(html)} in {(datetime.now()-start).total_seconds():.2f}s")
    return Response(html, mimetype="text/html; charset=utf-8")


@app.route("/license")
def license_page():
    """Render the LICENSE file as a styled in-app page so the dashboard
    footer's 'non-commercial use' link has somewhere to point.

    The LICENSE file lives at the project root (next to docker-compose.yml,
    README.md, etc.). Two layouts to handle:
      - Local non-Docker dev: src/app.py runs; ROOT = <project>/; LICENSE at ROOT/LICENSE.
      - Docker: /app/app.py runs; ROOT = /; LICENSE is bind-mounted at /app/LICENSE
        (next to the code, see docker-compose.yml).
    Try the project-root path first, fall back to next-to-code for Docker.
    """
    src_dir = Path(__file__).resolve().parent
    candidates = [ROOT / "LICENSE", src_dir / "LICENSE"]
    license_path = next((c for c in candidates if c.exists()), None)
    if license_path is None:
        license_text = "LICENSE file not found."
    else:
        license_text = license_path.read_text(encoding="utf-8")
    return render_template("license.html", license_text=license_text)


def _is_port_in_use(host: str, port: int) -> bool:
    """True if anything on `host:port` accepts a TCP connection.

    Why connect-test instead of bind-test: on Windows, a server listening on
    `0.0.0.0:5000` does NOT prevent a second process from binding
    `127.0.0.1:5000`. Both end up "listening" on port 5000 and the second
    one silently shadows the first for loopback traffic. Connecting is the
    only check that catches that case."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.25)
        try:
            s.connect((host, port))
        except (ConnectionRefusedError, socket.timeout, OSError):
            return False
        return True


def _find_free_port(host: str, preferred: int, max_tries: int = 20) -> int:
    """Return `preferred` if nothing answers on `host:preferred`, otherwise
    scan upward (preferred+1, +2, ...) for up to `max_tries` ports and return
    the first one that doesn't answer. Raises OSError if all are busy."""
    for offset in range(max_tries):
        port = preferred + offset
        if not _is_port_in_use(host, port):
            return port
    raise OSError(f"no free port found in range {preferred}..{preferred + max_tries - 1}")


if __name__ == "__main__":
    # Bind host:
    #   - Local dev (default): 127.0.0.1 so the app is unreachable from the LAN.
    #   - Docker / container:  0.0.0.0 so the host port mapping can reach it.
    #     The container's published port is itself bound to 127.0.0.1 on the
    #     host (see docker-compose.yml), so the LAN exposure stays the same.
    HOST = os.environ.get("PHOENIX_BIND_HOST", "127.0.0.1")
    PREFERRED_PORT = int(os.environ.get("PHOENIX_PORT", "5000"))
    # Connect-test the loopback interface even when binding 0.0.0.0 — if
    # something already answers there, we'd shadow it. Skip the check when
    # the user explicitly forced a port via env (containers always know
    # their port is free).
    probe_host = "127.0.0.1" if HOST in ("0.0.0.0", "::") else HOST
    if os.environ.get("PHOENIX_PORT"):
        port = PREFERRED_PORT
    else:
        port = _find_free_port(probe_host, PREFERRED_PORT)
    if port != PREFERRED_PORT:
        log.warning(
            f"port {PREFERRED_PORT} is in use (another Phoenix instance? leftover Flask?). "
            f"Falling back to port {port}. Open http://{probe_host}:{port}/ in your browser."
        )
    else:
        log.info(f"listening on http://{HOST}:{port}/")
    app.run(host=HOST, port=port, debug=False)
