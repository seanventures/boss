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
            hint = ("       From your laptop the API is http://<host>:8000, not 127.0.0.1.")
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

  ON THE SERVER — run this there:
      grep '^BOSS_API_KEY=' /opt/boss/.env | cut -d= -f2-

  FROM YOUR LAPTOP, with BOSS on a server — this prints all three variables, ready to paste:
      scripts/install/remote.sh --host root@YOUR_HOST --key ~/.ssh/your_key env

      Or apply them directly:
      eval "$(scripts/install/remote.sh --host root@YOUR_HOST --key ~/.ssh/your_key env)"

  BOSS ON THIS MACHINE:
      grep '^BOSS_API_KEY=' .env | cut -d= -f2-

  Then either export it:
      export BOSS_API_KEY=<the value>
  or pass it:
      --key "<the value>"

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


def from_input(field: str) -> dict:
    return {"from_input": field}


def from_step(step_id: str, field: str) -> dict:
    return {"from_step": step_id, "field": field}
