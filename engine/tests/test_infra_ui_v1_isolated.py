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
    import re as _re

    for name, source in (("elastic_fabric", elastic), ("goldeneye_paas", goldeneye)):
        # The tables must exist before they are read, or "no hosts" and "no table yet" are the
        # same empty answer.
        assert "def ensure_tables" in source, name
        assert "self.ensure_tables(connection)" in source, name
        # A store that was never set up must say so rather than report an empty, healthy world.
        assert '"not_configured"' in source, name

        # NEVER INVENT A HOST.
        #
        # This used to assert the literal string '"connected": bool(live)' in both files, which
        # is a variable NAME: goldeneye_paas calls its liveness flag `connected`, spells the
        # same behaviour correctly, and failed the test for it. The property is that
        # `connected` is DERIVED from a liveness signal and never asserted true outright.
        assert _re.search(r'"connected":\s*bool\(', source), (
            f"{name}: connected must be derived from a liveness check")
        assert _re.search(r'"connected":\s*True', source) is None, (
            f"{name}: connected must never be hardcoded true — that invents a host")


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
        if area == "metatron":
            # ITS PAGE IS GONE, ITS NAME IS NOT. The always-on captain had no durable session
            # in this database and the operator never used it, so the nav entry was removed and
            # what it claimed to be — a captain with the whole factory in view — is what the one
            # BOSS Agent area now does. The AreaId union keeps the name so an existing
            # `?area=metatron` bookmark still resolves rather than dropping somebody on the
            # organisation picker.
            assert 'id: "metatron"' not in shell
            continue
        assert f'id: "{area}"' in shell, f"AREAS dropped hidden {area}"
