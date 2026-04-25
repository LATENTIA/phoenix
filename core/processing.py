"""
Subprocess + ingest invocation helpers used by the Flask app.

These wrap the CLI scripts (`ibkr_flex.py`, `ingest.py`) so the Flask layer
stays free of I/O concerns. Every helper logs to the project's `ibkr.processing`
logger.
"""

import logging
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


def run_subprocess(cmd: Iterable[str], *, cwd: Path, timeout: int = 300) -> dict:
    """
    Run a Python subprocess, capture output, return a dict the Flask layer can
    serialize back to the browser as JSON.
    """
    full_cmd = [sys.executable, *cmd]
    log.info(f"run: cmd={' '.join(cmd)}")
    start = datetime.now()
    try:
        proc = subprocess.run(
            full_cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        log.error(f"run: TIMEOUT after {timeout}s — {' '.join(cmd)}")
        return {"cmd": " ".join(cmd), "returncode": -1,
                "stdout": "", "stderr": f"Timed out after {timeout}s",
                "elapsed_s": float(timeout),
                "friendly_message": f"Timed out after {timeout}s. Try again or check your network."}
    except Exception as e:
        log.error(f"run: FAILED to spawn — {e}")
        return {"cmd": " ".join(cmd), "returncode": -2,
                "stdout": "", "stderr": str(e), "elapsed_s": 0.0,
                "friendly_message": f"Could not start subprocess: {e}"}
    elapsed = (datetime.now() - start).total_seconds()
    log.info(f"run: done in {elapsed:.1f}s exit={proc.returncode}")
    if proc.stdout:
        log.info(f"run: stdout ({len(proc.stdout)} chars) first 500: {proc.stdout[:500]}")
    if proc.stderr:
        log.warning(f"run: stderr ({len(proc.stderr)} chars): {proc.stderr[:1000]}")
    return {
        "cmd": " ".join(cmd),
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
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
