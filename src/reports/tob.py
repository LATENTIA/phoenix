"""
Parse an IBKR Flex Query XML statement, add ECB EUR/USD reference rates,
and compute EUR amounts and Belgian TOB (0.35%).

Input:  Flex Query XML produced by ibkr_flex.py (Activity Flex Query, XML format,
        Trades section enabled).
Output: CSV + HTML report in parsed/<account>/<accountId>_<period>_<generated>.{csv,html}
"""

import argparse
import csv as csv_mod
import html
from reports._helpers import (
    ACCOUNT_ALIASES, ACCOUNT_LETTER,
    resolve_account as _resolve_account,
    fmt_date as _fmt_date, fmt_when as _fmt_when, fmt_num as _fmt_num,
)
import sys
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import pandas as pd

from core.ecb_fx_parser import get_eur_usd_rate_for_day

pd.set_option('display.max_columns', None)
pd.set_option('display.max_rows', None)

TOB_RATE = 0.0035

DISPLAY_COLS = [
    'TradeDate', 'symbol', 'description', 'buySell', 'quantity',
    'tradePrice', 'proceeds', 'ibCommission',
    'EUR_USD_Rate', 'Rate_Source', 'Total_EUR', 'TOB',
]


def load_flex_trades(xml_path: Path) -> tuple[pd.DataFrame, dict]:
    """
    Parse Flex Query XML and return (DataFrame, statement metadata).
    The DataFrame has one row per execution (<Trade levelOfDetail='EXECUTION'>).
    Metadata dict contains accountId, fromDate, toDate, whenGenerated, period.
    """
    tree = ET.parse(xml_path)
    root = tree.getroot()

    stmt = root.find(".//FlexStatement")
    meta = dict(stmt.attrib) if stmt is not None else {}

    trade_elements = root.findall(".//Trades/Trade[@levelOfDetail='EXECUTION']")
    if not trade_elements:
        print(f"No EXECUTION-level <Trade> elements found in {xml_path}")
        return pd.DataFrame(), meta

    rows = [dict(el.attrib) for el in trade_elements]
    df = pd.DataFrame(rows)

    print(f"Loaded {len(df)} executions from {xml_path}")
    return df, meta


def load_statement_csv_trades(path: Path) -> pd.DataFrame:
    """
    Load trades from an IBKR activity statement CSV into the same schema as
    load_flex_trades (so they can be concatenated for the TOB report).
    Keeps only Trades/Data rows with DataDiscriminator='Order'.
    """
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
            dt_str = (d.get("Date/Time") or "").split(",")[0].strip()
            try:
                dt = datetime.strptime(dt_str, "%Y-%m-%d")
            except ValueError:
                continue
            qty_raw = d.get("Quantity", "")
            try:
                qty_f = float(qty_raw)
                buysell = "BUY" if qty_f > 0 else ("SELL" if qty_f < 0 else "")
            except ValueError:
                buysell = ""
            rows.append({
                "tradeDate": dt.strftime("%Y%m%d"),
                "symbol": d.get("Symbol", ""),
                "description": "",
                "buySell": buysell,
                "quantity": qty_raw,
                "tradePrice": d.get("T. Price", ""),
                "proceeds": d.get("Proceeds", ""),
                "ibCommission": d.get("Comm/Fee", ""),
                "currency": d.get("Currency", "USD"),
            })
    df = pd.DataFrame(rows)
    print(f"Loaded {len(df)} trades from {path}")
    return df


def _latest_year_xml(account: str) -> Path | None:
    """Find downloaded/<account>_<year>.xml with the highest year. Fall back to _ytd.xml."""
    download_dir = Path("downloaded")
    if not download_dir.exists():
        return None
    candidates = []
    for p in download_dir.glob(f"{account}_*.xml"):
        stem = p.stem  # e.g. 'business_2026'
        suffix = stem[len(account) + 1:] if stem.startswith(account + "_") else ""
        if suffix.isdigit() and len(suffix) == 4:
            candidates.append((int(suffix), p))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]
    legacy = download_dir / f"{account}_ytd.xml"
    return legacy if legacy.exists() else None


def load_rate_overrides(account: str) -> dict[str, float]:
    """Extract per-date EUR/USD overrides from any *_reconciled.csv in downloaded/<account>/."""
    overrides: dict[str, float] = {}
    csv_dir = Path("downloaded") / account
    if not csv_dir.exists():
        return overrides

    for path in sorted(csv_dir.glob("*_reconciled.csv")):
        try:
            df = pd.read_csv(path)
        except Exception as e:
            print(f"[rates] failed to read {path.name}: {e}")
            continue
        if "AssetCategory" in df.columns:
            df = df[df["AssetCategory"].astype(str).str.lower() != "crypto"]
        dt_col = next((c for c in df.columns if c.lower() in ("datetime", "date/time", "date")), None)
        if not dt_col:
            continue
        rate_col = next((c for c in df.columns if c.lower() == "rate"), None)
        for row in df.to_dict("records"):
            dt_raw = str(row[dt_col]).split(",")[0].strip()
            try:
                dt = datetime.strptime(dt_raw, "%Y-%m-%d").date().isoformat()
            except ValueError:
                continue
            rate = None
            if rate_col:
                try:
                    rate = float(row[rate_col])
                except (ValueError, TypeError):
                    rate = None
            if rate is None and "Proceeds" in df.columns and "Total_EUR" in df.columns:
                try:
                    p = abs(float(row["Proceeds"]))
                    t = float(row["Total_EUR"])
                    if t > 0:
                        rate = p / t
                except (ValueError, TypeError):
                    rate = None
            if rate and rate > 0:
                overrides[dt] = rate

    if overrides:
        print(f"[rates] loaded {len(overrides)} override rates from {csv_dir}/")
    return overrides


def add_eur_usd_rate(trades_df: pd.DataFrame, rate_overrides: dict[str, float] | None = None) -> pd.DataFrame:
    """
    Adds TradeDate (YYYY-MM-DD), EUR_USD_Rate, and Rate_Source columns.
    Uses rate_overrides first, falls back to ECB. Fetches each unique date only once.
    """
    if trades_df.empty or 'tradeDate' not in trades_df.columns:
        trades_df['TradeDate'] = None
        trades_df['EUR_USD_Rate'] = None
        trades_df['Rate_Source'] = None
        return trades_df

    rate_overrides = rate_overrides or {}
    trades_df['TradeDate'] = (
        pd.to_datetime(trades_df['tradeDate'], format='%Y%m%d', errors='coerce')
          .dt.strftime('%Y-%m-%d')
    )

    unique_dates = trades_df['TradeDate'].dropna().unique()
    rate_cache: dict[str, float | None] = {}
    source_cache: dict[str, str] = {}
    for d in unique_dates:
        if d in rate_overrides:
            rate_cache[d] = rate_overrides[d]
            source_cache[d] = "override"
            continue
        try:
            rate_cache[d] = get_eur_usd_rate_for_day(d)
            source_cache[d] = "ecb"
        except Exception as e:
            print(f"Failed to fetch rate for {d}: {e}")
            rate_cache[d] = None
            source_cache[d] = ""

    trades_df['EUR_USD_Rate'] = trades_df['TradeDate'].map(rate_cache)
    trades_df['Rate_Source'] = trades_df['TradeDate'].map(source_cache).fillna("")
    return trades_df


def add_eur_and_tob(trades_df: pd.DataFrame) -> pd.DataFrame:
    """
    Total_EUR = |proceeds| / EUR_USD_Rate
    TOB       = Total_EUR * 0.0035
    """
    if trades_df.empty or 'proceeds' not in trades_df.columns:
        trades_df['Total_EUR'] = None
        trades_df['TOB'] = None
        return trades_df

    proceeds = pd.to_numeric(trades_df['proceeds'], errors='coerce').abs()
    rate = pd.to_numeric(trades_df['EUR_USD_Rate'], errors='coerce')
    rate = rate.where(rate != 0)

    trades_df['Total_EUR'] = proceeds / rate
    trades_df['TOB'] = trades_df['Total_EUR'] * TOB_RATE
    return trades_df


def _build_table_rows(display_df: pd.DataFrame) -> str:
    """Build <tbody> rows manually so we can inject checkbox + data attributes."""
    rows_html = []
    for row in display_df.to_dict("records"):
        total_eur = row.get('Total_EUR')
        tob = row.get('TOB')
        trade_date = row.get('TradeDate') or ''
        buysell = str(row.get('buySell') or '').strip()

        def cell(val, decimals=None, css='', col=''):
            # `col` echoes the matching <th data-col=...> attribute so the
            # filing-view CSS can hide a whole column with one selector.
            if decimals is not None:
                txt = _fmt_num(val, decimals)
            elif pd.isna(val):
                txt = ''
            else:
                txt = str(val)
            col_attr = f' data-col="{col}"' if col else ''
            return f'<td class="{css}"{col_attr}>{html.escape(txt)}</td>'

        # Smart quantity formatting:
        #   * whole-number stocks (5, 100, -23)    -> '5'              '100'      '-23'
        #   * small fractional crypto (0.001 BTC)  -> '0.001'
        #   * fractional stocks (e.g. 12.5 ETF)    -> '12.5'
        # Previous code did int(float(qty)), which truncated 0.5 BTC to 0
        # and made every crypto row look like a zero-quantity trade.
        qty = row.get('quantity')
        try:
            if qty in (None, '') or pd.isna(qty):
                qty_str = ''
            else:
                q = float(qty)
                if q == int(q):
                    qty_str = f"{int(q):,}"
                elif abs(q) < 1:
                    # Tiny fractions: up to 8 decimals (BTC satoshi precision)
                    # with trailing zeros stripped for readability.
                    qty_str = f"{q:,.8f}".rstrip("0").rstrip(".")
                else:
                    # Larger fractions: 4 decimals is enough for any equity
                    # and most crypto pairs.
                    qty_str = f"{q:,.4f}".rstrip("0").rstrip(".")
        except (ValueError, TypeError):
            qty_str = str(qty)

        data_total_eur = float(total_eur) if pd.notna(total_eur) else 0.0
        data_tob = float(tob) if pd.notna(tob) else 0.0
        commission_val = row.get("ibCommission")
        try:
            data_commission = float(commission_val) if pd.notna(commission_val) and commission_val != '' else 0.0
        except (ValueError, TypeError):
            data_commission = 0.0
        symbol_attr = html.escape(str(row.get("symbol") or ""))

        buysell_class = 'buy' if buysell == 'BUY' else ('sell' if buysell == 'SELL' else '')

        rows_html.append(
            f'<tr data-date="{html.escape(trade_date)}" '
            f'data-symbol="{symbol_attr}" '
            f'data-commission="{data_commission}" '
            f'data-total-eur="{data_total_eur}" data-tob="{data_tob}">'
            f'<td class="check-col" data-col="check"><input type="checkbox" class="row-check" checked></td>'
            f'{cell(trade_date, col="date")}'
            f'{cell(row.get("symbol"), col="symbol")}'
            f'{cell(row.get("description"), col="description")}'
            f'<td class="{buysell_class}" data-col="side">{html.escape(buysell)}</td>'
            f'<td class="num" data-col="qty">{html.escape(qty_str)}</td>'
            f'{cell(row.get("tradePrice"), 4, "num", col="price")}'
            f'{cell(row.get("proceeds"), 2, "num", col="proceeds-usd")}'
            f'{cell(row.get("ibCommission"), 2, "num", col="commission")}'
            f'{cell(row.get("EUR_USD_Rate"), 4, "num", col="fx")}'
            f'<td data-col="fx-src"><span class="src-{row.get("Rate_Source") or ""}">{html.escape(str(row.get("Rate_Source") or ""))}</span></td>'
            f'{cell(total_eur, 2, "num", col="total-eur")}'
            f'{cell(tob, 2, "num", col="tob")}'
            '</tr>'
        )
    return '\n'.join(rows_html)


def render_html(trades_df: pd.DataFrame, meta: dict, as_partial: bool = False) -> str:
    """Render the TOB report. `as_partial=True` returns only the body fragment
    (no <html>/<head>/<body>) for the dashboard shell. Default returns a
    self-contained HTML document for CLI export / accountant email handoff."""
    from core.templating import render_report

    display_df = trades_df[[c for c in DISPLAY_COLS if c in trades_df.columns]].copy()
    for col in ['tradePrice', 'proceeds', 'ibCommission', 'EUR_USD_Rate', 'Total_EUR', 'TOB']:
        if col in display_df.columns:
            display_df[col] = pd.to_numeric(display_df[col], errors='coerce')
    if 'TradeDate' in display_df.columns:
        # Newest-first by default — the user is usually reviewing recent
        # activity. tob.js exposes a header-click toggle to flip to oldest-
        # first when needed (e.g. audit trail from the beginning).
        display_df = display_df.sort_values('TradeDate', ascending=False, kind='stable').reset_index(drop=True)

    rows_html = _build_table_rows(display_df)

    min_date = display_df['TradeDate'].dropna().min() if 'TradeDate' in display_df else ''
    max_date = display_df['TradeDate'].dropna().max() if 'TradeDate' in display_df else ''

    years = sorted(set(
        d[:4] for d in display_df.get('TradeDate', pd.Series(dtype=str)).dropna().astype(str)
        if len(d) >= 4 and d[:4].isdigit()
    ))
    default_year = years[-1] if years else ''
    year_buttons_html = ''.join(
        f'<button type="button" class="year-btn{" active" if y == default_year else ""}" data-year="{y}">{y}</button>'
        for y in years
    )
    default_from = f"{default_year}-01-01" if default_year else min_date
    default_to = max_date if default_year and default_year == max_date[:4] else (
        f"{default_year}-12-31" if default_year else max_date
    )

    return render_report(
        "tob.html",
        css_files=["css/tob.css"],
        js_files=["js/tob.js"],
        as_partial=as_partial,
        account=meta.get('accountId', 'unknown'),
        period_str=f"{_fmt_date(meta.get('fromDate', ''))} → {_fmt_date(meta.get('toDate', ''))}",
        period_label=meta.get('period', ''),
        when_label=_fmt_when(meta.get('whenGenerated', '')),
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        rows=rows_html,
        year_buttons=year_buttons_html,
        min_date=min_date, max_date=max_date,
        default_from=default_from, default_to=default_to,
        tob_pct=f"{TOB_RATE*100:.2f}",
    )


def build_tob_html(account_code: str, use_overrides: bool = False,
                   as_partial: bool = False) -> str:
    """
    Compute and render the TOB report directly from the SQLite DB.
    Returns the HTML string. Returns a "no data" placeholder if DB is empty
    for this account.

    `as_partial=True` returns the report body fragment (no <html>/<head>/
    <body>) for inlining into the dashboard shell. The default returns a
    complete standalone document — used by the CLI exporter and the
    accountant-handoff email path.
    """
    from core import db as _db
    conn = _db.connect()
    _db.init_schema(conn)
    trades_db = _db.get_trades(conn, account_code)
    conn.close()

    if trades_db.empty:
        name = {"P": "personal", "B": "business"}.get(account_code, account_code)
        from core.templating import render
        return render('empty_report.html', kind='TOB', account=name)

    name = {"P": "personal", "B": "business"}.get(account_code, account_code)
    trades_df = trades_db.copy()
    trades_df['tradeDate'] = trades_df['tradeDate'].astype(str).str.replace('-', '', regex=False)
    trades_df['ibCommission'] = trades_df.get('commission_usd')
    trades_df['proceeds'] = trades_df.get('proceeds_usd')
    # Derive Buy/Sell from quantity sign (IBKR convention: positive = buy,
    # negative = sell). The CSV ingest path sets buySell explicitly, but the
    # DB path has only `quantity`, so before this line every TOB row from the
    # DB rendered with a blank side column.
    _qty_num = pd.to_numeric(trades_df['quantity'], errors='coerce')
    trades_df['buySell'] = _qty_num.apply(
        lambda q: 'BUY' if pd.notna(q) and q > 0 else ('SELL' if pd.notna(q) and q < 0 else '')
    )

    raw_dates = trades_df['tradeDate'].dropna().astype(str)
    raw_dates = raw_dates[raw_dates.str.match(r'^\d{8}$')]
    meta = {
        'accountId': name,
        'fromDate': raw_dates.min() if not raw_dates.empty else '',
        'toDate':   raw_dates.max() if not raw_dates.empty else '',
        'period':   '',
        'whenGenerated': '',
    }

    overrides = load_rate_overrides(name) if use_overrides else {}
    trades_df = add_eur_usd_rate(trades_df, rate_overrides=overrides)
    trades_df = add_eur_and_tob(trades_df)
    return render_html(trades_df, meta, as_partial=as_partial)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Parse IBKR Flex XML and compute EUR amounts + TOB.")
    ap.add_argument("--account", "-a", type=_resolve_account, default=None,
                    help="Account: P|personal or B|business. Input defaults to downloaded/<account>.xml.")
    ap.add_argument("--input", default=None, help="Path to Flex XML file (overrides --account input)")
    ap.add_argument("--no-html", action="store_true", help="Skip HTML report generation")
    ap.add_argument("--use-overrides", action="store_true",
                    help="Use EUR/USD rates from downloaded/<LETTER>_csv/*_reconciled.csv instead of ECB")
    args = ap.parse_args(argv)

    account_for_input = args.account or 'personal'
    letter = ACCOUNT_LETTER.get(account_for_input, 'P')

    # Load all trades from the SQLite DB (populated by ingest.py).
    from core import db as _db
    conn = _db.connect()
    _db.init_schema(conn)
    trades_db = _db.get_trades(conn, letter)
    conn.close()

    if trades_db.empty:
        print(
            f"No trades in DB for '{account_for_input}'. Run `python ingest.py` "
            f"(or click 'Refresh all' in the dashboard) to populate the database first.",
            file=sys.stderr,
        )
        return 1

    # The DB stores tradeDate as 'YYYY-MM-DD' (ISO), but parser.py expects
    # 'YYYYMMDD' (raw IBKR Flex format). Normalize once for the rest of the pipeline.
    trades_df = trades_db.copy()
    trades_df['tradeDate'] = (
        trades_df['tradeDate'].astype(str).str.replace('-', '', regex=False)
    )
    trades_df['ibCommission'] = trades_df.get('commission_usd')
    trades_df['proceeds'] = trades_df.get('proceeds_usd')

    # Build minimal meta dict so render_html keeps working.
    raw_dates = trades_df['tradeDate'].dropna().astype(str)
    raw_dates = raw_dates[raw_dates.str.match(r'^\d{8}$')]
    meta = {
        'accountId': account_for_input,
        'fromDate': raw_dates.min() if not raw_dates.empty else '',
        'toDate':   raw_dates.max() if not raw_dates.empty else '',
        'period':   '',
        'whenGenerated': '',
    }

    rate_overrides = {}
    if args.use_overrides:
        rate_overrides = load_rate_overrides(args.account or 'personal')
    trades_df = add_eur_usd_rate(trades_df, rate_overrides=rate_overrides)
    trades_df = add_eur_and_tob(trades_df)

    available = [c for c in DISPLAY_COLS if c in trades_df.columns]
    print(trades_df[available])

    num_trades = len(trades_df)
    sum_total_eur = trades_df['Total_EUR'].dropna().sum()
    sum_tob = trades_df['TOB'].dropna().sum()
    print(f"\nNumber of trades: {num_trades}")
    print(f"Sum of Total_EUR: {sum_total_eur:.2f}")
    print(f"Sum of TOB:       {sum_tob:.2f}")

    account = args.account or 'personal'

    out_dir = Path('parsed') / account
    out_dir.mkdir(parents=True, exist_ok=True)

    csv_path = out_dir / f"{account}.csv"
    trades_df.to_csv(csv_path, index=False)
    print(f"Saved CSV  to {csv_path}")

    if not args.no_html:
        html_path = out_dir / f"{account}.html"
        html_path.write_text(render_html(trades_df, meta), encoding='utf-8')
        print(f"Saved HTML to {html_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
