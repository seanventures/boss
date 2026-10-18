"""The install card: its tier comes from what the fix touches, and an approval is of exactly what
the card showed (BOSSAPP-571 R5, spec §8.2, decision 5, C-3).

  only tier-2 paths, no flags      → tier 2 (one person holding the seat)
  a tier-3 path, or a host effect  → tier 3 (two people, or the single-operator waiver)
  the fix changed after the card   → the old card is withdrawn; approving it records nothing
  Request changes                  → one more repair round with the note, then a new card
  Reject                           → the self-fix ends, the recommendation reopens with the note
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from boss_engine.services import boss_recommendations
from boss_engine.services import self_fix as sf
from boss_engine.services import self_fix_build
from boss_engine.services import self_fix_install as sfi
from boss_engine.services import self_fix_policy as sp
from boss_engine.services.decisions import DecisionService, InvalidDecisionTransitionError

from ._self_fix_support import approve, harness, self_fix_on  # noqa: F401
from .test_workflow_capability_runner import eng, project  # noqa: F401


@pytest.fixture()
def card(harness):
    harness.script("good_fix")
    sfx = harness.propose()["self_fix"]
    out = self_fix_build.Build(engine=harness.eng, settings=harness.settings, sfx_id=sfx["id"],
                               source=harness.mirror).run()
    assert out["status"] == sf.AWAITING_APPROVAL
    return sfx["id"]


def test_the_tier_follows_paths_and_flags():
    tier2 = sp.classify([sp.FileChange("engine/boss_engine/services/app_start.py", "M",
                                       "x = 1\n", "x = 2\n")])
    tier3_path = sp.classify([sp.FileChange("engine/boss_engine/services/software_factory.py",
                                            "M", "x = 1\n", "x = 2\n")])
    tier3_flag = sp.classify([sp.FileChange("engine/boss_engine/services/app_start.py", "M",
                                            "x = 1\n", 'x = 1\nCMD = "docker rm -f old"\n')])
    assert (tier2.tier, tier3_path.tier, tier3_flag.tier) == (2, 3, 3)
    assert sp.HOST_EFFECT in tier3_flag.flags


def test_the_card_carries_its_tier_and_says_who_approves(harness, card):
    with harness.eng.begin() as c:
        c.execute(text("UPDATE self_fixes SET tier = 3, flags = '[\"host_effect\"]'::jsonb "
                       "WHERE id = :i"), {"i": card})
        decision_id = sfi.request_approval(c, harness.settings, card)
        c.execute(text("UPDATE self_fixes SET decision_id = :d WHERE id = :i"),
                  {"d": decision_id, "i": card})
        row = c.execute(text("SELECT risk_tier, recommendation FROM decisions WHERE id = :i"),
                        {"i": decision_id}).mappings().first()
    drawn = row["recommendation"]["card"]
    assert row["risk_tier"] == 3 and drawn["tier"] == 3 and "host_effect" in drawn["flags"]
    assert "Two people" in drawn["people"]
    assert drawn["command"] == f"boss self-fix install {card}"
    assert drawn["tests"]["none_weakened"] and drawn["why"]


def test_a_card_drawn_before_the_fix_changed_cannot_be_approved(harness, card):
    with harness.eng.begin() as c:
        stale = sf.get(c, card)["decision_id"]
        c.execute(text("UPDATE self_fixes SET fix_tree_sha = :t WHERE id = :i"),
                  {"t": "0" * 40, "i": card})
        fresh = sfi.request_approval(c, harness.settings, card)
        c.execute(text("UPDATE self_fixes SET decision_id = :d WHERE id = :i"),
                  {"d": fresh, "i": card})
        status = c.execute(text("SELECT status FROM decisions WHERE id = :i"),
                           {"i": stale}).scalar()
        hashes = [c.execute(text("SELECT recommendation->'action_preview'->>'content_hash' "
                                 "FROM decisions WHERE id = :i"), {"i": d}).scalar()
                  for d in (stale, fresh)]
    assert status == "withdrawn" and hashes[0] != hashes[1]
    with harness.eng.begin() as c:
        approve(harness, card)  # seeds the members; approves the FRESH card
    with harness.eng.begin() as c:
        with pytest.raises(InvalidDecisionTransitionError):
            DecisionService(harness.settings).transition(c, stale, action="approve",
                                                         actor="ceo", note=None)


def test_request_changes_is_one_more_repair_round(harness, card):
    with harness.eng.begin() as c:
        for role in ("ceo",):
            c.execute(text("INSERT INTO project_members (id, project_id, member_id, display_name,"
                           " member_type, role_keys, permissions) VALUES (:i, :p, :m, :m, "
                           "'human', '[\"ceo\",\"cao\"]'::jsonb, '[\"decision_resolve\"]'::jsonb) "
                           "ON CONFLICT DO NOTHING"),
                      {"i": f"pm_{harness.project}_{role}", "p": harness.project, "m": role})
        decision_id = sf.get(c, card)["decision_id"]
        DecisionService(harness.settings).transition(c, decision_id, action="revise",
                                                     actor="ceo", note="name the function")
        row = sf.get(c, card)
        job = c.execute(text("SELECT payload FROM jobs WHERE project_id = :p AND kind = "
                             "'self_fix.build' ORDER BY created_at DESC LIMIT 1"),
                        {"p": harness.project}).scalar()
    assert row["status"] == sf.FIXING and row["repair_rounds"] == 1
    assert "name the function" in job["note"]
    again = self_fix_build.Build(engine=harness.eng, settings=harness.settings, sfx_id=card,
                                 source=harness.mirror, note=job["note"]).run()
    assert again["status"] == sf.AWAITING_APPROVAL, again
    assert again["decision_id"] != decision_id


def test_reject_ends_it_and_reopens_the_recommendation_with_the_note(harness, card):
    with harness.eng.begin() as c:
        c.execute(text("INSERT INTO project_members (id, project_id, member_id, display_name, "
                       "member_type, role_keys, permissions) VALUES (:i, :p, 'ceo', 'ceo', "
                       "'human', '[\"ceo\",\"cao\"]'::jsonb, '[\"decision_resolve\"]'::jsonb) "
                       "ON CONFLICT DO NOTHING"), {"i": f"pm_{harness.project}_ceo",
                                                   "p": harness.project})
        DecisionService(harness.settings).transition(c, sf.get(c, card)["decision_id"],
                                                     action="reject", actor="ceo",
                                                     note="I would rather fix this by hand")
        row = sf.get(c, card)
        rec = boss_recommendations.list_recommendations(c, harness.project)[0]
    assert row["status"] == sf.REJECTED
    assert rec["status"] == "open" and "by hand" in rec["attempts"][-1]["why"]
