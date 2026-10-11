"""official.docker-compose: the environment provider. Assembles the app's compose file and every
resource plugin's fragment into ONE file the existing `boss.deploy.yaml kind: compose` deploy runs.

Input  `config.appCompose` (the app's own compose file, relative to the workdir), the fragments
       other plugins wrote under `compose/`, and `ctx.state["resources"]`: name -> that resource's
       state and bindings, handed over by BOSS.
Output `.boss/compose.yaml` (or `config.output`): app services gain `depends_on` every resource
       (healthy when it has a health check) and `environment: {NAME: ${NAME}}` for every binding;
       resource services keep their `${BOSS_*}` placeholders. `state.deploy` is the boss.deploy.yaml
       snippet: `compose.file`, `compose.project` and `env: {NAME: {secret: ...}}`, so values are
       resolved from the vault at deploy time and never written to the file.

verify   the Docker engine answers and every service in the project is running (healthy where it
         has a check), and each app service received its binding names (names only).
destroy  removes the app services and the assembled file; refused while resources still run.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from ..platform.contract import HookContext, OperationResult, OperationStatus, PlannedChange
from ._runtime import HookFailure, OfficialPlugin, Session, evidence, labels, project_name

SECTIONS = ("services", "volumes", "configs", "networks")


class DockerComposePlugin(OfficialPlugin):
    directory = "docker-compose"

    def output(self, ctx: HookContext) -> str:
        return ctx.resource.config.get("output") or ".boss/compose.yaml"

    def _validate(self, ctx: HookContext) -> list[str]:
        out = []
        for key in ("appCompose", "output"):
            v = ctx.resource.config.get(key)
            if v and (Path(v).is_absolute() or ".." in Path(v).parts):
                out.append(f"config.{key} must be a relative path inside the app's workdir")
        return out

    def _changes(self, ctx: HookContext) -> list[PlannedChange]:
        return [PlannedChange(action="create", target=f"file:{self.output(ctx)}",
                              detail="the app's compose plus every resource fragment"),
                PlannedChange(action="update", target=f"project:{project_name(ctx)}", detail="docker compose up")]

    # -- assembling ---------------------------------------------------------------------------
    def assemble(self, ctx: HookContext) -> tuple[dict, list[str], dict, dict]:
        """(compose, app service names, env name -> ref for `up`, resource service -> has health check)."""
        work = Path(ctx.workdir)
        merged: dict = {s: {} for s in SECTIONS}
        app_services: list[str] = []
        if rel := ctx.resource.config.get("appCompose"):
            p = work / rel
            if not p.is_file():
                raise HookFailure("invalid_request", f"config.appCompose {rel!r} does not exist")
            app = yaml.safe_load(p.read_text()) or {}
            for sec in SECTIONS:
                merged[sec].update(app.get(sec) or {})
            wanted = ctx.resource.config.get("appServices")
            app_services = [s for s in (app.get("services") or {}) if not wanted or s in wanted]
        resources = ctx.state.get("resources") or {}
        env_refs: dict[str, str] = {}
        healthy: dict[str, bool] = {}
        for name, rstate in sorted(resources.items()):
            frag_rel = (rstate or {}).get("fragment")
            if frag_rel:
                frag = yaml.safe_load((work / frag_rel).read_text()) or {}
                for sec in SECTIONS:
                    for key, body in (frag.get(sec) or {}).items():
                        if key in merged[sec]:
                            raise HookFailure("conflict", f"{sec}.{key} from resource {name!r} collides with "
                                              "a definition already in the app's compose")
                        merged[sec][key] = body
                        if sec == "services":
                            healthy[key] = "healthcheck" in body
            env_refs.update((rstate or {}).get("env") or {})
            for env_name, ref in ((rstate or {}).get("bindings") or {}).items():
                env_refs[env_name] = ref["secretRef"] if isinstance(ref, dict) else str(ref)
        bindings = sorted(n for r in resources.values() for n in ((r or {}).get("bindings") or {}))
        for svc in app_services:
            body = merged["services"][svc]
            env = body.get("environment") or {}
            if isinstance(env, list):
                env = dict(e.split("=", 1) if "=" in e else (e, None) for e in env)
            for n in bindings:
                env.setdefault(n, f"${{{n}}}")
            body["environment"] = env
            deps = body.get("depends_on") or {}
            if isinstance(deps, list):
                deps = {d: {"condition": "service_started"} for d in deps}
            for rsvc, has_check in healthy.items():
                deps.setdefault(rsvc, {"condition": "service_healthy" if has_check else "service_started"})
            body["depends_on"] = deps
            body.setdefault("labels", {})
            if isinstance(body["labels"], dict):
                body["labels"].update(labels(ctx, self.plugin_id))
        return {k: v for k, v in merged.items() if v}, app_services, env_refs, healthy

    def do_provision(self, ctx: HookContext, s: Session) -> OperationResult:
        compose, app_services, env_refs, healthy = self.assemble(ctx)
        out = Path(ctx.workdir) / self.output(ctx)
        out.parent.mkdir(parents=True, exist_ok=True)
        text = yaml.safe_dump(compose, sort_keys=False)
        env = {name: s.get(ref) for name, ref in env_refs.items()}
        for v in env.values():
            if v and v in text:
                raise HookFailure("secret_in_file", "a secret value reached the assembled compose file")
        out.write_text(text, encoding="utf-8")
        s.compose(["config", "-q"], files=[out], env=env, what="checking the assembled compose file")
        s.compose(["up", "-d", "--wait", "--wait-timeout", "240"], files=[out], env=env, timeout=300,
                  what="docker compose up")
        deploy = {"kind": "compose", "compose": {"file": self.output(ctx), "project": project_name(ctx)},
                  "env": {name: {"secret": ref.rsplit("/", 1)[-1]} for name, ref in sorted(env_refs.items())}}
        state = {**ctx.state, "output": self.output(ctx), "project": project_name(ctx), "appServices": app_services,
                 "resourceServices": sorted(healthy), "deploy": deploy}
        return ctx.result(state=state, artifacts=[self.output(ctx)],
                          evidence=[evidence("assembled", True, "config",
                                             f"{len(compose.get('services', {}))} services: {len(app_services)} app, "
                                             f"{len(healthy)} resource")])

    # -- verify -------------------------------------------------------------------------------
    def ps(self, ctx: HookContext, s: Session) -> list[dict]:
        proc = s.run(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project_name(ctx)}",
                      "--format", "{{json .}}"], what="listing the project")
        return [json.loads(line) for line in s.text(proc).splitlines() if line.strip()]

    def do_verify(self, ctx: HookContext, s: Session) -> OperationResult:
        out = Path(ctx.workdir) / self.output(ctx)
        if not out.is_file():
            raise HookFailure("not_provisioned", f"{self.output(ctx)} has not been assembled")
        rows = self.ps(ctx, s)
        expected = set(ctx.state.get("appServices", [])) | set(ctx.state.get("resourceServices", []))
        running = {}
        for r in rows:
            svc = dict(kv.split("=", 1) for kv in r.get("Labels", "").split(",") if "=" in kv).get("com.docker.compose.service")
            running[svc] = r.get("State") == "running" and "unhealthy" not in r.get("Status", "")
        missing = sorted(e for e in expected if not running.get(e))
        ev = [evidence("services_running", not missing, "connection",
                       f"docker engine: {sum(running.values())} of {len(expected)} services running"
                       + (f"; not running: {', '.join(missing)}" if missing else ""))]
        names = sorted({n for r in (ctx.state.get("resources") or {}).values() for n in ((r or {}).get("bindings") or {})})
        for svc in ctx.state.get("appServices", []):
            cid = s.run(["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={project_name(ctx)}",
                         "--filter", f"label=com.docker.compose.service={svc}"]).stdout.decode().split()
            got = set()
            if cid:
                envs = json.loads(s.run(["docker", "inspect", "-f", "{{json .Config.Env}}", cid[0]]).stdout.decode() or "[]")
                got = {e.split("=", 1)[0] for e in envs}
            lacking = [n for n in names if n not in got]
            ev.append(evidence(f"bindings:{svc}", not lacking, "config",
                               f"{svc} has {', '.join(names) or 'no bindings'}" if not lacking else f"{svc} lacks {', '.join(lacking)}"))
        if all(e["passed"] for e in ev):
            return ctx.result(evidence=ev, state=ctx.state)
        return ctx.result(OperationStatus.FAILED, evidence=ev, errors=[{"code": "verify_failed", "message": "environment not healthy"}])

    def do_destroy(self, ctx: HookContext, s: Session) -> OperationResult:
        self.guard_destroy(ctx)
        resource_left = [r for r in self.ps(ctx, s)
                         if "com.docker.compose.service=res-" in r.get("Labels", "")]
        if resource_left:
            raise HookFailure("has_dependents", f"{len(resource_left)} resource service(s) still run in "
                              f"{project_name(ctx)}; destroy those resources first", status=OperationStatus.REFUSED)
        removed = []
        for r in self.ps(ctx, s):
            s.run(["docker", "rm", "-f", "-v", r["ID"]], check=False)
            removed.append("container")
        s.remove_network_if_unused()
        out = Path(ctx.workdir) / self.output(ctx)
        if out.exists():
            out.unlink()
            removed.append(f"file:{self.output(ctx)}")
        return ctx.result(evidence=[evidence("removed", True, "cleanup", ", ".join(removed) or "nothing left")], state={})


Plugin = DockerComposePlugin
