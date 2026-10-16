#!/usr/bin/env python3
"""Build the "Dependency upgrade PR" workflow on a BOSS install.

An EXAMPLE. Nothing here ships with BOSS and nothing runs it on install. README.md does the
same thing by hand in the console.

WHAT IT BUILDS
--------------
    assess  →  route ─ skip ──→  tell_skip
                     └ upgrade → gate → build → ci → tell
                                          └ on_error → tell_failed
    engineering_captain            PERSON  engineering_captain  release_manager  NEEDS A PERSON

An agent reads the repository and says whether the upgrade is worth doing and how. If not, the
owner gets a note saying why. If so, a PERSON opens the gate, BOSS's governed build opens the
pull request, the run waits for CI, and the owner is emailed the result — after approval.

THREE THINGS THIS SHOWS. A `condition` that routes on a value the agent declared; a `decision`
gate in front of the step that writes to GitHub; and an `on_error` path, so a build that fails
tells somebody instead of stopping the run in silence.

THE MERGE IS YOURS. BOSS opens the PR; nothing here merges it.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import (  # noqa: E402
    agent_step, connect, define_steps, enable_skills, from_input, from_step, grant, parser,
    publish, step,
)

NAME = "Dependency upgrade PR"
DESCRIPTION = (
    "Assess a dependency upgrade against the repository, stop for a person, open the pull "
    "request through the governed build, wait for CI, and email the owner the result."
)

ASSESSOR = "engineering_captain"
ASSESSOR_SKILLS = ["repo.read", "ci.status"]

STEPS = [
    {"label": "Tell the owner", "skill_id": "email.send_real_guarded",
     "summary": "Email the owner what happened, once a person has read it.",
     "inputs": [{"name": "to", "required": True}, {"name": "body", "required": True},
                {"name": "subject", "required": False}],
     "agent": "engineering_captain"},
]

ASSESS_BRIEF = (
    "Assess upgrading the dependency named in `package` to `target_version` (or to the latest "
    "release, if no version is given) in the project's repository. Use the repository tools "
    "you hold: find where the package is declared and used, read the lockfile, and check the "
    "current CI status. Decide `verdict`: 'upgrade' if the upgrade is worth opening as a pull "
    "request now, or 'skip' if it is already current, unused, blocked by a major breaking "
    "change, or if CI is already red. Write `plan` as the exact instructions an engineer "
    "would follow: the files to change, the version to set, the commands to run, and the tests "
    "that prove it — or, for 'skip', the reason in two sentences. Write `title` as the pull "
    "request title, under 70 characters. Do not guess a version that you did not find."
)


def build_spec(keys: dict[str, str], args) -> dict:
    """The workflow definition, built from the custom step keys the install assigned."""
    build: dict = {
        "id": "build", "kind": "capability",
        "binding": {"capability": "build",
                    "inputs": {"title": from_step("assess", "title"),
                               "instructions": from_step("assess", "plan"),
                               "repository": from_input("repository", default="")}},
        "approval": {"mode": "auto"}, "next": ["ci"],
        # ONE ATTEMPT, THEN THE ERROR PATH. A governed build that fails has a reason on the
        # run; retrying it blind spends money to read the same reason.
        "max_attempts": 1, "on_error": "tell_failed",
    }
    ci: dict = {
        "id": "ci", "kind": "capability",
        "binding": {"capability": "wait_for_ci",
                    "inputs": {"pull_request_url": from_step("build", "pull_request_url")}},
        "approval": {"mode": "auto"}, "next": ["tell"],
    }
    if not args.no_agents:
        build["captain"] = "engineering_captain"
        ci["captain"] = "release_manager"
    return {
        "entry_step": "assess",
        "input_schema": {
            "required": ["package", "to"],
            "properties": {
                "package": {"title": "Package",
                            "description": "The dependency to upgrade, as it is named in the "
                                           "manifest.",
                            "examples": ["requests"]},
                "target_version": {"title": "Target version",
                                   "description": "Optional. Leave blank for the latest release."},
                "to": {"title": "Owner email", "format": "email",
                       "description": "Who is told the outcome."},
                "repository": {"title": "Repository",
                               "description": "Optional. owner/name, when the project has more "
                                              "than one repository bound."},
                "assess_brief": {"title": "Assessment brief", "format": "textarea",
                                 "description": "Optional; leave blank for the stored brief.",
                                 "examples": [ASSESS_BRIEF]},
            },
        },
        "steps": [
            agent_step("assess", from_input("assess_brief", default=ASSESS_BRIEF), ["route"],
                       agent=ASSESSOR, args=args,
                       produces=["verdict", "plan", "title"],
                       context={"package": from_input("package"),
                                "target_version": from_input("target_version", default=""),
                                "repository": from_input("repository", default="")}),
            # ROUTE ON WHAT THE AGENT DECIDED. The value is the agent's own `verdict`, read
            # from its output; anything but 'upgrade' is a skip.
            {"id": "route", "kind": "condition",
             "expression": {"path": "steps.assess.output.verdict", "op": "eq",
                            "value": "upgrade"},
             "on_true": "gate", "on_false": "tell_skip"},
            step("tell_skip", keys["Tell the owner"],
                 {"to": from_input("to"),
                  "subject": "Dependency upgrade skipped — here is why",
                  "body": from_step("assess", "plan")},
                 [], agent="engineering_captain", approval="manual", args=args),
            # A PERSON OPENS THE GATE. `build` is risk tier 1 — BOSS would run it unattended —
            # and it writes a branch and a pull request to GitHub. That is an external effect,
            # so the workflow puts a person in front of it, with the plan to read.
            {"id": "gate", "kind": "decision",
             "question": "Open a pull request for this upgrade? Read the plan first.",
             "guidance": "The plan is the exact change the build will make. Reject if the "
                         "version is wrong, the plan touches files it should not, or CI is "
                         "already red.",
             "approval": {"mode": "manual", "approver_role": "human"},
             "next": ["build"]},
            build,
            ci,
            step("tell", keys["Tell the owner"],
                 {"to": from_input("to"),
                  # THE PR LINK AS THE SUBJECT, THE CI SUMMARY AS THE BODY. A step wires whole
                  # values; two facts take two fields.
                  "subject": from_step("build", "pull_request_url"),
                  "body": from_step("ci", "ci_summary")},
                 [], agent="engineering_captain", approval="manual", args=args),
            step("tell_failed", keys["Tell the owner"],
                 {"to": from_input("to"),
                  "subject": "Dependency upgrade: the build could not open a PR",
                  "body": from_step("assess", "plan")},
                 [], agent="engineering_captain", approval="manual", args=args),
        ],
    }


def main() -> None:
    args = parser("Build the dependency upgrade PR workflow.").parse_args()
    api = connect(args, NAME)
    enable_skills(api, sorted({s["skill_id"] for s in STEPS} | set(ASSESSOR_SKILLS)), args)
    grant(api, ASSESSOR, sorted(set(ASSESSOR_SKILLS) | {s["skill_id"] for s in STEPS
                                                        if s.get("agent") == ASSESSOR}), args)
    keys = define_steps(api, STEPS, args)
    if args.dry_run:
        print(f"  WORKFLOW\n    would create {NAME!r}\n")
        return

    publish(api, name=NAME, description=DESCRIPTION, args=args, spec=build_spec(keys, args))

    print(f"""  Done.

  Start a run: Workflows → Library → "{NAME}" → ⋯ → Start.

    package          the dependency, as named in the manifest — "requests"
    target_version   optional — blank means the latest release
    to               who is told the outcome
    repository       optional — owner/name when more than one is bound

  The run stops at the gate with the agent's plan. Nothing touches GitHub until you
  approve it; nothing is merged by this workflow at all. Every email stops too.
""")


if __name__ == "__main__":
    main()
