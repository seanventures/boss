"""Render every wireframe in screens/ to PNG: desktop 1600x1000 (dark + light) and phone 390x844.

    /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 render.py [name-filter]

Full-page captures; the phone pass hides the design-note pins (they are desktop annotations).
"""
import pathlib
import sys

from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).resolve().parent
SCREENS = HERE / "screens"
OUT = HERE / "png"
# (suffix, viewport, query)
PASSES = [
    ("", {"width": 1600, "height": 1000}, ""),
    ("-light", {"width": 1600, "height": 1000}, "theme=light"),
    ("-phone", {"width": 390, "height": 844}, "notes=0"),
]
EXTRA = {  # extra states of the same page
    "01-nav": [("-collapsed", {"width": 1600, "height": 1000}, "nav=collapsed"),
               ("-phone-open", {"width": 390, "height": 844}, "drawer=1&notes=0")],
}


def main() -> None:
    flt = sys.argv[1] if len(sys.argv) > 1 else ""
    OUT.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for page_file in sorted(SCREENS.glob("*.html")):
            stem = page_file.stem
            if flt and flt not in stem:
                continue
            for suffix, vp, query in PASSES + EXTRA.get(stem, []):
                ctx = browser.new_context(viewport=vp, device_scale_factor=1 if vp["width"] > 500 else 2)
                page = ctx.new_page()
                page.goto(page_file.as_uri() + (f"?{query}" if query else ""))
                page.wait_for_load_state("networkidle")
                page.evaluate("document.fonts.ready")
                page.add_style_tag(content="*{animation:none!important;transition:none!important}")
                full = not page.evaluate("!!document.querySelector('.drawer,.sheet,.pal')")
                page.screenshot(path=str(OUT / f"{stem}{suffix}.png"), full_page=full)
                ctx.close()
                print("rendered", f"{stem}{suffix}.png")
        browser.close()


if __name__ == "__main__":
    main()
