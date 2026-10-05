"""Every Log and Govern control is the thing a tap at its centre lands on, at 1440 and 390.

THE INCIDENT (activity-govern re-check, 2026-10-05). At 390 "Lines / By run" sat in the views
scroll strip with flex-shrink 1 and collapsed to 2px: on screen, untappable. A box test passes
that (the button exists, nothing overflows); a tap does not.

apps/web/tests/log-govern-layout-fixture.test.tsx renders the Log and Govern and writes them as
pages here. A real Chromium lays them out and, for every visible button, link, field and tab:
scrolls it into view (its own scroll strip too), asks what is at its centre, and requires that
to be the control or inside it, with a box at least 24px across and not clipped by a box it
sits in. Skipped without Chromium.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[3] / "apps" / "web"

TAP = r"""(sel) => {
  const bad = [];
  const name = (el) => `${el.dataset.testid || el.getAttribute('aria-label') || el.className || el.tagName}`.slice(0, 40)
    + (el.textContent ? ` "${el.textContent.trim().slice(0, 20)}"` : "");
  for (const el of document.querySelectorAll(sel)) {
    if (el.closest('[hidden], [aria-hidden="true"], .lx-list-inner, svg')) continue;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') continue;
    let hidden = false;
    for (let p = el.parentElement; p; p = p.parentElement) if (getComputedStyle(p).display === 'none') { hidden = true; break; }
    if (hidden) continue;
    el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
    const r = el.getBoundingClientRect();
    if (r.width < 24 || r.height < 16) { bad.push(`too small ${Math.round(r.width)}x${Math.round(r.height)}: ${name(el)}`); continue; }
    // Clipped by a box it sits in (a group squeezed narrower than its buttons): not all of it can be tapped.
    let clipped = null;
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      const o = getComputedStyle(p);
      if (o.overflowX === 'visible' && o.overflowY === 'visible') continue;
      const c = p.getBoundingClientRect();
      if (r.left < c.left - 1 || r.right > c.right + 1 || r.top < c.top - 1 || r.bottom > c.bottom + 1) { clipped = p; break; }
    }
    if (clipped) { bad.push(`clipped by ${name(clipped)}: ${name(el)}`); continue; }
    const at = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    if (!at || !(at === el || el.contains(at))) bad.push(`covered by ${at ? name(at) : 'nothing'}: ${name(el)}`);
  }
  return bad;
}"""

CONTROLS = ".lx button, .lx a[href], .lx input, .lx select, .gv button, .gv a[href], .gv input, .gv select"


@pytest.fixture(scope="module")
def pages(tmp_path_factory):
    pw = pytest.importorskip("playwright.sync_api")
    if not shutil.which("npx"):
        pytest.skip("npx is not installed")
    out = tmp_path_factory.mktemp("log-govern-layout")
    run = subprocess.run(["npx", "vitest", "run", "tests/log-govern-layout-fixture.test.tsx"], cwd=WEB,
                         env={**os.environ, "LAYOUT_OUT": str(out)}, capture_output=True, text=True,
                         timeout=300)
    assert run.returncode == 0, run.stdout[-2000:] + run.stderr[-2000:]
    with pw.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as error:  # noqa: BLE001 — no browser here: nothing to tap
            pytest.skip(f"Chromium is not available: {error}")
        yield browser, out
        browser.close()


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("page", ["log.html", "govern.html"])
def test_every_control_can_be_tapped(pages, width, page):
    browser, out = pages
    tab = browser.new_page(viewport={"width": width, "height": 900})
    try:
        tab.goto((out / page).as_uri())
        tab.wait_for_timeout(300)
        assert tab.locator(CONTROLS).count() >= 8
        bad = tab.evaluate(TAP, CONTROLS)
        assert not bad, "\n".join(bad[:40])
        if page == "log.html":
            by_run = tab.locator('[data-testid="lx-by-run"]')
            by_run.scroll_into_view_if_needed()
            assert by_run.bounding_box()["width"] >= 24
            by_run.click(timeout=3000)  # Playwright's own actionability: visible, stable, receives events
    finally:
        tab.close()
