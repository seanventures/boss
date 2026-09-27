"""A run seeded six agents with no connection to the work, and omitted the two doing it.

`default_roles` answered with STAGE_ROLES — the map BOSS attributes model calls to — for every
run of every workflow. Right for the software factory, whose stages ARE those roles. Nonsense
for anything else: the video workflow, whose steps name `video_editor` and `media_publisher`,
seeded an Adversarial Reviewer, a Chief Architect, a QA Lead, a Security Reviewer, a Software
Engineer and a UX Engineer.

Not cosmetic. The panel under a run reads "Only these agents may sign off this app's gates.
Anyone else is refused" — so this decides who can approve, and it was naming six strangers while
leaving out the two agents actually on the job.
"""
from boss_engine.services.agent_assignment import default_roles

VIDEO = {
    "steps": [
        {"id": "fetch-link", "kind": "capability", "captain": "video_editor"},
        {"id": "edit", "kind": "capability", "captain": "video_editor"},
        {"id": "publish", "kind": "capability", "captain": "media_publisher",
         "approval": {"mode": "manual"}},
    ]
}


def test_a_workflow_that_names_its_agents_gets_those_agents():
    assert default_roles(VIDEO) == ["media_publisher", "video_editor"]


def test_none_of_the_software_factory_roles_come_along():
    """The six that were seeded onto a video run, by name, so this cannot quietly regress."""
    seeded = set(default_roles(VIDEO))
    for stranger in ("adversarial_reviewer", "chief_architect", "qa", "security_reviewer",
                     "software_engineer", "ux_engineer"):
        assert stranger not in seeded, f"{stranger} has nothing to do with this workflow"


def test_an_approver_is_on_the_run_even_if_no_step_is_captained_by_them():
    """A gate naming somebody the project has not assigned is the refusal this seeding exists
    to avoid — that reasoning was right and is kept."""
    spec = {"steps": [
        {"id": "edit", "captain": "video_editor"},
        {"id": "gate", "kind": "decision",
         "approval": {"mode": "manual", "approver_role": "release_manager"}},
    ]}
    assert default_roles(spec) == ["release_manager", "video_editor"]


def test_a_workflow_that_names_nobody_still_gets_a_team():
    """Every catalogue workflow. Falling back to STAGE_ROLES is right where the spec is silent —
    an empty team would mean nobody may approve anything."""
    roles = default_roles({"steps": [{"id": "a", "kind": "capability"}]})
    assert len(roles) > 1
    assert "software_engineer" in roles


def test_no_spec_at_all_behaves_as_before():
    assert default_roles() == default_roles(None) != []
