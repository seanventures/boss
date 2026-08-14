"""Factory quality loop — evidence collection, gap detection, one improve pass."""

from boss_engine.services.factory_quality_loop import (
    MAX_QUALITY_PASSES,
    attach_evidence_to_run,
    collect_workspace_evidence,
    measured_quality_gaps,
    quality_improve_prompt,
    score_run_with_workspace,
    should_run_quality_pass,
)


def _files(**extra: str) -> dict[str, str]:
    base = {
        "package.json": '{"scripts":{"verify":"npm test","test:api":"vitest"}}',
        "src/App.tsx": "export function App() { return <main>dashboard</main> }",
    }
    base.update(extra)
    return base


def test_collects_thin_ui_evidence_from_a_minimal_app():
    evidence = collect_workspace_evidence(_files())
    assert "ok" in evidence["ui_evidence"]["states_covered"]
    assert "empty" not in evidence["ui_evidence"]["states_covered"]
    assert evidence["ux_evidence"]["has_e2e"] is False
    assert should_run_quality_pass(evidence, tests_passed=True) is True


def test_rich_workspace_does_not_need_a_quality_pass():
    evidence = collect_workspace_evidence(
        _files(
            **{
                "package.json": '{"scripts":{"verify":"x","test:e2e":"playwright","a11y":"axe"}}',
                "src/App.tsx": (
                    "export function App() { return <main aria-label='app'>"
                    "<p>loading</p><p>empty state</p><p>try again</p><p>dashboard</p>"
                    "</main> }"
                ),
                "e2e/create.spec.ts": "test('create issue', () => {})",
            }
        )
    )
    assert set(evidence["ui_evidence"]["states_covered"]) >= {"loading", "empty", "error", "ok"}
    assert evidence["ux_evidence"]["has_e2e"] is True
    assert evidence["ui_evidence"]["has_a11y_script"] is True
    assert should_run_quality_pass(evidence, tests_passed=True) is False


def test_quality_pass_is_blocked_without_green_tests_or_after_budget():
    evidence = collect_workspace_evidence(_files())
    assert should_run_quality_pass(evidence, tests_passed=False) is False
    assert should_run_quality_pass(
        evidence, tests_passed=True, quality_passes_used=MAX_QUALITY_PASSES
    ) is False


def test_improve_prompt_forbids_editing_tests_and_inventing_scores():
    evidence = collect_workspace_evidence(_files())
    prompt = quality_improve_prompt(
        evidence, title="BugBox", gaps=[{"dimension": "ui"}, {"dimension": "ux"}]
    )
    assert "Do NOT edit" in prompt
    assert "Do NOT invent" in prompt
    assert "loading" in prompt
    assert "ui" in prompt and "ux" in prompt


def test_scorecard_receives_workspace_evidence_without_inventing_rubric():
    run = {
        "id": "crun_q",
        "status": "completed",
        "state": {
            "design": {"plan": "Build it."},
            "tests": [{"command": "npm run verify", "exit_code": 0}],
            "tests_passed": True,
        },
        "finalize_ctx": {"qa_benchmark": {"id": "bugbox", "name": "BugBox",
                                          "acceptance_scenarios": ["Create an issue"]}},
    }
    evidence = collect_workspace_evidence(
        _files(
            **{
                "src/App.tsx": (
                    "<main aria-label='app'><p>loading</p><p>empty state</p>"
                    "<p>try again</p><p>dashboard</p></main>"
                ),
                "tests/create.test.ts": "it('create issue', () => {})",
            }
        )
    )
    attached = attach_evidence_to_run(run, evidence)
    assert attached["state"]["ui_evidence"]["states_covered"]
    assert attached["state"]["ux_evidence"]["rubric"] == {}
    card = score_run_with_workspace(run, evidence)
    dims = {item["id"]: item for item in card["dimensions"]}
    assert dims["ui"]["status"] == "scored"
    assert dims["ux"]["status"] == "not_scored"  # no rubric, jtbd.passed is None
    gaps = measured_quality_gaps(card)
    assert any(item["dimension"] == "ux" for item in gaps)


def _governed_pipeline_source() -> str:
    """Executable source of the governed runner, comments stripped.

    Helpers in this module are worthless if the pipeline never calls them.
    The contract is the live source, not a comment that says the loop exists.
    """
    import inspect

    from boss_engine.services import governed_code_task_service as gcts

    source = inspect.getsource(gcts)
    return "\n".join(
        ln for ln in source.splitlines() if not ln.lstrip().startswith("#")
    )


def test_governed_pipeline_calls_the_quality_loop_hooks():
    src = _governed_pipeline_source()
    assert "should_run_quality_pass(" in src
    assert "quality_improve_prompt(" in src
    assert '"code_task.quality_pass"' in src


def test_governed_pipeline_restores_impl_when_quality_pass_fails():
    src = _governed_pipeline_source()
    assert "if not q_ok and snapshot:" in src
    assert ".write_text(_qc" in src
    assert "reverted = True" in src


def test_governed_pipeline_writes_ui_ux_evidence_into_run_state():
    src = _governed_pipeline_source()
    assert '"ui_evidence": (workspace_evidence or {}).get("ui_evidence")' in src
    assert '"ux_evidence": (workspace_evidence or {}).get("ux_evidence")' in src
