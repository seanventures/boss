"""Line-extractor conformance. The score is computed. It is not model precision."""
from __future__ import annotations

from pathlib import Path

from boss_engine.services.knowledge.patterns import (
    LineExtractor,
    conformance_corpus,
    score_extractor,
)


ROOT = Path(__file__).resolve().parents[2]


def test_line_extractor_precision_on_one_hundred_labelled_facts():
    corpus = conformance_corpus()
    paraphrase = next(item for item in corpus if item["text"].startswith("The owner of "))
    assert LineExtractor().extract(paraphrase["text"]) == []
    assert paraphrase["facts"]
    scored = score_extractor(LineExtractor(), corpus)
    assert scored["items"] == 100
    assert scored["labelled_facts"] == 100
    assert scored["false_positive"] == 0
    assert scored["false_negative"] > 0
    assert scored["precision"] == scored["true_positive"] / (
        scored["true_positive"] + scored["false_positive"]
    )
    assert scored["precision"] >= 0.85
    assert scored["extractor"] == "line"
    assert scored["model_precision"] is None


def test_extractor_source_does_not_read_the_labelled_facts():
    source = (ROOT / "engine/boss_engine/services/knowledge/patterns.py").read_text()
    extract = source.split("def extract", 1)[1].split("def conformance_corpus", 1)[0]
    assert "conformance_corpus" not in extract
    assert "score_extractor" not in extract


def test_opt_in_image_pins_age_1_6_and_compose_stays_on_stock_postgres():
    dockerfile = (ROOT / "infra/postgres/Dockerfile").read_text()
    assert "apache-age-1.6.0-src.tar.gz" in dockerfile
    assert "postgresql-16-pgvector" in dockerfile
    assert "shared_preload_libraries = 'age'" in dockerfile
    community = (ROOT / "scripts/install/community-compose.yml").read_text()
    assert "image: postgres:16-alpine" in community
    assert "boss-postgres-kg" not in community
    compose_files = list((ROOT / "scripts/install").glob("*compose*.yml"))
    compose_files += list((ROOT / "infra").glob("*compose*.yml"))
    assert compose_files
    for path in compose_files:
        assert "boss-postgres-kg" not in path.read_text(), path
