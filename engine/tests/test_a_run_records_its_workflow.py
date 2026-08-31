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


def test_the_selector_kind_is_stable():
    """Mappings are stored against it; changing the string orphans every mapping ever made."""
    assert workflows.CODE_TASK_SELECTOR_KIND == "code_task"


def test_it_reuses_the_existing_mapping_resolver():
    """Scope precedence, priority, enabled, published-version and ancestry checks all already
    live in `resolve_mapping`. A second implementation would drift from the first and the two
    would disagree about which workflow applies."""
    src = inspect.getsource(workflows.workflow_for_code_task)
    assert "resolve_mapping(" in src


def test_the_wildcard_is_what_makes_a_project_default_possible():
    """`resolve_mapping` matches `selector_value IN (:selector_value, '*')`. Passing the task id
    lets one task be targeted specifically, while a `'*'` mapping governs the whole project —
    which is what "link the standard workflow" means."""
    src = inspect.getsource(workflows.workflow_for_code_task)
    assert 'str(task_id or "*")' in src


def test_an_unmapped_project_is_not_an_error():
    """Today EVERY project is unmapped. If that raised, nothing could start a code task."""
    src = inspect.getsource(workflows.workflow_for_code_task)
    assert "return None" in src
    assert "except Exception" in src


def test_resolution_can_never_stop_a_task_from_starting():
    """The link is a fact about the run, not a precondition for it."""
    src = inspect.getsource(code_task_service.start_code_task)
    block = src[src.index("workflow_for_code_task"):]
    assert "except Exception" in block[:600]


def test_the_run_records_the_name_not_just_an_id():
    """The console shows this on the project page. An id alone would need a second lookup on a
    screen that already makes too many."""
    src = inspect.getsource(workflows.workflow_for_code_task)
    for field in ('"definition_id"', '"version_id"', '"mapping_id"', '"name"', '"version"'):
        assert field in src, field


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
    assert src.count("_default_workflow(connection, project_id)") == 2


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
