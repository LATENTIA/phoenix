"""
Dividend report. Gross dividends, foreign withholding tax, EUR conversion,
estimated Belgian tax. Reads from the `dividends` and `withholding_tax`
tables populated by `ingest.py`.

Tax model (Belgium, individuals receiving foreign dividends through a
foreign broker like IBKR Ireland):

  - The broker does NOT withhold the 30% Belgian "precompte mobilier"
    automatically. The taxpayer must self-declare and pay.
  - Foreign withholding tax (e.g. 15% under the BE-US treaty when W-8BEN
    is filed) is NOT creditable against the Belgian 30% for individuals.
  - The 30% Belgian rate applies to the dividend NET of the foreign WHT
    (post-2017 rule, "FBB intégral" abolished).

So for a $100 US dividend with 15% withheld at source, the Belgian
individual pays:
    foreign WHT = $15
    base for Belgian tax = $85
    Belgian tax = $85 × 30% = $25.50
    total tax burden = $40.50  (effective ~40.5%)

We display the facts (gross, foreign WHT, net) and an *estimated* Belgian
tax line; the accountant makes the final call. Rules differ by source
country and may shift over time.
"""

import argparse
import html
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from core import db as _db
from core.ecb_fx_parser import get_eur_usd_rate_for_day
from core.templating import render, render_report
from reports._helpers import (
    ACCOUNT_ALIASES, ACCOUNT_LETTER,
    fmt_num, fmt_qty,
    resolve_account as _resolve_account,
)


log = logging.getLogger("phoenix.dividends")


# ---------- Belgian tax constants ----------

BELGIAN_PRECOMPTE_RATE = 0.30      # 30% mobilier précompte on net foreign dividends
US_TREATY_WHT_RATE = 0.15          # treaty rate with W-8BEN
US_DEFAULT_WHT_RATE = 0.30         # if no W-8BEN filed

# Annual exemption, confirmed amounts from SPF Finances:
#   https://fin.belgium.be/fr/particuliers/avantages-fiscaux/exoneration-dividendes
# Income year 2025 (tax year 2026): €833
# Income year 2026 (tax year 2027): €833
# Historical figures (for reference, used on this account's pre-2025 data):
EXEMPTION_CAP_PER_YEAR = {
    2022: 800.0,
    2023: 833.0,
    2024: 833.0,
    2025: 833.0,   # confirmed by SPF Finances
    2026: 833.0,   # confirmed by SPF Finances
}
EXEMPTION_CAP_DEFAULT = 833.0      # used for years not in the dict above

# Dividend types eligible for the Belgian exemption.
#
# Per SPF Finances:
#   ELIGIBLE   : ordinary Belgian and foreign dividends
#                cooperative dividends, social-purpose entities
#   EXCLUDED   : collective investment schemes (UCITS / ETFs)
#                common investment funds
#                dividends through "legal constructions"
#
# We only see IBKR's classification ("Ordinary Dividend", "Bonus Dividend",
# "Payment in Lieu of Dividend") in the source data. We treat:
#   "Ordinary Dividend" / "Bonus Dividend" / unclassified → eligible
#   "Payment in Lieu"                                     → NOT eligible
# IMPORTANT: this does not detect ETF/UCITS dividends, which the user must
# manually exclude. The Rules & References tab calls this out.
EXEMPTION_ELIGIBLE_TYPES = {"Ordinary Dividend", "Bonus Dividend", ""}


def _exemption_cap_for(year: int) -> float:
    """Annual dividend-exemption cap (EUR) for a given income year."""
    return EXEMPTION_CAP_PER_YEAR.get(year, EXEMPTION_CAP_DEFAULT)


def _is_eligible_for_exemption(dividend_type: str) -> bool:
    return (dividend_type or "") in EXEMPTION_ELIGIBLE_TYPES


# ---------- EUR conversion ----------

def _eur_for(amount_usd: Optional[float], date_iso: str,
             cache: dict) -> Optional[float]:
    """Convert a USD amount to EUR using the ECB rate on `date_iso`.
    Caches per-date lookups. Returns None when amount or rate is missing."""
    if amount_usd is None or pd.isna(amount_usd) or not date_iso:
        return None
    if date_iso not in cache:
        try:
            cache[date_iso] = get_eur_usd_rate_for_day(date_iso)
        except Exception:
            cache[date_iso] = None
    rate = cache[date_iso]
    if not rate:
        return None
    return float(amount_usd) / float(rate)


# ---------- Pairing dividend rows with their WHT entries ----------

def _pair_dividends_with_wht(div_df: pd.DataFrame, wht_df: pd.DataFrame) -> pd.DataFrame:
    """For each dividend row, find the matching WHT row (same date, same
    symbol, and same per-share figure). Adds these columns to the dividend frame:
        wht_amount, wht_country, wht_pct
    A dividend with no matching WHT row gets None / 0.
    """
    df = div_df.copy()
    df["wht_amount"] = None
    df["wht_country"] = None
    df["wht_pct"] = None

    if wht_df is None or wht_df.empty:
        return df

    # Index WHT by (date, symbol, per_share) for O(1) lookup.
    wht_idx: dict[tuple, list[dict]] = {}
    for r in wht_df.to_dict("records"):
        key = (r.get("pay_date"), r.get("symbol"), r.get("per_share"))
        wht_idx.setdefault(key, []).append(r)
    fallback_idx: dict[tuple, list[dict]] = {}
    for r in wht_df.to_dict("records"):
        key = (r.get("pay_date"), r.get("symbol"))
        fallback_idx.setdefault(key, []).append(r)

    out_amounts, out_countries, out_pcts = [], [], []
    for d in df.to_dict("records"):
        key_full = (d.get("pay_date"), d.get("symbol"), d.get("per_share"))
        candidates = wht_idx.get(key_full)
        if not candidates:
            # PIL rows have no per-share figure; fall back to (date, symbol).
            candidates = fallback_idx.get((d.get("pay_date"), d.get("symbol")))
        if candidates:
            w = candidates[0]
            amt = float(w.get("amount") or 0)
            out_amounts.append(amt)
            out_countries.append(w.get("source_country") or "")
            gross = float(d.get("amount") or 0)
            out_pcts.append((abs(amt) / gross * 100) if gross else None)
        else:
            out_amounts.append(0.0)
            out_countries.append("")
            out_pcts.append(None)
    df["wht_amount"] = out_amounts
    df["wht_country"] = out_countries
    df["wht_pct"] = out_pcts
    return df


# ---------- Per-year aggregation ----------

def annual_summary(paired: pd.DataFrame) -> pd.DataFrame:
    """One row per calendar year of dividend payments. Computes:

      - gross / WHT / net (in EUR and USD-reference)
      - exempt_eur:        portion of eligible-type gross covered by the annual cap
      - exemption_cap_eur: the cap that applied in that year
      - belgian_tax_eur:        30% × net (no exemption)
      - exemption_saving_eur:   the refund the exemption gives back
      - belgian_tax_after_eur:  belgian_tax_eur - exemption_saving_eur (>= 0)
      - after_all_tax_eur:      net_eur - belgian_tax_after_eur
    """
    if paired.empty:
        return pd.DataFrame()
    df = paired.copy()
    df["year"] = df["pay_date"].str[:4]
    df = df[df["year"].str.isdigit()]
    if df.empty:
        return pd.DataFrame()

    # Tag each row as exemption-eligible or not.
    df["_eligible"] = df["dividend_type"].fillna("").apply(_is_eligible_for_exemption)

    rows = []
    for year, sub in df.groupby("year"):
        year_int = int(year)
        cap = _exemption_cap_for(year_int)

        gross_eur = float(sub["amount_eur"].sum() or 0.0)
        gross_usd = float(sub["amount"].sum() or 0.0)
        wht_eur = float(sub["wht_amount_eur"].sum() or 0.0)
        wht_usd = float(sub["wht_amount"].sum() or 0.0)
        net_eur = gross_eur + wht_eur
        net_usd = gross_usd + wht_usd

        # Eligible vs other split (in EUR), to compute the exemption refund correctly.
        elig = sub[sub["_eligible"]]
        gross_eligible_eur = float(elig["amount_eur"].sum() or 0.0)
        wht_eligible_eur = float(elig["wht_amount_eur"].sum() or 0.0)

        exempt_gross_eur = min(gross_eligible_eur, cap)
        # Foreign WHT applies pro-rata to the exempt portion of the eligible gross.
        if gross_eligible_eur > 0:
            wht_on_exempt = wht_eligible_eur * (exempt_gross_eur / gross_eligible_eur)
        else:
            wht_on_exempt = 0.0
        exempt_net_eur = exempt_gross_eur + wht_on_exempt   # wht is negative

        # Tax without exemption: 30% on the net (gross - foreign WHT).
        belgian_tax_no_exempt = net_eur * BELGIAN_PRECOMPTE_RATE

        # Per SPF Finances: for *foreign* dividends with no Belgian precompte
        # withheld, the exemption is an EXCLUSION FROM DECLARATION; the
        # exempt portion (and its share of foreign WHT) simply isn't declared.
        # Net of foreign WHT after exemption = total_net - exempt_net.
        net_after_exemption = net_eur - exempt_net_eur
        belgian_tax_after = max(0.0, net_after_exemption * BELGIAN_PRECOMPTE_RATE)
        # Saving = the precompte avoided on the exempt portion.
        exemption_saving = belgian_tax_no_exempt - belgian_tax_after

        after_all_tax = net_eur - belgian_tax_after

        rows.append({
            "year": year,
            "n_payments": int(len(sub)),
            "n_symbols": int(sub["symbol"].nunique()),
            "gross_usd": gross_usd,
            "gross_eur": gross_eur,
            "wht_usd": wht_usd,
            "wht_eur": wht_eur,
            "net_usd": net_usd,
            "net_eur": net_eur,
            "exemption_cap_eur": cap,
            "exempt_gross_eur": exempt_gross_eur,
            "exempt_net_eur": exempt_net_eur,
            "belgian_tax_eur": belgian_tax_no_exempt,
            "exemption_saving_eur": exemption_saving,
            "belgian_tax_after_eur": belgian_tax_after,
            "after_all_tax_eur": after_all_tax,
        })
    return pd.DataFrame(rows).sort_values("year", ascending=False).reset_index(drop=True)


def per_symbol_summary(paired: pd.DataFrame) -> pd.DataFrame:
    """One row per symbol across all years."""
    if paired.empty:
        return pd.DataFrame()
    g = paired.groupby("symbol", as_index=False).agg(
        n_payments=("amount", "count"),
        gross_usd=("amount", "sum"),
        gross_eur=("amount_eur", "sum"),
        wht_usd=("wht_amount", "sum"),
        wht_eur=("wht_amount_eur", "sum"),
        countries=("wht_country", lambda s: ",".join(sorted({c for c in s if c}))),
    )
    g["net_eur"] = g["gross_eur"] + g["wht_eur"]
    return g.sort_values("gross_eur", ascending=False).reset_index(drop=True)


# ---------- Source-file inventory ----------

def source_file_status(conn, account_code: str) -> list[dict]:
    """Per source-file: did it carry any dividend / WHT rows?  Used in the
    'Sources' panel so the user can see which years have data and which
    files were ingested but contained nothing dividend-related."""
    rows = conn.execute(
        """SELECT
              sf.id,
              sf.path,
              sf.kind,
              sf.ingested_at,
              (SELECT COUNT(*) FROM dividends d        WHERE d.source_id = sf.id) AS n_div,
              (SELECT COUNT(*) FROM withholding_tax w  WHERE w.source_id = sf.id) AS n_wht
           FROM source_files sf
           WHERE sf.account_code = ?
           ORDER BY sf.path""",
        (account_code,),
    ).fetchall()
    out = []
    for r in rows:
        # Try to derive the year range from the filename (Uxxx_YYYYMMDD_YYYYMMDD.csv)
        import re as _re
        name = Path(r["path"]).name
        period = "·"
        m = _re.search(r"_(\d{8})_(\d{8})", name)
        if m:
            try:
                a = datetime.strptime(m.group(1), "%Y%m%d").strftime("%Y-%m-%d")
                b = datetime.strptime(m.group(2), "%Y%m%d").strftime("%Y-%m-%d")
                period = f"{a} → {b}"
            except ValueError:
                pass
        elif "_" in name and name.endswith(".xml"):
            # year-stamped XML (e.g. personal_2026.xml)
            ym = _re.search(r"_(\d{4})\.xml$", name)
            if ym:
                period = f"{ym.group(1)} (YTD)"
        out.append({
            "name": name,
            "kind": r["kind"],
            "period": period,
            "ingested_at": r["ingested_at"],
            "n_div": r["n_div"],
            "n_wht": r["n_wht"],
        })
    return out


# ---------- Public API: build_dividends_html ----------

def _empty_report_html(account_code: str) -> str:
    name = ACCOUNT_ALIASES.get(account_code, account_code)
    return render("empty_report.html", kind="Dividends", account=name)


def build_dividends_html(account_code: str, as_partial: bool = False) -> str:
    """Compute the dividend report and render the HTML.
    `as_partial=True` returns the body fragment for the dashboard shell;
    default returns a standalone document for CLI export."""
    conn = _db.connect()
    _db.init_schema(conn)
    div = _db.get_dividends(conn, account_code)
    wht = _db.get_withholding(conn, account_code)
    sources = source_file_status(conn, account_code)
    conn.close()

    if div.empty:
        return _empty_report_html(account_code)

    # Convert each row to EUR using ECB rates on the pay date.
    fx_cache: dict = {}
    div = div.copy()
    div["amount_eur"] = [
        _eur_for(a, d, fx_cache) for a, d in zip(div["amount"], div["pay_date"])
    ]
    if not wht.empty:
        wht = wht.copy()
        wht["amount_eur"] = [
            _eur_for(a, d, fx_cache) for a, d in zip(wht["amount"], wht["pay_date"])
        ]
    else:
        # Ensure the column exists for the merge / aggregations below.
        wht = pd.DataFrame(columns=["pay_date", "symbol", "per_share",
                                     "amount", "amount_eur", "source_country"])

    paired = _pair_dividends_with_wht(div, wht)
    paired["wht_amount_eur"] = [
        _eur_for(a, d, fx_cache) if a else 0.0
        for a, d in zip(paired["wht_amount"], paired["pay_date"])
    ]

    annual = annual_summary(paired)
    by_symbol = per_symbol_summary(paired)

    return render_html(
        account=ACCOUNT_ALIASES.get(account_code, account_code),
        annual=annual,
        by_symbol=by_symbol,
        per_payment=paired,
        sources=sources,
        as_partial=as_partial,
    )


# ---------- HTML rendering ----------

def _render_annual_rows(annual: pd.DataFrame) -> str:
    if annual.empty:
        return '<tr><td colspan="10" class="muted">No dividends yet.</td></tr>'
    rows = []
    for r in annual.to_dict("records"):
        # Tag row: was the cap actually saturated?
        cap_label = (
            f"{fmt_num(r['exempt_gross_eur'])} <span class='muted'>"
            f"/ {fmt_num(r['exemption_cap_eur'])}</span>"
        )
        rows.append(
            f"<tr><td>{r['year']}</td>"
            f"<td class='num'>{int(r['n_payments'])}</td>"
            f"<td class='num muted'>{int(r['n_symbols'])}</td>"
            f"<td class='num pos'>{fmt_num(r['gross_eur'])}</td>"
            f"<td class='num neg'>{fmt_num(r['wht_eur'])}</td>"
            f"<td class='num'><strong>{fmt_num(r['net_eur'])}</strong></td>"
            f"<td class='num pos'>{cap_label}</td>"
            f"<td class='num neg'>{fmt_num(-r['belgian_tax_after_eur'])}</td>"
            f"<td class='num'><strong>{fmt_num(r['after_all_tax_eur'])}</strong></td>"
            f"<td class='num muted'>{fmt_num(r['gross_usd'])} USD</td>"
            f"</tr>"
        )
    # Footer with totals
    total_gross = annual["gross_eur"].sum()
    total_wht = annual["wht_eur"].sum()
    total_net = annual["net_eur"].sum()
    total_exempt = annual["exempt_gross_eur"].sum()
    total_be_after = annual["belgian_tax_after_eur"].sum()
    total_after = annual["after_all_tax_eur"].sum()
    total_payments = int(annual["n_payments"].sum())
    rows.append(
        f"<tr class='total-row'><td><strong>Total</strong></td>"
        f"<td class='num'><strong>{total_payments}</strong></td>"
        f"<td class='num muted'>·</td>"
        f"<td class='num pos'><strong>{fmt_num(total_gross)}</strong></td>"
        f"<td class='num neg'><strong>{fmt_num(total_wht)}</strong></td>"
        f"<td class='num'><strong>{fmt_num(total_net)}</strong></td>"
        f"<td class='num pos'><strong>{fmt_num(total_exempt)}</strong></td>"
        f"<td class='num neg'><strong>{fmt_num(-total_be_after)}</strong></td>"
        f"<td class='num'><strong>{fmt_num(total_after)}</strong></td>"
        f"<td class='num muted'>·</td></tr>"
    )
    return "\n".join(rows)


def _render_symbol_rows(by_sym: pd.DataFrame) -> str:
    if by_sym.empty:
        return '<tr><td colspan="6" class="muted">No dividends yet.</td></tr>'
    rows = []
    for r in by_sym.to_dict("records"):
        countries = r.get("countries") or ""
        country_html = (f"<span class='country-tag'>{html.escape(countries)}</span>"
                        if countries else "<span class='muted'>·</span>")
        rows.append(
            f"<tr><td><strong>{html.escape(str(r['symbol']))}</strong></td>"
            f"<td class='num muted'>{int(r['n_payments'])}</td>"
            f"<td>{country_html}</td>"
            f"<td class='num pos'>{fmt_num(r['gross_eur'])}</td>"
            f"<td class='num neg'>{fmt_num(r['wht_eur'])}</td>"
            f"<td class='num'><strong>{fmt_num(r['net_eur'])}</strong></td></tr>"
        )
    return "\n".join(rows)


def _render_payment_rows(paired: pd.DataFrame) -> str:
    if paired.empty:
        return '<tr><td colspan="9" class="muted">No dividends yet.</td></tr>'
    df = paired.sort_values("pay_date", ascending=False)
    rows = []
    for r in df.to_dict("records"):
        wht_pct = r.get("wht_pct")
        wht_pct_cell = f"{wht_pct:.1f}%" if wht_pct is not None else "·"
        country = r.get("wht_country") or ""
        country_html = (f"<span class='country-tag'>{html.escape(country)}</span>"
                        if country else "<span class='muted'>·</span>")
        type_html = html.escape(r.get("dividend_type") or "")
        rows.append(
            f"<tr><td>{html.escape(str(r['pay_date']))}</td>"
            f"<td><strong>{html.escape(str(r['symbol']))}</strong></td>"
            f"<td class='muted'>{type_html}</td>"
            f"<td class='num'>{fmt_num(r.get('per_share'), 4) if r.get('per_share') is not None else '·'}</td>"
            f"<td class='num pos'>{fmt_num(r['amount'])}</td>"
            f"<td class='num pos'>{fmt_num(r.get('amount_eur'))}</td>"
            f"<td class='num neg'>{fmt_num(r.get('wht_amount_eur'))}</td>"
            f"<td>{country_html} <span class='muted'>{wht_pct_cell}</span></td>"
            f"<td class='num'><strong>{fmt_num((r.get('amount_eur') or 0) + (r.get('wht_amount_eur') or 0))}</strong></td>"
            f"</tr>"
        )
    return "\n".join(rows)


def _render_sources_rows(sources: list[dict]) -> str:
    if not sources:
        return '<tr><td colspan="5" class="muted">No source files ingested yet.</td></tr>'
    rows = []
    for s in sources:
        if s["n_div"] == 0 and s["n_wht"] == 0:
            badge = '<span class="src-badge src-empty">no dividends</span>'
        else:
            badge = (f'<span class="src-badge src-has">'
                     f'{s["n_div"]} div · {s["n_wht"]} wht</span>')
        kind_tag = f'<span class="kind-tag kind-{html.escape(s["kind"])}">{html.escape(s["kind"])}</span>'
        rows.append(
            f"<tr><td>{html.escape(s['name'])}</td>"
            f"<td>{kind_tag}</td>"
            f"<td class='muted'>{html.escape(s['period'])}</td>"
            f"<td>{badge}</td>"
            f"<td class='muted'>{html.escape(s.get('ingested_at') or '')}</td></tr>"
        )
    return "\n".join(rows)


def render_html(
    *,
    account: str,
    annual: pd.DataFrame,
    by_symbol: pd.DataFrame,
    per_payment: pd.DataFrame,
    sources: list[dict],
    as_partial: bool = False,
) -> str:
    annual_rows = _render_annual_rows(annual)
    symbol_rows = _render_symbol_rows(by_symbol)
    payment_rows = _render_payment_rows(per_payment)
    sources_rows = _render_sources_rows(sources)

    # Headline totals
    if annual.empty:
        total_gross_eur = total_wht_eur = total_net_eur = 0.0
        total_belgian_tax_no_exempt = total_belgian_tax_after = 0.0
        total_exemption_saving = total_exempt_gross = 0.0
        total_after_all_tax = 0.0
        n_payments = n_years = 0
    else:
        total_gross_eur = float(annual["gross_eur"].sum())
        total_wht_eur = float(annual["wht_eur"].sum())
        total_net_eur = float(annual["net_eur"].sum())
        total_belgian_tax_no_exempt = float(annual["belgian_tax_eur"].sum())
        total_belgian_tax_after = float(annual["belgian_tax_after_eur"].sum())
        total_exemption_saving = float(annual["exemption_saving_eur"].sum())
        total_exempt_gross = float(annual["exempt_gross_eur"].sum())
        total_after_all_tax = float(annual["after_all_tax_eur"].sum())
        n_payments = int(annual["n_payments"].sum())
        n_years = len(annual)

    # Files with no dividends (for the "files we ingested but skipped" banner)
    empty_files = [s["name"] for s in sources if s["n_div"] == 0 and s["n_wht"] == 0]

    # Per-year dividend coverage. For each calendar year a source file covers,
    # record whether ANY file for that year carried at least one dividend row.
    # Years where every covering file came back empty are surfaced to the user
    # so they can fix it (typically: enable Cash Transactions in the Flex Query
    # for the current year, or check that the CSV statement actually contained
    # a Dividends section for that year).
    import re as _re_y
    year_to_div_count: dict[int, int] = {}
    year_to_files: dict[int, list[str]] = {}
    for s in sources:
        # Pull the year(s) this file covers from its period string. Handles
        # both "YYYY-MM-DD → YYYY-MM-DD" CSV ranges and "YYYY (YTD)" XML stamps.
        years_in_file: set[int] = set()
        for ym in _re_y.findall(r"(\d{4})", s.get("period", "") or ""):
            try:
                yi = int(ym)
                if 1990 <= yi <= 2100:
                    years_in_file.add(yi)
            except ValueError:
                pass
        for yi in years_in_file:
            year_to_div_count[yi] = year_to_div_count.get(yi, 0) + s["n_div"]
            year_to_files.setdefault(yi, []).append(s["name"])

    # Years that have a source file ingested but zero dividend rows.
    empty_years = sorted(y for y, c in year_to_div_count.items() if c == 0)
    # Years that have at least one dividend row.
    years_with_data = sorted(y for y, c in year_to_div_count.items() if c > 0)
    # Calendar years missing entirely (no file covering them) between the
    # first and last year we know about. Useful when a CSV is missing.
    if year_to_div_count:
        all_years = set(year_to_div_count.keys())
        lo, hi = min(all_years), max(all_years)
        missing_years = sorted(y for y in range(lo, hi + 1) if y not in all_years)
    else:
        missing_years = []

    # Latest exemption cap (for the rules text)
    latest_cap = _exemption_cap_for(2026)

    return render_report(
        "dividends.html",
        css_files=["css/dividends.css"],
        js_files=["js/dividends.js"],
        as_partial=as_partial,
        account=account,
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        # KPIs
        n_payments=n_payments,
        n_years=n_years,
        total_gross_eur=total_gross_eur,
        total_wht_eur=total_wht_eur,
        total_net_eur=total_net_eur,
        total_belgian_tax_no_exempt=total_belgian_tax_no_exempt,
        total_belgian_tax_after=total_belgian_tax_after,
        total_exemption_saving=total_exemption_saving,
        total_exempt_gross=total_exempt_gross,
        total_after_all_tax=total_after_all_tax,
        # Tables
        annual_rows=annual_rows,
        symbol_rows=symbol_rows,
        payment_rows=payment_rows,
        sources_rows=sources_rows,
        empty_files=empty_files,
        empty_years=empty_years,
        years_with_data=years_with_data,
        missing_years=missing_years,
        # Constants for the rules text
        belgian_rate_pct=BELGIAN_PRECOMPTE_RATE * 100,
        us_treaty_rate_pct=US_TREATY_WHT_RATE * 100,
        latest_cap=latest_cap,
        exemption_cap_per_year=EXEMPTION_CAP_PER_YEAR,
    )


# ---------- CLI ----------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Dividend report for an IBKR account.")
    ap.add_argument("-a", "--account", required=True, type=_resolve_account,
                    help="Account: P|personal or B|business")
    args = ap.parse_args(argv)
    code = ACCOUNT_LETTER[args.account]
    sys.stdout.write(build_dividends_html(code))
    return 0


if __name__ == "__main__":
    sys.exit(main())
