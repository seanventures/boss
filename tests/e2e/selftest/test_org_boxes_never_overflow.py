"""Nothing in Agents › Organization leaves its box, at 1440, 1280 and 390.

THE INCIDENT (boss-1, 2026-10-04). In Teams at ~1440 the Leadership card's Waiting row —
"Rally 1d 2h Needs you Create the tickets · Meeting to tic…" — ran out of its card and was drawn
over the Architecture card's rows. The fixtures had short names; real runs do not.

apps/web/tests/org-layout-fixture.test.tsx renders Teams, the phone list, Flow's nodes and
Stuck now, and the Agent panel with very long agent, team, model, ability, step and run names,
and writes the pages here. A real Chromium lays them out, and for every card (a team card, a
Flow node, the Stuck now strip, a phone row, the drawer) this asserts:
  - no element's box reaches outside its card;
  - no two text boxes inside it overlap.
Skipped where Playwright's Chromium is not installed.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[3] / "apps" / "web"

CARDS = ".ag-orgteam, .ag-flow-node, .ag-flow-stuck, .ag-zones-lrow, .org-drawer"

MEASURE = r"""(cards) => {
  const problems = [];
  const visible = (el) => {
    if (el.closest('.ag-sr, [hidden], [aria-hidden="true"], svg, .org-menu')) return false;
    const st = getComputedStyle(el);
    return st.display !== 'none' && st.visibility !== 'hidden';
  };
  const name = (el) => `${el.dataset.testid || el.className || el.tagName}`.slice(0, 50)
    + (el.textContent ? ` "${el.textContent.trim().slice(0, 30)}"` : "");
  for (const card of document.querySelectorAll(cards)) {
    const c = card.getBoundingClientRect();
    if (!c.width) continue;
    for (const el of card.querySelectorAll('*')) {
      if (!visible(el)) continue;
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height) continue;
      if (r.left < c.left - 1 || r.right > c.right + 1) {
        problems.push(`outside ${name(card)}: ${name(el)} [${Math.round(r.left)}..${Math.round(r.right)}] vs [${Math.round(c.left)}..${Math.round(c.right)}]`);
      }
    }
    const boxes = [];
    const walker = document.createTreeWalker(card, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
      if (!n.textContent.trim() || !n.parentElement || !visible(n.parentElement)) continue;
      const range = document.createRange();
      range.selectNodeContents(n);
      // What is PAINTED: a text run cut by its element (an ellipsis) is measured to that element.
      const p = n.parentElement.getBoundingClientRect();
      for (const q of range.getClientRects()) {
        const r = { left: Math.max(q.left, p.left), right: Math.min(q.right, p.right),
                    top: Math.max(q.top, p.top), bottom: Math.min(q.bottom, p.bottom) };
        if (r.right - r.left > 1 && r.bottom - r.top > 1) boxes.push([r, n.textContent.trim().slice(0, 30), n]);
      }
    }
    for (let i = 0; i < boxes.length; i += 1) for (let j = i + 1; j < boxes.length; j += 1) {
      const [a, ta, na] = boxes[i]; const [b, tb, nb] = boxes[j];
      if (na === nb) continue;
      const w = Math.min(a.right, b.right) - Math.max(a.left, b.left);
      const h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
      if (w > 1 && h > 1) problems.push(`overlap in ${name(card)}: "${ta}" × "${tb}"`);
    }
  }
  return problems;
}"""


@pytest.fixture(scope="module")
def pages(tmp_path_factory):
    pw = pytest.importorskip("playwright.sync_api")
    if not shutil.which("npx"):
        pytest.skip("npx is not installed")
    out = tmp_path_factory.mktemp("org-layout")
    run = subprocess.run(["npx", "vitest", "run", "tests/org-layout-fixture.test.tsx"], cwd=WEB,
                         env={**os.environ, "ORG_LAYOUT_OUT": str(out)}, capture_output=True, text=True,
                         timeout=300)
    assert run.returncode == 0, run.stdout[-2000:] + run.stderr[-2000:]
    with pw.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as error:  # noqa: BLE001 — no browser here: nothing to measure
            pytest.skip(f"Chromium is not available: {error}")
        yield browser, out
        browser.close()


@pytest.mark.parametrize("width,page", [(1440, "wide.html"), (1280, "wide.html"), (390, "narrow.html")])
def test_nothing_leaves_its_card_and_no_text_overlaps(pages, width, page):
    browser, out = pages
    tab = browser.new_page(viewport={"width": width, "height": 900})
    try:
        tab.goto((out / page).as_uri())
        tab.wait_for_timeout(300)
        assert tab.locator(CARDS).count() >= 3
        problems = tab.evaluate(MEASURE, CARDS)
        assert not problems, "\n".join(problems[:40])
        # And the page itself never scrolls sideways.
        assert tab.evaluate("document.documentElement.scrollWidth - innerWidth") <= 0
    finally:
        tab.close()
