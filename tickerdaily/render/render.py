"""Renders the carousel HTML template to 1080x1350 JPEGs (Instagram's 4:5 portrait size)."""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

TEMPLATE_DIR = Path(__file__).parent


def render_html(facts: dict, post: dict, fmt: str = "wrap", logos: dict | None = None) -> str:
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=True, undefined=StrictUndefined)
    return env.get_template(f"{fmt}.html.j2").render(f=facts, p=post, logos=logos or {})


def render_pngs(facts: dict, post: dict, out_dir: Path, fmt: str = "wrap", logos: dict | None = None) -> list[Path]:
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    html = render_html(facts, post, fmt, logos)
    (out_dir / "carousel.html").write_text(html)
    paths = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1080, "height": 1350})
        page.set_content(html, wait_until="networkidle")
        page.evaluate("document.fonts.ready")
        # Long copy must never push content into the footer: shrink step by step if needed.
        tightened = page.evaluate("""() => {
            const fits = s => { const f = s.querySelector('.foot');
                const kids = [...s.children].filter(c => c !== f && !c.classList.contains('spacer'));
                const bottom = Math.max(...kids.map(c => c.getBoundingClientRect().bottom));
                return !f || bottom <= f.getBoundingClientRect().top - 12; };
            const out = [];
            document.querySelectorAll('.slide').forEach((s, i) => {
                for (const cls of ['tight', 'tighter']) { if (fits(s)) break; s.classList.add(cls); out.push(i + 1); }
            });
            return out;
        }""")
        if tightened:
            print(f"[render] tightened layout on slide(s) {sorted(set(tightened))}", flush=True)
        for i, el in enumerate(page.query_selector_all(".slide"), start=1):
            # Instagram's publishing API accepts JPEG only.
            path = out_dir / f"slide_{i}.jpg"
            el.screenshot(path=str(path), type="jpeg", quality=92)
            paths.append(path)
        browser.close()
    return paths
