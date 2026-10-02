"""A skill's declared risk tier has to survive being registered in a project.

`workflow_custom_steps.resolve_risk` reads `project_skills.config.risk_tier`. Registering a
skill without it means every custom step built on that skill binds at UNKNOWN_RISK — which is
wrong in both directions at once: a tier-0 read "always requires a person", so a workflow cannot
run unattended at all, and a tier-3 publish is understated as 2.
"""
import inspect


def test_the_enable_path_passes_the_tier_to_the_project_row():
    """Found by building a real workflow: every step came back "tier 2, requires a person",
    including video.plan_edit, which renders nothing and declares tier 0."""
    from apps.api.app import main

    source = inspect.getsource(main._set_project_skill_state)
    assert "ensure_skill(" in source
    assert "risk_tier" in source, "the catalogue tier must reach the project_skills row"
    # And it must come off the catalogue entry, not be invented here.
    assert 'skill["risk_tier"]' in source or "skill.get(\"risk_tier\")" in source


def test_enabling_also_repairs_a_row_that_predates_the_fix():
    """ensure_skill only fires when the row is ABSENT.

    So every skill registered before the tier was carried across keeps an empty config and goes
    on binding at UNKNOWN_RISK forever. Enabling is what an operator already does when a skill
    misbehaves, which makes it the reachable place for the repair — better than a migration
    nobody knows to run.
    """
    from apps.api.app import main

    source = inspect.getsource(main._set_project_skill_state)
    assert "UPDATE project_skills SET config" in source
    assert "risk_tier" in source.split("UPDATE project_skills")[1][:400]


def test_the_bootstrap_path_does_the_same():
    """Two ways in, one rule. A skill seeded at bootstrap and one enabled later must land with
    the same tier, or a workflow behaves differently depending on how its skill arrived."""
    from boss_engine.services import skill_registry

    source = inspect.getsource(skill_registry.seed_system_skills)
    assert "config=" in source and "risk_tier" in source


def test_resolve_risk_still_reads_that_exact_field():
    """If this moves, both fixes above become silent no-ops.

    REWRITTEN when the skill catalogue became one module. resolve_risk no longer reads
    project_skills itself — reading it alone is what made every AUTHORED skill "not registered
    in this project" — it asks skill_catalogue, which is where `project_skills.config.risk_tier`
    is now read (as a floor under the spec's own tier). The property is the same: the field the
    two fixes above write is the field the step's risk is resolved from.
    """
    from boss_engine.services import skill_catalogue, workflow_custom_steps

    assert "skill_catalogue import entry, risk_facts" in inspect.getsource(
        workflow_custom_steps.resolve_risk)
    # _build_entries is the merge; entries() returns it, once per skill_catalogue.one_pass.
    source = inspect.getsource(skill_catalogue._build_entries)
    assert "FROM project_skills" in source
    assert '.get("risk_tier")' in source
