"""A documentation edit can only ever be the size of the edit.

A scripted doc edit once replaced an EMPTY slice (its end marker came before its start), which
inserts the replacement between every character: GETTING_STARTED.md went from 18 KB to 29 MB of
one repeated paragraph. Every Markdown page an operator reads is held to a sane size and to no
paragraph repeated over and over, so that class of edit fails here instead of shipping.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAGES = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md")),
         *sorted((ROOT / "scripts" / "install").rglob("*.md"))]
LIMIT_BYTES = 400_000


def test_every_page_is_a_readable_size():
    big = [f"{p.relative_to(ROOT)}: {p.stat().st_size} bytes" for p in PAGES
           if p.stat().st_size > LIMIT_BYTES]
    assert not big, big


def test_no_paragraph_is_repeated_over_and_over():
    runaway = []
    for page in PAGES:
        paragraphs = [b.strip() for b in page.read_text(encoding="utf-8", errors="replace")
                      .split("\n\n") if len(b.strip()) > 80]
        for text, count in Counter(paragraphs).items():
            if count > 5:
                runaway.append(f"{page.relative_to(ROOT)}: {count}× {text[:60]!r}")
    assert not runaway, runaway[:5]
