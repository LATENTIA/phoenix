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
                  **context) -> str:
    """
    Render a report template with the given CSS + JS inlined into the page.
    The template should reference {{ css }} and {{ js }} placeholders.
    """
    css = _read_static(*css_files)
    js = _read_static(*js_files)
    return render(template_name, css=css, js=js, **context)
