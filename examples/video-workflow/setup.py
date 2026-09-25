#!/usr/bin/env python3
"""Build the "Video edit and publish" workflow on a BOSS install.

WHAT THIS IS, AND WHAT IT IS NOT
--------------------------------
An EXAMPLE. Nothing here ships with BOSS and nothing runs it on install. It uses only the
public API — the same calls the console makes when you click through the builder — so every
line of it corresponds to something you could do by hand. README.md does exactly that, by hand,
and is worth reading first if you want to understand what you are getting.

What BOSS *does* ship is the capability this assembles: the video skills, the `video_editor`
and `media_publisher` roles, and ffmpeg in the images. This script only wires them into one
particular workflow. Yours will differ, which is the point.

WHAT IT BUILDS
--------------
    fetch-link  →  plan-edit  →  apply-plan  →  publish
    video_editor   video_editor   video_editor   media_publisher   ← the agent for each step
    auto           auto           auto           NEEDS A PERSON

You supply two things when a run starts: the video link, and what you want done to it in plain
English. Everything else is passed from one step to the next.

IT IS SAFE TO RE-RUN. Steps and the workflow are matched by name and reused, so running it
twice does not leave you with two of everything — which is what the first version of this
script did to the author.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

WORKFLOW_NAME = "Video edit and publish"
DESCRIPTION = (
    "Post a video link with editing instructions. BOSS fetches it, plans the edit, renders it, "
    "and waits for you before publishing."
)

#: The four steps, each binding one skill BOSS ships. `agent` is the role that RUNS the step —
#: and it is not decoration: publish_youtube permits media_publisher and refuses video_editor,
#: so the split below is enforced rather than conventional.
STEPS = [
    {
        "label": "Fetch the video",
        "skill_id": "media.fetch_link",
        "summary": "Turn the pasted link into a video BOSS holds.",
        "inputs": [{"name": "url", "required": True}],
        "agent": "video_editor",
    },
    {
        "label": "Plan the edit",
        "skill_id": "video.plan_edit",
        "summary": "Read the instructions and write the edit plan. Renders nothing.",
        "inputs": [{"name": "media_artifact_id", "required": True},
                   {"name": "instructions", "required": True}],
        "agent": "video_editor",
    },
    {
        "label": "Apply the edit",
        "skill_id": "video.apply_plan",
        "summary": "Run the plan and record what each operation changed.",
        "inputs": [{"name": "media_artifact_id", "required": True},
                   {"name": "plan", "required": True}],
        "agent": "video_editor",
    },
    {
        "label": "Publish to YouTube",
        "skill_id": "publish_youtube",
        "summary": "Upload the approved video as private.",
        "inputs": [{"name": "media_artifact_id", "required": True}],
        "agent": "media_publisher",
    },
]


class Api:
    def __init__(self, base: str, key: str, project: str, actor: str):
        self.base, self.key, self.project, self.actor = base.rstrip("/"), key, project, actor

    def __call__(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"Authorization": f"Bearer {self.key}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            body_text = error.read().decode()[:600]
            try:
                return error.code, json.loads(body_text)
            except json.JSONDecodeError:
                return error.code, {"detail": body_text}
        except urllib.error.URLError as error:
            die(f"cannot reach {self.base}: {error.reason}\n"
                f"       Is --api right? From your laptop it is http://<host>:8000, "
                f"not 127.0.0.1.")
            raise  # unreachable; keeps type checkers happy


def die(message: str) -> None:
    print(f"\n  ERROR  {message}\n", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the video editing workflow on a BOSS install.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Every value can also come from the environment: BOSS_API, BOSS_API_KEY, "
               "BOSS_PROJECT, BOSS_ACTOR.")
    parser.add_argument("--api", default=os.environ.get("BOSS_API", "http://127.0.0.1:8000"),
                        help="the BOSS API (default: %(default)s)")
    parser.add_argument("--key", default=os.environ.get("BOSS_API_KEY", ""),
                        help="BOSS_API_KEY from the install's .env")
    parser.add_argument("--project", default=os.environ.get("BOSS_PROJECT", "boss-dogfood"),
                        help="the project to build it in (default: %(default)s)")
    parser.add_argument("--actor", default=os.environ.get("BOSS_ACTOR", "ceo"),
                        help="who is doing this; must be a principal your API key may act as")
    parser.add_argument("--no-agents", action="store_true",
                        help="leave the steps unassigned, so each runs as the workflow itself")
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would be done and change nothing")
    args = parser.parse_args()

    if not args.key:
        die("no API key. Pass --key, or export BOSS_API_KEY.\n"
            "       On the server it is the BOSS_API_KEY line in /opt/boss/.env")

    api = Api(args.api, args.key, args.project, args.actor)
    print(f"\n  BOSS      {api.base}")
    print(f"  project   {api.project}")
    print(f"  as        {api.actor}\n")

    code, _ = api("GET", f"/v1/settings?project_id={api.project}")
    if code == 401:
        die("the API rejected that key (401).")
    if code == 404:
        die(f"no project '{api.project}' on this install. Pass --project.")
    if code >= 400:
        die(f"the API answered {code} — is --api pointing at BOSS?")

    if args.dry_run:
        print("  DRY RUN — nothing will be changed.\n")

    enable_skills(api, args)
    keys = define_steps(api, args)
    build_workflow(api, args, keys)

    print(f"""
  Done.

  Start a run from the console: Delivery → Workflows → "{WORKFLOW_NAME}" → Start one.
  It asks for two things:

    video_url      a link to the video — Google Drive, or any direct HTTPS link
    instructions   what to do, in plain English. For example:
                   "Cut 2:20 to 2:25 where I fumble, make it vertical for phones,
                    and caption the opening three seconds."

  The last step stops and waits for you. That is not a setting — publish_youtube is
  risk tier 3, and BOSS requires a person at or above the floor whatever a workflow asks.
""")


def enable_skills(api: Api, args) -> None:
    """Register the skills in this project.

    A skill BOSS ships is available, not installed. Until it is enabled it exists in the
    catalogue and not in `project_skills`, and binding a step to it fails with "not registered
    in this project" — which is the first thing that stops people.
    """
    print("  SKILLS")
    for step in STEPS:
        skill = step["skill_id"]
        if args.dry_run:
            print(f"    would enable {skill}")
            continue
        code, body = api("POST", f"/v1/skills/{skill}/enable",
                         {"project_id": api.project, "actor": api.actor})
        if code == 403:
            die(f"'{api.actor}' may not enable skills here. Try --actor with the principal "
                f"your key represents; the API says who when it refuses.")
        if code >= 400:
            die(f"could not enable {skill}: {body.get('detail') or body}")
        tier = (body.get("config") or {}).get("risk_tier")
        print(f"    {skill:22} enabled (risk tier {tier})")
    print()


def define_steps(api: Api, args) -> dict[str, str]:
    """One step per skill, reused if it is already there."""
    code, body = api("GET", f"/v1/delivery-workflows/custom-steps?project_id={api.project}")
    if code >= 400:
        die(f"could not read this project's steps: {body.get('detail') or body}")
    existing = {s["label"]: s for s in (body.get("steps") or [])}

    print("  STEPS")
    keys: dict[str, str] = {}
    for step in STEPS:
        if step["label"] in existing:
            row = existing[step["label"]]
            note = "already defined"
        elif args.dry_run:
            print(f"    would define {step['label']!r} running {step['skill_id']}")
            keys[step["label"]] = "dry-run"
            continue
        else:
            code, made = api("POST", "/v1/delivery-workflows/custom-steps", {
                "project_id": api.project, "actor": api.actor, "binding_kind": "skill",
                "label": step["label"], "summary": step["summary"],
                "skill_id": step["skill_id"], "inputs": step["inputs"],
            })
            if code >= 400:
                die(f"could not define {step['label']!r}: {made.get('detail') or made}")
            row = made.get("custom_step") or made.get("step") or made
            note = "defined"
        keys[step["label"]] = row["step_key"]
        gate = "  · always needs a person" if row.get("approval_forced_manual") else ""
        print(f"    {row['step_key']:22} {note}, runs {step['skill_id']}{gate}")
    print()
    return keys


def build_workflow(api: Api, args, keys: dict[str, str]) -> None:
    """Create the definition if it is missing, then publish the wiring as a version."""
    code, body = api("GET", f"/v1/delivery-workflows/{slug()}/spec?project_id={api.project}")
    definition_id = body.get("definition_id") or body.get("id") if code == 200 else None

    print("  WORKFLOW")
    if definition_id:
        print(f"    reusing {definition_id}")
    elif args.dry_run:
        print(f"    would create {WORKFLOW_NAME!r}")
        return
    else:
        code, made = api("POST", "/v1/delivery-workflows/create", {
            "project_id": api.project, "actor": api.actor,
            "name": WORKFLOW_NAME, "description": DESCRIPTION,
        })
        if code >= 400:
            die(f"could not create the workflow: {made.get('detail') or made}")
        definition_id = made["definition"]["id"]
        print(f"    created {definition_id}")

    if args.dry_run:
        return

    spec = {
        "entry_step": "fetch-link",
        "steps": [
            step_spec("fetch-link", keys["Fetch the video"], STEPS[0],
                      {"url": {"from_input": "video_url"}}, ["plan-edit"], args),
            step_spec("plan-edit", keys["Plan the edit"], STEPS[1], {
                "media_artifact_id": {"from_step": "fetch-link", "field": "media_artifact_id"},
                "instructions": {"from_input": "instructions"},
            }, ["apply-plan"], args),
            step_spec("apply-plan", keys["Apply the edit"], STEPS[2], {
                "media_artifact_id": {"from_step": "fetch-link", "field": "media_artifact_id"},
                "plan": {"from_step": "plan-edit", "field": "plan"},
            }, ["publish"], args),
            # No separate review step. publish_youtube is risk tier 3 and BOSS forces a manual
            # approval at or above the floor, so the gate is enforced rather than added here.
            step_spec("publish", keys["Publish to YouTube"], STEPS[3], {
                "media_artifact_id": {"from_step": "apply-plan", "field": "media_artifact_id"},
            }, [], args, approval="manual"),
        ],
    }
    code, saved = api("POST", "/v1/delivery-workflows/save", {
        "project_id": api.project, "actor": api.actor,
        "definition_id": definition_id, "spec": spec,
    })
    if code == 422:
        die(f"BOSS refused the workflow:\n         {saved.get('detail') or saved}")
    if code >= 400:
        die(f"could not save the workflow: {saved.get('detail') or saved}")
    published = saved.get("version") or {}
    version = published.get("version")
    if published.get("reactivated"):
        print(f"    version {version} already existed and is live again")
    elif published.get("duplicate"):
        # Publishing identical content does not make a new version. Saying "published" here
        # would claim a change that did not happen, on a script whose whole promise is that
        # running it twice is safe.
        print(f"    unchanged — this is already version {version}")
    else:
        print(f"    published version {version}")
    for step in spec["steps"]:
        agent = step.get("captain") or "(the workflow itself)"
        print(f"      {step['id']:12} {agent}")
    print()


def step_spec(step_id, step_key, definition, inputs, nxt, args, approval="auto") -> dict:
    spec = {
        "id": step_id,
        "kind": "capability",
        "binding": {"custom_step": step_key, "inputs": inputs},
        "approval": {"mode": approval},
        "next": nxt,
    }
    if not args.no_agents:
        spec["captain"] = definition["agent"]
    return spec


def slug() -> str:
    return WORKFLOW_NAME.lower().replace(" ", "_")


if __name__ == "__main__":
    main()
