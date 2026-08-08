"""Deployment connectors — one pipeline, many destinations.

Design: docs/product/DEPLOYMENT_CONNECTORS_DESIGN.md

Every connector implements the same five verbs and nothing else in BOSS knows which one is in
use. Branching per destination inside the deploy path is the defect this codebase keeps
paying for — the preview resolver had one decision copied into two paths and served the wrong
app; the failure taxonomy was written twice; test-oracle logic was inferred two ways. A rule
implemented per-path drifts per-path, and here it would drift across four clouds.

    validate(target, settings) -> list[str]   what is missing, BEFORE anything is attempted
    plan(target, run_id)       -> dict        what WOULD change; never mutates
    apply(target, run_id, plan)-> dict        place the artifact; idempotent by receipt
    observe(target)            -> dict        is it SERVING? from the destination, not exit 0
    rollback(target, to)       -> dict        return to a point recorded BEFORE apply

Only the SSH/container connector is implemented. Cloudflare, Kubernetes and Terraform are
designed but deliberately unbuilt: nine empty deployment tables are what happened last time
generalisation came before one working case.
"""
from __future__ import annotations

import json as _json
import os
import shlex
import urllib.error
import urllib.request
from typing import Any, Optional, Protocol

from ..config import Settings
from ..db import begin as begin_conn
from ..ports import sql as sql_text


# What a connector needs before it can place anything. Machine-readable rather than prose in a
# design doc, so `validate()` can check bindings generically instead of each connector
# hard-coding a look at two global settings — which is what made every SSH target share one key.
SecretRequirement = dict[str, Any]  # {"name", "kind", "required", "purpose"}


class DeploymentConnector(Protocol):
    kind: str

    def required_secrets(self) -> list[SecretRequirement]: ...
    def validate(self, target: dict, settings: Settings, connection=None) -> list[str]: ...
    def bootstrap(self, target: dict, settings: Settings,
                  connection=None) -> dict[str, Any]: ...
    def destroy(self, target: dict, settings: Settings, connection=None, *,
                include_data: bool = False, stop_only: bool = False) -> dict[str, Any]: ...
    def plan(self, engine, settings: Settings, target: dict, run_id: str,
             ref: Optional[str] = None) -> dict: ...
    def _ensure_port(self, engine, settings: Settings, target: dict,
                     connection=None) -> dict:
        """Give this target a port nothing else on the host is using, and remember it.

        The onboarding form says "blank = BOSS picks", and nothing did the picking: an undeclared
        port fell through to the 8080 default, which is occupied on the first real host and made
        `docker run` fail on external connectivity. A default is a guess about someone else's
        machine, and here it was a wrong one.

        Bootstrap already knows how to read the host's listeners and choose a free port; this
        reuses it rather than adding a second port-selection rule that could disagree.
        """
        # Two separate obligations, and conflating them hid the second: a target may already
        # KNOW its port while its domain still fails to name it. Returning early on the first
        # left the stored domain pointing at the host's default port — which on a shared machine
        # is another service entirely, so the console linked to somebody else's app.
        chosen = _app_port(target, default=0)
        if not chosen:
            report = self.bootstrap(target, settings, connection)
            chosen = report.get("suggested_port")
            if not chosen:
                return target

        resources = [r for r in (target.get("required_resources") or [])
                     if not (isinstance(r, dict) and r.get("kind") == "app_port")]
        resources.append({"kind": "app_port", "name": str(chosen)})

        from ..db import begin

        with begin(engine) as c:
            c.execute(sql_text(
                "UPDATE deployment_targets SET required_resources = CAST(:r AS JSONB), "
                "updated_at = now() WHERE id = :i"),
                {"r": _json.dumps(resources), "i": target.get("id")})
            # The domain must name the PORT the app is published on. A domain of "1.2.3.4"
            # with the app on :3010 sends the health check to port 80 — and on a shared host
            # that is somebody else's web server, which cheerfully returns 200. That is a
            # deployment reported healthy on the strength of an unrelated service: the exact
            # false-green this connector exists to prevent.
            #
            # So: fill it when absent AND correct it when it names no port.
            existing = (target.get("domains") or [None])[0]
            _u, host, _p = _split_ref(target.get("target_ref", ""))
            needs_port = (not existing) or (":" not in str(existing).split("/")[0])
            if needs_port:
                base = str(existing).split("/")[0] if existing else host
                fixed = f"{base}:{chosen}"
                c.execute(sql_text(
                    "UPDATE deployment_targets SET domains = CAST(:d AS JSONB) WHERE id = :i"),
                    {"d": _json.dumps([fixed]), "i": target.get("id")})
                target = {**target, "domains": [fixed]}

        return {**target, "required_resources": resources}

    def apply(self, engine, settings: Settings, target: dict, run_id: str,
              plan: dict, ref: Optional[str] = None) -> dict: ...
    def observe(self, target: dict, settings: Settings) -> dict: ...
    def rollback(self, engine, settings: Settings, target: dict,
                 to: dict) -> dict: ...


def _app_port(target: dict, default: int = 8080) -> int:
    """The host port the app is published on, declared by the target.

    A default of 8080 is a guess about someone else's machine. The first real target already
    had 8080 occupied by an unrelated service, so `docker run -p 8080:80` would have failed
    after the build and transfer were paid for. A target that shares a host with anything else
    must be able to SAY which port is its own.
    """
    for res in target.get("required_resources") or []:
        if isinstance(res, dict) and res.get("kind") == "app_port":
            try:
                return int(res.get("name") or res.get("value") or default)
            except (TypeError, ValueError):
                break
    return default


def _resource(target: dict, kind: str) -> Optional[str]:
    """Read a declared resource off the target — one accessor, so a new kind cannot be read
    two slightly different ways."""
    for res in target.get("required_resources") or []:
        if isinstance(res, dict) and res.get("kind") == kind:
            return str(res.get("name") or "") or None
    return None


def _app_entry(target: dict) -> Optional[str]:
    """Which file or directory IS this app, when there is no code task lineage to infer it from.

    A repo holding several apps cannot be guessed at — guessing is what once deployed Sudoku to
    a stock-app target — so a target in a shared repo has to say. Single-app repos can leave it
    unset and the resolver finds the only candidate.
    """
    return _resource(target, "app_entry")


def _health_url(domain: str, check: dict, app_port: Optional[int] = None) -> str:
    """Where observe() looks. `scheme` is explicit, never inferred from the shape of a domain.

    Inferring it (e.g. "has a port, so http") is the kind of rule that is invisible until it is
    wrong. A staging box reached by IP is plain http; a real domain is https; the target says
    which rather than the code guessing.
    """
    scheme = str(check.get("scheme") or "https").lower()
    # A domain with no port, for an app published on one, checks the host's default port —
    # which on a shared machine belongs to someone else and answers 200. Belt and braces with
    # _ensure_port: this catches targets whose domain was set by hand.
    host_part = str(domain).split("/")[0]
    if app_port and ":" not in host_part and scheme == "http":
        domain = f"{host_part}:{app_port}"
    return f"{scheme}://{domain}{check.get('path', '/')}"


def _split_ref(target_ref: str) -> tuple[str, str, int]:
    """`user@host:port` in any of its shorter forms -> (user, host, port)."""
    ref = (target_ref or "").strip()
    user = "root"
    if "@" in ref:
        user, ref = ref.split("@", 1)
    port = 22
    if ":" in ref:
        ref, raw = ref.rsplit(":", 1)
        if raw.isdigit():
            port = int(raw)
    return user, ref, port


class SSHContainerConnector:
    """Place a built app on a Linux host over SSH.

    Wraps the existing deploy_service rather than reimplementing it: that module already has
    build/transfer/nginx/health-gate/rollback and is the thing that has been reasoned about.
    This adds the contract, not the mechanism.
    """

    kind = "ssh"

    def required_secrets(self) -> list[SecretRequirement]:
        return [{"name": "ssh_key", "kind": "ssh_private_key", "required": True,
                 "purpose": "authenticate to the host as the deploy user"}]

    def _credential_problems(self, target: dict, settings: Settings,
                             connection=None) -> list[str]:
        """Can this target's declared secrets actually be obtained?

        Resolution order, deliberately explicit:
          1. the target's own binding (a managed secret_ref, or a legacy env-var indirection)
          2. the global BOSS_SSH_HOST_PRIVATE_KEY_FILE / _KEY, so targets predating per-target
             credentials keep deploying while the console migrates them

        Reported as problems rather than raised, because the whole point of validate() is that a
        missing credential costs a query instead of a build and a transfer.
        """
        from .secret_backends import SecretUnavailable, resolve_target_secret

        for req in self.required_secrets():
            name = req["name"]
            if connection is not None:
                try:
                    if resolve_target_secret(connection, target, name, settings):
                        continue
                except SecretUnavailable as exc:
                    return [f"secret {name!r} is bound but unusable: {exc}"]
            elif any(isinstance(b, dict) and b.get("name") == name
                     for b in (target.get("secret_refs") or [])):
                # No connection to resolve with; a binding exists, so defer to deploy time
                # rather than reporting a problem we cannot actually check.
                continue

            if not req.get("required", True):
                continue

            key_path = (getattr(settings, "ssh_host_private_key_file", "") or "").strip()
            key_material = (getattr(settings, "ssh_host_private_key", "") or "").strip()
            if key_path:
                expanded = os.path.expanduser(key_path)
                if not os.path.isfile(expanded):
                    return [f"ssh key file {key_path} does not exist"]
                if os.stat(expanded).st_mode & 0o077:
                    return [f"ssh key {key_path} is group/world readable; ssh will refuse it"]
            elif not key_material:
                return [f"no {name} bound to this target and no global ssh key configured "
                        f"(bind a secret to the target, or set "
                        f"BOSS_SSH_HOST_PRIVATE_KEY_FILE)"]
        return []

    # -- validate ---------------------------------------------------------------
    def validate(self, target: dict, settings: Settings, connection=None) -> list[str]:
        """Everything that would make apply() fail, found up front and for free.

        This exists because `provider_account_id` surfaced only at execution time during the
        first end-to-end run — after the work of getting there. A missing credential must cost
        a query, not a build.
        """
        problems: list[str] = []
        user, host, _port = _split_ref(target.get("target_ref", ""))
        if not host:
            problems.append("target_ref has no host (expected user@host)")
        if not user:
            problems.append("target_ref has no user")

        # The credential THIS target is bound to, resolved from its backend. Checking a global
        # setting instead is what forced every SSH target to share one key; a binding that is
        # recorded but never resolved is not a binding.
        problems.extend(self._credential_problems(target, settings, connection))

        if not bool(getattr(settings, "ssh_host_deploy_allowed", False)):
            problems.append(
                "outward SSH deploys are disabled (BOSS_SSH_HOST_DEPLOY_ALLOWED is false)")

        if not (target.get("domains") or []):
            problems.append("target has no domain, so health cannot be observed")
        if not (target.get("health_checks") or []):
            problems.append("target declares no health_checks; observe() would have no verdict")

        # Local checks passed; now ask the HOST whether it can actually receive a deployment.
        # Skipped when anything above already failed, so a broken config costs no round trip.
        if not problems:
            problems.extend(self._host_problems(target, settings, connection))
        return problems

    # Prerequisites that only the destination can answer. These were discovered the expensive
    # way: the first real deployment built the app, transferred it, and only then failed on
    # `mkdir /opt/boss-apps: Permission denied`. A precondition the host can be ASKED about
    # must not be discovered by attempting the deployment.
    def _host_problems(self, target: dict, settings: Settings,
                       connection=None) -> list[str]:
        from .deploy_service import REMOTE_BASE, _app_name, _ssh_key_file

        # The directory apply() actually writes, derived the SAME way apply() derives it. A
        # preflight that checks the parent while the deploy writes the child is a check that
        # passes and a deploy that fails — which is exactly what happened here: /opt/boss-apps
        # was writable, /opt/boss-apps/stock-app was root-owned from an earlier attempt.
        app_dir = f"{REMOTE_BASE}/{_app_name(target.get('repo') or '', target.get('app_name'))}"

        # Probe with the credential this target will ACTUALLY deploy with. Probing with the
        # global key while deploying with a bound one would make preflight a test of the wrong
        # thing — green here, refused by the host later.
        from .deploy_service import _discard_key_file, _materialise_key
        from .secret_backends import SecretUnavailable, resolve_target_secret

        ephemeral = None
        try:
            material = (resolve_target_secret(connection, target, "ssh_key", settings)
                        if connection is not None else None)
            if material:
                key = ephemeral = _materialise_key(material)
            else:
                key = _ssh_key_file(settings)
        except (SecretUnavailable, Exception) as exc:  # noqa: BLE001 — a problem, never a crash
            return [f"ssh key unusable: {exc}"]

        try:
            return self._probe_host(key, target, app_dir)
        finally:
            if ephemeral:
                _discard_key_file(ephemeral)

    def _probe_host(self, key: str, target: dict, app_dir: str) -> list[str]:
        from .deploy_service import REMOTE_BASE, _ssh

        user, host, port = _split_ref(target.get("target_ref", ""))

        # One round trip answers everything: reachability, the deploy dir, and the runtime.
        # An absent app dir is fine — apply() creates it. An existing one BOSS cannot write is
        # not, so "missing" and "unwritable" must not collapse into the same answer.
        probe = (f"test -w {REMOTE_BASE} && echo BASE_OK || echo BASE_NO; "
                 f"{{ test ! -e {app_dir} || test -w {app_dir}; }} && echo APP_OK || echo APP_NO; "
                 f"command -v docker >/dev/null && echo DOCKER_OK || echo DOCKER_NO")
        try:
            res = _ssh(key, host, user, port, probe, timeout=25)
        except Exception as exc:  # noqa: BLE001
            return [f"host {host} unreachable over ssh: {type(exc).__name__}: {exc}"[:200]]

        if res.returncode != 0:
            return [f"ssh to {user}@{host} failed: "
                    f"{(res.stderr or res.stdout or '').strip()[:200]}"]

        out = res.stdout or ""
        problems: list[str] = []
        if "BASE_OK" not in out:
            # Actionable on purpose: an operator should not have to read our source to learn
            # what to grant. This is host provisioning, like installing docker — the operator
            # prepares the destination, BOSS performs the deployment.
            problems.append(
                f"{REMOTE_BASE} is not writable by {user} on {host}; prepare the host once: "
                f"sudo mkdir -p {REMOTE_BASE} && sudo chown {user} {REMOTE_BASE}")
        if "APP_NO" in out:
            problems.append(
                f"{app_dir} exists on {host} but is not writable by {user}; "
                f"fix once: sudo chown -R {user} {app_dir}")
        if "DOCKER_OK" not in out:
            problems.append(f"docker is not on PATH for {user}@{host}")
        return problems

    # -- bootstrap --------------------------------------------------------------
    def bootstrap(self, target: dict, settings: Settings, connection=None) -> dict[str, Any]:
        """Prepare the host so a deployment can succeed, and choose a free port.

        These are the steps a human ran over SSH to onboard the first two apps: create the
        deploy directory, confirm docker, and hunt for a port nothing else was using. The port
        hunt is the one that actually bit — the connector defaulted to 8080, which another
        service already held, and the deployment failed after the build was paid for.

        Idempotent and non-destructive: it creates and verifies. It never removes anything,
        because on a shared host the things it would remove belong to someone else.
        """
        from .deploy_service import (REMOTE_BASE, _app_name, _discard_key_file,
                                     _materialise_key, _ssh, _ssh_key_file)
        from .secret_backends import SecretUnavailable, resolve_target_secret

        user, host, port = _split_ref(target.get("target_ref", ""))
        app_dir = f"{REMOTE_BASE}/{_app_name(target.get('repo') or '', target.get('app_name'))}"
        ephemeral = None
        try:
            material = (resolve_target_secret(connection, target, "ssh_key", settings)
                        if connection is not None else None)
            key = ephemeral = _materialise_key(material) if material else _ssh_key_file(settings)
        except (SecretUnavailable, Exception) as exc:  # noqa: BLE001
            return {"ok": False, "problems": [f"ssh key unusable: {exc}"], "changed": []}

        try:
            # `sudo -n` so a host without passwordless sudo fails fast with an instruction
            # rather than hanging on a password prompt nobody can answer.
            script = (
                f"set -e; "
                f"if [ ! -d {REMOTE_BASE} ] || [ ! -w {REMOTE_BASE} ]; then "
                f"  sudo -n mkdir -p {REMOTE_BASE} && sudo -n chown {user} {REMOTE_BASE} "
                f"    && echo CREATED_BASE || echo NEED_SUDO; "
                f"else echo BASE_OK; fi; "
                f"mkdir -p {app_dir}/data 2>/dev/null && echo APP_OK || echo APP_NO; "
                f"command -v docker >/dev/null && echo DOCKER_OK || echo DOCKER_NO; "
                # Ports this app's OWN container publishes. A live target legitimately holds
                # its port, and counting that as a conflict would move a healthy app to a new
                # port every time someone re-ran bootstrap.
                f"echo OWN_BEGIN; "
                f"docker port {shlex.quote(_app_name(target.get('repo') or '', target.get('app_name')))} "
                f"  2>/dev/null | sed 's/.*://' | sort -n | uniq; "
                f"echo PORTS_BEGIN; "
                f"(ss -lntH 2>/dev/null || netstat -lnt 2>/dev/null) "
                f"  | awk '{{print $4}}' | sed 's/.*://' | sort -n | uniq")
            res = _ssh(key, host, user, port, script, timeout=45)
        finally:
            if ephemeral:
                _discard_key_file(ephemeral)

        if res.returncode != 0:
            return {"ok": False, "changed": [],
                    "problems": [f"ssh to {user}@{host} failed: "
                                 f"{(res.stderr or res.stdout or '').strip()[:200]}"]}

        out = res.stdout or ""
        changed, problems = [], []
        if "CREATED_BASE" in out:
            changed.append(f"created {REMOTE_BASE} owned by {user}")
        if "NEED_SUDO" in out:
            problems.append(
                f"{REMOTE_BASE} is missing and passwordless sudo is unavailable; run once: "
                f"sudo mkdir -p {REMOTE_BASE} && sudo chown {user} {REMOTE_BASE}")
        if "APP_NO" in out:
            problems.append(f"cannot create {app_dir} as {user}")
        elif "APP_OK" in out:
            changed.append(f"ensured {app_dir}/data")
        if "DOCKER_OK" not in out:
            problems.append(f"docker is not on PATH for {user}@{host}")

        def _ports(section: str) -> set[int]:
            if section not in out:
                return set()
            body = out.split(section, 1)[1].split("PORTS_BEGIN", 1)[0] \
                if section == "OWN_BEGIN" else out.split(section, 1)[1]
            return {int(t) for t in (ln.strip() for ln in body.splitlines()) if t.isdigit()}

        own = _ports("OWN_BEGIN")
        # This app's own published ports are not conflicts — they are the status quo.
        taken = _ports("PORTS_BEGIN") - own

        chosen = _app_port(target, default=0) or None
        if chosen and chosen in taken:
            problems.append(f"port {chosen} is already in use on {host}")
            chosen = None
        if not chosen:
            # A high, unremarkable range: low ports collide with the services a shared host
            # already runs, which is precisely how the 8080 collision happened.
            chosen = next((p for p in range(3010, 3200) if p not in taken), None)
            if chosen:
                changed.append(f"selected free port {chosen}")

        return {"ok": not problems, "problems": problems, "changed": changed,
                "app_dir": app_dir, "suggested_port": chosen,
                "ports_in_use": sorted(taken)[:40]}

    # -- teardown ---------------------------------------------------------------
    def destroy(self, target: dict, settings: Settings, connection=None, *,
                include_data: bool = False, stop_only: bool = False) -> dict[str, Any]:
        """Remove a deployed app from its host, and report the space reclaimed.

        Three levels, because "remove the app" means different things at different moments:

          stop_only          the container goes, the files stay. Reversible by a redeploy, and
                             the right answer for "take it offline for now".
          destroy            container + release directories (dist, dist.prev). The app is gone;
                             its DATA survives.
          include_data       also the data directory. This is the only irreversible one, so it
                             is never implied — a caller has to ask for it by name.

        OWNERSHIP IS VERIFIED BEFORE ANYTHING IS REMOVED. This host also runs unrelated
        production services, and a teardown that trusted a name would delete somebody else's
        container the first time two apps were called the same thing. A container qualifies only
        if it carries BOSS's label or mounts this app's directory; a path qualifies only if it
        sits under REMOTE_BASE. Anything else is reported as a refusal, not removed.
        """
        from .deploy_service import (REMOTE_BASE, _app_name, _discard_key_file,
                                     _materialise_key, _rm_rf_cmd, _ssh, _ssh_key_file)
        from .secret_backends import SecretUnavailable, resolve_target_secret

        user, host, port = _split_ref(target.get("target_ref", ""))
        app = _app_name(target.get("repo") or "", target.get("app_name"))
        remote = f"{REMOTE_BASE}/{app}"

        # Belt and braces: `app` is already sanitised by _app_name, but this is the one code
        # path that deletes, so the guard is worth stating rather than assuming.
        if not app or "/" in app or ".." in app:
            return {"ok": False, "removed": [], "refused": [f"unsafe app name {app!r}"]}

        ephemeral = None
        try:
            material = (resolve_target_secret(connection, target, "ssh_key", settings)
                        if connection is not None else None)
            key = ephemeral = _materialise_key(material) if material else _ssh_key_file(settings)
        except (SecretUnavailable, Exception) as exc:  # noqa: BLE001
            return {"ok": False, "removed": [], "refused": [f"ssh key unusable: {exc}"]}

        app_q = shlex.quote(app)
        remote_q = shlex.quote(remote)
        try:
            # Establish ownership and measure BEFORE touching anything, so the report can say
            # what was reclaimed and the refusal path costs nothing.
            probe = (
                f"echo OWNER=$(docker inspect -f '{{{{index .Config.Labels \"boss.app\"}}}}' "
                f"  {app_q} 2>/dev/null); "
                f"echo MOUNTED=$(docker inspect -f '{{{{range .Mounts}}}}{{{{.Source}}}} {{{{end}}}}' "
                f"  {app_q} 2>/dev/null | grep -c {remote_q} || true); "
                f"echo EXISTS=$(docker inspect {app_q} >/dev/null 2>&1 && echo yes || echo no); "
                f"echo SIZE=$(du -sk {remote_q} 2>/dev/null | cut -f1); "
                f"echo DATASIZE=$(du -sk {remote_q}/data 2>/dev/null | cut -f1)")
            pre = _ssh(key, host, user, port, probe, timeout=30)
            if pre.returncode != 0:
                return {"ok": False, "removed": [],
                        "refused": [f"ssh to {user}@{host} failed: "
                                    f"{(pre.stderr or '').strip()[:200]}"]}

            info = dict(line.split("=", 1) for line in (pre.stdout or "").splitlines()
                        if "=" in line)
            exists = info.get("EXISTS") == "yes"
            owned = info.get("OWNER") == app or info.get("MOUNTED", "0").strip() not in ("", "0")
            size_kb = int(info.get("SIZE") or 0)
            data_kb = int(info.get("DATASIZE") or 0)

            if exists and not owned:
                # The decisive refusal: a container with the right name that BOSS did not
                # deploy. Removing it would be destroying a stranger's service.
                return {"ok": False, "removed": [],
                        "refused": [f"a container named {app} exists on {host} but is not "
                                    f"BOSS-managed (no boss.app label, and it does not mount "
                                    f"{remote}); refusing to remove it"]}

            steps = [f"docker rm -f {app_q} >/dev/null 2>&1 || true"]
            removed = [f"container {app}"] if exists else []
            reclaimed_kb = 0

            if not stop_only:
                # Only ever paths UNDER this app's directory, never the directory of another.
                steps.append(_rm_rf_cmd(f"{remote}/dist"))
                steps.append(_rm_rf_cmd(f"{remote}/dist.prev"))
                removed.append(f"{remote}/dist (+dist.prev)")
                reclaimed_kb = max(0, size_kb - data_kb)
                if include_data:
                    steps.append(_rm_rf_cmd(f"{remote}/data"))
                    steps.append(_rm_rf_cmd(remote))
                    removed.append(f"{remote} including data")
                    reclaimed_kb = size_kb

            res = _ssh(key, host, user, port, "; ".join(steps), timeout=120)
            after = _ssh(key, host, user, port,
                         f"df -Pk / | tail -1 | awk '{{print $4}}'", timeout=20)
        finally:
            if ephemeral:
                _discard_key_file(ephemeral)

        return {
            "ok": res.returncode == 0,
            "app": app, "host": host, "stop_only": stop_only, "data_removed": include_data,
            "removed": removed, "refused": [],
            "reclaimed_kb": reclaimed_kb,
            "data_kept_kb": 0 if include_data else data_kb,
            "host_free_kb": int((after.stdout or "0").strip() or 0),
            "detail": (res.stderr or "").strip()[:300] or None,
        }

    # -- plan -------------------------------------------------------------------
    def _source_for(self, target: dict, settings: Settings, run_id: str,
                    ref: Optional[str] = None) -> Optional[dict]:
        """Where the build comes from when there is no code task run. ONE definition, used by
        both plan() and apply() — two would drift, which is how plan() kept demanding a run
        after apply() had stopped needing one."""
        if run_id:
            return None
        return {"owner": (_resource(target, "repo_owner")
                          or getattr(settings, "target_github_owner", "")),
                "repo": target.get("repo"), "ref": ref or "main",
                "project_id": target.get("project_id"),
                "app_entry": _app_entry(target)}

    def plan(self, engine, settings: Settings, target: dict, run_id: str,
             ref: Optional[str] = None) -> dict:
        """Read-only. An operator must be able to see what a promotion would do."""
        from .deploy_service import plan_ssh_deploy

        user, host, _ = _split_ref(target.get("target_ref", ""))
        plan = plan_ssh_deploy(engine, settings, run_id, host=host, user=user,
                               name=target.get("app_name"),
                               source=self._source_for(target, settings, run_id, ref))
        plan["connector"] = self.kind
        plan["domains"] = target.get("domains") or []
        return plan

    # -- apply ------------------------------------------------------------------
    def _ensure_port(self, engine, settings: Settings, target: dict,
                     connection=None) -> dict:
        """Give this target a port nothing else on the host is using, and remember it.

        The onboarding form says "blank = BOSS picks", and nothing did the picking: an undeclared
        port fell through to the 8080 default, which is occupied on the first real host and made
        `docker run` fail on external connectivity. A default is a guess about someone else's
        machine, and here it was a wrong one.

        Bootstrap already knows how to read the host's listeners and choose a free port; this
        reuses it rather than adding a second port-selection rule that could disagree.
        """
        # Two separate obligations, and conflating them hid the second: a target may already
        # KNOW its port while its domain still fails to name it. Returning early on the first
        # left the stored domain pointing at the host's default port — which on a shared machine
        # is another service entirely, so the console linked to somebody else's app.
        chosen = _app_port(target, default=0)
        if not chosen:
            report = self.bootstrap(target, settings, connection)
            chosen = report.get("suggested_port")
            if not chosen:
                return target

        resources = [r for r in (target.get("required_resources") or [])
                     if not (isinstance(r, dict) and r.get("kind") == "app_port")]
        resources.append({"kind": "app_port", "name": str(chosen)})

        from ..db import begin

        with begin(engine) as c:
            c.execute(sql_text(
                "UPDATE deployment_targets SET required_resources = CAST(:r AS JSONB), "
                "updated_at = now() WHERE id = :i"),
                {"r": _json.dumps(resources), "i": target.get("id")})
            # The domain must name the PORT the app is published on. A domain of "1.2.3.4"
            # with the app on :3010 sends the health check to port 80 — and on a shared host
            # that is somebody else's web server, which cheerfully returns 200. That is a
            # deployment reported healthy on the strength of an unrelated service: the exact
            # false-green this connector exists to prevent.
            #
            # So: fill it when absent AND correct it when it names no port.
            existing = (target.get("domains") or [None])[0]
            _u, host, _p = _split_ref(target.get("target_ref", ""))
            needs_port = (not existing) or (":" not in str(existing).split("/")[0])
            if needs_port:
                base = str(existing).split("/")[0] if existing else host
                fixed = f"{base}:{chosen}"
                c.execute(sql_text(
                    "UPDATE deployment_targets SET domains = CAST(:d AS JSONB) WHERE id = :i"),
                    {"d": _json.dumps([fixed]), "i": target.get("id")})
                target = {**target, "domains": [fixed]}

        return {**target, "required_resources": resources}

    def apply(self, engine, settings: Settings, target: dict, run_id: str,
              plan: dict, ref: Optional[str] = None) -> dict:
        """Place the artifact. Refuses if validate() is unhappy — a plan built on a broken
        target is not worth executing, and finding out mid-transfer is worse.

        `ref` is the commit the DEPLOYMENT asked for and outranks the code task's branch. The
        two disagree as soon as the PR merges, and deploying the branch while recording the ref
        made the console report a build that was not the one running.
        """
        from ..db import begin

        with begin(engine) as c:
            problems = self.validate(target, settings, c)
        if problems:
            return {"status": "blocked", "problems": problems}

        # Resolved at the moment of use and handed straight to the deploy; never
        # stored, logged, or written into the run record.
        from .secret_backends import resolve_target_secret

        with begin(engine) as c:
            key_material = resolve_target_secret(c, target, "ssh_key", settings)

        from .deploy_service import deploy_ssh

        user, host, port = _split_ref(target.get("target_ref", ""))
        # No code task run? Build straight from the target's repo at `ref`. The target knows
        # its own repo and (optionally) which entrypoint is its app, which is exactly the
        # information a run would otherwise have carried.
        # Resolve a port BEFORE building: discovering "8080 is taken" after a clone and a
        # transfer wastes the whole build, which is the failure preflight exists to prevent.
        with begin_conn(engine) as _c:
            target = self._ensure_port(engine, settings, target, _c)

        source = self._source_for(target, settings, run_id, ref)
        result = deploy_ssh(engine, settings, run_id, host=host, user=user, ssh_port=port,
                            app_port=_app_port(target), name=target.get("app_name"), ref=ref,
                            key_material=key_material, source=source)
        result["connector"] = self.kind
        return result

    # -- observe ----------------------------------------------------------------
    def observe(self, target: dict, settings: Settings) -> dict:
        """Ask the DESTINATION whether it is serving.

        Deliberately not derived from apply()'s exit status: a process that started is not a
        service that works, and a canary that trusts an exit code will promote a broken build.
        """
        checks = target.get("health_checks") or []
        domains = target.get("domains") or []
        if not checks or not domains:
            return {"healthy": None, "reason": "no health check or domain declared"}

        results = []
        healthy = True
        for check in checks:
            if check.get("kind") != "http":
                results.append({"check": check, "skipped": "unsupported kind"})
                continue
            url = _health_url(domains[0], check, _app_port(target, default=0) or None)
            expect = int(check.get("expect", 200))
            try:
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=15) as resp:
                    code = resp.status
                ok = code == expect
            except urllib.error.HTTPError as exc:
                code, ok = exc.code, exc.code == expect
            except Exception as exc:  # noqa: BLE001 — unreachable IS the answer, not an error
                code, ok = None, False
                results.append({"url": url, "error": f"{type(exc).__name__}: {exc}"[:120]})
                healthy = False
                continue
            healthy = healthy and ok
            results.append({"url": url, "status": code, "expected": expect, "ok": ok})
        return {"healthy": healthy, "checks": results}

    # -- rollback ---------------------------------------------------------------
    def rollback(self, engine, settings: Settings, target: dict, to: dict) -> dict:
        """Return to a point recorded BEFORE apply.

        Rollback points are never derived after the fact: when the change is what broke the
        system, anything computed from the current state is guesswork.
        """
        from .deploy_service import _rollback_remote, _ssh_key_file

        if not to or not to.get("remote"):
            return {"status": "unavailable",
                    "reason": "no rollback point was recorded before this deployment"}
        user, host, port = _split_ref(target.get("target_ref", ""))
        try:
            _rollback_remote(_ssh_key_file(settings), host, user, port,
                             remote=to["remote"])
            return {"status": "rolled_back", "to": to}
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:200]}


# The registry. `target_kind` selects an implementation; adding a connector is a registry
# entry, never a branch in the pipeline.
_CONNECTORS: dict[str, DeploymentConnector] = {
    "ssh": SSHContainerConnector(),
    "container": SSHContainerConnector(),
}


def connector_for(target: dict) -> Optional[DeploymentConnector]:
    kind = str(target.get("target_kind") or "").lower().replace("-", "_")
    return _CONNECTORS.get(kind)


def supported_kinds() -> list[str]:
    return sorted(_CONNECTORS)


def preflight(target: dict, settings: Settings, connection=None) -> dict[str, Any]:
    """Is this target deployable right now? Cheap, read-only, and safe to call from a console.

    Returns `ready` plus the concrete list of what is missing — an operator should never have
    to run a deployment to discover a credential is absent.
    """
    conn = connector_for(target)
    if conn is None:
        return {"ready": False,
                "problems": [f"no connector for target_kind "
                             f"{target.get('target_kind')!r}; have {supported_kinds()}"]}
    problems = conn.validate(target, settings, connection)
    return {"ready": not problems, "problems": problems, "connector": conn.kind,
            "required_secrets": conn.required_secrets()}
