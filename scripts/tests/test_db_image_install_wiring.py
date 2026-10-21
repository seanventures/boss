"""The installer moves an Alpine database to the Debian image before anything opens it (KG spec §3.1).

The migration itself runs against real Docker in engine/tests/test_db_image_upgrade_real_docker.py;
this checks the wiring that decides whether it runs at all.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_the_db_service_builds_the_debian_image_into_its_own_volume() -> None:
    compose = yaml.safe_load((ROOT / "scripts/install/community-compose.yml").read_text())
    db = compose["services"]["db"]
    assert db["build"] == {"context": ".", "dockerfile": "db-image/Dockerfile"}
    assert "boss-community-db:" in db["image"] and "BOSS_RECOVERY_DB_IMAGE_ID" in db["image"]
    data = [v for v in db["volumes"] if v.endswith(":/var/lib/postgresql/data")]
    # Never the Alpine-era volume: reopening it under glibc corrupts text indexes.
    assert data == ["boss_community_pg_debian:/var/lib/postgresql/data"]
    assert "boss_community_pg" not in compose["volumes"]


def test_the_db_image_pins_its_bases_and_guards_the_data_directory() -> None:
    dockerfile = (ROOT / "scripts/install/db-image/Dockerfile").read_text()
    bases = re.findall(r"^ARG \w+=(\S+)$", dockerfile, re.MULTILINE)
    assert len(bases) == 3 and all("@sha256:" in b for b in bases)
    assert all("trixie" in b or "apache/age" in b for b in bases)
    assert 'ENTRYPOINT ["boss-db-entrypoint.sh"]' in dockerfile


def test_install_migrates_after_the_recovery_backup_and_before_the_database_starts() -> None:
    text = (ROOT / "install.sh").read_text()
    backup = text.index('bash "$_backup_helper"')
    migrate = text.index("scripts/install/db_image_migrate.py --docker-prefix")
    first_up = text.index("up -d --no-build db api")
    assert backup < migrate < first_up
    assert "from-compose --old-db-container" in text[migrate:first_up]
