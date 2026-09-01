"""A run records which workflow governs it.

The workflow runtime and the coding pipeline were built as two systems that never met. Measured
on the live project: 54 workflow definitions, exactly ONE workflow run ever started (stuck in
`waiting`), and zero code tasks carrying any reference to a workflow — because there was
nowhere on a run to put one. The console could show a catalogue of workflows and a list of runs
with nothing on either screen connecting them.

This is the first half of the join: resolve the governing workflow when the run is created and
write it down. It deliberately does NOT change how a run executes — the pipeline's phase
sequence is still its own — because a link that silently altered execution would be a much
larger change wearing a small one's clothes.
"""

from __future__ import annotations

import inspect

from boss_engine.services import code_task_service, workflows


def test_it_resolves_against_the_selector_the_product_actually_writes():
    """THE MISTAKE THIS CORRECTS. `POST /v1/delivery-workflows/apply` — the "use this workflow
    for this kind of work" action in the Workflows area — creates mappings with
    selector_kind="work_class". The first version of this resolver invented "code_task", which
    nothing writes, so applying a workflow in the console would have had no effect on what a
    project resolved to and the two halves of one feature could never have met."""
    assert workflows.CODE_TASK_SELECTOR_KIND == "work_class"


def test_a_specific_work_class_beats_the_catalogue_wildcard():
    """`any` is the catalogue's own wildcard. Tried after `project.new`, so a workflow applied
    specifically to new projects wins over one applied to everything."""
    assert workflows.CODE_TASK_WORK_CLASSES == ("project.new", "any")


def test_it_reuses_the_existing_mapping_resolver():
    """Scope precedence, priority, enabled, published-version and ancestry checks all already
    live in `resolve_mapping`. A second implementation would drift from the first and the two
    would disagree about which workflow applies."""
    src = inspect.getsource(workflows.workflow_for_code_task)
    assert "resolve_mapping(" in src


def test_it_tries_each_work_class_in_order():
    src = inspect.getsource(workflows.workflow_for_code_task)
    assert "for work_class in CODE_TASK_WORK_CLASSES" in src
    assert "break" in src


def test_an_unmapped_project_is_not_an_error():
    """Today EVERY project is unmapped. If that raised, nothing could start a code task."""
    src = inspect.getsource(workflows.workflow_for_code_task)
    assert "return None" in src
    assert "except Exception" in src


def test_resolution_can_never_stop_a_task_from_starting():
    """The link is a fact about the run, not a precondition for it."""
    src = inspect.getsource(code_task_service.start_code_task)
    block = src[src.index("from .workflows import"):]
    assert "except Exception" in block[:1400]


def test_the_run_records_the_name_not_just_an_id():
    """The console shows this on the project page. An id alone would need a second lookup on a
    screen that already makes too many."""
    src = inspect.getsource(workflows.workflow_for_code_task)
    for field in ('"definition_id"', '"version_id"', '"mapping_id"', '"name"', '"version"'):
        assert field in src, field


def test_an_explicit_choice_outranks_a_resolved_one():
    """The picker on the New project form names a version. The person starting the work saying
    which path it should follow outranks any mapping resolved on their behalf."""
    src = inspect.getsource(code_task_service.start_code_task)
    block = src[src.index("from .workflows import"):]
    assert 'run_finalize.pop("workflow_version_id", None)' in block
    assert block.index("workflow_by_version(") < block.index("workflow_for_code_task(")


def test_an_unknown_version_falls_back_rather_than_recording_a_lie():
    """If the id names nothing in this project, resolution continues — recording a 'chosen'
    workflow that was never honoured would be worse than recording none."""
    src = inspect.getsource(code_task_service.start_code_task)
    block = src[src.index("from .workflows import"):]
    assert "if not governing:" in block


def test_a_version_from_another_project_cannot_be_borrowed():
    """The id arrives in a request payload. Scoping the lookup to the project is what stops one
    project naming another's workflow."""
    src = inspect.getsource(workflows.workflow_by_version)
    assert "d.project_id = :project_id" in src


def test_it_is_stamped_at_creation_not_inferred_later():
    """A mapping can change, be disabled, or point at a new version. Resolved at creation, the
    run keeps the workflow that actually governed it; inferred at read time it would silently
    rewrite its own history."""
    src = inspect.getsource(code_task_service.start_code_task)
    assert src.index("workflow_for_code_task") < src.index("INSERT INTO code_task_runs")


def test_it_does_not_claim_to_drive_execution():
    """The pipeline still runs its own hard-coded sequence. The docstring has to say so, because
    a reader who assumes otherwise will look for step dispatch that does not exist."""
    doc = workflows.workflow_for_code_task.__doc__ or ""
    assert "does NOT change how a run executes" in doc


# ── The default, when nobody has chosen ────────────────────────────────────────

def test_an_unmapped_project_falls_back_to_the_standard_workflow():
    """Every project is unmapped until somebody maps it, so without a fallback the answer to
    "which workflow governs my project" would be "none" for every project that has ever
    existed — technically true and useless."""
    src = inspect.getsource(workflows.workflow_for_code_task)
    assert "_default_workflow(connection, project_id)" in src


def test_the_default_is_the_catalogue_standard():
    assert workflows.DEFAULT_WORKFLOW_KEY == "standard_product_delivery"


def test_the_default_only_picks_a_published_active_version():
    """A draft or retired workflow governing a run would be a link to something that cannot be
    executed or is no longer meant to be."""
    src = inspect.getsource(workflows._default_workflow)
    assert "d.status = 'active'" in src
    assert "v.status = 'published'" in src
    assert "ORDER BY v.version DESC" in src


def test_a_default_is_distinguishable_from_a_choice():
    """'Deliberately put on this workflow' and 'got the standard one because nothing was
    chosen' are different facts, and only one of them is a decision. The console shows which."""
    assert '"source": "default"' in inspect.getsource(workflows._default_workflow)
    assert '"source": "mapping"' in inspect.getsource(workflows.workflow_for_code_task)


def test_the_fallback_does_not_invent_a_mapping():
    """Writing a mapping row on the customer's behalf would make an inferred fallback
    indistinguishable from a choice they made, and the next person to read the mappings table
    could not tell which was which."""
    src = inspect.getsource(workflows._default_workflow)
    assert "INSERT" not in src.upper()
    assert '"mapping_id": None' in src


def test_a_project_without_the_catalogue_gets_nothing_rather_than_a_guess():
    src = inspect.getsource(workflows._default_workflow)
    assert "if not row:" in src and "return None" in src


# ── The outer run a project actually gets ──────────────────────────────────────

def test_start_project_run_uses_the_workflow_the_project_resolves_to():
    """Resolution and starting are the same decision. If they disagreed, a project would show
    one workflow on its page and run a different one."""
    src = inspect.getsource(workflows.start_project_run)
    assert "workflow_by_version(" in src, "an explicit choice is ignored"
    assert "workflow_for_code_task(" in src, "the project's mapping is ignored"
    assert src.index("workflow_by_version(") < src.index("workflow_for_code_task(")


def test_the_run_is_started_with_the_directive_not_the_title():
    """The catalogue's first steps read `directive`: research seeds from it and the panel
    debates it. A panel handed a title has a label to discuss, not a brief."""
    src = inspect.getsource(workflows.start_project_run)
    assert '"directive": directive' in src
    assert '"title": title' in src


def test_starting_the_same_project_twice_does_not_race_itself():
    """Two nine-step pipelines over one repository is worse than a missing run."""
    src = inspect.getsource(workflows.start_project_run)
    assert "dedupe_key=" in src


def test_a_project_without_a_workflow_still_starts():
    """A fleet that has not installed the catalogue must still be able to work. It gets the
    direct path and no outer run, rather than an error."""
    src = inspect.getsource(workflows.start_project_run)
    assert "return None" in src

    src = inspect.getsource(code_task_service.start_code_task)
    block = src[src.index("start_project_run"):]
    assert "except Exception" in block, "a failed outer run would sink the project"


def test_the_code_task_records_the_run_that_governs_it():
    """Otherwise the two halves of one project cannot be shown together."""
    src = inspect.getsource(code_task_service.start_code_task)
    assert "'workflow_run_id'" in src


def test_a_simulated_project_starts_no_pipeline():
    """A no-spend fixture must not convene a panel and open a pull request. The simulated path
    returns before the outer run is started, which is what makes that true."""
    src = inspect.getsource(code_task_service.start_code_task)
    assert src.index("return run_id") < src.index("start_project_run")


def test_only_a_NEW_project_gets_an_outer_run():
    """A follow-up, a retry, a deferred-findings task or one slice of a decomposition is work
    INSIDE a project that already has a run. Giving each its own nine-step pipeline would
    convene a panel per retry and ask for an approval per slice."""
    src = inspect.getsource(code_task_service.start_code_task)
    for marker in ("is_followup", "parent_run_id", "deferred_from_run", "qa_benchmark"):
        assert marker in src, marker
    # Slice 1 of a decomposition IS the project; 2..N are the rest of the same one.
    assert 'decomposition_index' in src
    assert "_is_new_project" in src
    assert "if _is_new_project:" in src


def test_a_project_born_from_a_panel_adopts_that_panel():
    """Otherwise the workflow convenes a SECOND panel and asks for a SECOND approval for work
    whose panel and approval just happened."""
    src = inspect.getsource(code_task_service.start_code_task)
    assert '"adopt_meeting_run_id"' in src
    assert '"adopt_decision_id"' in src
    assert '"adopt_code_task_run_id"' in src


def test_a_scaffolded_web_app_is_still_a_new_project():
    """`root_run_id` names a branch lineage, not a parent task: a web-app build mints one for
    the scaffold (`root = make_id("crun")`) BEFORE any task exists. Counting it as a
    continuation skipped the outer run for the panel path — which is how most projects start."""
    src = inspect.getsource(code_task_service.start_code_task)
    guard = src[src.index("_continuation = ("):src.index("_is_new_project = ")]
    assert "root_run_id" not in guard


def test_the_run_follows_the_workflow_the_task_records():
    """The workflow was resolved once, honouring the form's picker and then any mapping. Letting
    start_project_run resolve it again lets the two disagree — the project page naming one
    workflow and the run that governs it following another."""
    src = inspect.getsource(code_task_service.start_code_task)
    call = src[src.index("started = start_project_run("):]
    assert 'workflow_version_id=(run_finalize.get("workflow") or {}).get("version_id")' in call


def test_choosing_a_workflow_makes_a_follow_up_its_own_piece_of_work():
    """"Add to this project" lets the operator pick the path a change should follow. A picker
    whose choice is then ignored is worse than no picker — so an explicit choice starts an
    outer run even for work that would otherwise be a continuation."""
    src = inspect.getsource(code_task_service.start_code_task)
    assert "_chose_a_workflow" in src
    assert "_is_new_project = _chose_a_workflow or (" in src


def test_choosing_nothing_leaves_a_follow_up_alone():
    """The default has to stay: a retry or a small follow-up is work inside the project's
    existing run, not a fresh nine-step pipeline per iteration."""
    src = inspect.getsource(code_task_service.start_code_task)
    guard = src[src.index("_chose_a_workflow ="):src.index("try:", src.index("_chose_a_workflow ="))]
    for marker in ("is_followup", "parent_run_id", "deferred_from_run", "qa_benchmark"):
        assert marker in guard, marker
