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
