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
        # "metatron" was dropped from this list (2026-10-02): the area was removed from AREAS
        # when the BOSS Agent absorbed it, and the visibility rule is now generated from AREAS,
        # so an AreaId with no AREAS entry is a blank screen
        # (apps/web/tests/every-area-is-actually-visible.test.ts forbids it).
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
