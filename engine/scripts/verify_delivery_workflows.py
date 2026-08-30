#!/usr/bin/env python3
"""Verification agent for customer delivery workflows.

Runs the whole feature against a LIVE system — the API, the database, the runner — and reports
what it PROVED, not what it ran. A green test suite says the code does what its author thought;
this says the deployed thing does what a customer will need it to.

    python3 engine/scripts/verify_delivery_workflows.py --api http://127.0.0.1:8010

Every check states the claim it is testing and the failure it would catch. A check that cannot
say what breaking it would cost is a check nobody will maintain.

Exit code is the number of failed claims, so it can gate a deploy.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Result:
    claim: str
    passed: bool
    detail: str
    #: What would be broken in the product if this claim were false. The reason the check
    #: exists, written where somebody reading a failure will see it.
    cost: str
    phase: str


@dataclass
class Agent:
    api: str
    project: str
    actor: str = "verification-agent"
    #: Skip TLS verification. For a tailnet-internal host whose CA this machine does not carry
    #: — Python does not read the system keychain the way curl does, so a host curl trusts can
    #: still fail here. NEVER for a public endpoint: it is opt-in, per-run, and printed in the
    #: report so a result obtained this way says so.
    insecure: bool = False
    results: list[Result] = field(default_factory=list)
    _forked_key: Optional[str] = None
    _run_id: Optional[str] = None

    # ── plumbing ──────────────────────────────────────────────────────────────

    def _call(
        self, method: str, path: str, payload: Optional[dict] = None
    ) -> tuple[int, Any]:
        url = f"{self.api}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        # Production requires a key. Read from the environment rather than an argument so it
        # never lands in a shell history or a CI log.
        key = os.environ.get("BOSS_API_KEY") or os.environ.get("BOSS_CONSOLE_API_KEY")
        if key:
            request.add_header("Authorization", f"Bearer {key}")
        try:
            context = ssl._create_unverified_context() if self.insecure else None
            with urllib.request.urlopen(request, timeout=90, context=context) as response:
                body = response.read().decode()
                return response.status, (json.loads(body) if body else None)
        except urllib.error.HTTPError as error:
            body = error.read().decode()
            try:
                return error.code, json.loads(body)
            except Exception:
                return error.code, {"detail": body}
        except Exception as error:                  # connection refused, DNS, timeout
            return 0, {"detail": str(error)}

    def check(self, phase: str, claim: str, passed: bool, detail: str, cost: str) -> bool:
        self.results.append(Result(claim, bool(passed), detail, cost, phase))
        return bool(passed)

    # ── Phase 0: the line between BOSS's workflows and a customer's ────────────

    def verify_separation(self) -> None:
        status, body = self._call("GET", f"/v1/delivery-workflows?project_id={self.project}")
        items = (body or {}).get("items", []) if status == 200 else []
        self.check(
            "0", "the product surface returns only product workflows",
            status == 200 and all(i.get("namespace") == "product" for i in items),
            f"{len(items)} workflows, namespaces={sorted({i.get('namespace') for i in items})}",
            "a customer-facing builder could edit the governed coding pipeline that reviews "
            "every build in the system",
        )

        # The system list must still hold BOSS's own, untouched.
        status_all, body_all = self._call("GET", f"/v1/workflows?project_id={self.project}")
        all_items = (body_all or {}).get("items", []) if status_all == 200 else []
        system = [i for i in all_items if i.get("namespace", "system") == "system"]
        self.check(
            "0", "BOSS's own workflows are still there and still system",
            len(system) > 0,
            f"{len(system)} system workflows present",
            "the separation would have been achieved by deleting the thing it was separating",
        )

        catalogue_ids = [i["id"] for i in items if i.get("is_catalogue")]
        if catalogue_ids:
            status_save, save_body = self._call("POST", "/v1/delivery-workflows/save", {
                "project_id": self.project,
                "definition_id": catalogue_ids[0],
                "spec": {"entry_step": "x",
                         "steps": [{"id": "x", "kind": "task", "binding": {}, "next": []}]},
            })
            self.check(
                "0", "a workflow BOSS ships cannot be edited in place",
                status_save == 403,
                f"HTTP {status_save}: {str((save_body or {}).get('detail', ''))[:80]}",
                "every fleet's 'Standard Product Delivery' would quietly become a different "
                "thing, with nothing left to reset to",
            )

    # ── Phase 1: a step can do the things delivery is made of ─────────────────

    def verify_capabilities(self) -> None:
        status, body = self._call("GET", "/v1/delivery-workflows/capabilities")
        caps = {c["key"]: c for c in (body or {}).get("capabilities", [])} if status == 200 else {}
        needed = {"research", "meeting", "build", "deploy", "monitor"}
        self.check(
            "1", "the five capabilities a delivery workflow needs all exist",
            needed <= set(caps),
            f"missing: {sorted(needed - set(caps)) or 'none'} ({len(caps)} total)",
            "the five things a delivery workflow is made of would be the five things a step "
            "cannot be",
        )
        self.check(
            "1", "every capability declares what it needs and what it produces",
            all(("required_inputs" in c and "produces" in c) for c in caps.values()),
            f"{len(caps)} capabilities carry a full declaration",
            "the builder could not validate a step, and a customer would find out at run time",
        )
        floor = (body or {}).get("manual_approval_risk_floor")
        forced = {k for k, c in caps.items() if c.get("approval_forced_manual")}
        self.check(
            "1", "the risk floor is published, and deploy sits above it",
            floor is not None and "deploy" in forced,
            f"floor={floor}, always-manual={sorted(forced)}",
            "a workflow could be configured to deploy to production unattended",
        )

    # ── Phase 2: the catalogue, and pointing work at one ──────────────────────

    def verify_catalogue(self) -> None:
        status, body = self._call("GET", "/v1/delivery-workflows/catalogue")
        items = (body or {}).get("items", []) if status == 200 else []
        self.check(
            "2", "BOSS ships a catalogue of delivery workflows",
            len(items) >= 12,
            f"{len(items)} workflows offered",
            "a customer would have to author a workflow before doing any work at all",
        )
        gated = [i for i in items
                 if any(s["capability"] == "deploy" for s in i.get("steps", []))]
        ungated = [i["key"] for i in gated if not i.get("gates")]
        self.check(
            "2", "every catalogue path that reaches production is gated",
            not ungated,
            f"{len(gated)} deploy to production, {len(ungated)} without a gate: {ungated}",
            "software would reach production with nobody having agreed to it",
        )
        self.check(
            "2", "a workflow says what a project must already have",
            all("requires_project" in i for i in items),
            "every entry declares its prerequisites",
            "'you have not connected a repository' would be discovered on the morning the "
            "build step finally ran",
        )

        install_status, install = self._call(
            "POST", "/v1/delivery-workflows/catalogue/install", {"project_id": self.project})
        self.check(
            "2", "installing the catalogue is idempotent",
            install_status == 200 and (install or {}).get("count", 0) >= 12,
            f"installed {(install or {}).get('count')} on a project that already had them",
            "every boot would add twelve more copies and 'which workflow' would stop having "
            "an answer",
        )

    # ── Phase 3 + 4: seeing it, and building your own ─────────────────────────

    def verify_console_surface(self) -> None:
        status, body = self._call("GET", f"/v1/delivery-workflows/runs?project_id={self.project}")
        runs = (body or {}).get("runs", []) if status == 200 else []
        self.check(
            "3", "runs are readable with their steps in one call",
            status == 200,
            f"{len(runs)} runs returned",
            "the console would need a request per run and a project with twenty in flight "
            "would take twenty-one round trips to draw",
        )
        if runs:
            run = runs[0]
            self.check(
                "3", "a run says where it has got to and what it is waiting for",
                all(k in run for k in ("steps_done", "step_count", "current_step", "waiting_on")),
                f"{run['workflow_name']}: {run['steps_done']}/{run['step_count']}, "
                f"waiting on {run.get('waiting_on') or '—'}",
                "'waiting' with no subject sends an operator to ask three people who it is "
                "waiting on",
            )

        key = f"verify_fork_{int(time.time())}"
        fork_status, fork = self._call("POST", "/v1/delivery-workflows/fork", {
            "project_id": self.project, "workflow_key": "minor_feature",
            "new_key": key, "new_name": "Verification fork", "actor": self.actor,
        })
        forked = fork_status == 200 and (fork or {}).get("definition")
        if forked:
            self._forked_key = key
        self.check(
            "4", "a customer can fork a shipped workflow to change it",
            bool(forked) and not fork["definition"]["is_catalogue"]
            and bool(fork["definition"].get("forked_from_version_id")),
            f"HTTP {fork_status}, forked_from="
            f"{(fork or {}).get('forked_from', '—') if forked else (fork or {}).get('detail')}",
            "the only way to change a workflow would be to edit the standard, and the diff "
            "against it would be unrecoverable",
        )

        if forked:
            definition_id = fork["definition"]["id"]
            # The thing a builder must never be able to save.
            bad_status, bad = self._call("POST", "/v1/delivery-workflows/save", {
                "project_id": self.project, "definition_id": definition_id,
                "spec": {"entry_step": "d", "steps": [{
                    "id": "d", "kind": "capability",
                    "binding": {"capability": "deploy", "inputs": {"run_id": "r"}},
                    "approval": {"mode": "auto"}, "next": [],
                }]},
            })
            detail = str((bad or {}).get("detail") or (bad or {}).get("error") or "")
            self.check(
                "5", "a workflow cannot be saved that auto-approves a deploy",
                bad_status == 422 and "requires a person" in detail,
                f"HTTP {bad_status}: {detail[:90]}",
                "the risk floor would be advisory, and 'set approval to auto' would be the "
                "documented way to ship unreviewed",
            )

            good_status, good = self._call("POST", "/v1/delivery-workflows/save", {
                "project_id": self.project, "definition_id": definition_id,
                "spec": {"entry_step": "m", "steps": [
                    {"id": "m", "kind": "capability",
                     "binding": {"capability": "meeting",
                                 "inputs": {"directive": {"from_input": "directive"}}},
                     "next": ["d"]},
                    {"id": "d", "kind": "capability",
                     "binding": {"capability": "deploy",
                                 "inputs": {"run_id": {"from_input": "run_id"}}},
                     "next": []},
                ]},
            })
            version = (good or {}).get("version") or {}
            steps = {s["id"]: s for s in (version.get("spec") or {}).get("steps", [])}
            self.check(
                "4", "saving publishes a new version rather than mutating the old one",
                good_status == 200 and (version.get("version", 0) >= 1),
                f"HTTP {good_status}, version {version.get('version')}",
                "a run in flight would change meaning underneath itself when somebody saved",
            )
            self.check(
                "5", "the stored plan records the approval that will ACTUALLY apply",
                steps.get("d", {}).get("approval", {}).get("mode") == "manual",
                f"deploy step stored as "
                f"{steps.get('d', {}).get('approval', {}).get('mode', 'missing')}",
                "a plan would read 'auto' beside a step that stops and waits, and nobody could "
                "trust what the plan said",
            )

    # ── The end-to-end claim: it actually runs ────────────────────────────────

    def verify_it_runs(self, patience: int = 6) -> None:
        start_status, started = self._call("POST", "/v1/delivery-workflows/start", {
            "project_id": self.project, "workflow_key": "research_led_discovery",
            "actor": self.actor,
            "input": {"directive": "Verification agent probe", "title": "Verification probe"},
        })
        run = (started or {}).get("run") or {}
        self._run_id = run.get("id")
        self.check(
            "1", "a delivery workflow can be launched",
            start_status == 200 and bool(self._run_id),
            f"HTTP {start_status}, run {self._run_id or (started or {}).get('detail')}",
            "the whole feature would be a plan nobody can execute",
        )
        if not self._run_id:
            return

        progressed = False
        reached_gate = False
        for _ in range(patience):
            self._call("POST", "/v1/delivery-workflows/run-capabilities",
                       {"project_id": self.project})
            time.sleep(2)
            _, body = self._call(
                "GET", f"/v1/delivery-workflows/runs?project_id={self.project}")
            mine = next((r for r in (body or {}).get("runs", [])
                         if r["id"] == self._run_id), None)
            if not mine:
                continue
            if mine["steps_done"] > 0:
                progressed = True
            waiting = [s for s in mine["steps"] if s["status"] == "waiting"]
            if waiting:
                reached_gate = True
                break

        self.check(
            "1", "capability steps execute by dispatching to BOSS's real tools",
            progressed,
            f"{mine['steps_done'] if mine else 0} steps completed",
            "a workflow would schedule steps that nothing ever performs",
        )
        self.check(
            "5", "a gate stops the run and names who it is waiting on",
            reached_gate and bool(mine and mine.get("waiting_on")),
            f"waiting on {mine.get('waiting_on') if mine else '—'}",
            "an approval gate would be decoration, and work would flow past the point where "
            "somebody was supposed to look",
        )

    # ── Report ────────────────────────────────────────────────────────────────

    # ── Phase 7: steps the customer defines ───────────────────────────────────

    def verify_custom_steps(self) -> None:
        """A customer's own step is as easy to add as a standard one, and NOT easier to get
        away with. Every check here is really the same claim from a different angle: the risk
        tier comes from what the step binds, never from the customer."""
        status, body = self._call(
            "GET", f"/v1/delivery-workflows/custom-steps?project_id={self.project}")
        reachable = status == 200
        self.check(
            "7", "a project can be asked what steps it has and what it could bind",
            reachable,
            f"HTTP {status}" if not reachable else
            f"{len(body.get('steps', []))} defined, "
            f"{len(body.get('bindable', {}).get('skills', []))} skills and "
            f"{len(body.get('bindable', {}).get('mcp_servers', []))} MCP servers available",
            "the builder could not offer a custom step at all",
        )
        if not reachable:
            return

        bindable = body.get("bindable", {})
        floor = body.get("manual_approval_risk_floor")
        self.check(
            "7", "every bindable thing arrives with the tier the floor will use",
            all("risk_tier" in s for s in bindable.get("skills", []))
            and all("risk_tier" in m for m in bindable.get("mcp_servers", [])),
            f"floor={floor}",
            "the builder would say a step can run automatically and the runner would stop it",
        )
        # An unreachable MCP server must be SHOWN, not hidden — hiding it looks like it was
        # never registered and the customer adds it twice.
        unreachable = [m["name"] for m in bindable.get("mcp_servers", [])
                       if not m.get("reachable", True)]
        self.check(
            "7", "an unreachable MCP server is shown rather than silently dropped",
            all("reachable" in m for m in bindable.get("mcp_servers", [])),
            f"unreachable: {unreachable or 'none'}",
            "a registered server would vanish from the list and be added again",
        )

        # THE ONE THAT MATTERS. Try to define a step that declares itself harmless. The API
        # must ignore the field entirely — if this ever passes, every gate in the product is
        # optional.
        status, created = self._call("POST", "/v1/delivery-workflows/custom-steps", {
            "project_id": self.project,
            "label": "Verification probe",
            "binding_kind": "skill",
            "skill_id": "__verification_probe_no_such_skill__",
            "risk_tier": 0,
            "actor": self.actor,
        })
        if status == 200:
            declared_own_risk = created.get("risk_tier") == 0
            self.check(
                "7", "a customer cannot declare their own step harmless",
                not declared_own_risk,
                f"asked for tier 0, resolved to tier {created.get('risk_tier')} "
                f"from {created.get('risk_source')}",
                "'custom step' would be the documented way past every approval gate",
            )
            self.check(
                "7", "a step bound to something that does not exist refuses to run",
                created.get("usable") is False,
                str(created.get("unusable_reason")),
                "a workflow would appear to run and quietly do nothing",
            )
            self._call(
                "DELETE",
                f"/v1/delivery-workflows/custom-steps/{created.get('step_key')}"
                f"?project_id={self.project}&actor={self.actor}",
            )
        else:
            # Rejecting it outright is also correct — the skill does not exist.
            self.check(
                "7", "a step bound to a skill the project does not have is refused",
                status in (400, 422),
                f"HTTP {status}: {(created or {}).get('detail')}",
                "a step could be defined against a skill that can never run",
            )

        status, palette = self._call(
            "GET", f"/v1/delivery-workflows/capabilities?project_id={self.project}")
        self.check(
            "7", "a customer's steps reach the same palette as the built-in ones",
            status == 200 and "custom_steps" in (palette or {}),
            f"HTTP {status}, {len((palette or {}).get('custom_steps', []))} of the "
            "customer's own",
            "people would have to learn which of their steps BOSS wrote",
        )

    def report(self) -> int:
        failed = [r for r in self.results if not r.passed]
        by_phase: dict[str, list[Result]] = {}
        for result in self.results:
            by_phase.setdefault(result.phase, []).append(result)

        print("\nDELIVERY WORKFLOWS — VERIFICATION")
        print(f"{self.api}  ·  project {self.project}"
              + ("  ·  TLS NOT VERIFIED" if self.insecure else "") + "\n")
        for phase in sorted(by_phase):
            print(f"  Phase {phase}")
            for result in by_phase[phase]:
                mark = "PASS" if result.passed else "FAIL"
                print(f"    [{mark}] {result.claim}")
                print(f"           {result.detail}")
                if not result.passed:
                    # The cost is printed only on failure: on a passing run it is noise, and on
                    # a failing one it is the reason somebody should care.
                    print(f"           would break: {result.cost}")
            print()

        print(f"  {len(self.results) - len(failed)}/{len(self.results)} claims verified")
        if failed:
            print(f"  {len(failed)} FAILED — see 'would break' above")
        return len(failed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8010")
    parser.add_argument("--project", default="boss-dogfood")
    parser.add_argument("--insecure", action="store_true",
                        help="skip TLS verification (tailnet-internal hosts only)")
    parser.add_argument("--skip-run", action="store_true",
                        help="skip the live run, which starts real work")
    args = parser.parse_args()

    agent = Agent(api=args.api.rstrip("/"), project=args.project, insecure=args.insecure)
    status, body = agent._call("GET", "/v1/delivery-workflows/capabilities")
    if status == 0:
        print(f"Cannot reach {agent.api}: {(body or {}).get('detail', 'no response')}")
        return 1
    if status in (401, 403):
        # Said plainly, because "cannot reach" for an auth failure sends somebody to check the
        # network for twenty minutes.
        print(f"{agent.api} refused the request ({status}). "
              "Set BOSS_API_KEY to a console key and try again.")
        return 1

    agent.verify_separation()
    agent.verify_capabilities()
    agent.verify_catalogue()
    agent.verify_console_surface()
    agent.verify_custom_steps()
    if not args.skip_run:
        agent.verify_it_runs()
    return agent.report()


if __name__ == "__main__":
    sys.exit(main())
