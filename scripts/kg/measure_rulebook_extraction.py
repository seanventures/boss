#!/usr/bin/env python3
"""Measure a rulebook extraction against the NorthPeak Bank answer key (rulebook source G5).

Reads, never writes. Two ways:

  # A real run: the data-quality rules a project's extraction proposed (read-only SELECTs).
  DATABASE_URL=postgresql://…/boss PYTHONPATH=.:engine \\
      python3 scripts/kg/measure_rulebook_extraction.py --project northpeak-lending [--scan rbx_…] [--json]

  # The scripted conformance engine on the shipped PDF, no database and no model: what the
  # pipeline (reader → sections → prompt → parse) gives when the analyst reads perfectly.
  PYTHONPATH=.:engine python3 scripts/kg/measure_rulebook_extraction.py --scripted

Reports precision and recall on rule ids, retired rules proposed (must be 0), threshold metric /
value / text accuracy, and per-field accuracy; exits 1 when recall or precision is below
--min (default 0.95).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "engine")]


def _scripted() -> list[dict]:
    from boss_engine.services.knowledge import document_upload, rulebook_extract
    from tools.dq_sample.scripted_analyst import answer

    pdf = ROOT / "tools" / "dq_sample" / "NorthPeak_Loan_DQ_Rulebook_v3.2.pdf"
    sections = document_upload._as_dicts(document_upload.propose_sections(
        document_upload.read_pages(pdf.read_bytes(), pdf.name)))
    clauses = [{"id": s["key"], "name": document_upload._label(s), "body": "\n".join(s["lines"]).strip(),
                "attrs": {"page": s["page"]}} for s in sections]
    out = []
    for batch in rulebook_extract.batches(clauses):
        judged = rulebook_extract._parse_judgement(
            answer(f"{rulebook_extract.role_prompt()}\n\n{rulebook_extract.situation('Rulebook', batch)}")) or {}
        for rule in judged.get("rules") or []:
            rule = dict(rule)
            rule["tables"] = rule.get("tables") or rulebook_extract._tables_of(rule.get("columns") or [])
            out.append(rule)
    return out


def _from_project(project: str, scan: str | None) -> list[dict]:
    from sqlalchemy import create_engine, text

    from boss_engine.services.knowledge import rulebook_extract

    url = os.environ.get("DATABASE_URL") or ""
    if not url:
        sys.exit("DATABASE_URL is required to measure a project's extraction.")
    engine = create_engine(url, future=True)
    with engine.connect() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        try:
            return rulebook_extract.proposals_of(connection, project, scan_id=scan)
        finally:
            connection.rollback()


def main(argv: list[str]) -> int:
    from tools.dq_sample import measure

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--project", help="the project whose extraction to measure")
    parser.add_argument("--scan", help="one extraction (its scan id); default every DQ rule of the project")
    parser.add_argument("--scripted", action="store_true", help="measure the scripted analyst on the shipped PDF")
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    parser.add_argument("--min", type=float, default=0.95, help="the bar for precision and recall")
    args = parser.parse_args(argv)
    if not args.scripted and not args.project:
        parser.error("name --project, or pass --scripted")
    proposals = _scripted() if args.scripted else _from_project(args.project, args.scan)
    result = measure.measure(proposals)
    print(json.dumps(result, indent=2, default=str) if args.json else measure.report(result))
    ok = result["precision"] >= args.min and result["recall"] >= args.min and not result["retired_proposed"]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
