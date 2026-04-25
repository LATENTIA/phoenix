"""
IBKR Flex Web Service connector.

Two-step flow per IBKR spec:
  1) SendRequest  -> returns a ReferenceCode
  2) GetStatement -> returns the statement (may 'warn' that it is not yet ready)

Docs: https://www.interactivebrokers.com/campus/ibkr-api-page/flex-web-service/

Setup (one-time, in Client Portal for each IBKR account):
  Reports -> Flex Queries -> create an "Activity Flex Query"
    - Output format: XML
    - Sections: at minimum "Trades"
    - Period: Year to Date (or whatever fits)
  Reports -> Flex Queries -> "Flex Web Service" -> enable + generate a token.

Usage:
    python ibkr_flex.py --account personal
    python ibkr_flex.py --account business

Tokens are read from the env vars declared in ACCOUNTS below.
Downloaded files go to ./downloaded/<account>.xml
"""

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

import requests


FLEX_BASE = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
SEND_REQUEST_URL = f"{FLEX_BASE}/SendRequest"
GET_STATEMENT_URL = f"{FLEX_BASE}/GetStatement"
API_VERSION = "3"

# IBKR "statement not yet ready" warn code — must poll/retry.
NOT_READY_CODE = "1019"

DOWNLOAD_DIR = Path("downloaded")

# Legacy CLI fallback: if you run `python ibkr_flex.py -a personal` directly
# (without --token / --query-id flags), it reads from this dict. The normal
# path is to add accounts via the dashboard's "Add account" flow, which
# stores everything in the local DB. Fill in your own values here only if
# you want CLI-only use without the dashboard.
ACCOUNTS: dict[str, dict] = {
    "personal": {
        "token_env": "IBKR_FLEX_TOKEN",
        "queries": {
            "ytd": "",   # paste your IBKR Flex Query ID here for CLI use
        },
    },
    "business": {
        "token_env": "IBKR_FLEX_TOKEN_BUSINESS",
        "queries": {
            "ytd": "",   # paste your IBKR Flex Query ID here for CLI use
        },
    },
}

DEFAULT_PERIOD = "ytd"

# CLI shorthand: P -> personal, B -> business
ACCOUNT_ALIASES = {"P": "personal", "B": "business"}


def resolve_account(value: str) -> str:
    """Map shorthand (P/B) or full name to a canonical account key."""
    if value in ACCOUNTS:
        return value
    if value in ACCOUNT_ALIASES:
        return ACCOUNT_ALIASES[value]
    up = value.upper()
    if up in ACCOUNT_ALIASES:
        return ACCOUNT_ALIASES[up]
    raise argparse.ArgumentTypeError(
        f"Unknown account '{value}'. Use one of: "
        f"{', '.join(sorted(ACCOUNTS))} or {', '.join(sorted(ACCOUNT_ALIASES))}."
    )


class FlexError(RuntimeError):
    """IBKR Flex API error. `code` and `upstream_msg` capture the structured
    parts of IBKR's response so callers can format clean user-facing messages
    instead of dumping a Python traceback."""

    def __init__(self, msg: str, *, code: str = "", upstream_msg: str = ""):
        super().__init__(msg)
        self.code = code
        self.upstream_msg = upstream_msg


def _post(url: str, params: dict) -> str:
    resp = requests.get(
        url,
        params=params,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.text


def request_statement(token: str, query_id: str) -> str:
    """Step 1: request generation and return the ReferenceCode."""
    body = _post(SEND_REQUEST_URL, {"t": token, "q": query_id, "v": API_VERSION})
    root = ET.fromstring(body)

    status = (root.findtext("Status") or "").strip()
    if status != "Success":
        code = (root.findtext("ErrorCode") or "").strip()
        msg = (root.findtext("ErrorMessage") or "").strip()
        raise FlexError(
            f"SendRequest failed: status={status} code={code} msg={msg}",
            code=code, upstream_msg=msg,
        )

    ref_code = (root.findtext("ReferenceCode") or "").strip()
    if not ref_code:
        raise FlexError("SendRequest succeeded but returned no ReferenceCode")
    return ref_code


def fetch_statement(token: str, ref_code: str) -> Optional[str]:
    """
    Step 2: try to download the statement.
    Returns the statement body on success, or None if IBKR says it's not yet ready.
    Raises FlexError for any other failure.
    """
    body = _post(GET_STATEMENT_URL, {"t": token, "q": ref_code, "v": API_VERSION})

    if not body.lstrip().startswith("<FlexStatementResponse"):
        return body

    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise FlexError(f"Could not parse GetStatement response: {e}\n{body[:500]}")

    status = (root.findtext("Status") or "").strip()
    code = (root.findtext("ErrorCode") or "").strip()
    msg = (root.findtext("ErrorMessage") or "").strip()

    if code == NOT_READY_CODE:
        return None
    raise FlexError(
        f"GetStatement failed: status={status} code={code} msg={msg}",
        code=code, upstream_msg=msg,
    )


def download_statement(
    token: str,
    query_id: str,
    *,
    max_wait_s: int = 120,
    poll_interval_s: int = 5,
) -> str:
    """Full two-step flow with polling. Returns the statement body."""
    ref_code = request_statement(token, query_id)
    print(f"[flex] reference code: {ref_code}")

    deadline = time.monotonic() + max_wait_s
    attempt = 0
    while True:
        attempt += 1
        body = fetch_statement(token, ref_code)
        if body is not None:
            print(f"[flex] statement ready after {attempt} attempt(s)")
            return body
        if time.monotonic() >= deadline:
            raise FlexError(f"Statement not ready after {max_wait_s}s")
        print(f"[flex] not ready, retrying in {poll_interval_s}s...")
        time.sleep(poll_interval_s)


def save_statement(body: str, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(body, encoding="utf-8")
    return out_path


def _extract_year_from_xml(body: str) -> Optional[str]:
    """Parse the FlexStatement header and return the year of fromDate (e.g. '2026')."""
    try:
        root = ET.fromstring(body)
        stmt = root.find(".//FlexStatement")
        if stmt is not None:
            from_date = stmt.attrib.get("fromDate", "")
            if len(from_date) >= 4 and from_date[:4].isdigit():
                return from_date[:4]
    except Exception:
        pass
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Download an IBKR Flex Query statement.")
    parser.add_argument(
        "--account", "-a",
        type=resolve_account,
        help="Account: P|personal or B|business (from ACCOUNTS dict)",
    )
    parser.add_argument(
        "--period", "-p",
        default=DEFAULT_PERIOD,
        help=f"Period key from ACCOUNTS[account]['queries'] (default: {DEFAULT_PERIOD})",
    )
    parser.add_argument("--token", help="Override token (else read from env var for the account)")
    parser.add_argument("--query-id", help="Override query ID (else looked up by account+period)")
    parser.add_argument("--out", help=f"Output file path (default: {DOWNLOAD_DIR}/<account>_<period>.xml)")
    parser.add_argument("--max-wait", type=int, default=120)
    parser.add_argument("--poll", type=int, default=5)
    args = parser.parse_args(argv)

    token = args.token
    query_id = args.query_id
    out_path = args.out

    if args.account:
        cfg = ACCOUNTS[args.account]
        queries = cfg.get("queries", {})
        if not query_id:
            if args.period not in queries:
                available = ", ".join(queries.keys()) or "(none configured)"
                print(
                    f"Period '{args.period}' not configured for account '{args.account}'. "
                    f"Available: {available}. Edit ibkr_flex.py to add it.",
                    file=sys.stderr,
                )
                return 2
            query_id = queries[args.period]
        token = token or os.environ.get(cfg["token_env"])
        # Save under downloaded/<account>/<account>_<period>.xml — one folder per account
        out_path = out_path or str(DOWNLOAD_DIR / args.account / f"{args.account}_{args.period}.xml")
        if not token:
            print(
                f"Missing token for '{args.account}' — set env var {cfg['token_env']} or pass --token.",
                file=sys.stderr,
            )
            return 2

    if not token or not query_id or not out_path:
        print(
            "Missing required args. Either pass --account, or all of --token/--query-id/--out.",
            file=sys.stderr,
        )
        return 2

    body = download_statement(
        token, query_id,
        max_wait_s=args.max_wait,
        poll_interval_s=args.poll,
    )

    # For YTD downloads (default), derive the year from the XML and stamp it
    # into the filename so re-running next year doesn't overwrite this year's file.
    if args.account and args.period == DEFAULT_PERIOD and not args.out:
        year = _extract_year_from_xml(body)
        if year:
            out_path = str(DOWNLOAD_DIR / args.account / f"{args.account}_{year}.xml")

    out = save_statement(body, Path(out_path))
    print(f"[flex] saved to {out} ({len(body)} bytes)")

    # Also refresh the local ECB rate cache so TOB / P&L reports always have
    # the latest daily rates available offline.
    try:
        from core.ecb_fx_parser import refresh_from_ecb
        refresh_from_ecb()
    except Exception as e:
        print(f"[ecb] failed to refresh rate cache: {e}", file=sys.stderr)

    return 0


def _exit_with_friendly_error(prefix: str, message: str, exit_code: int) -> int:
    """Print a single clean stderr line (no traceback) and return an exit code.
    The dashboard surfaces stderr verbatim; keep it short and human-readable.
    """
    print(f"{prefix} {message}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except FlexError as e:
        # Exit 3 = IBKR-side rejection. The message already comes from IBKR
        # and is human-readable (e.g. code 1001 "Statement could not be
        # generated at this time. Please try again shortly.").
        if e.upstream_msg:
            sys.exit(_exit_with_friendly_error(
                "[flex]", f"IBKR error {e.code}: {e.upstream_msg}", 3))
        sys.exit(_exit_with_friendly_error("[flex]", str(e), 3))
    except requests.RequestException as e:
        # Exit 4 = network error talking to ECB / IBKR.
        sys.exit(_exit_with_friendly_error("[flex]", f"network error: {e}", 4))
    except KeyboardInterrupt:
        sys.exit(130)
