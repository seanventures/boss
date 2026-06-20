#!/usr/bin/env python3
"""Re-render the mermaid diagrams in GOVERNED_CODING_PIPELINE.md to standalone, zoomable SVGs
under docs/product/diagrams/. The .md stays the source of truth (mermaid blocks render inline on
GitHub); the SVGs are what the "View as zoomable image" links open for infinite vector zoom.

Run after editing a diagram in the .md:
    python3 docs/product/build_diagrams.py
Requires mermaid-cli (auto-fetched via npx): https://github.com/mermaid-js/mermaid-cli
"""
import json
import re
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
DIAGRAMS = HERE / "diagrams"
SLUGS = ["01-value-flow", "02-pipeline", "03-cost-dial"]  # in document order

DIAGRAMS.mkdir(exist_ok=True)
md = (HERE / "GOVERNED_CODING_PIPELINE.md").read_text()
blocks = re.findall(r"```mermaid\n(.*?)```", md, re.S)

pconf = Path(tempfile.gettempdir()) / "puppeteer-mmdc.json"
pconf.write_text(json.dumps({"args": ["--no-sandbox", "--disable-setuid-sandbox"]}))

for i, body in enumerate(blocks):
    slug = SLUGS[i] if i < len(SLUGS) else f"{i + 1:02d}-diagram"
    mmd = DIAGRAMS / f"{slug}.mmd"
    svg = DIAGRAMS / f"{slug}.svg"
    mmd.write_text(body.strip() + "\n")
    subprocess.run(
        ["npx", "-y", "@mermaid-js/mermaid-cli", "-p", str(pconf),
         "-i", str(mmd), "-o", str(svg), "-b", "white"],
        check=True,
    )
    print("rendered", svg.name)

print(f"done — {len(blocks)} diagram(s)")
