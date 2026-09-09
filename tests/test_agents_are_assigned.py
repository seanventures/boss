"""Who is on this piece of work, and what happens when they are not.

BOSS had no answer. `roles` says an agent exists in a project; a workflow step names an
`approver_role` and nothing else; `workflow_runs` and `workflow_step_runs` have no role column.
The only implicit assignment is `llm_telemetry.STAGE_ROLES`, which maps a pipeline stage to a
role for COST ATTRIBUTION at write time and is never consulted for scheduling or permission.

The operator asked for assignment to MEAN something — "Enforced" over "Descriptive only" — and
was right to: this console has shipped decoration before, the template tags that rendered as
labels and never filtered anything.

Enforcement has two failure modes and this pins both:

  * too lax — assignment displayed and ignored, which is the decoration it replaced;
  * too strict — an app with nobody on it refuses every gate, so the feature reads as breakage.
    Every app that predates the table would become unrunnable.

Run directly (pytest collection wipes the local BOSS database):
    PYTHONPATH=engine .venv/bin/python tests/test_agents_are_assigned.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, "engine")

from boss_engine.services import agent_assignment as aa  # noqa: E402

_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'' if ok else f'  <- {detail}'}")
    if not ok:
        _failures.append(name)


class FakeConn:
    """An in-memory project_agents table, honouring the unique key and the upsert rule."""

    def __init__(self, rows=()):
        self.rows = [dict(r) for r in rows]

    def execute(self, statement, params=None):
        raw = " ".join(str(statement).split()).lower()
        params = dict(params or {})
        rows: list[dict] = []
        if raw.startswith("select role_key from project_agents"):
            # project_team / the seeding lookup. Distinct from the one below: it selects only
            # the key, and routing it to the same branch would have hidden the fallback.
            rows = [{"role_key": r["role_key"]} for r in self.rows
                    if r["project_id"] == params["p"] and r["app_key"] == params["a"]]
            rows.sort(key=lambda r: r["role_key"])
        elif raw.startswith("select role_key, source"):
            rows = [r for r in self.rows
                    if r["project_id"] == params["p"] and r["app_key"] == params["a"]]
            rows.sort(key=lambda r: r["role_key"])
        elif raw.startswith("select app_key, app_name, source"):
            rows = [r for r in self.rows
                    if r["project_id"] == params["p"] and r["role_key"] == params["r"]]
        elif raw.startswith("insert into project_agents"):
            existing = next((r for r in self.rows
                             if r["project_id"] == params["p"]
                             and r["app_key"] == params["a"]
                             and r["role_key"] == params["r"]), None)
            if existing:
                # The upsert rule: an operator's choice is never demoted to a default.
                if existing["source"] != aa.OPERATOR:
                    existing["source"] = params["s"]
                existing["app_name"] = params["n"]
                rows = [{"inserted": False}]
            else:
                self.rows.append({"project_id": params["p"], "app_key": params["a"],
                                  "app_name": params["n"], "role_key": params["r"],
                                  "source": params["s"], "assigned_by": params["by"],
                                  "created_at": None})
                rows = [{"inserted": True}]
        elif raw.startswith("delete from project_agents"):
            before = len(self.rows)
            self.rows = [r for r in self.rows
                         if not (r["project_id"] == params["p"]
                                 and r["app_key"] == params["a"]
                                 and r["role_key"] == params["r"])]
            rows = [{"role_key": params["r"]}] if len(self.rows) < before else []
        captured = rows

        class _R:
            def mappings(self): return self
            def all(self): return [dict(r) for r in captured]
            def first(self): return dict(captured[0]) if captured else None
        return _R()


def main() -> int:
    print("== the defaults are the agents that would have run anyway ==")
    defaults = aa.default_roles()
    # Not a guess: STAGE_ROLES is what BOSS already attributes every model call to, so seeding
    # from it assigns the agents that were doing the work before assignment existed.
    for expected in ("software_engineer", "chief_architect", "qa", "adversarial_reviewer"):
        check(f"{expected} is a default", expected in defaults, str(defaults))
    check("a workflow's approver is added too",
          "release_manager" in aa.default_roles(
              {"steps": [{"approval": {"approver_role": "release_manager"}}]}))

    print("== assigning ==")
    conn = FakeConn()
    out = aa.assign(conn, project_id="p1", app_name="Interview Scorecard App",
                    role_keys=["qa", "software_engineer"], actor="param")
    check("it reports what it added", set(out["added"]) == {"qa", "software_engineer"}, str(out))
    check("and who is now on it", set(out["assigned"]) == {"qa", "software_engineer"}, str(out))
    again = aa.assign(conn, project_id="p1", app_name="Interview Scorecard App",
                      role_keys=["qa"], actor="param")
    check("assigning twice adds nothing", again["added"] == [], str(again))

    print("== identity, not string equality ==")
    # 'Lead‐Triage Inbox' as stored carries a U+2010 hyphen. Matching by string would file the
    # same app under two keys and lose the assignment the operator made.
    aa.assign(conn, project_id="p1", app_name="Lead‐Triage Inbox",
              role_keys=["qa"], actor="param")
    same = aa.may_act(conn, project_id="p1", app_name="Lead-Triage Inbox", role_key="qa")
    check("a U+2010 hyphen is the same app", same["allowed"], str(same))

    print("== enforcement stops what contradicts a decision ==")
    yes = aa.may_act(conn, project_id="p1", app_name="Interview Scorecard App", role_key="qa")
    check("an assigned agent may act", yes["allowed"], str(yes))
    no = aa.may_act(conn, project_id="p1", app_name="Interview Scorecard App",
                    role_key="release_manager")
    check("an unassigned one may not", not no["allowed"], str(no))
    check("and the refusal names who IS assigned",
          "qa" in (no.get("reason") or ""), no.get("reason"))
    check("and says how to fix it", "Assign them" in (no.get("reason") or ""), no.get("reason"))

    print("== and does NOT stop what nobody has decided about ==")
    # The too-strict failure mode. Every app created before this table exists has no row, and
    # refusing those would make the feature read as breakage.
    untouched = aa.may_act(conn, project_id="p1", app_name="Some Older App",
                           role_key="release_manager")
    check("an app with no assignment allows anyone", untouched["allowed"], str(untouched))
    check("and says why", "no agents are assigned" in untouched["reason"], untouched["reason"])

    print("== seeding never overwrites a person's decision ==")
    seeded = FakeConn()
    aa.assign(seeded, project_id="p1", app_name="Change Calendar",
              role_keys=["qa"], actor="param")
    out = aa.ensure_defaults(seeded, project_id="p1", app_name="Change Calendar")
    check("an app that already has somebody is left alone",
          out["assigned"] == ["qa"] and out["seeded"] == [], str(out))
    fresh = FakeConn()
    out = aa.ensure_defaults(fresh, project_id="p1", app_name="Brand New App")
    check("an app with nobody gets the defaults",
          set(out["assigned"]) == set(aa.default_roles()), str(out["assigned"]))
    check("marked as defaults, not as choices",
          all(r["source"] == aa.DEFAULT for r in fresh.rows), str(fresh.rows[:2]))
    # And a later operator assignment of the same role is a PROMOTION, not a no-op.
    aa.assign(fresh, project_id="p1", app_name="Brand New App",
              role_keys=["qa"], actor="param")
    qa_row = next(r for r in fresh.rows if r["role_key"] == "qa")
    check("an operator choosing a seeded role promotes it",
          qa_row["source"] == aa.OPERATOR, str(qa_row))
    # ...and re-seeding must not demote it back.
    aa.assign(fresh, project_id="p1", app_name="Brand New App",
              role_keys=["qa"], actor="boss", source=aa.DEFAULT)
    qa_row = next(r for r in fresh.rows if r["role_key"] == "qa")
    check("and re-seeding never demotes it", qa_row["source"] == aa.OPERATOR, str(qa_row))

    print("== taking an agent off ==")
    out = aa.unassign(fresh, project_id="p1", app_name="Brand New App", role_key="qa")
    check("it goes", out["ok"] and "qa" not in out["assigned"], str(out))
    check("and is then refused", not aa.may_act(
        fresh, project_id="p1", app_name="Brand New App", role_key="qa")["allowed"])

    print("== the enforcement is actually wired to a gate ==")
    src = open("engine/boss_engine/services/workflow_capability_runner.py").read()
    check("_open_gate checks the approver", "agent_assignment.may_act" in src)
    check("a refusal fails the step rather than passing quietly",
          "workflow.assignment_refused" in src)
    starts = open("engine/boss_engine/services/workflows.py").read()
    check("and start_run seeds the defaults first",
          "agent_assignment.ensure_defaults" in starts)

    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("assignment means something")
    return no_row_is_not_no_one()




def no_row_is_not_no_one() -> int:
    """"Nobody is assigned" was a lie, and the key mismatch is why it kept appearing.

    Two defects, one symptom. The panel reported an empty table as a fact about the world —
    agents unquestionably ran those workflows — and the table was empty because the row had been
    written under a DIFFERENT KEY than the one the console asked for:

        start_run seeded  "Build a pricing experiment log.\\nWho it is for: a team changing…"
                          -> "build a pricing experiment log who it is for a team changing…"
        console asked     "Pricing Experiment Log"   (appIdentity, as every card is titled)
                          -> "pricing experiment log"

    So the seeding worked, the row existed, and nothing could ever find it. Observed live on a
    running Standard Product Delivery v2 with ten steps and seven agents seeded onto it.
    """
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'' if ok else f'  <- {detail}'}")
        if not ok:
            failures.append(name)

    print("== one key, whichever name the caller holds ==")
    raw = "Build a pricing experiment log.\nWho it is for: a team changing prices"
    check("the raw directive and the display name agree",
          aa.app_key(raw) == aa.app_key("Pricing Experiment Log"),
          f"{aa.app_key(raw)!r} vs {aa.app_key('Pricing Experiment Log')!r}")
    check("and it is the display name's key", aa.app_key(raw) == "pricing experiment log",
          aa.app_key(raw))
    # The U+2010 case still holds through the namer.
    check("a U+2010 hyphen still matches",
          aa.app_key("Lead‐Triage Inbox") == aa.app_key("Lead-Triage Inbox"))
    check("the project-default pseudo-app is not mangled",
          aa.app_key(aa.PROJECT_DEFAULT) == aa.PROJECT_DEFAULT)

    print("== an app with no row is running the project's team, not nobody ==")
    conn = FakeConn()
    team = aa.effective(conn, "p1", aa.app_key("Some App"))
    check("it returns agents", bool(team), str(team))
    check("marked builtin, so the screen can say 'by default'",
          all(t["source"] == aa.BUILTIN for t in team), str(team[:2]))
    check("and they are the ones that actually run it",
          {t["role_key"] for t in team} == set(aa.default_roles()),
          str(sorted(t["role_key"] for t in team)))

    print("== a recorded assignment wins over the default ==")
    aa.assign(conn, project_id="p1", app_name="Some App", role_keys=["qa"], actor="param")
    now = aa.effective(conn, "p1", aa.app_key("Some App"))
    check("only what was recorded", [t["role_key"] for t in now] == ["qa"], str(now))
    check("and not marked builtin", now[0]["source"] == aa.OPERATOR, str(now[0]))

    print("== the project's own team, chosen once, seeds every app ==")
    aa.assign(conn, project_id="p1", app_name=aa.PROJECT_DEFAULT,
              role_keys=["qa", "chief_architect"], actor="param")
    check("project_team reads it",
          set(aa.project_team(conn, "p1")) == {"qa", "chief_architect"},
          str(aa.project_team(conn, "p1")))
    fresh = aa.effective(conn, "p1", aa.app_key("A Brand New App"))
    check("and an app with no row runs it",
          {t["role_key"] for t in fresh} == {"qa", "chief_architect"}, str(fresh))

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {', '.join(failures)}")
        return 1
    print("no row is not no one")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
