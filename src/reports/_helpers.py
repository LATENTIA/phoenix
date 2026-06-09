"""
Shared utilities used by both the TOB and P&L report generators.
Imported as: `from reports import _helpers`
"""

import argparse
from datetime import datetime

import pandas as pd


# Account aliases (P/B short codes ↔ name) and tax-relevant types.
ACCOUNT_ALIASES = {"P": "personal", "B": "business"}
ACCOUNT_LETTER = {"personal": "P", "business": "B"}

# Ticker renames: IBKR may display a symbol differently across years.
# Map them to a single canonical symbol so positions/trades line up.
# Add your own entries as needed, e.g. {"ABCDEF": "ABC", "XYZ.OLD": "XYZ"}.
# (Most of the work is now done automatically by the symbol-change detector
# in `reports/pnl.py`; this map is for cosmetic canonicalization only.)
TICKER_ALIASES: dict[str, str] = {}


def resolve_account(value: str) -> str:
    """argparse type — accept P/B/personal/business, return the full lowercase name."""
    if value in ACCOUNT_ALIASES.values():
        return value
    up = value.upper()
    if up in ACCOUNT_ALIASES:
        return ACCOUNT_ALIASES[up]
    raise argparse.ArgumentTypeError(
        f"Unknown account '{value}'. Use P|personal or B|business."
    )


def canon_symbol(sym) -> str:
    """Apply ticker-rename map so the same security has one canonical name."""
    if sym is None:
        return ""
    s = str(sym).strip()
    return TICKER_ALIASES.get(s, s)


def to_float(v):
    """Best-effort float coercion. Strips comma thousand separators. Returns None for blanks/junk."""
    if v is None or v == "":
        return None
    if isinstance(v, str):
        v = v.replace(",", "").strip()
        if not v:
            return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_flex_datetime(raw: str) -> str | None:
    """Parse IBKR Flex datetime like '20260114;093734' → ISO 'YYYY-MM-DD HH:MM:SS'."""
    if not raw:
        return None
    raw = raw.replace(";", " ").strip()
    for fmt in ("%Y%m%d %H%M%S", "%Y%m%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def parse_csv_datetime(raw: str) -> str | None:
    """Parse IBKR CSV datetime like '2025-04-08, 15:24:49' → ISO 'YYYY-MM-DD HH:MM:SS'."""
    if not raw:
        return None
    raw = raw.replace(",", " ").strip()
    for fmt in ("%Y-%m-%d  %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def fmt_date(yyyymmdd: str) -> str:
    """'20260114' → '2026-01-14'. Pass-through for anything that's not 8 digits."""
    if not yyyymmdd or len(yyyymmdd) != 8:
        return yyyymmdd or ""
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


def fmt_when(when_generated: str) -> str:
    """'20260423;055521' → '2026-04-23 05:55:21'."""
    if not when_generated or ";" not in when_generated:
        return when_generated or ""
    d, t = when_generated.split(";", 1)
    try:
        dt = datetime.strptime(d + t, "%Y%m%d%H%M%S")
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return when_generated


def fmt_num(val, decimals: int = 2) -> str:
    """Number formatter that returns '' on blanks/NaN."""
    if pd.isna(val) or val == "":
        return ""
    try:
        return f"{float(val):,.{decimals}f}"
    except (ValueError, TypeError):
        return str(val)


def fmt_qty(v):
    """Integer for whole numbers (stocks); up to 8 decimals for fractional (crypto)."""
    if v is None or pd.isna(v):
        return ""
    f = float(v)
    if abs(f - round(f)) < 1e-9:
        return f"{int(round(f)):,}"
    return f"{f:,.8f}".rstrip("0").rstrip(".")
