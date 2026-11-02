"""Renders the carousel HTML template to 1080x1350 JPEGs (Instagram's 4:5 portrait size)."""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

TEMPLATE_DIR = Path(__file__).parent


def render_html(facts: dict, post: dict) -> str:
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=True, undefined=StrictUndefined)
    return env.get_template("carousel.html.j2").render(f=facts, p=post)


def render_pngs(facts: dict, post: dict, out_dir: Path) -> list[Path]:
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    html = render_html(facts, post)
    (out_dir / "carousel.html").write_text(html)
    paths = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1080, "height": 1350})
        page.set_content(html, wait_until="networkidle")
        page.evaluate("document.fonts.ready")
        for i, el in enumerate(page.query_selector_all(".slide"), start=1):
            # Instagram's publishing API accepts JPEG only.
            path = out_dir / f"slide_{i}.jpg"
            el.screenshot(path=str(path), type="jpeg", quality=92)
            paths.append(path)
        browser.close()
    return paths
