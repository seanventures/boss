"""The panel knows which workflow it is in, and what that workflow must end with.

"Does the meeting panel work on the goal based on the workflow type, or does the meeting panel
 not know about the objective?"

It did not know. The `meeting` step's binding passed `{"directive": <the run's directive>}` and,
in some workflows, the research dossier. The `request_meeting` tool accepted `directive`, `url`,
`task_id`, `research` — and nothing else. `convene_prd_meeting_core` then set `meeting_type` to
the default `prd_panel` in every case. All twelve catalogue workflows convene a panel; all
twelve convened the SAME panel.

The clearest cost is Research-Led Discovery, whose own description is "Find out whether it is
worth building at all. Ends in a decision and a document, not in code." Its panel was asked for
an implementation plan for code that was never going to be written.

Run directly (pytest collection wipes the local BOSS database):
    PYTHONPATH=engine .venv/bin/python tests/test_panel_knows_its_workflow.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, "engine")

from boss_engine.services.workflow_catalogue import CATALOGUE  # noqa: E402

_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'' if ok else f'  <- {detail}'}")
    if not ok:
        _failures.append(name)


def main() -> int:
    print("== every workflow says what it is for ==")
    for entry in CATALOGUE:
        objective = (entry.get("objective") or "").strip()
        check(f"{entry['key']} has an objective", bool(objective))
        # A one-liner that restates the name teaches the panel nothing. The objective has to
        # say what the RUN must end with.
        check(f"{entry['key']}'s objective is substantive", len(objective) > 80,
              f"{len(objective)} chars")
        check(f"{entry['key']}'s objective is not its description",
              objective != (entry.get("description") or "").strip())

    print("== objectives actually differ, workflow to workflow ==")
    # If they were boilerplate the panel would be no better informed than before.
    objectives = [(e.get("objective") or "").strip() for e in CATALOGUE]
    check("no two workflows share an objective", len(set(objectives)) == len(objectives))

    print("== the discovery panel is told not to write a build plan ==")
    discovery = next(e for e in CATALOGUE if e["key"] == "research_led_discovery")
    text = discovery["objective"].lower()
    check("it says the run ends in a decision and a record",
          "decision" in text and ("document" in text or "record" in text), text)
    check("and says it is not an implementation plan", "not an implementation plan" in text, text)

    print("== the objective reaches the panel ==")
    from boss_engine.services import workflow_capability_runner as runner
    src = open("engine/boss_engine/services/workflow_capability_runner.py").read()
    check("the context carries it", '"objective": str(spec.get("objective") or "")' in src)
    check("and the meeting step passes it on", 'if key == "meeting":' in src and
          '"objective": objective' in src)
    tools = open("engine/boss_engine/tools/builtin.py").read()
    check("request_meeting accepts it", '"objective": {' in tools)
    # It must go FIRST: a panel told afterwards that nothing will be built has already written
    # a build plan.
    i_obj = tools.find("--- WHAT THIS PANEL IS FOR ---")
    i_res = tools.find("--- RESEARCH ALREADY DONE FOR THIS ---")
    check("and it is folded in ahead of the ask", i_obj > -1 and i_obj < i_res,
          f"objective at {i_obj}, research at {i_res}")

    print("== the catalogue spec carries it, so a run can read it back ==")
    cat = open("engine/boss_engine/services/workflow_catalogue.py").read()
    check("seed writes it into the spec", '"objective": entry.get("objective") or ""' in cat)
    check("and the workflow's name with it", '"workflow_name": entry["name"]' in cat)

    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("every panel knows what it is for")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
