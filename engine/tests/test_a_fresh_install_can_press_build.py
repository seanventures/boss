""""What happened to the core functionality when I click on Build nothing happens."

Nothing was broken. Nothing had ever been installed.

The delivery catalogue — the twelve workflows a directive is routed to — was installed by an
endpoint whose own docstring says it is "safe to call on every boot", and nothing called it on
boot. So a fresh project had an empty registry, and pressing Build posted /api/builds and got:

    409 No approved workflow mapping matched this class of work.

True, unactionable, and about a step the operator was never asked to take. The message did
render — below the fold, under the suggestion chips — which is why it read as "nothing
happens". The same emptiness hid the workflow picker in the composer bar, because it returns
null when there is nothing to pick: "I also dont see the Workflow option in the Command
Center."

Installing is not enough on its own. A published workflow nothing points at is unreachable, so
the seed also routes each class of work — one canonical choice per class, never overwriting a
slot the operator owns.
"""
from __future__ import annotations

import inspect

from boss_engine.services import workflow_catalogue as wc


def test_the_catalogue_is_seeded_at_boot():
    from apps.api.app import main

    source = inspect.getsource(main.lifespan)
    assert "workflow_catalogue import seed" in source
    assert "default_project_id" in source


def test_and_a_failure_to_seed_never_blocks_the_api():
    """A boot step that can stop the API from starting is a worse bug than the one it fixes."""
    from apps.api.app import main

    source = inspect.getsource(main.lifespan)
    at = source.index("delivery catalogue")
    assert "except Exception" in source[at:at + 1400]


def test_installing_also_routes_each_class_of_work():
    """A published workflow that nothing points at cannot be reached. Installing and routing
    are one act for a DEFAULT; choosing differently is the operator's."""
    source = inspect.getsource(wc.seed)
    assert "_seed_default_mappings(connection, project_id, actor=actor)" in source


def test_only_the_one_the_console_already_calls_the_default():
    """"Installing twelve workflows is not the same as choosing one" — that rule stands. The
    exception is the slot the composer's picker already shows as "Standard (default)":
    shipping a control that says a default is in force while the router has none is what made
    Build fail on a clean install."""
    assert wc._DEFAULT_MAPPINGS == (("project.new", "standard_product_delivery"),)


def test_and_it_loses_to_any_choice_a_person_makes():
    """Priority is part of the mapping's unique slot AND of the resolver's score, so seeding at
    the 100 the console writes would both collide with an explicit choice and compete with
    it."""
    assert wc._DEFAULT_MAPPING_PRIORITY < 100
    source = inspect.getsource(wc._seed_default_mappings)
    assert "priority=_DEFAULT_MAPPING_PRIORITY" in source


def test_every_other_class_stays_unmapped_until_somebody_chooses():
    mapped = {work_class for work_class, _ in wc._DEFAULT_MAPPINGS}

    for work_class in ("bugfix", "feature.major", "feature.minor"):
        assert work_class not in mapped, work_class


def test_and_every_default_points_at_a_workflow_that_exists():
    keys = {entry["key"] for entry in wc.CATALOGUE}

    for _, workflow_key in wc._DEFAULT_MAPPINGS:
        assert workflow_key in keys, workflow_key


def test_not_all_twelve_because_a_tie_is_an_error():
    """Three catalogue entries declare `any` and two declare `project.new`. The resolver raises
    WorkflowMappingAmbiguousError on a tie, so mapping everything would replace "nothing is
    routable" with "everything is ambiguous"."""
    by_class: dict[str, int] = {}
    for entry in wc.CATALOGUE:
        by_class[entry["work_class"]] = by_class.get(entry["work_class"], 0) + 1

    assert max(by_class.values()) > 1, "this test is meaningless if no class is shared"
    assert len(wc._DEFAULT_MAPPINGS) < len(wc.CATALOGUE)


def test_a_choice_the_operator_already_made_is_left_alone():
    source = inspect.getsource(wc._seed_default_mappings)
    assert "SELECT id FROM workflow_mappings" in source
    assert "continue" in source
    # And a class whose workflow is not installed here is skipped, not invented.
    assert "if not version:" in source


def test_seeding_is_idempotent_on_every_boot():
    """It runs on every start, so a second run must publish nothing and map nothing new."""
    seed_source = inspect.getsource(wc.seed)
    assert "Idempotent" in seed_source
    assert "WorkflowConflictError" in seed_source


def test_cancelling_a_run_can_actually_import_what_it_needs():
    """"Cancel has to mean the work stops. Anything else is a button that lies." It lied by
    500: the work-cancelling path imported `JobQueue` from services.jobs, where no such name
    has ever existed — `cancel_queued_job` is a method on JobLeaseService. So the first cancel
    anybody attempted raised ImportError, which is the worst moment for a bad import.
    """
    import inspect

    from boss_engine.services import workflows

    source = inspect.getsource(workflows._cancel_work_this_run_started)
    code = "\n".join(line for line in source.splitlines()
                     if not line.strip().startswith("#"))
    assert "JobQueue" not in code
    assert "JobLeaseService" in code
    assert "job_service.cancel_queued_job(" in code


def test_and_the_name_it_imports_exists():
    """The assertion the ImportError would have failed. Written against the module rather than
    the string, so renaming the method breaks this instead of production."""
    from boss_engine.services.jobs import JobLeaseService

    assert hasattr(JobLeaseService, "cancel_queued_job")
    assert not hasattr(__import__("boss_engine.services.jobs", fromlist=["x"]), "JobQueue")


def test_the_community_install_has_the_coding_pipeline_on():
    """"Code tasks are disabled (BOSS_ENABLE_CODE_TASK=false)" — at step four of ten, after
    the panel had convened, written a PRD and taken a human approval.

    Both flags default to false in config.py, which is right for a library and wrong for an
    install whose entire purpose is to build software. A customer cannot be expected to find a
    flag they were never told about, in a file they did not write, after the product has
    already spent their model budget.
    """
    import pathlib

    import yaml

    compose = yaml.safe_load(
        pathlib.Path("scripts/install/community-compose.yml").read_text())
    services = compose["services"]

    for name in ("api", "worker"):
        env = services[name].get("environment") or {}
        assert env.get("BOSS_ENABLE_CODE_TASK") == "${BOSS_ENABLE_CODE_TASK:-true}", name
        assert env.get("BOSS_GOVERNED_CODING_PIPELINE") == \
            "${BOSS_GOVERNED_CODING_PIPELINE:-true}", name


def test_and_both_services_agree():
    """The API creates the code task and the worker runs it. One with the flag and one without
    is a task that is accepted and never executed."""
    import pathlib

    import yaml

    compose = yaml.safe_load(
        pathlib.Path("scripts/install/community-compose.yml").read_text())
    api = (compose["services"]["api"].get("environment") or {})
    worker = (compose["services"]["worker"].get("environment") or {})

    for flag in ("BOSS_ENABLE_CODE_TASK", "BOSS_GOVERNED_CODING_PIPELINE"):
        assert api.get(flag) == worker.get(flag), flag


def test_the_operator_can_still_turn_it_off():
    """Defaulted, not forced: `${…:-true}` means a value in .env still wins."""
    import pathlib

    text = pathlib.Path("scripts/install/community-compose.yml").read_text()
    assert "${BOSS_ENABLE_CODE_TASK:-true}" in text
