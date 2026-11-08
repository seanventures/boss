"""A step's label says what the run does there (Add a project v2 walk, 2026-11-07).

The PRD approval was labelled "PRD approval (automatic)" while, on an install with identity
enforced (BOSS_MEETING_PRD_AUTO_APPROVE unset → off), a person signs it: the preview showed an
amber "you approve" hand next to a label saying automatic. label_for() is THE label every surface
reads (workflow_readiness._label, the palette), and it says "(automatic)" only where the install
approves the step itself, so the label and the "you approve" mark can never disagree.
"""
from __future__ import annotations

import pytest

from boss_engine.services import workflow_capabilities as caps
from boss_engine.services import workflow_readiness


@pytest.mark.parametrize("prd_auto", [True, False])
def test_no_label_says_automatic_where_a_person_is_asked(prd_auto):
    for key in caps.CAPABILITY_KEYS:
        label = caps.label_for(key, prd_auto)
        if caps.stops_for_a_person(key, None, prd_auto_approve=prd_auto):
            assert "automatic" not in label.lower(), f"{key}: '{label}' but a person is asked"


def test_the_prd_approval_says_automatic_only_when_it_is():
    assert caps.label_for("spec_review", True) == "PRD approval (automatic)"
    assert caps.label_for("spec_review", False) == "PRD approval"
    assert not caps.stops_for_a_person("spec_review", None, prd_auto_approve=True)
    assert caps.stops_for_a_person("spec_review", None, prd_auto_approve=False)


@pytest.mark.parametrize("prd_auto", [True, False])
def test_every_surface_reads_the_one_label(monkeypatch, prd_auto):
    monkeypatch.setattr(caps, "prd_auto_approved", lambda settings=None: prd_auto)
    step = {"id": "spec_review", "binding": {"capability": "spec_review"}}
    assert workflow_readiness._label(step, {}) == caps.label_for("spec_review", prd_auto)  # noqa: SLF001
    row = next(c for c in caps.palette() if c["key"] == "spec_review")
    assert row["label"] == caps.label_for("spec_review", prd_auto)
    assert row["stops_for_a_person"] is (not prd_auto)


def test_the_legend_word_is_a_few_words():
    from boss_engine.services.project_experience.add_project import _short  # noqa: SLF001

    assert _short({"binding": {"capability": "spec_review"}}, "PRD approval") == "PRD"
    assert _short({"binding": {"capability": "deploy"}, "title": "Deploy again (more tests)"},
                  "Deploy again (more tests)") == "deploy again"
    assert _short({"binding": {"capability": "deploy"}}, "Deploy") == "deploy"
