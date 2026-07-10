"""
Methodology page. A single document describing every case Phoenix handles
and where in the codebase the logic lives.

This is intentionally static (no per-account data). It exists to give the
user, their accountant, and a future maintainer a single source of truth
for "how does Phoenix decide X". Sections cover:

  1. Trade ingestion + FIFO lot accounting
  2. Corporate actions (splits, mergers, delistings)
  3. Transfers between IBKR sub-accounts
  4. Open-position reconciliation
  5. FX conversion (ECB EUR/USD daily)
  6. TOB (Belgian Tax on Bourse Operations)
  7. P&L (realised, unrealised, total)
  8. Performance metrics
  9. CGT 2026+ (Belgian capital-gains tax with 2025-12-31 basis reset)
 10. Dividends + foreign withholding tax + Belgian exemption
 11. Capital events filtered out of dividends (InterimLiquidation, ROC, ...)

Updated whenever calculation rules change. Linked from every other report.
"""

from datetime import datetime

from core.templating import render_report
from reports._helpers import ACCOUNT_ALIASES


# Pull the same constants the dividend engine uses, so any change there
# reflects here automatically (single source of truth).
from reports.dividends import (
    BELGIAN_PRECOMPTE_RATE,
    US_TREATY_WHT_RATE,
    US_DEFAULT_WHT_RATE,
    EXEMPTION_CAP_PER_YEAR,
)
from reports.corporate_tax import CIT_RATE
from core.loaders import NON_DIVIDEND_TYPES


def build_methodology_html(account_code: str | None = None,
                           as_partial: bool = False) -> str:
    """Render the methodology page. account_code is accepted but unused
    (page is account-agnostic); we keep the signature aligned with other
    report builders so the dashboard wiring stays uniform.
    `as_partial=True` returns the body fragment for the dashboard shell;
    default returns a standalone document."""
    ctx = dict(
        now=datetime.now().strftime("%Y-%m-%d %H:%M"),
        belgian_rate_pct=BELGIAN_PRECOMPTE_RATE * 100,
        us_treaty_rate_pct=US_TREATY_WHT_RATE * 100,
        us_default_rate_pct=US_DEFAULT_WHT_RATE * 100,
        exemption_cap_per_year=EXEMPTION_CAP_PER_YEAR,
        cit_rate_pct=int(CIT_RATE * 100),     # for the new section 10b
        non_dividend_types=sorted(NON_DIVIDEND_TYPES),
        account=ACCOUNT_ALIASES.get(account_code or "", account_code or ""),
    )
    return render_report(
        "methodology.html",
        css_files=["css/dividends.css"],     # reuse the dividends styling
        js_files=["js/dividends.js"],        # tab + privacy toggle
        as_partial=as_partial,
        **ctx,
    )
