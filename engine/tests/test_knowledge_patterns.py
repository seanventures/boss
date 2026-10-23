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


def test_the_one_database_image_is_alpine_with_pgvector_and_age():
    """Operator decision 2026-10-22: one image, Postgres 16 on Alpine (the base and libc installs
    already run), pgvector 0.8.x and AGE 1.6.0 compiled in, the default everywhere, pinned by tag."""
    dockerfile = (ROOT / "scripts/install/db-image/Dockerfile").read_text()
    assert "ARG PG_IMAGE=postgres:16.12-alpine3.23" in dockerfile
    assert "PGVECTOR_VERSION=0.8.1" in dockerfile and "AGE_VERSION=1.6.0" in dockerfile
    assert 'OPTFLAGS=""' in dockerfile                      # no -march=native: portable
    assert "shared_preload_libraries = 'age'" in dockerfile
    assert "bookworm" not in dockerfile and "debian" not in dockerfile.lower()
    assert not (ROOT / "infra/postgres/Dockerfile").exists()
    community = (ROOT / "scripts/install/community-compose.yml").read_text()
    assert "image: ${BOSS_RECOVERY_DB_IMAGE_ID:-boss-community-db:pg16.12-vector0.8.1-age1.6.0}" in community
    assert "shared_preload_libraries=age" in community
    assert "image: postgres:16-alpine" not in community
    schema = (ROOT / "db/schema.sql").read_text()
    assert "CREATE EXTENSION IF NOT EXISTS vector" in schema and "CREATE EXTENSION IF NOT EXISTS age" in schema
