"""The Document step produces a document.

    succeeded · 0s · 'Document' is recorded from the work it accompanies.

That is what Research-Led Discovery's final step reported, on a run that finished 4-of-4 green.
The operator asked why it had no output. It had none because `document` declared
`produces=("document_id",)` and `tool=None`, so it fell through the runner's honest-no-op branch
— and there was nothing accompanying it to be recorded from.

For that workflow the gap is not cosmetic. Its own description is "Ends in a decision and a
document, not in code": the document IS the deliverable, and the run produced no document.

These tests pin the writer's two hard rules:

  * it invents nothing — every section comes from a step that actually ran;
  * it says what is missing, because a record whose gaps are invisible reads as complete.

Run directly (pytest collection wipes the local BOSS database):
    PYTHONPATH=engine .venv/bin/python tests/test_document_step_writes_something.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, "engine")

from boss_engine.services import workflow_capability_runner as runner  # noqa: E402
from boss_engine.services.workflow_capabilities import get as get_capability  # noqa: E402

_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'' if ok else f'  <- {detail}'}")
    if not ok:
        _failures.append(name)


class _FakeResult:
    def __init__(self, row): self._row = row
    def mappings(self): return self
    def first(self): return self._row


class _FakeConnection:
    """Answers the one query the writer makes: the decision it was told about."""
    def __init__(self, decision=None): self._decision = decision
    def execute(self, *_args, **_kwargs): return _FakeResult(self._decision)


def _capture(monkey: dict):
    """Stand in for create_artifact, keeping what it was handed."""
    def _create_artifact(_conn, _settings, project_id, task_id, artifact_type, title, content):
        monkey.update({"project_id": project_id, "task_id": task_id,
                       "artifact_type": artifact_type, "title": title, "content": content})
        return "art_test1234"
    return _create_artifact


def _run(context, inputs=None, decision=None):
    import boss_engine.services.core as core
    import boss_engine.config as config
    captured: dict = {}
    real_create, real_settings = core.create_artifact, config.get_settings
    core.create_artifact = _capture(captured)
    config.get_settings = lambda: None
    try:
        result = runner._write_the_record(
            _FakeConnection(decision), "proj_1", context, inputs or {})
    finally:
        core.create_artifact, config.get_settings = real_create, real_settings
    return result, captured


FULL = {
    "run_id": "wfr_1",
    "input": {"directive": "Build a changelog-to-email composer.", "title": "Changelog to email"},
    "steps": {
        "research": {"discovery_id": "disc_9", "dossier": "Three tools do this; none for teams."},
        "meeting": {"meeting_run_id": "mrun_5", "prd_markdown": "## Scope\nGroup PRs by theme.",
                    "task_id": "task_7"},
        "spec_review": {"decision_id": "dec_3"},
    },
}


def main() -> int:
    print("== it writes what the run produced ==")
    result, got = _run(FULL, {"title": "Changelog to email"},
                       decision={"question": "Approve the spec", "status": "approved",
                                 "approved_by": "param", "resolution_note": None})
    md = got["content"]["markdown"]
    check("a document id comes back", result.get("document_id") == "art_test1234", str(result))
    check("status is published", result.get("status") == "published", str(result.get("status")))
    check("stored as a change record", got["artifact_type"] == "change_record",
          got["artifact_type"])
    check("hung off the panel's task", got["task_id"] == "task_7", str(got["task_id"]))
    check("the directive is in it", "changelog-to-email composer" in md)
    check("the research is in it", "none for teams" in md)
    check("the spec is in it", "Group PRs by theme" in md)
    check("the decision is named with who answered it", "param" in md and "approved" in md)
    check("and it does not claim a build happened", "## What was built" not in md)

    print("== it says what is NOT in it ==")
    # A record assembled from whatever a run happened to contain reads as complete unless it
    # says otherwise. Research-Led Discovery has no build; the record must admit that.
    check("missing sections are listed", "Not part of this run" in md and "a build" in md, md[-300:])

    print("== it invents nothing ==")
    bare, got_bare = _run({"run_id": "wfr_2", "input": {"directive": "Do a thing."}, "steps": {}})
    bare_md = got_bare["content"]["markdown"]
    for absent in ("What the research found", "The spec the panel produced", "The decision"):
        check(f"no '{absent}' section without one", absent not in bare_md)
    check("it still records the directive", "Do a thing." in bare_md)
    # The note names what it found, and a directive IS something — claiming "nothing to put in
    # it" over a recorded directive would be the same overstatement in the other direction.
    check("and the note names what it did find",
          (bare.get("note") or "").startswith("Change record written from directive"),
          str(bare.get("note")))
    empty, _ = _run({"run_id": "wfr_4", "input": {}, "steps": {}})
    check("a run with genuinely nothing says so",
          "nothing to put in it" in (empty.get("note") or ""), str(empty.get("note")))

    print("== a run with no title still gets a document ==")
    # `title` was a REQUIRED input, which made the step that writes the record the one step a
    # run could be started without satisfying — _resolve_inputs raises before the writer runs.
    check("title is no longer required", "title" not in get_capability("document").required_inputs,
          str(get_capability("document").required_inputs))
    check("it is optional instead", "title" in get_capability("document").optional_inputs)
    untitled, got_untitled = _run(
        {"run_id": "wfr_3", "input": {"directive": "Ship the vendor watcher."}, "steps": {}})
    check("it names itself from the directive",
          got_untitled["title"].startswith("Ship the vendor watcher"), got_untitled["title"])

    print("== a decision that cannot be read does not sink the step ==")
    unreadable, got_unreadable = _run(
        {**FULL, "steps": {"spec_review": {"decision_id": "dec_gone"}}}, decision=None)
    check("the record is still written", unreadable.get("document_id") == "art_test1234")
    check("and it names the decision by id", "dec_gone" in got_unreadable["content"]["markdown"])

    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("the Document step documents")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
