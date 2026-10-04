"""An approval answers ONE run's gate, not every run of the same workflow.

The operator approved a single release and watched five start. All five deploy steps — five
different apps, five different runs — carried the same decision id and were leased in the same
second.

create_blocking_decision deduplicates on (project_id, question, task_id) while pending. That is
right for a repeated alert: a repeat is evidence the same cause is ongoing, and stacking twenty
copies of one alert buries the decisions somebody actually has to make. It is catastrophic for
a release gate, where each run is a distinct thing to approve — and the gate's question was
"Ship it: standard_product_delivery", byte-identical for every run of that workflow.

The bug was not in the deduplication. It was in asking a question that could not tell two
releases apart.
"""

from __future__ import annotations

import inspect

from boss_engine.services import workflow_capability_runner as runner


def _gate_source() -> str:
    return inspect.getsource(runner._open_gate)


class TestTheQuestionIdentifiesTheRun:
    def test_the_question_is_kept_distinct_per_run(self):
        """In words now ("Email the newsletter — Weekly newsletter"), without a raw run id —
        so a second open gate with the same words is told apart by `_unique_question`, which
        compares the run each pending decision belongs to."""
        source = _gate_source()
        assert "_unique_question(" in source and 'run["id"]' in source, (
            "two runs of one workflow produce the same question and collide onto one decision"
        )
        unique = inspect.getsource(runner._unique_question)
        assert "workflow_run_id" in unique and "status='pending'" in unique

    def test_the_workflow_key_alone_is_no_longer_the_question(self):
        source = _gate_source()
        assert 'f"{subject[\'label\']}: {run.get(\'workflow_key\') or \'workflow\'}"' not in source

    def test_the_question_names_the_work(self):
        """An approver is asked about a specific piece of work, not a workflow in the abstract."""
        source = _gate_source()
        assert "title" in source


class TestTheDeduplicationItselfIsUnchanged:
    """The fix must not disable a guard that exists for a good reason."""

    def test_repeated_alerts_still_collapse(self):
        from boss_engine.services import core

        source = inspect.getsource(core._create_blocking_decision_postgres)
        assert "AND question = :question" in source
        assert "occurrences" in source, (
            "a repeat still extends the deadline and counts the occurrence, rather than "
            "stacking copies an operator cannot act on"
        )


def test_two_runs_of_one_workflow_ask_two_different_questions():
    """The property, stated directly, using the same shape the gate builds."""
    def question(run):
        subject_name = str((run.get("input") or {}).get("title")
                           or run.get("workflow_key") or "workflow").strip()[:80]
        return f"Ship it: {subject_name} · run {run['id'][-8:]}"

    a = {"id": "wfr_84f9cef08c204c8c8f", "workflow_key": "standard_product_delivery",
         "input": {"title": "Path Verification"}}
    b = {"id": "wfr_ae113617ec13405e84", "workflow_key": "standard_product_delivery",
         "input": {"title": "Incident-Timeline Builder"}}
    assert question(a) != question(b)

    # Even two runs with the SAME title — a re-run, which this console creates deliberately —
    # must not share an approval.
    c = {"id": "wfr_111111111111111111", "workflow_key": "k", "input": {"title": "Same"}}
    d = {"id": "wfr_222222222222222222", "workflow_key": "k", "input": {"title": "Same"}}
    assert question(c) != question(d)
