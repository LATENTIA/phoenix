"""
Tiny rendering helper used by the TOB / P&L report builders.

Reports embed their CSS + JS inline (so the HTML is self-contained and works
both when served via Flask and when written to a file by the CLI scripts).
"""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape


ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = ROOT / "templates"
STATIC_DIR = ROOT / "static"

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)


def _read_static(*paths: str) -> str:
    """Concatenate static files (css or js) into one string for inlining."""
    return "\n".join((STATIC_DIR / p).read_text(encoding="utf-8") for p in paths)


def render(template_name: str, **context) -> str:
    """Render a Jinja2 template located in templates/."""
    return _env.get_template(template_name).render(**context)


def render_report(template_name: str, *,
                  css_files: list[str],
                  js_files: list[str],
                  as_partial: bool = False,
                  **context) -> str:
    """
    Render a report. Two modes:

      `as_partial=False` (default, legacy path)
        Renders the full standalone document via `template_name`, with the
        provided CSS + JS files concatenated and inlined into `{{ css }}` /
        `{{ js }}` placeholders in the base template. Returns a complete
        `<html>…</html>` document — used by the CLI exporter, the share-link
        email-handoff path, and (currently) the dashboard iframe.

      `as_partial=True` (new — Phase 2 dashboard shell)
        Renders ONLY the content partial corresponding to `template_name`.
        Naming convention: `tob.html` → `partials/tob_content.html`. The
        partial is "naked" HTML — no `<html>`, no `<head>`, no `<body>`.
        CSS + JS injection is skipped because the dashboard shell loads
        them once, statically. Returns just the report body fragment.

    Both modes receive identical context, so any caller can flip between
    them without touching the data-prep code. Adding `as_partial=False`
    as a default keeps every existing callsite working unchanged.
    """
    if as_partial:
        partial_name = "partials/" + template_name.replace(".html", "_content.html")
        partial_html = render(partial_name, **context)
        # CSS is linked from the dashboard <head> once (Phase 2), so partial
        # responses don't carry styles. But the per-report JS DOES need to
        # ship with the partial — that's what binds filters, sub-tabs, view
        # toggles, etc. to the freshly injected DOM. The dashboard's
        # showReport() re-executes <script> tags after the innerHTML inject
        # (innerHTML alone wouldn't run them). Each report's IIFE is safe
        # to re-execute on every tab switch because its DOM queries are
        # scoped to the partial just inserted.
        js = _read_static(*js_files)
        return partial_html + f"\n<script data-report-js>\n{js}\n</script>\n"
    # Full-document path: prepend tokens.css so the standalone HTML carries
    # the night-ledger palette + typography even when its report-specific
    # CSS no longer defines them (Phase 2C dropped the duplicate :root /
    # body / heading blocks from each report CSS).
    css = _read_static("css/tokens.css", *css_files)
    js = _read_static(*js_files)
    return render(template_name, css=css, js=js, **context)
