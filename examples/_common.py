"""Shared plumbing for the example setup scripts.

Every example does the same four things — enable skills, define steps, create a workflow,
publish the wiring — through the same public API the console uses. This is that, once, so each
example is only what makes it different.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request


def _tls_context() -> ssl.SSLContext | None:
    """Trust what curl trusts.

    The stock macOS Python ships its own narrow CA bundle, so an HTTPS BOSS behind an ordinary
    certificate raises CERTIFICATE_VERIFY_FAILED while `curl` to the same URL is fine. The
    error surfaces as "cannot reach <host>", which reads as the server being down — a bad first
    experience on a install that is working perfectly.

    None means "use the default", which is right everywhere certifi is absent.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return None


TLS = _tls_context()


def die(message: str) -> None:
    print(f"\n  ERROR  {message}\n", file=sys.stderr)
    sys.exit(1)


class Api:
    def __init__(self, base: str, key: str, project: str, actor: str):
        self.base, self.key, self.project, self.actor = base.rstrip("/"), key, project, actor

    def __call__(self, method: str, path: str, body=None):
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"Authorization": f"Bearer {self.key}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=180, context=TLS) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            raw = error.read().decode()[:600]
            try:
                return error.code, json.loads(raw)
            except json.JSONDecodeError:
                return error.code, {"detail": raw}
        except urllib.error.URLError as error:
            hint = ("       For a VM install, start scripts/install/remote.sh tunnel "
                    "and use http://127.0.0.1:8000 on your laptop.")
            if "CERTIFICATE_VERIFY" in str(error.reason):
                # Say what it actually is. "cannot reach" sends somebody to check the server.
                hint = ("       That is a certificate TRUST problem on this machine, not a\n"
                        "       problem with BOSS — curl to the same URL will work. Install\n"
                        "       certifi (pip install certifi) and run this again.")
            die(f"cannot reach {self.base}: {error.reason}\n{hint}")

    def check(self):
        code, _ = self("GET", f"/v1/settings?project_id={self.project}")
        if code == 401:
            die("the API rejected that key (401).")
        if code == 404:
            die(f"no project '{self.project}' on this install. Pass --project.")
        if code >= 400:
            die(f"the API answered {code} — is --api pointing at BOSS?")


def parser(description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=description,
        epilog="Also reads BOSS_API, BOSS_API_KEY, BOSS_PROJECT, BOSS_ACTOR.")
    p.add_argument("--api", default=os.environ.get("BOSS_API", "http://127.0.0.1:8000"))
    p.add_argument("--key", default=os.environ.get("BOSS_API_KEY", ""))
    p.add_argument("--project", default=os.environ.get("BOSS_PROJECT", "boss-dogfood"))
    p.add_argument("--actor", default=os.environ.get("BOSS_ACTOR", "ceo"))
    p.add_argument("--no-agents", action="store_true",
                   help="leave steps unassigned, so each runs as the workflow itself")
    p.add_argument("--dry-run", action="store_true",
                   help="say what would be done and change nothing")
    return p


#: WHERE THE KEY IS, printed when it is missing rather than left as an exercise.
#:
#: "no API key" told somebody what was wrong and nothing about what to do, on a variable that
#: appears in every command in every README. The three cases below are the three places BOSS
#: is ever run from, and each one is a line to paste.
HOW_TO_FIND_THE_KEY = """no API key.

  Where to find it, depending on where BOSS is running:

  ON THE SERVER — run this from the default install directory:
      cd ~/boss && source ./boss-community.env.sh
      export BOSS_API="$BOSS_API_URL" BOSS_PROJECT="$BOSS_PROJECT_ID"

  FROM YOUR LAPTOP, with BOSS on a server — start the SSH tunnel in another
  terminal, then print the three export lines and paste them into this one:
      scripts/install/remote.sh --host root@YOUR_HOST --key ~/.ssh/your_key tunnel
      scripts/install/remote.sh --host root@YOUR_HOST --key ~/.ssh/your_key env

  BOSS ON THIS MACHINE:
      source ./boss-community.env.sh
      export BOSS_API="$BOSS_API_URL" BOSS_PROJECT="$BOSS_PROJECT_ID"

  The generated shell file exports BOSS_API_KEY. Avoid --key on the command
  line because arguments can appear in process listings.

  The key is a secret. `env` prints it to your terminal, so do not paste that output
  into a chat or a ticket."""


def connect(args, description: str) -> Api:
    if not args.key:
        die(HOW_TO_FIND_THE_KEY)
    api = Api(args.api, args.key, args.project, args.actor)
    print(f"\n  {description}")
    print(f"  BOSS      {api.base}")
    print(f"  project   {api.project}")
    print(f"  as        {api.actor}\n")
    api.check()
    if args.dry_run:
        print("  DRY RUN — nothing will be changed.\n")
    return api


def enable_skills(api: Api, skills: list[str], args) -> None:
    """A skill BOSS ships is available, not installed — this is what registers it."""
    print("  SKILLS")
    for skill in skills:
        if args.dry_run:
            print(f"    would enable {skill}")
            continue
        code, body = api("POST", f"/v1/skills/{skill}/enable",
                         {"project_id": api.project, "actor": api.actor})
        if code == 403:
            die(f"'{api.actor}' may not enable skills here. Use --actor with the principal "
                f"your key represents — the API names it when it refuses.")
        if code >= 400:
            die(f"could not enable {skill}: {body.get('detail') or body}")
        print(f"    {skill:26} enabled (risk tier "
              f"{(body.get('config') or {}).get('risk_tier')})")
    print()


def grant(api: Api, agent: str, skills: list[str], args) -> None:
    """Give an agent the skills it needs, which is the only thing that lets it use them.

    ENABLING IS NOT GRANTING, and the difference cost a real debugging pass. `enable_skills`
    registers a skill in the project's catalogue; `role_skills` is what the runtime checks before
    letting an agent call one. A role spec's `allowed_skills` list reaches the agent's PROMPT and
    nothing else — so every example installed here once had agents that had been TOLD what they
    could use and held nothing, and every step naming one was refused at the first call.

    Idempotent, so re-running an example is safe.
    """
    print(f"  SKILLS FOR {agent}")
    for skill in skills:
        if args.dry_run:
            print(f"    would give {agent} {skill}")
            continue
        code, body = api("POST", "/v1/role-skills", {
            "project_id": api.project, "actor": api.actor,
            "role_key": agent, "skill_id": skill})
        if code >= 400:
            die(f"could not give {agent} the skill {skill}: {body.get('detail') or body}")
        print(f"    {skill:26} held by {agent}")
    print()


def define_steps(api: Api, steps: list[dict], args) -> dict[str, str]:
    """One step per skill, matched by name and reused so re-running is safe."""
    code, body = api("GET", f"/v1/delivery-workflows/custom-steps?project_id={api.project}")
    if code >= 400:
        die(f"could not read this project's steps: {body.get('detail') or body}")
    have = {s["label"]: s for s in (body.get("steps") or [])}

    print("  STEPS")
    keys: dict[str, str] = {}
    for step in steps:
        if step["label"] in have:
            row, note = have[step["label"]], "already defined"
            # REUSING IS NOT THE SAME AS LEAVING IT ALONE.
            #
            # Matching by label made re-running safe, and then never updated what it found — so
            # a step defined by an earlier version of an example kept ITS inputs for ever. On a
            # live install "Publish to YouTube" still declared only `media_artifact_id` long
            # after the example had added `title` and `description`, so the workflow wired two
            # fields the step did not know about. Both were then treated as REQUIRED, and the
            # run reached the final approval gate before failing on a field the operator was
            # never asked for.
            wanted = {(i["name"], bool(i.get("required", True))) for i in step["inputs"]}
            current = {(i["name"], bool(i.get("required", True)))
                       for i in (row.get("inputs") or [])}
            if wanted != current and not args.dry_run:
                code, updated = api(
                    "PATCH",
                    f"/v1/delivery-workflows/custom-steps/{row['step_key']}",
                    {"project_id": api.project, "actor": api.actor,
                     "summary": step["summary"], "inputs": step["inputs"]})
                if code >= 400:
                    die(f"could not update {step['label']!r}: "
                        f"{updated.get('detail') or updated}")
                row = updated.get("custom_step") or updated.get("step") or row
                note = "updated"
        elif args.dry_run:
            print(f"    would define {step['label']!r} running {step['skill_id']}")
            keys[step["label"]] = "dry-run"
            continue
        else:
            code, made = api("POST", "/v1/delivery-workflows/custom-steps", {
                "project_id": api.project, "actor": api.actor, "binding_kind": "skill",
                "label": step["label"], "summary": step["summary"],
                "skill_id": step["skill_id"], "inputs": step["inputs"]})
            if code >= 400:
                die(f"could not define {step['label']!r}: {made.get('detail') or made}")
            row, note = (made.get("custom_step") or made.get("step") or made), "defined"
        keys[step["label"]] = row["step_key"]
        gate = "  · always needs a person" if row.get("approval_forced_manual") else ""
        print(f"    {row['step_key']:26} {note}, runs {step['skill_id']}{gate}")
    print()
    return keys


def publish(api: Api, *, name: str, description: str, spec: dict, args) -> str:
    """Create the workflow if it is missing, then publish the wiring as a version."""
    slug = name.lower().replace(" ", "_")
    code, body = api("GET", f"/v1/delivery-workflows/{slug}/spec?project_id={api.project}")
    definition_id = (body.get("definition_id") or body.get("id")) if code == 200 else None

    print("  WORKFLOW")
    if definition_id:
        print(f"    reusing {definition_id}")
    elif args.dry_run:
        print(f"    would create {name!r}")
        return ""
    else:
        code, made = api("POST", "/v1/delivery-workflows/create", {
            "project_id": api.project, "actor": api.actor,
            "name": name, "description": description})
        if code >= 400:
            die(f"could not create the workflow: {made.get('detail') or made}")
        definition_id = made["definition"]["id"]
        print(f"    created {definition_id}")

    if args.dry_run:
        return definition_id

    code, saved = api("POST", "/v1/delivery-workflows/save", {
        "project_id": api.project, "actor": api.actor,
        "definition_id": definition_id, "spec": spec})
    if code == 422:
        die(f"BOSS refused the workflow:\n         {saved.get('detail') or saved}")
    if code >= 400:
        die(f"could not save the workflow: {saved.get('detail') or saved}")

    published = saved.get("version") or {}
    version = published.get("version")
    if published.get("reactivated"):
        print(f"    version {version} already existed and is live again")
    elif published.get("duplicate"):
        print(f"    unchanged — this is already version {version}")
    else:
        print(f"    published version {version}")
    for step in spec["steps"]:
        print(f"      {step['id']:16} {step.get('captain') or '(the workflow itself)'}")
    print()
    return definition_id


def step(step_id: str, key: str, inputs: dict, nxt: list, *,
         agent: str = "", approval: str = "auto", args=None) -> dict:
    spec = {"id": step_id, "kind": "capability",
            "binding": {"custom_step": key, "inputs": inputs},
            "approval": {"mode": approval}, "next": nxt}
    if agent and not (args and args.no_agents):
        spec["captain"] = agent
    return spec


def agent_step(step_id: str, instructions, nxt: list, *,
               agent: str = "", context: dict | None = None,
               produces: list[str] | None = None,
               approval: str = "auto", args=None) -> dict:
    """A step that says WHAT IS WANTED and lets an agent work out HOW.

    THE DIFFERENCE FROM `step`. That one binds a skill: the step is "run this thing". This one
    binds nothing — the instruction is the specification, and the agent's own granted skills are
    the vocabulary. That is what stops an example needing a new step every time somebody wants
    something slightly different done to their video.

    `instructions` is usually `from_input("instructions")`: the operator types what they want when
    they start the run. A literal string pins it, for a workflow that always does the same thing.

    `agent` names who does it. Left empty, BOSS picks the agent whose purpose and skills fit the
    instruction — which is the honest default for "just get this done".
    """
    spec = {"id": step_id, "kind": "capability",
            "binding": {"agent_work": True,
                        "inputs": {**(context or {}), "instructions": instructions},
                        # WHAT A LATER STEP MAY WIRE TO. An agent step hands on whatever its
                        # skills returned; these are the values it is asked to DECIDE — a
                        # title read out of a brief, say — which no skill returns.
                        **({"produces": list(produces)} if produces else {})},
            "approval": {"mode": approval}, "next": nxt}
    if agent and not (args and args.no_agents):
        spec["captain"] = agent
    return spec


def from_input(field: str) -> dict:
    return {"from_input": field}


def from_step(step_id: str, field: str) -> dict:
    return {"from_step": step_id, "field": field}
