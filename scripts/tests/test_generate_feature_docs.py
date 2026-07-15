from pathlib import Path

from scripts.generate_feature_docs import (
    generate,
    parse_traceability,
)


def test_feature_docs_generate_and_detect_drift(tmp_path: Path):
    source = tmp_path / "TRACEABILITY.md"
    output = tmp_path / "features"
    source.write_text(
        """# Traceability

| Requirement label | Source | Implementation files | Test coverage | Status |
|---|---|---|---|---|
| EVT-001 Reactive dispatch | Spec 00 | `event_dispatch.py` | `test_events.py` | Implemented: durable and deduplicated |
| DOC-002 Docs worker | Spec 06 | planned | planned | Planned |

## Later table

| Requirement | Source | Planning/spec files | Runtime tests | Status |
| --- | --- | --- | --- | --- |
| ACC-001 Account connect | PRD v2 | `commercial.py` | `test_api.py` | Partial: hosted login remains planned |
""",
        encoding="utf-8",
    )

    features = parse_traceability(source)
    assert [(feature.label, feature.status) for feature in features] == [
        ("EVT-001", "Implemented"),
        ("DOC-002", "Planned"),
        ("ACC-001", "Partial"),
    ]
    assert generate(source, output, check=False) == 0
    assert generate(source, output, check=True) == 0
    assert "boss-status--implemented" in (output / "implemented.md").read_text()
    assert "EVT-001 — Reactive dispatch" in (output / "index.md").read_text()

    (output / "index.md").write_text("stale\n", encoding="utf-8")
    assert generate(source, output, check=True) == 1
