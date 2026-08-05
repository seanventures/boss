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

import os
import urllib.error
import urllib.request
from typing import Any, Optional, Protocol

from ..config import Settings


class DeploymentConnector(Protocol):
    kind: str

    def validate(self, target: dict, settings: Settings) -> list[str]: ...
    def plan(self, engine, settings: Settings, target: dict, run_id: str) -> dict: ...
    def apply(self, engine, settings: Settings, target: dict, run_id: str,
              plan: dict) -> dict: ...
    def observe(self, target: dict, settings: Settings) -> dict: ...
    def rollback(self, engine, settings: Settings, target: dict,
                 to: dict) -> dict: ...


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

    # -- validate ---------------------------------------------------------------
    def validate(self, target: dict, settings: Settings) -> list[str]:
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

        key_path = (getattr(settings, "ssh_host_private_key_file", "") or "").strip()
        key_material = (getattr(settings, "ssh_host_private_key", "") or "").strip()
        if key_path:
            expanded = os.path.expanduser(key_path)
            if not os.path.isfile(expanded):
                problems.append(f"ssh key file {key_path} does not exist")
            elif os.stat(expanded).st_mode & 0o077:
                problems.append(f"ssh key {key_path} is group/world readable; ssh will refuse it")
        elif not key_material:
            problems.append(
                "no ssh key configured (BOSS_SSH_HOST_PRIVATE_KEY_FILE or _KEY)")

        if not bool(getattr(settings, "ssh_host_deploy_allowed", False)):
            problems.append(
                "outward SSH deploys are disabled (BOSS_SSH_HOST_DEPLOY_ALLOWED is false)")

        if not (target.get("domains") or []):
            problems.append("target has no domain, so health cannot be observed")
        if not (target.get("health_checks") or []):
            problems.append("target declares no health_checks; observe() would have no verdict")
        return problems

    # -- plan -------------------------------------------------------------------
    def plan(self, engine, settings: Settings, target: dict, run_id: str) -> dict:
        """Read-only. An operator must be able to see what a promotion would do."""
        from .deploy_service import plan_ssh_deploy

        user, host, _ = _split_ref(target.get("target_ref", ""))
        plan = plan_ssh_deploy(engine, settings, run_id, host=host, user=user,
                               name=target.get("app_name"))
        plan["connector"] = self.kind
        plan["domains"] = target.get("domains") or []
        return plan

    # -- apply ------------------------------------------------------------------
    def apply(self, engine, settings: Settings, target: dict, run_id: str,
              plan: dict) -> dict:
        """Place the artifact. Refuses if validate() is unhappy — a plan built on a broken
        target is not worth executing, and finding out mid-transfer is worse."""
        problems = self.validate(target, settings)
        if problems:
            return {"status": "blocked", "problems": problems}

        from .deploy_service import deploy_ssh

        user, host, port = _split_ref(target.get("target_ref", ""))
        result = deploy_ssh(engine, settings, run_id, host=host, user=user, ssh_port=port,
                            name=target.get("app_name"))
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
            url = f"https://{domains[0]}{check.get('path', '/')}"
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


def preflight(target: dict, settings: Settings) -> dict[str, Any]:
    """Is this target deployable right now? Cheap, read-only, and safe to call from a console.

    Returns `ready` plus the concrete list of what is missing — an operator should never have
    to run a deployment to discover a credential is absent.
    """
    conn = connector_for(target)
    if conn is None:
        return {"ready": False,
                "problems": [f"no connector for target_kind "
                             f"{target.get('target_kind')!r}; have {supported_kinds()}"]}
    problems = conn.validate(target, settings)
    return {"ready": not problems, "problems": problems, "connector": conn.kind}
