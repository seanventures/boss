"""The DQ case study is BOSS's own: its pipeline design came from a business subject-matter expert
at another bank, and none of that bank's internal system names, decision codes or wording may
ship. This guard reads everything the sample ships — its data, the rulebook PDF's text, the
catalogue entries, the role and skill files, the mock connectors — and refuses any word on a
list kept here only as hashes (so the guard itself does not repeat the names it keeps out)."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: sha256 prefixes of the lower-cased words that must not appear.
FORBIDDEN = {"e73b48f750be9535", "4b8823ed9e5c2392", "73396f3affd1e0c4", "1d697955057274c7",
             "5b39eb394e303bee", "4087b6426d9adf2b"}

SHIPPED = [
    "tools/dq_sample/data.py", "tools/dq_sample/rulebook.py", "tools/dq_sample/README.md",
    "tools/mcp_dq_mock.py", "tools/mcp_service_desk_mock.py",
    "engine/boss_engine/services/dq_catalogue.py",
    "engine/boss_engine/services/knowledge/dq_incident_pipeline.py",
    "engine/boss_engine/services/knowledge/dq_platform.py",
    "engine/boss_engine/services/knowledge/dq_services.py",
    "engine/boss_engine/services/knowledge/dq_mcp.py",
    "packages/specpack/ontology/banking-dq.yaml",
    *sorted(str(p.relative_to(ROOT)) for p in (ROOT / "packages/specpack/roles").glob("dq_*.yaml")),
    *sorted(str(p.relative_to(ROOT))
            for p in (ROOT / "packages/specpack/skills/dq_incident_rca").rglob("*.md")),
]


def _hits(text: str) -> list[str]:
    words = set(re.findall(r"[a-z]+", text.lower()))
    return sorted(w[:2] + "…" for w in words
                  if hashlib.sha256(w.encode()).hexdigest()[:16] in FORBIDDEN)


@pytest.mark.parametrize("path", SHIPPED)
def test_no_shipped_file_names_the_other_bank(path):
    assert _hits((ROOT / path).read_text(encoding="utf-8")) == [], path


def test_the_catalogue_entries_name_no_other_bank():
    from boss_engine.services import dq_catalogue, workflow_catalogue

    entries = [e for e in workflow_catalogue.CATALOGUE if e["key"].startswith("dq_")]
    assert {e["key"] for e in entries} == {"dq_incident_rca", "dq_incident_escalation",
                                           "dq_monitor_intake"}
    text = json.dumps([entries, dq_catalogue.BRIEFS, dq_catalogue.FIELD_TEXT], default=str)
    assert _hits(text) == []


def test_the_rulebook_pdf_names_no_other_bank():
    pypdf = pytest.importorskip("pypdf")
    from boss_engine.services.knowledge import dq_rulebook_sample

    reader = pypdf.PdfReader(str(dq_rulebook_sample.pdf_path()))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert "DQ Monitor" in text and _hits(text) == []
