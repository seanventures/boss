"""Isolated (no Postgres) contracts for infra honesty + Community UI v1 cut.

Firecracker stays frozen. Not public GA.
"""

from __future__ import annotations

from pathlib import Path

from boss_engine.config import Settings


ROOT = Path(__file__).resolve().parents[2]


def test_ui_v1_cut_doc_exists_and_names_the_home_nav():
    path = ROOT / "docs/product/UI_V1_CUT.md"
    assert path.is_file(), "docs/product/UI_V1_CUT.md must exist"
    text = path.read_text(encoding="utf-8")
    assert "Not public GA" in text
    for nav in (
        "activity",
        "work",
        "decisions",
        "reliability",
        "code_tasks",
        "reports",
        "budget",
        "enterprise",
    ):
        assert f"`{nav}`" in text, nav
    assert "Cloud Easy Connect" in text
    assert "PaidSurfaceGate" in text
    assert "host.connected" in text
    assert "Areas are not deleted" in text or "not deleted" in text.lower()


def test_create_droplet_fail_closes_before_adapter_when_flag_off():
    source = (ROOT / "engine/boss_engine/services/infrastructure.py").read_text(
        encoding="utf-8"
    )
    flag_idx = source.index("infra_real_provision_allowed")
    adapter_idx = source.index("self.digitalocean.create_droplet")
    assert flag_idx < adapter_idx
    blocked = source[flag_idx:adapter_idx]
    assert "BOSS_INFRA_REAL_PROVISION_ALLOWED" in blocked
    assert "mark_failed" in blocked
    assert "_record_resource_receipt" in blocked
    assert 'status="failed"' in blocked or "status='failed'" in blocked


def test_fabric_and_goldeneye_status_ensure_tables_and_never_invent_host():
    elastic = (ROOT / "engine/boss_engine/services/elastic_fabric.py").read_text(
        encoding="utf-8"
    )
    goldeneye = (ROOT / "engine/boss_engine/services/goldeneye_paas.py").read_text(
        encoding="utf-8"
    )
    assert "def ensure_tables" in elastic
    assert "def ensure_tables" in goldeneye
    assert "self.ensure_tables(connection)" in elastic
    assert "self.ensure_tables(connection)" in goldeneye
    assert '"connected": bool(live)' in elastic
    assert '"connected": bool(live)' in goldeneye
    assert '"not_configured"' in elastic
    assert '"not_configured"' in goldeneye


def test_firecracker_claims_stay_frozen_by_default():
    settings = Settings()
    assert settings.microvm_claims_frozen is True
    features = (ROOT / "docs/community/FEATURES.md").read_text(encoding="utf-8")
    assert "BOSS_MICROVM_CLAIMS_FROZEN" in features
    assert "frozen" in features.lower()


def test_area_id_union_still_includes_hidden_community_areas():
    shell = (ROOT / "apps/web/app/ConsoleShell.tsx").read_text(encoding="utf-8")
    start = shell.index("type AreaId =")
    end = shell.index("type AreaDef")
    union = shell[start:end]
    hidden = (
        "infra",
        "metatron",
        "studio",
        "sandboxes",
        "github",
        "ops",
        "org",
        "foundations",
        "components",
        "pipeline",
        "comms",
        "assurance",
        "capabilities",
    )
    for area in hidden:
        assert f'"{area}"' in union, f"AreaId union dropped {area}"
        assert f'id: "{area}"' in shell, f"AREAS dropped hidden {area}"
