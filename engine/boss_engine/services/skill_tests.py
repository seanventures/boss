"""Saved test cases for a skill, runs pinned to a version, and the publish gate (BOSSAPP-844).

A skill changes by versions (skill_versions). Without saved cases, "is v4 better than v3" is a
feeling. Here a case is a named input kept with the skill; a RUN executes one case against one
version through eval_harness (the same execution road and eval_runs record as the eval suite);
and a GRADE is a typed answer — exactly ``pass`` or ``fail``, with notes — given by a person or
by a typed decision (typed_decisions.Question with those two options).

THE GATE IS SOFT ON PURPOSE. Publishing a version to the agents that hold the skill
(skill_versions.publish) is refused only when the skill HAS cases and the latest graded run of
that version failed one. A skill nobody wrote cases for publishes as before; a version whose
runs are not graded yet publishes too (the gate reads results, it does not demand them).
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id

GRADES = ("pass", "fail")


class SkillTestError(ValueError):
    status = 422


def _loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return fallback
    return value if value is not None else fallback


def _skill(connection: Connection, project_id: str, skill_key: str) -> dict[str, Any]:
    from .skill_definitions import get_skill

    skill = get_skill(connection, project_id, skill_key)
    if skill is None:
        raise SkillTestError(f"no skill {skill_key!r} in this project.")
    return skill


def _case(row: Any) -> dict[str, Any]:
    return {"id": row["id"], "name": row["name"], "input": _loads(row["input"], {}),
            "created_by": row["created_by"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None}


def list_cases(connection: Connection, project_id: str, skill_key: str) -> list[dict[str, Any]]:
    skill = _skill(connection, project_id, skill_key)
    rows = connection.execute(
        text("SELECT * FROM skill_test_cases WHERE skill_id = :s ORDER BY name"),
        {"s": skill["id"]}).mappings().all()
    return [_case(r) for r in rows]


def save_case(connection: Connection, *, project_id: str, actor: str, skill_key: str,
              name: str, case_input: Any) -> dict[str, Any]:
    skill = _skill(connection, project_id, skill_key)
    label = str(name or "").strip()[:120]
    if not label:
        raise SkillTestError("a test case needs a name.")
    if case_input is None:
        case_input = {}
    if not isinstance(case_input, dict):
        raise SkillTestError("a test case's input is an object of the skill's inputs.")
    row = connection.execute(
        text("INSERT INTO skill_test_cases (id, project_id, skill_id, name, input, created_by) "
             "VALUES (:id, :p, :s, :n, CAST(:i AS JSONB), :by) "
             "ON CONFLICT (skill_id, name) DO UPDATE SET input = EXCLUDED.input "
             "RETURNING *"),
        {"id": make_id("stc"), "p": project_id, "s": skill["id"], "n": label,
         "i": json.dumps(case_input, default=str), "by": actor}).mappings().one()
    emit_event(connection, project_id, "skill.test_case_saved", actor, "skill_test_case",
               row["id"], {"skill_key": skill_key, "name": label})
    return _case(row)


def delete_case(connection: Connection, *, project_id: str, actor: str, skill_key: str,
                case_id: str) -> dict[str, Any]:
    skill = _skill(connection, project_id, skill_key)
    gone = connection.execute(
        text("DELETE FROM skill_test_cases WHERE id = :i AND skill_id = :s RETURNING id"),
        {"i": case_id, "s": skill["id"]}).first()
    if gone is None:
        raise SkillTestError(f"no test case {case_id!r} on {skill_key}.")
    emit_event(connection, project_id, "skill.test_case_deleted", actor, "skill_test_case",
               case_id, {"skill_key": skill_key})
    return {"deleted": case_id}


def _run(row: Any) -> dict[str, Any]:
    return {"id": row["id"], "case_id": row["case_id"],
            "skill_version_id": row["skill_version_id"], "eval_run_id": row["eval_run_id"],
            "status": row["status"], "output": _loads(row["output"], {}), "grade": row["grade"],
            "notes": row["notes"], "graded_by": row["graded_by"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None}


def run_cases(connection: Connection, settings: Any, *, project_id: str, actor: str,
              skill_key: str, version: Any = None) -> dict[str, Any]:
    """Run every saved case against one version (default: the current one)."""
    from . import eval_harness, skill_versions

    skill = _skill(connection, project_id, skill_key)
    chosen = (skill_versions.get_version(connection, project_id, skill_key, version)
              if version not in (None, "") else
              skill_versions.current(connection, project_id, skill_key))
    if chosen is None:
        raise SkillTestError(f"{skill_key} has no saved version yet — save it once first.")
    cases = list_cases(connection, project_id, skill_key)
    if not cases:
        raise SkillTestError(f"{skill_key} has no test cases yet — add one first.")
    done = eval_harness.run_skill_cases(connection, settings, project_id, skill, chosen, cases,
                                        actor)
    runs = []
    for outcome in done["outcomes"]:
        row = connection.execute(
            text("INSERT INTO skill_test_runs (id, project_id, case_id, skill_version_id, "
                 "eval_run_id, status, output, created_at) VALUES (:id, :p, :c, :v, :e, :st, "
                 "CAST(:o AS JSONB), clock_timestamp()) RETURNING *"),
            {"id": make_id("str"), "p": project_id, "c": outcome["case_id"], "v": chosen["id"],
             "e": done["eval_run"]["id"], "st": outcome["status"],
             "o": json.dumps(outcome["output"], default=str)[:200_000]}).mappings().one()
        runs.append(_run(row))
    emit_event(connection, project_id, "skill.tests_run", actor, "skill_definition", skill["id"],
               {"skill_key": skill_key, "version_no": chosen["version_no"], "cases": len(runs)})
    return {"skill_key": skill_key, "version_no": chosen["version_no"],
            "version_id": chosen["id"], "eval_run_id": done["eval_run"]["id"], "runs": runs}


def grade_run(connection: Connection, *, project_id: str, actor: str, run_id: str, grade: str,
              notes: str = "") -> dict[str, Any]:
    """The typed answer for one run: exactly pass or fail, with notes."""
    answer = str(grade or "").strip().lower()
    if answer not in GRADES:
        raise SkillTestError("a grade is pass or fail.")
    row = connection.execute(
        text("UPDATE skill_test_runs SET grade = :g, notes = :n, graded_by = :a, "
             "graded_at = now() WHERE id = :i AND project_id = :p RETURNING *"),
        {"g": answer, "n": str(notes or "")[:2000], "a": actor, "i": run_id, "p": project_id},
    ).mappings().first()
    if row is None:
        raise SkillTestError(f"no test run {run_id!r}.")
    emit_event(connection, project_id, "skill.test_graded", actor, "skill_test_run", run_id,
               {"grade": answer})
    return _run(row)


GRADE_QUESTION_KEY = "grade"


def grade_question(skill_label: str, case_name: str):
    """The typed decision a grader answers: fixed options, nothing else accepted."""
    from .typed_decisions import Question

    return Question(
        key=GRADE_QUESTION_KEY,
        instructions=(f"Does the output of the skill '{skill_label}' for the test case "
                      f"'{case_name}' do what the case expects?"),
        options={"pass": "the output is correct for this case",
                 "fail": "the output is wrong, incomplete or unsafe for this case"})


def grade_with_decider(connection: Connection, *, project_id: str, run_id: str,
                       decider: Any) -> dict[str, Any]:
    """Grade one run by a typed decision (typed_decisions.Decider). graded_by names the backend.

    The caller supplies the decider, so the spend governance of the deciding model stays with
    the code that owns it; an answer outside pass|fail is refused, never guessed."""
    row = connection.execute(
        text("SELECT r.*, c.name AS case_name, c.input AS case_input, d.label "
             "FROM skill_test_runs r JOIN skill_test_cases c ON c.id = r.case_id "
             "JOIN skill_definitions d ON d.id = c.skill_id "
             "WHERE r.id = :i AND r.project_id = :p"),
        {"i": run_id, "p": project_id}).mappings().first()
    if row is None:
        raise SkillTestError(f"no test run {run_id!r}.")
    question = grade_question(row["label"], row["case_name"])
    context = json.dumps({"input": _loads(row["case_input"], {}),
                          "output": _loads(row["output"], {})}, default=str)[:20_000]
    batch = decider.decide(context, [question])
    decision = batch.decisions.get(GRADE_QUESTION_KEY)
    if decision is None or decision.answer not in GRADES:
        raise SkillTestError("the grader gave no pass/fail answer.")
    return grade_run(connection, project_id=project_id,
                     actor=f"typed_decision:{getattr(batch, 'backend', 'decider')}",
                     run_id=run_id, grade=decision.answer, notes=decision.reason or "")


def runs_for(connection: Connection, project_id: str, skill_key: str,
             version_id: Optional[str] = None) -> list[dict[str, Any]]:
    skill = _skill(connection, project_id, skill_key)
    rows = connection.execute(
        text("SELECT r.*, v.version_no, c.name AS case_name FROM skill_test_runs r "
             "JOIN skill_test_cases c ON c.id = r.case_id "
             "JOIN skill_versions v ON v.id = r.skill_version_id "
             "WHERE c.skill_id = :s AND (CAST(:v AS TEXT) IS NULL OR r.skill_version_id = :v) "
             "ORDER BY r.created_at DESC LIMIT 500"),
        {"s": skill["id"], "v": version_id}).mappings().all()
    out = []
    for row in rows:
        item = _run(row)
        item["version_no"] = int(row["version_no"])
        item["case_name"] = row["case_name"]
        out.append(item)
    return out


def latest_by_case(connection: Connection, project_id: str, skill_key: str,
                   version_id: str) -> dict[str, dict[str, Any]]:
    """{case_id: the newest run of that case against this version}."""
    latest: dict[str, dict[str, Any]] = {}
    for run in runs_for(connection, project_id, skill_key, version_id):
        latest.setdefault(run["case_id"], run)
    return latest


def compare(connection: Connection, project_id: str, skill_key: str,
            version: Any = None) -> dict[str, Any]:
    """Each case's latest output on `version` beside its latest on the version before it."""
    from . import skill_versions

    chosen = (skill_versions.get_version(connection, project_id, skill_key, version)
              if version not in (None, "") else
              skill_versions.current(connection, project_id, skill_key))
    if chosen is None:
        return {"skill_key": skill_key, "version_no": None, "previous_no": None, "cases": []}
    previous = None
    if chosen["version_no"] > 1:
        try:
            previous = skill_versions.get_version(connection, project_id, skill_key,
                                                  chosen["version_no"] - 1)
        except skill_versions.VersionNotFound:
            previous = None
    now = latest_by_case(connection, project_id, skill_key, chosen["id"])
    before = latest_by_case(connection, project_id, skill_key,
                            previous["id"]) if previous else {}
    cases = [{**case, "current": now.get(case["id"]), "previous": before.get(case["id"])}
             for case in list_cases(connection, project_id, skill_key)]
    return {"skill_key": skill_key, "version_no": chosen["version_no"],
            "previous_no": previous["version_no"] if previous else None, "cases": cases}


def publish_gate(connection: Connection, project_id: str, skill_key: str,
                 version_id: str) -> dict[str, Any]:
    """{allowed, reason}: refused only when cases exist and this version's latest graded run of
    one of them failed."""
    cases = list_cases(connection, project_id, skill_key)
    if not cases:
        return {"allowed": True, "reason": "no test cases — nothing to hold it back"}
    latest = latest_by_case(connection, project_id, skill_key, version_id)
    graded = {cid: r for cid, r in latest.items() if r["grade"]}
    failed = [r for r in graded.values() if r["grade"] == "fail"]
    if failed:
        names = {c["id"]: c["name"] for c in cases}
        listed = ", ".join(sorted(names.get(r["case_id"], r["case_id"]) for r in failed))
        return {"allowed": False,
                "reason": f"this version failed its latest test run ({listed}). Fix it, or "
                          "re-run and grade the cases, before giving it to agents."}
    if not graded:
        return {"allowed": True, "reason": "not graded yet"}
    return {"allowed": True, "reason": "its latest graded runs passed"}
