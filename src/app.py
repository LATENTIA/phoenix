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
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

from core import accounts as account_service
from core import db
from core import processing
from core import secrets as phoenix_secrets


# Two anchor paths for the rest of the file:
#
#   SRC_DIR : directory containing app.py and the rest of the Python sources.
#             Host:      <project>/src
#             Container: /app
#             Used for: subprocess cwd when invoking ibkr_flex.py.
#
#   ROOT    : project root (one level above SRC_DIR on the host).
#             Host:      <project>
#             Container: /  (irrelevant; env vars override the data-path
#                             defaults that depend on ROOT)
#             Used for: data-path fallbacks (DOWNLOADED_DIR / LOG_DIR /
#             default DB), LICENSE lookup, legacy-data-detection.
SRC_DIR = Path(__file__).resolve().parent
ROOT = SRC_DIR.parent

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

# ---------- Proxy awareness ----------
# In production, Caddy sits in front of Phoenix and terminates TLS. Without
# this middleware, Flask's request.remote_addr would be Caddy's Docker IP
# (172.x.x.x) for every request, which means flask-limiter would rate-limit
# all of Caddy's outgoing requests as one client. ProxyFix reads the
# X-Forwarded-* headers that Caddy sets and patches request.remote_addr,
# request.scheme, and request.host accordingly so downstream code sees the
# real client.
#
# `x_for=1` etc. mean "trust exactly ONE proxy layer". This is correct
# whether you're behind just Caddy (prod) or directly accessing Flask (dev,
# no proxy — middleware is a no-op since no X-Forwarded-* headers exist).
# Bumping these numbers would let a client spoof headers, so leave them at 1.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)


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

    Public exceptions (no basic-auth challenge):
      - /share/<token>*   — share-link URLs validate their own token in the
                             view function. Lets accountants reach the share
                             dashboard without basic-auth credentials.
      - /static/*         — CSS / JS / images, no sensitive data.
      - /favicon.ico      — convenience.
    """
    expected_user = os.environ.get("PHOENIX_AUTH_USER")
    expected_hash = os.environ.get("PHOENIX_AUTH_PASS_HASH")
    if not expected_user or not expected_hash:
        return None        # auth disabled

    path = request.path
    if path.startswith("/share/") or path.startswith("/static/") or path == "/favicon.ico":
        return None        # public path — no basic-auth challenge

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


# ---------- Background ECB EUR/USD weekly auto-refresh ----------
# Runs in a daemon thread inside the Flask process. Single gunicorn worker
# (we enforce GUNICORN_WORKERS=1) so there's only one scheduler instance.
# If you ever scale to multiple workers, move this to a sidecar container
# or use a distributed scheduler (apscheduler + sqlalchemy job store).
def _start_fx_scheduler() -> None:
    """Schedule a weekly incremental ECB rate sync, plus a 30-second
    delayed kickoff so the first refresh happens automatically on every
    container start (catches up missed weeks after downtime)."""
    if os.environ.get("PHOENIX_DISABLE_SCHEDULER") == "1":
        log.info("scheduler: disabled via PHOENIX_DISABLE_SCHEDULER=1")
        return

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.date import DateTrigger
        from datetime import timedelta
        from core import ecb_fx_parser
    except ImportError as e:
        log.warning(f"scheduler: apscheduler not installed ({e}); FX won't auto-refresh. "
                    "Run `pip install -r requirements.txt` to enable.")
        return

    def _run_fx_sync() -> None:
        try:
            conn = db.connect()
            db.init_schema(conn)
            result = ecb_fx_parser.sync_to_db_incremental(conn)
            conn.close()
            log.info(f"fx-scheduler: {result}")
        except Exception as e:
            log.error(f"fx-scheduler: FAILED {type(e).__name__}: {e}")

    scheduler = BackgroundScheduler(daemon=True, timezone="UTC")
    # Weekly cadence: Monday 06:00 UTC. ECB publishes daily rates ~16:00 CET
    # (15:00 UTC) on business days, so 06:00 UTC Monday gets us the previous
    # week + Friday's rate cleanly.
    scheduler.add_job(
        _run_fx_sync,
        trigger=CronTrigger(day_of_week="mon", hour=6, minute=0),
        id="ecb_fx_weekly",
        replace_existing=True,
        misfire_grace_time=24 * 3600,   # if container was off, run within 24h of next boot
    )
    # Startup catch-up: 30 seconds after boot so we don't compete with the
    # other init steps. Runs once.
    from datetime import datetime as _dt
    scheduler.add_job(
        _run_fx_sync,
        trigger=DateTrigger(run_date=_dt.utcnow() + timedelta(seconds=30)),
        id="ecb_fx_startup_catchup",
        replace_existing=True,
    )
    scheduler.start()
    log.info("fx-scheduler: started (weekly Mon 06:00 UTC + startup catch-up in 30s)")


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
_start_fx_scheduler()


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
        # cwd=SRC_DIR so the relative script name "ibkr_flex.py" resolves
        # next to app.py. Using ROOT (project root) would put cwd at "/" in
        # the container and Python couldn't find the script.
        result = processing.run_subprocess(
            cmd, cwd=SRC_DIR, env_extra={"IBKR_FLEX_TOKEN": token},
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


# ---------------------------------------------------------------------------
# Manual trade entry. Users add stocks / crypto the engine doesn't ingest
# automatically (off-IBKR trades, manual corrections, etc.). Stored with
# is_manual=1 so they survive re-ingest and can be deleted later.
# ---------------------------------------------------------------------------

@app.route("/trades/manual", methods=["POST"])
@limiter.limit("30 per minute")
def trades_manual_add():
    """Create a manual trade row. Body (JSON):

      {
        "account_code": "P",           # required
        "symbol": "BTC",               # required, will be UPPER'd
        "asset_class": "stock"|"crypto",
        "trade_date": "YYYY-MM-DD",
        "side": "buy"|"sell",
        "quantity": 0.5,               # always positive — sign comes from `side`
        "price": 62000.00,
        "currency": "USD",
        "commission": 0.0,             # optional, defaults 0
        "description": "..."           # optional
      }
    """
    data = request.get_json(silent=True) or {}

    # Pull + sanitise. Don't trust the client.
    account_code = (data.get("account_code") or "").strip().upper()
    symbol = (data.get("symbol") or "").strip().upper()
    asset_class = (data.get("asset_class") or "").strip().lower()
    trade_date = (data.get("trade_date") or "").strip()
    side = (data.get("side") or "").strip().lower()
    currency = (data.get("currency") or "USD").strip().upper()
    description = (data.get("description") or "").strip()[:200]   # cap length

    # Validate enums.
    errors = []
    if not account_code:
        errors.append("account_code is required")
    if not symbol or len(symbol) > 12 or not symbol.replace(".", "").replace("-", "").isalnum():
        errors.append("symbol must be 1-12 alphanumeric chars (dot/dash ok)")
    if asset_class not in ("stock", "crypto"):
        errors.append("asset_class must be 'stock' or 'crypto'")
    if side not in ("buy", "sell"):
        errors.append("side must be 'buy' or 'sell'")
    try:
        datetime.strptime(trade_date, "%Y-%m-%d")
    except ValueError:
        errors.append("trade_date must be YYYY-MM-DD")
    if not currency.isalpha() or len(currency) != 3:
        errors.append("currency must be a 3-letter ISO code (USD, EUR, GBP, ...)")

    # Numeric fields.
    try:
        quantity = float(data.get("quantity") or 0)
        if quantity <= 0:
            errors.append("quantity must be > 0")
    except (TypeError, ValueError):
        errors.append("quantity must be a number"); quantity = 0
    try:
        price = float(data.get("price") or 0)
        if price < 0:
            errors.append("price cannot be negative")
    except (TypeError, ValueError):
        errors.append("price must be a number"); price = 0
    try:
        commission = float(data.get("commission") or 0)
        if commission < 0:
            errors.append("commission cannot be negative")
    except (TypeError, ValueError):
        errors.append("commission must be a number"); commission = 0

    if errors:
        return jsonify({"ok": False, "errors": errors}), 400

    # Confirm the account exists before we insert (also catches typos).
    accs = account_service.get_accounts()
    if account_code not in accs:
        return jsonify({"ok": False, "errors": [f"unknown account_code {account_code!r}"]}), 400

    conn = db.connect()
    db.init_schema(conn)
    try:
        trade_id = db.insert_manual_trade(
            conn,
            account_code=account_code,
            symbol=symbol,
            asset_class=asset_class,
            trade_date=trade_date,
            side=side,
            quantity=quantity,
            price=price,
            currency=currency,
            commission=commission,
            description=description,
        )
    except ValueError as e:
        conn.close()
        return jsonify({"ok": False, "errors": [str(e)]}), 400
    conn.close()
    log.info(f"trades/manual: added id={trade_id} {side} {quantity} {symbol} @ {price} {currency} for {account_code}")
    return jsonify({"ok": True, "trade_id": trade_id})


@app.route("/trades/manual/<int:trade_id>", methods=["DELETE"])
@limiter.limit("30 per minute")
def trades_manual_delete(trade_id: int):
    """Delete a manual trade. account_code is required as a safety check —
    you can only delete trades within the account you specify, and only if
    is_manual=1 (IBKR-imported rows are off-limits)."""
    account_code = (request.args.get("account") or "").strip().upper()
    if not account_code:
        return jsonify({"ok": False, "error": "missing ?account=<code>"}), 400

    conn = db.connect()
    db.init_schema(conn)
    deleted = db.delete_manual_trade(conn, trade_id=trade_id, account_code=account_code)
    conn.close()
    if not deleted:
        return jsonify({
            "ok": False,
            "error": "trade not found, not manual, or belongs to a different account",
        }), 404
    log.info(f"trades/manual: deleted id={trade_id} from {account_code}")
    return jsonify({"ok": True, "trade_id": trade_id})


@app.route("/trades/manual", methods=["GET"])
@limiter.limit("60 per minute")
def trades_manual_list():
    """List all manual trades for one account (used by the dashboard)."""
    account_code = (request.args.get("account") or "").strip().upper()
    if not account_code:
        return jsonify({"ok": False, "error": "missing ?account=<code>"}), 400
    conn = db.connect()
    db.init_schema(conn)
    rows = db.list_manual_trades(conn, account_code).to_dict("records")
    conn.close()
    return jsonify({"ok": True, "rows": rows})


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


# ---------------------------------------------------------------------------
# FX rate maintenance (ECB EUR/USD reference rates).
# ---------------------------------------------------------------------------
# These two routes back the Settings page's FX section. The scheduled job
# below also calls into the same sync function so manual + automatic paths
# share one code path.

@app.route("/fx/status")
@limiter.limit("60 per minute")
def fx_status():
    """Return the latest date in fx_rates and the total row count."""
    conn = db.connect()
    db.init_schema(conn)
    row = conn.execute("SELECT MAX(date), COUNT(*) FROM fx_rates").fetchone()
    conn.close()
    return jsonify({"max_date": row[0], "row_count": row[1] or 0})


@app.route("/fx/refresh", methods=["POST"])
@limiter.limit("10 per minute")
def fx_refresh():
    """Manually trigger an incremental ECB EUR/USD sync. Idempotent — safe
    to call repeatedly. The scheduled weekly job calls the same underlying
    function."""
    from core import ecb_fx_parser
    conn = db.connect()
    db.init_schema(conn)
    try:
        result = ecb_fx_parser.sync_to_db_incremental(conn)
        conn.close()
        log.info(f"fx/refresh: {result}")
        return jsonify({"ok": True, **result})
    except Exception as e:
        conn.close()
        log.error(f"fx/refresh FAILED: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/settings")
def settings_page():
    """Settings page hosting destructive operations (delete account, empty DB)
    and maintenance actions (refresh FX rates). Moved off the dashboard so
    these ops aren't one mis-click away."""
    accs = account_service.get_accounts()
    statuses = {
        a["name"]: account_service.report_status(a["name"], downloaded_dir=DOWNLOADED_DIR)
        for a in accs.values()
    }
    conn = db.connect()
    db.init_schema(conn)
    share_links = db.list_share_links(conn).to_dict("records")
    conn.close()
    # Enrich each share link with the human-readable account name for display.
    code_to_name = {code: a["name"] for code, a in accs.items()}
    for s in share_links:
        s["account_name"] = code_to_name.get(s["account_code"], s["account_code"])
    return render_template(
        "settings.html",
        accounts_full=accs,
        statuses=statuses,
        share_links=share_links,
    )


# ---------------------------------------------------------------------------
# Read-only share links. Anyone with the URL gets in (no basic auth).
# The token is the credential; revoke or delete it to cut access.
# ---------------------------------------------------------------------------

# Reports the dashboard knows how to render (must match the keys used by the
# main dashboard's showReport() in dashboard.js). Used to validate the tab
# list when creating a share link.
KNOWN_REPORT_KINDS = ("tob", "pnl", "performance", "cgt", "dividends", "methodology")


def _share_or_404(token: str):
    """Validate a share token, touch its last-accessed timestamp, return the
    share-link dict or abort(404). Centralised so every share route behaves
    identically — same response for revoked / expired / nonexistent tokens,
    no information leaked about which case it was."""
    conn = db.connect()
    db.init_schema(conn)
    share = db.validate_share_token(conn, token)
    if share is None:
        conn.close()
        abort(404)
    db.touch_share_link_access(conn, share["id"])
    conn.close()
    return share


@app.route("/share/<token>")
@limiter.limit("60 per minute")
def share_dashboard(token: str):
    """View-only dashboard scoped to one account and a fixed set of tabs.
    The token IS the credential — no basic-auth prompt, no edit operations,
    no account switcher. Iframe sub-requests go to /share/<token>/report/<k>."""
    share = _share_or_404(token)

    accs = account_service.get_accounts()
    code_to_name = {code: a["name"] for code, a in accs.items()}
    account_name = code_to_name.get(share["account_code"], share["account_code"])
    account_type = (accs.get(share["account_code"]) or {}).get("type", "personal")

    allowed_tabs = [t for t in share["allowed_tabs"].split(",") if t]
    # Filter to the tabs Phoenix actually knows how to render. Defends
    # against stale data if the report set ever shrinks.
    allowed_tabs = [t for t in allowed_tabs if t in KNOWN_REPORT_KINDS]

    return render_template(
        "share_dashboard.html",
        token=token,
        account_name=account_name,
        account_type=account_type,
        allowed_tabs=allowed_tabs,
        label=share.get("label") or "",
        expires_at=share.get("expires_at"),
        created_at=share.get("created_at"),
    )


@app.route("/share/<token>/report/<kind>")
@limiter.limit("60 per minute")
def share_report(token: str, kind: str):
    """Render the report inside the share dashboard's iframe. Validates the
    token, checks the requested kind is in the share's allowed_tabs, then
    renders that report for the share's account_code. 404 on any failure —
    don't reveal whether the issue was the token, the tab, or the account."""
    share = _share_or_404(token)

    allowed = set(t for t in share["allowed_tabs"].split(",") if t)
    # Performance is rendered by the P&L builder with a ?tab=performance
    # query string (mirrors the main dashboard), so 'pnl' access is granted
    # if either 'pnl' or 'performance' is in the share's allowed_tabs.
    if kind == "pnl" and ("pnl" in allowed or "performance" in allowed):
        pass
    elif kind in allowed and kind in KNOWN_REPORT_KINDS:
        pass
    else:
        abort(404)

    code = share["account_code"]
    log.info(f"share/report: token-id={share['id']} kind={kind} account={code}")
    try:
        if kind == "tob":
            from reports import tob as _tob
            html = _tob.build_tob_html(code)
        elif kind in ("pnl", "performance"):
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
            abort(404)
    except Exception as e:
        log.error(f"share/report FAILED kind={kind}: {e}")
        log.error(traceback.format_exc())
        raise
    return Response(html, mimetype="text/html; charset=utf-8")


# ---------- Admin routes for managing share links ----------

@app.route("/share-links", methods=["POST"])
@limiter.limit("10 per minute")
def share_links_create():
    """Admin-only: create a new view-only share link. Body (JSON):

        {
          "account_code": "B",
          "allowed_tabs": ["tob", "pnl", "dividends"],
          "label": "Accountant 2025 review",
          "expires_in_days": 30          # optional; null/missing = no expiry
        }
    """
    data = request.get_json(silent=True) or {}
    account_code = (data.get("account_code") or "").strip().upper()
    allowed_tabs = data.get("allowed_tabs") or []
    label = (data.get("label") or "").strip()
    expires_in_days = data.get("expires_in_days")

    errors = []
    accs = account_service.get_accounts()
    if account_code not in accs:
        errors.append(f"unknown account_code {account_code!r}")
    if not isinstance(allowed_tabs, list) or not allowed_tabs:
        errors.append("allowed_tabs must be a non-empty list")
    else:
        bad = [t for t in allowed_tabs if t not in KNOWN_REPORT_KINDS]
        if bad:
            errors.append(f"unknown tabs: {bad}. Known: {list(KNOWN_REPORT_KINDS)}")
    expires_at = None
    if expires_in_days not in (None, "", 0):
        try:
            days = int(expires_in_days)
            if days <= 0 or days > 3650:    # 10 years cap
                errors.append("expires_in_days must be 1..3650")
            else:
                from datetime import timedelta
                expires_at = (datetime.utcnow() + timedelta(days=days)).isoformat(timespec="seconds")
        except (TypeError, ValueError):
            errors.append("expires_in_days must be an integer")
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400

    conn = db.connect()
    db.init_schema(conn)
    try:
        share = db.create_share_link(
            conn,
            account_code=account_code,
            allowed_tabs=allowed_tabs,
            label=label,
            expires_at=expires_at,
        )
    except Exception as e:
        conn.close()
        return jsonify({"ok": False, "errors": [str(e)]}), 400
    conn.close()
    log.info(f"share-links/create: id={share['id']} account={account_code} "
             f"tabs={share['allowed_tabs']} expires={expires_at}")
    return jsonify({"ok": True, "share": share})


@app.route("/share-links/<int:share_id>/revoke", methods=["POST"])
@limiter.limit("30 per minute")
def share_links_revoke(share_id: int):
    """Revoke (don't delete) a share link. Idempotent. The row is kept so the
    admin sees the history in the settings table."""
    conn = db.connect()
    db.init_schema(conn)
    ok = db.revoke_share_link(conn, share_id)
    conn.close()
    if not ok:
        return jsonify({"ok": False, "error": "share link not found"}), 404
    log.info(f"share-links/revoke: id={share_id}")
    return jsonify({"ok": True})


@app.route("/share-links/<int:share_id>", methods=["DELETE"])
@limiter.limit("30 per minute")
def share_links_delete(share_id: int):
    """Hard-delete a share link. No audit trail. Use revoke if you want
    to keep the historical record."""
    conn = db.connect()
    db.init_schema(conn)
    ok = db.delete_share_link(conn, share_id)
    conn.close()
    if not ok:
        return jsonify({"ok": False, "error": "share link not found"}), 404
    log.info(f"share-links/delete: id={share_id}")
    return jsonify({"ok": True})


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
