"""official.docker-compose: the environment provider. Assembles every resource plugin's compose
fragment, plus the wiring the app's own services need, into ONE override file the existing
`boss.deploy.yaml kind: compose` deploy adds with a second `-f`.

Input  `ctx.state["resources"]`: name -> that resource's state (its `fragment`) and `bindings`
       (env name -> ref), handed over by BOSS; `config.appServices`: the app's compose services
       that use the resources.
Output in STATE (the workdir does not outlive the hook): `compose`, the override YAML, where
       resource services keep their `${NAME}` placeholders and every app service gains
       `depends_on` each resource (healthy when it has a health check) and
       `environment: {NAME: ${NAME}}` for every binding; and `deploy`, the boss.deploy.yaml
       snippet: the override file name, the project and `env: {NAME: {secret: NAME}}`. Values are
       resolved from the vault when the deploy writes its env file and never appear here.

verify   the override is valid compose (`docker compose config` on the host, with the app's
         services stubbed) and the Docker engine reports every resource service of the project
         running, and healthy where it has a check.
destroy  removes the app services' containers; refused while resource services still run.
"""
from __future__ import annotations

import json

import yaml

from ..platform.contract import HookContext, OperationResult, OperationStatus, PlannedChange
from ._runtime import HookFailure, OfficialPlugin, Session, evidence, labels, project_name

SECTIONS = ("services", "volumes", "configs", "networks")
OVERRIDE_FILE = ".boss/compose.resources.yaml"


class DockerComposePlugin(OfficialPlugin):
    directory = "docker-compose"

    def _changes(self, ctx: HookContext) -> list[PlannedChange]:
        return [PlannedChange(action="create", target=f"file:{OVERRIDE_FILE}",
                              detail="every resource fragment + the app services' depends_on and bindings"),
                PlannedChange(action="update", target=f"project:{project_name(ctx)}",
                              detail="the stack deploy runs it with the app's compose file")]

    def resources(self, ctx: HookContext) -> dict:
        res = ctx.state.get("resources")
        if not isinstance(res, dict) or not res:
            raise HookFailure("no_resources", "BOSS did not hand over the app's resources "
                              "(state.resources: name -> fragment and bindings)")
        return res

    def assemble(self, ctx: HookContext) -> tuple[dict, dict[str, bool], list[str]]:
        """(override, resource service -> has health check, binding env names)."""
        merged: dict = {sec: {} for sec in SECTIONS}
        healthy: dict[str, bool] = {}
        names: list[str] = []
        for rname, rstate in sorted(self.resources(ctx).items()):
            for sec in SECTIONS:
                for key, body in ((rstate or {}).get("fragment") or {}).get(sec, {}).items():
                    if key in merged[sec]:
                        raise HookFailure("conflict", f"{sec}.{key} of resource {rname!r} is already defined")
                    merged[sec][key] = body
                    if sec == "services":
                        healthy[key] = "healthcheck" in body
            names += [n for n in ((rstate or {}).get("bindings") or {}) if n not in names]
        for app in ctx.resource.config.get("appServices") or []:
            if app in merged["services"]:
                raise HookFailure("conflict", f"app service {app!r} has the name of a resource service")
            merged["services"][app] = {
                "environment": {n: f"${{{n}}}" for n in sorted(names)},
                "depends_on": {svc: {"condition": "service_healthy" if hc else "service_started"}
                               for svc, hc in sorted(healthy.items())},
                "labels": labels(ctx, self.plugin_id),
            }
        return {k: v for k, v in merged.items() if v}, healthy, sorted(names)

    def check(self, ctx: HookContext, s: Session, override: dict) -> None:
        """`docker compose config` on the host: the override plus a stub for each app service."""
        stub = json.loads(json.dumps(override))
        for app in ctx.resource.config.get("appServices") or []:
            stub["services"][app]["image"] = "boss-stub"
        s.compose(["config", "-q"], stub, what="checking the assembled compose")

    def do_provision(self, ctx: HookContext, s: Session) -> OperationResult:
        override, healthy, names = self.assemble(ctx)
        self.check(ctx, s, override)
        text = s.check_fragment(override)
        deploy = {"compose": {"files": ["<the app's compose file>", OVERRIDE_FILE], "project": project_name(ctx)},
                  "env": {n: {"secret": n} for n in names}}
        state = {**ctx.state, "compose": text, "deploy": deploy, "project": project_name(ctx),
                 "resourceServices": sorted(healthy), "appServices": list(ctx.resource.config.get("appServices") or [])}
        return ctx.result(state=state, evidence=[evidence(
            "assembled", True, "config", f"{len(healthy)} resource services, {len(state['appServices'])} app services wired, "
            f"{len(names)} bindings")])

    def services(self, ctx: HookContext, s: Session) -> dict[str, dict]:
        out = s.run(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project_name(ctx)}",
                     "--format", "{{json .}}"], what="listing the project")
        rows = {}
        for line in out.stdout.splitlines():
            if line.strip():
                r = json.loads(line)
                lab = dict(kv.split("=", 1) for kv in r.get("Labels", "").split(",") if "=" in kv)
                rows[lab.get("com.docker.compose.service", "")] = r
        return rows

    def do_verify(self, ctx: HookContext, s: Session) -> OperationResult:
        if "compose" not in ctx.state:
            raise HookFailure("not_provisioned", f"{ctx.resource_id} has not been assembled")
        self.check(ctx, s, yaml.safe_load(ctx.state["compose"]))
        rows = self.services(ctx, s)
        expected = ctx.state.get("resourceServices", [])
        down = [e for e in expected if rows.get(e, {}).get("State") != "running"
                or "unhealthy" in rows.get(e, {}).get("Status", "")]
        ev = [evidence("compose_valid", True, "config", "docker compose config accepts the override"),
              evidence("resources_running", not down, "connection",
                    f"docker engine: {len(expected) - len(down)} of {len(expected)} resource services running"
                    + (f"; not running: {', '.join(down)}" if down else ""))]
        if not down:
            return ctx.result(evidence=ev, state=ctx.state)
        return ctx.result(OperationStatus.FAILED, evidence=ev, errors=[{"code": "verify_failed",
                                                                        "message": "resource services are not running"}])

    def do_destroy(self, ctx: HookContext, s: Session) -> OperationResult:
        self.guard_destroy(ctx)
        rows = self.services(ctx, s)
        left = [svc for svc in rows if svc.startswith("res-")]
        if left:
            raise HookFailure("has_dependents", f"resource services still run in {project_name(ctx)}: "
                              f"{', '.join(sorted(left))}; destroy those resources first", status=OperationStatus.REFUSED)
        removed = []
        for svc, r in rows.items():
            s.run(["docker", "rm", "-f", "-v", r["ID"]], check=False)
            removed.append(f"container:{svc}")
        s.remove_network_if_unused()
        return ctx.result(evidence=[evidence("removed", True, "cleanup", ", ".join(removed) or "nothing left")], state={})
