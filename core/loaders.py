"""
IBKR statement-file loaders.

Reads Flex XML and Activity Statement CSV files into normalized pandas DataFrames.
Used by both the ETL pipeline (`ingest.py`) and the report layer (`reports/pnl.py`).

Each loader returns a DataFrame whose columns are stable across XML and CSV sources,
so downstream code can treat them interchangeably.
"""

import csv as csv_mod
import re
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import pandas as pd

from reports._helpers import (
    canon_symbol as _canon_symbol,
    parse_csv_datetime as _parse_csv_datetime,
    parse_flex_datetime as _parse_flex_datetime,
    to_float as _to_float,
)


# ---------- Trade loaders ----------

def load_flex_xml(path: Path) -> pd.DataFrame:
    """Load executions from a Flex XML file into a normalized DataFrame."""
    tree = ET.parse(path)
    root = tree.getroot()
    rows = []
    for el in root.findall(".//Trades/Trade[@levelOfDetail='EXECUTION']"):
        a = el.attrib
        dt_iso = _parse_flex_datetime(a.get("dateTime") or a.get("tradeDate", ""))
        rows.append({
            "source": f"xml:{path.name}",
            "tradeID": a.get("tradeID") or None,
            "dateTime": dt_iso,
            "tradeDate": dt_iso[:10] if dt_iso else None,
            "symbol": _canon_symbol(a.get("symbol", "")),
            "description": a.get("description", ""),
            "assetCategory": a.get("assetCategory", ""),
            "currency": a.get("currency", "USD"),
            "quantity": _to_float(a.get("quantity")),
            "tradePrice": _to_float(a.get("tradePrice")),
            "proceeds_usd": _to_float(a.get("proceeds")),
            "commission_usd": _to_float(a.get("ibCommission")),
        })
    return pd.DataFrame(rows)


def load_statement_csv(path: Path) -> pd.DataFrame:
    """Load order-level trades from an IBKR activity statement CSV."""
    rows = []
    header = None
    with open(path, "r", encoding="utf-8") as f:
        for cols in csv_mod.reader(f):
            if not cols or cols[0] != "Trades":
                continue
            if cols[1] == "Header":
                header = cols
                continue
            if not header or cols[1] != "Data" or len(cols) != len(header):
                continue
            d = dict(zip(header, cols))
            if d.get("DataDiscriminator") != "Order":
                continue
            dt_iso = _parse_csv_datetime(d.get("Date/Time") or "")
            rows.append({
                "source": f"csv:{path.name}",
                "tradeID": None,
                "dateTime": dt_iso,
                "tradeDate": dt_iso[:10] if dt_iso else None,
                "symbol": _canon_symbol(d.get("Symbol", "")),
                "description": "",
                "assetCategory": d.get("Asset Category", ""),
                "currency": d.get("Currency", "USD"),
                "quantity": _to_float(d.get("Quantity")),
                "tradePrice": _to_float(d.get("T. Price")),
                "proceeds_usd": _to_float(d.get("Proceeds")),
                "commission_usd": _to_float(d.get("Comm/Fee")),
            })
    return pd.DataFrame(rows)


# ---------- Corporate-action description parsing ----------

_TAIL_SYMBOL_RE = re.compile(r"\(([A-Z][A-Z0-9.]*)\s*,\s*[^,]+,\s*[A-Z0-9]+\)\s*$")
_SPLIT_RE = re.compile(r"Split\s+(\d+(?:\.\d+)?)\s+for\s+(\d+(?:\.\d+)?)")
_CASH_MERGER_RE = re.compile(r"Merged\(Acquisition\)\s+for\s+USD\s+([\d.]+)\s+per\s+Share")
_STOCK_MERGER_RE = re.compile(r"Merged\(Acquisition\)\s+WITH\s+[A-Z0-9]+\s+(\d+(?:\.\d+)?)\s+for\s+(\d+(?:\.\d+)?)")


def _parse_ca_row(description: str) -> dict | None:
    """Classify a Corporate Actions description into a type + params."""
    desc = (description or "").strip()
    if not desc:
        return None
    tail_m = _TAIL_SYMBOL_RE.search(desc)
    tail_sym_raw = tail_m.group(1) if tail_m else ""
    tail_sym = tail_sym_raw.replace(".OLD", "").replace(".NEW", "")

    if "Delisted" in desc:
        return {"type": "delist", "symbol": tail_sym, "desc": desc}
    sm = _SPLIT_RE.search(desc)
    if sm:
        return {
            "type": "split",
            "symbol": tail_sym,
            "ratio_new": float(sm.group(1)),
            "ratio_old": float(sm.group(2)),
            "desc": desc,
        }
    cm = _CASH_MERGER_RE.search(desc)
    if cm:
        return {"type": "cash_merger", "symbol": tail_sym, "per_share": float(cm.group(1)), "desc": desc}
    if "Merged(Acquisition) WITH" in desc:
        mm = _STOCK_MERGER_RE.search(desc)
        return {
            "type": "stock_merger",
            "symbol": tail_sym,
            "new_ratio": float(mm.group(1)) if mm else None,
            "old_ratio": float(mm.group(2)) if mm else None,
            "desc": desc,
        }
    return {"type": "other", "symbol": tail_sym, "desc": desc}


def load_corporate_actions_csv(path: Path) -> pd.DataFrame:
    """Parse the Corporate Actions section of an IBKR activity statement CSV."""
    rows = []
    header = None
    with open(path, "r", encoding="utf-8") as f:
        for cols in csv_mod.reader(f):
            if not cols or cols[0] != "Corporate Actions":
                continue
            if cols[1] == "Header":
                header = cols
                continue
            if not header or cols[1] != "Data" or len(cols) != len(header):
                continue
            d = dict(zip(header, cols))
            desc = d.get("Description", "")
            if not desc.strip():
                continue
            cls = _parse_ca_row(desc)
            if cls is None:
                continue
            dt_iso = _parse_csv_datetime(d.get("Date/Time") or d.get("Report Date") or "")
            rows.append({
                "source": f"csv-ca:{path.name}",
                "dateTime": dt_iso,
                "date": dt_iso[:10] if dt_iso else None,
                "description": desc,
                "type": cls["type"],
                "symbol": _canon_symbol(cls.get("symbol", "")),
                "ratio_new": cls.get("ratio_new"),
                "ratio_old": cls.get("ratio_old"),
                "per_share": cls.get("per_share"),
                "quantity": _to_float(d.get("Quantity")),
                "proceeds_usd": _to_float(d.get("Proceeds")),
                "realized_pnl_usd_ibkr": _to_float(d.get("Realized P/L")),
            })
    return pd.DataFrame(rows)


def load_transfers_csv(path: Path) -> pd.DataFrame:
    """Parse the Transfers section. Each row is a share movement between IBKR accounts."""
    rows = []
    header = None
    with open(path, "r", encoding="utf-8") as f:
        for cols in csv_mod.reader(f):
            if not cols or cols[0] != "Transfers":
                continue
            if cols[1] == "Header":
                header = cols
                continue
            if not header or cols[1] != "Data" or len(cols) != len(header):
                continue
            d = dict(zip(header, cols))
            if not d.get("Symbol"):
                continue
            date = d.get("Date", "").strip()
            try:
                datetime.strptime(date, "%Y-%m-%d")
            except ValueError:
                continue
            qty = _to_float(d.get("Qty"))
            if qty is None or qty == 0:
                continue
            direction = (d.get("Direction") or "").strip().upper()
            market_value = _to_float(d.get("Market Value"))
            per_share = (market_value / qty) if (market_value and qty) else None
            rows.append({
                "source": f"xfer:{path.name}",
                "date": date,
                "symbol": _canon_symbol(d.get("Symbol", "")),
                "direction": direction,
                "quantity": qty,
                "market_value_usd": market_value,
                "per_share_usd": per_share,
                "asset_category": d.get("Asset Category", ""),
                "xfer_account": d.get("Xfer Account", ""),
            })
    return pd.DataFrame(rows)


def load_open_positions_xml(path: Path) -> tuple[pd.DataFrame, str | None]:
    """Parse <OpenPositions> in a Flex XML; returns (positions_df, as_of_date)."""
    tree = ET.parse(path)
    root = tree.getroot()
    rows = []
    for el in root.findall(".//OpenPositions/OpenPosition"):
        a = el.attrib
        rows.append({
            "symbol": _canon_symbol(a.get("symbol", "")),
            "quantity": _to_float(a.get("position") or a.get("quantity")),
            "currency": a.get("currency", "USD"),
        })
    as_of = None
    stmt = root.find(".//FlexStatement")
    if stmt is not None:
        to_date = stmt.attrib.get("toDate", "")
        try:
            as_of = datetime.strptime(to_date, "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            pass
    return pd.DataFrame(rows), as_of


def load_open_positions_csv(path: Path) -> tuple[pd.DataFrame, str | None]:
    """Parse the Open Positions section. Returns (positions_df, as_of_date)."""
    rows = []
    header = None
    with open(path, "r", encoding="utf-8") as f:
        for cols in csv_mod.reader(f):
            if not cols or cols[0] != "Open Positions":
                continue
            if cols[1] == "Header":
                header = cols
                continue
            if not header or cols[1] != "Data" or len(cols) != len(header):
                continue
            d = dict(zip(header, cols))
            rows.append({
                "symbol": _canon_symbol(d.get("Symbol", "")),
                "quantity": _to_float(d.get("Quantity")),
                "currency": d.get("Currency", "USD"),
            })
    # Derive as-of date from the filename: U..._YYYYMMDD_YYYYMMDD.csv (end date)
    as_of = None
    m = re.search(r"_(\d{8})_(\d{8})", path.name)
    if m:
        try:
            as_of = datetime.strptime(m.group(2), "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            pass
    return pd.DataFrame(rows), as_of


# ---------- Dividend description parsing ----------

# Examples we need to handle:
#   "NKE(US6541061031) Cash Dividend USD 0.37 per Share (Ordinary Dividend)"
#   "BABA(US01609W1027) Cash Dividend USD 1.00 per Share (Ordinary Dividend)"
#   "NKE(US6541061031) Cash Dividend USD 0.37 per Share - US Tax"   (WHT row)
#   "JD(US47215P1066) Payment in Lieu of Dividend (Ordinary Dividend)"  (no per-share)
#   "VALE(US91912E1055) Cash Dividend USD 0.0481 per Share - BR Tax"
_DIV_HEAD_RE = re.compile(
    r"^(?P<symbol>[A-Z][A-Z0-9.]*)\s*\((?P<isin>[A-Z0-9]+)\)\s*"
)
_DIV_PERSHARE_RE = re.compile(
    r"Cash Dividend\s+(?P<ccy>[A-Z]{3})\s+(?P<per_share>[\d.]+)\s+per Share"
)
_DIV_PIL_RE = re.compile(r"Payment in Lieu of Dividend", re.IGNORECASE)
_DIV_TYPE_RE = re.compile(r"\((?P<type>[^)]+)\)\s*$")
_WHT_COUNTRY_RE = re.compile(r"-\s*(?P<country>[A-Z]+)\s+Tax\b")


# IBKR labels these as "Dividend" cash transactions at the top level, but the
# trailing tag in the description tells us they're capital events, not income:
#   InterimLiquidation     — bankruptcy estate distributing cash
#   LiquidationDividend    — final liquidation payout
#   Return Of Capital      — partial return of original investment (REITs etc.)
#   Capital Gains Distribution — mutual-fund cap-gains pass-through (different tax treatment)
# These should be netted against the cost basis in the CGT report (not yet
# implemented — follow-up). For the dividend report they must be excluded so
# they don't get taxed as ordinary dividend income.
NON_DIVIDEND_TYPES = {
    "interimliquidation",
    "liquidationdividend",
    "liquidation",
    "return of capital",
    "capital gains distribution",
}


def _is_non_dividend_payout(desc: str, parsed_type: str) -> bool:
    """True if a description / parsed-type indicates this is a capital event,
    not a real dividend payout. Case-insensitive substring match against the
    NON_DIVIDEND_TYPES set."""
    pt = (parsed_type or "").strip().lower()
    if pt in NON_DIVIDEND_TYPES:
        return True
    # Fall back to substring scan in the raw description, just in case the
    # parser missed the trailing-paren tag for an unusual format.
    desc_low = (desc or "").lower()
    return any(token in desc_low for token in NON_DIVIDEND_TYPES)


def _parse_dividend_description(desc: str) -> dict:
    """Extract symbol, ISIN, per-share amount, type, and WHT-country (if any).

    Robust to several IBKR description variants:
      - "Cash Dividend USD X per Share (Type)"
      - "Cash Dividend USD X per Share - CC Tax"   (WHT row)
      - "Payment in Lieu of Dividend (Type)"        (short-sale PIL row, no per-share)

    Returns a dict with: symbol, isin, per_share, dividend_type, source_country.
    Empty strings / None for missing pieces.
    """
    out = {"symbol": "", "isin": "", "per_share": None,
           "dividend_type": "", "source_country": ""}
    if not desc:
        return out
    s = desc.strip()
    m = _DIV_HEAD_RE.match(s)
    if not m:
        return out
    out["symbol"] = m.group("symbol")
    out["isin"] = m.group("isin")

    pm = _DIV_PERSHARE_RE.search(s)
    if pm:
        try:
            out["per_share"] = float(pm.group("per_share"))
        except (TypeError, ValueError):
            out["per_share"] = None
    elif _DIV_PIL_RE.search(s):
        # Payment in Lieu has no per-share figure; flag the type.
        out["dividend_type"] = "Payment in Lieu"

    tm = _DIV_TYPE_RE.search(s)
    if tm:
        # Don't overwrite an explicit "Payment in Lieu" tag set above.
        if not out["dividend_type"]:
            out["dividend_type"] = tm.group("type").strip()

    cm = _WHT_COUNTRY_RE.search(s)
    if cm:
        out["source_country"] = cm.group("country")
    return out


# ---------- Dividend / withholding-tax loaders (CSV) ----------

def load_dividends_csv(path: Path) -> pd.DataFrame:
    """Parse the 'Dividends' section of an IBKR Activity Statement CSV.

    Columns (post-parsing): pay_date, symbol, isin, description, currency,
    amount, per_share, dividend_type, source.
    Returns an empty DataFrame if the section is missing or empty.
    """
    rows = []
    header = None
    with open(path, "r", encoding="utf-8") as f:
        for cols in csv_mod.reader(f):
            if not cols or cols[0] != "Dividends":
                continue
            if cols[1] == "Header":
                header = cols
                continue
            if not header or cols[1] != "Data" or len(cols) != len(header):
                continue
            d = dict(zip(header, cols))
            desc = d.get("Description", "") or ""
            # Skip "Total" / aggregate rows that IBKR sometimes emits.
            if desc.strip().lower().startswith("total"):
                continue
            parsed = _parse_dividend_description(desc)
            if not parsed["symbol"]:
                continue
            # Skip capital events (liquidations, return-of-capital). These
            # get netted against the cost basis, not taxed as dividend income.
            if _is_non_dividend_payout(desc, parsed["dividend_type"]):
                continue
            rows.append({
                "source": f"csv-div:{path.name}",
                "pay_date": (d.get("Date") or "").strip(),
                "symbol": _canon_symbol(parsed["symbol"]),
                "isin": parsed["isin"],
                "description": desc,
                "currency": (d.get("Currency") or "USD").strip(),
                "amount": _to_float(d.get("Amount")),
                "per_share": parsed["per_share"],
                "dividend_type": parsed["dividend_type"],
            })
    return pd.DataFrame(rows)


def load_withholding_csv(path: Path) -> pd.DataFrame:
    """Parse the 'Withholding Tax' section. Rows pair to dividend rows by
    (pay_date, symbol, per_share). Amount is typically negative.

    Columns: pay_date, symbol, isin, description, currency, amount, per_share,
    source_country, code, source.
    """
    rows = []
    header = None
    with open(path, "r", encoding="utf-8") as f:
        for cols in csv_mod.reader(f):
            if not cols or cols[0] != "Withholding Tax":
                continue
            if cols[1] == "Header":
                header = cols
                continue
            if not header or cols[1] != "Data" or len(cols) != len(header):
                continue
            d = dict(zip(header, cols))
            desc = d.get("Description", "") or ""
            if desc.strip().lower().startswith("total"):
                continue
            parsed = _parse_dividend_description(desc)
            if not parsed["symbol"]:
                continue
            # Skip WHT rows attached to capital events (liquidations etc.).
            # The matching dividend row is excluded too, so the pair vanishes.
            if _is_non_dividend_payout(desc, parsed.get("dividend_type", "")):
                continue
            rows.append({
                "source": f"csv-wht:{path.name}",
                "pay_date": (d.get("Date") or "").strip(),
                "symbol": _canon_symbol(parsed["symbol"]),
                "isin": parsed["isin"],
                "description": desc,
                "currency": (d.get("Currency") or "USD").strip(),
                "amount": _to_float(d.get("Amount")),
                "per_share": parsed["per_share"],
                "source_country": parsed["source_country"],
                "code": (d.get("Code") or "").strip(),
            })
    return pd.DataFrame(rows)


# ---------- Dividend / withholding-tax loaders (XML) ----------

def load_dividends_xml(path: Path) -> pd.DataFrame:
    """Parse <CashTransactions type='Dividends'> from a Flex XML.

    Most users don't have this section enabled in their Flex Query; in that
    case the function returns an empty DataFrame and the caller should fall
    back to the CSV loader. To enable: in IBKR Client Portal, edit the Flex
    Query and add the 'Cash Transactions' section with type 'Dividends' and
    'Withholding Tax'.
    """
    rows = []
    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except ET.ParseError:
        return pd.DataFrame()

    for el in root.findall(".//CashTransactions/CashTransaction"):
        a = el.attrib
        ttype = (a.get("type") or "").lower()
        # IBKR uses "Dividends" or "Payment In Lieu Of Dividends" here.
        if "dividend" not in ttype:
            continue
        if "withholding" in ttype or "tax" in ttype:
            continue
        desc = a.get("description") or ""
        parsed = _parse_dividend_description(desc)
        if not parsed["symbol"]:
            # Fall back to the symbol attribute if description didn't parse.
            parsed["symbol"] = a.get("symbol", "") or ""
            if not parsed["symbol"]:
                continue
        # Skip capital events. The XML loader is the critical one for the RGTPQ
        # InterimLiquidation case because that data only lives in the Flex XML.
        if _is_non_dividend_payout(desc, parsed.get("dividend_type", "")):
            continue
        rows.append({
            "source": f"xml-div:{path.name}",
            "pay_date": _xml_date_to_iso(a.get("dateTime") or a.get("settleDate", "")),
            "symbol": _canon_symbol(parsed["symbol"]),
            "isin": parsed["isin"] or (a.get("isin") or ""),
            "description": desc,
            "currency": a.get("currency", "USD"),
            "amount": _to_float(a.get("amount")),
            "per_share": parsed["per_share"],
            "dividend_type": parsed["dividend_type"] or a.get("type", ""),
        })
    return pd.DataFrame(rows)


def load_withholding_xml(path: Path) -> pd.DataFrame:
    """Parse <CashTransactions type='Withholding Tax'> from a Flex XML.
    Returns empty DataFrame when the section isn't enabled in the Flex Query."""
    rows = []
    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except ET.ParseError:
        return pd.DataFrame()

    for el in root.findall(".//CashTransactions/CashTransaction"):
        a = el.attrib
        ttype = (a.get("type") or "").lower()
        if "withholding" not in ttype:
            continue
        desc = a.get("description") or ""
        parsed = _parse_dividend_description(desc)
        if not parsed["symbol"]:
            parsed["symbol"] = a.get("symbol", "") or ""
            if not parsed["symbol"]:
                continue
        # Skip WHT rows that pair with a capital event.
        if _is_non_dividend_payout(desc, parsed.get("dividend_type", "")):
            continue
        rows.append({
            "source": f"xml-wht:{path.name}",
            "pay_date": _xml_date_to_iso(a.get("dateTime") or a.get("settleDate", "")),
            "symbol": _canon_symbol(parsed["symbol"]),
            "isin": parsed["isin"] or (a.get("isin") or ""),
            "description": desc,
            "currency": a.get("currency", "USD"),
            "amount": _to_float(a.get("amount")),
            "per_share": parsed["per_share"],
            "source_country": parsed["source_country"],
            "code": a.get("code", ""),
        })
    return pd.DataFrame(rows)


def _xml_date_to_iso(raw: str) -> str:
    """Flex XML dates: '20240115' or '20240115;093200' or '2024-01-15'.
    Normalize to ISO 'YYYY-MM-DD'."""
    if not raw:
        return ""
    s = raw.split(";")[0].strip()
    # Already ISO?
    if "-" in s and len(s) >= 10:
        return s[:10]
    # Compact YYYYMMDD
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s
