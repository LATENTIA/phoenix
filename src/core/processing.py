"""
Subprocess + ingest invocation helpers used by the Flask app.

These wrap the CLI scripts (`ibkr_flex.py`, `ingest.py`) so the Flask layer
stays free of I/O concerns. Every helper logs to the project's `ibkr.processing`
logger.
"""

import logging
import os
import re
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET

import ingest


log = logging.getLogger("ibkr.processing")


_FRIENDLY_PREFIXES = ("[flex]", "[ecb]", "[ingest]")


# Regex pairs for redacting secrets in command-line strings before they hit
# log files, the browser console, or anywhere else they shouldn't go.
# Covers both forms: `--token X` (space-separated) and `--token=X` (equals).
# Kept conservative: only matches our own flag names, never aggressive enough
# to mangle legitimate args.
_SECRET_FLAGS = ("token", "password", "secret", "api-key", "apikey")
_REDACT_PATTERNS = [
    re.compile(rf"(--{flag})(\s+|=)(\S+)", re.IGNORECASE)
    for flag in _SECRET_FLAGS
]


def _redact_cmd(cmd) -> str:
    """Turn a command iterable or string into a log-safe string with any
    `--token <value>` / `--password=<value>` / etc. masked to `****`.

    Defensive even though the modern code paths pass secrets via env vars:
    if a token ever leaks back into argv (legacy call site, third-party
    library, copy-pasted command in a comment), this still keeps it out
    of logs."""
    s = cmd if isinstance(cmd, str) else " ".join(str(p) for p in cmd)
    for pat in _REDACT_PATTERNS:
        s = pat.sub(r"\1\2****", s)
    return s


def _extract_friendly_message(stderr: str, *, returncode: int) -> str | None:
    """Pull one human-readable line out of subprocess stderr for display in the UI.

    Order of preference:
      1. The first line tagged with one of our script prefixes ([flex], [ecb], [ingest]).
      2. The last line of a Python traceback (the exception summary).
      3. The first non-empty line.

    Returns None on success (returncode 0) or if stderr is empty.
    """
    if not stderr or returncode == 0:
        return None
    for raw in stderr.splitlines():
        line = raw.strip()
        if line.startswith(_FRIENDLY_PREFIXES):
            return line
    lines = [line for line in stderr.strip().splitlines() if line.strip()]
    if lines and lines[0].startswith("Traceback"):
        return lines[-1].strip()
    return lines[0].strip() if lines else None


def run_subprocess(
    cmd: Iterable[str],
    *,
    cwd: Path,
    timeout: int = 300,
    env_extra: dict | None = None,
) -> dict:
    """
    Run a Python subprocess, capture output, return a dict the Flask layer can
    serialize back to the browser as JSON.

    `env_extra` adds/overrides env vars for the child without touching the
    parent process. Use it to pass secrets (e.g. `IBKR_FLEX_TOKEN`) without
    putting them on the command line, where they'd show up in `/proc/*/cmdline`.

    Every cmd string that leaves this function (log lines, the returned dict,
    timeout/error messages) goes through `_redact_cmd` so a `--token <hex>`
    accidentally on argv still gets masked before display.
    """
    full_cmd = [sys.executable, *cmd]
    safe_cmd = _redact_cmd(cmd)
    log.info(f"run: cmd={safe_cmd}")

    child_env = None
    if env_extra:
        child_env = {**os.environ, **env_extra}

    start = datetime.now()
    try:
        proc = subprocess.run(
            full_cmd, cwd=cwd, capture_output=True, text=True,
            timeout=timeout, env=child_env,
        )
    except subprocess.TimeoutExpired:
        log.error(f"run: TIMEOUT after {timeout}s — {safe_cmd}")
        return {"cmd": safe_cmd, "returncode": -1,
                "stdout": "", "stderr": f"Timed out after {timeout}s",
                "elapsed_s": float(timeout),
                "friendly_message": f"Timed out after {timeout}s. Try again or check your network."}
    except Exception as e:
        log.error(f"run: FAILED to spawn — {e}")
        return {"cmd": safe_cmd, "returncode": -2,
                "stdout": "", "stderr": str(e), "elapsed_s": 0.0,
                "friendly_message": f"Could not start subprocess: {e}"}
    elapsed = (datetime.now() - start).total_seconds()
    log.info(f"run: done in {elapsed:.1f}s exit={proc.returncode}")
    if proc.stdout:
        log.info(f"run: stdout ({len(proc.stdout)} chars) first 500: "
                 f"{_redact_cmd(proc.stdout[:500])}")
    if proc.stderr:
        log.warning(f"run: stderr ({len(proc.stderr)} chars): "
                    f"{_redact_cmd(proc.stderr[:1000])}")
    return {
        "cmd": safe_cmd,
        "returncode": proc.returncode,
        "stdout": _redact_cmd(proc.stdout),
        "stderr": _redact_cmd(proc.stderr),
        "elapsed_s": elapsed,
        "friendly_message": _extract_friendly_message(
            proc.stderr, returncode=proc.returncode
        ),
    }


def run_ingest(code: str) -> str:
    """Run the ingest in-process for one account; returns a multiline log string."""
    lines: list[str] = []

    def _log_line(msg=""):
        line = str(msg)
        lines.append(line)
        log.info(f"ingest: {line}")

    log.info(f"ingest: starting for account code={code}")
    try:
        summary = ingest.ingest_all(account=code, log=_log_line)
    except Exception as e:
        log.error(f"ingest: FAILED — {e}")
        log.error(traceback.format_exc())
        return f"[ingest] FAILED: {e}"
    s = summary["status"]
    _log_line(f"DB now: trades={s['trades']} ca={s['corporate_actions']} "
              f"xfer={s['transfers']} op={s['open_positions']} fx={s['fx_rates']}")
    return "\n".join(lines)


def year_from_xml(path: Path) -> str | None:
    """Read the FlexStatement's fromDate (YYYYMMDD) and return the year."""
    try:
        root = ET.parse(path).getroot()
        stmt = root.find(".//FlexStatement")
        if stmt is not None:
            from_date = stmt.attrib.get("fromDate", "")
            if len(from_date) >= 4 and from_date[:4].isdigit():
                return from_date[:4]
    except Exception as e:
        log.warning(f"year_from_xml: {e}")
    return None
