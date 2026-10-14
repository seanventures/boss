"""Smoke-test a deployment through its own target, not a free URL probe.

`smoke_test` called `http_probe` with a URL. Community refuses free URL probes (there is no
project-bound destination or network policy behind one), so every Community workflow with a
smoke test after its deploy dead-lettered with "use recorded deployment health" — and in
editions that did run it, "GET / answered 200" was the whole test. HelpDesk proved that is not a
test: nginx served the app's unbuilt index.html (it loads /src/main.tsx, which no browser can
run), / answered 200, and /readyz — the health the app itself declares — answered 404.

So the smoke test is the deployment's own health, asked of the deployment's own target:

  * the run's deployment and its target, read from the run and fenced to the project — the one
    destination a smoke test may knock on, because the deploy put the app there;
  * the target's recorded checks, PLUS the health the app declares in its boss.deploy.yaml
    (`health: [{url: "{target.public_url}/readyz"}]`), mapped onto the target's address;
  * probed by the connector's own `observe`, the same path the deploy's health gate uses;
  * and a page that serves unbuilt source (`<script src="/src/main.tsx">`) fails by name.

What it returns is http_probe's shape (`url`, `status`, `ms`) so `workflow_outcomes.smoke_test`
judges it, plus the checks it ran; a failure is `ok: False` with the reason in words.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from typing import Any, Optional
from urllib.parse import urlparse

from sqlalchemy import text
from sqlalchemy.engine import Connection

log = logging.getLogger(__name__)

MANIFEST_NAME = "boss.deploy.yaml"

# A module script pointing into the source tree: the dev server's index.html, not a build.
_UNBUILT = re.compile(
    r"<script[^>]+src=[\"']/?src/[^\"']+\.(?:tsx|ts|jsx|mts|vue|svelte)[\"']", re.IGNORECASE)


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def run_deployment(connection: Connection, run_id: str) -> Optional[dict[str, Any]]:
    """The deployment this workflow run made, newest first, or None."""
    if not run_id:
        return None
    rows = connection.execute(
        text("SELECT output FROM workflow_step_runs WHERE workflow_run_id = :r "
             "AND status = 'succeeded' ORDER BY completed_at DESC NULLS LAST, created_at DESC"),
        {"r": run_id},
    ).mappings().all()
    for row in rows:
        output = _json(row["output"]) or {}
        awaited = output.get("await") or {}
        # A DEPLOY FROM THE APP'S OWN MANIFEST is a stack deployment: the smoke test fell
        # through to a free http_probe (refused in Community) after every one of them.
        stack_id = (awaited.get("id") if awaited.get("kind") == "stack_deployment" else None) \
            or output.get("stack_deployment_id")
        if stack_id:
            sdep = connection.execute(
                text("SELECT id, status, target_id, host_id, manifest_text, open_url, "
                     "code_task_run_id FROM stack_deployments WHERE id = :d"),
                {"d": stack_id}).mappings().first()
            if sdep:
                return {"id": sdep["id"], "status": sdep["status"], "stack": True,
                        "deployment_target_id": sdep["target_id"], "host_id": sdep["host_id"],
                        "manifest_text": sdep["manifest_text"], "open_url": sdep["open_url"],
                        "release_packet": {"code_task_run_id": sdep["code_task_run_id"]}}
        dep_id = (awaited.get("id") if awaited.get("kind") == "deployment" else None) \
            or output.get("deployment_id")
        if not dep_id:
            continue
        dep = connection.execute(
            text("SELECT id, status, deployment_target_id, release_packet FROM deployment_runs "
                 "WHERE id = :d"), {"d": dep_id}).mappings().first()
        if dep:
            return dict(dep)
    return None


def _target(connection: Connection, project_id: str, target_id: str) -> Optional[dict]:
    row = connection.execute(
        text("SELECT * FROM deployment_targets WHERE id = :t AND project_id = :p"),
        {"t": target_id, "p": project_id}).mappings().first()
    if not row:
        return None
    target = dict(row)
    for key in ("health_checks", "domains", "required_resources", "secret_refs"):
        target[key] = _json(target.get(key)) or []
    return target


def declared_health(manifest_text: str) -> list[dict[str, Any]]:
    """The app's own `health:` entries as target-relative checks: path + expected status.

    A URL is reduced to its path: "{target.public_url}/readyz" and "http://127.0.0.1:8000/readyz"
    are both the app's /readyz, and the smoke test asks it at the target's public address."""
    import yaml

    try:
        data = yaml.safe_load(manifest_text) or {}
    except yaml.YAMLError:
        return []
    raw = data.get("health") if isinstance(data, dict) else None
    if isinstance(raw, (dict, str)):
        raw = [raw]
    checks: list[dict[str, Any]] = []
    for item in raw or []:
        if isinstance(item, str):
            item = {"url": item}
        if not isinstance(item, dict):
            continue
        url = re.sub(r"\{[^}]*\}", "http://placeholder", str(item.get("url") or "").strip())
        path = urlparse(url).path or "/"
        try:
            expect = int(item.get("expect", 200))
        except (TypeError, ValueError):
            expect = 200
        checks.append({"path": path, "expect_status": expect, "declared_by": MANIFEST_NAME})
    return checks


def _manifest_for(connection: Connection, settings: Any, engine: Any,
                  project_id: str, code_task_run_id: str) -> tuple[Optional[str], str]:
    """(boss.deploy.yaml text, where it came from) for the app this build made.

    The build's own files say which app it is: a boss.deploy.yaml among them (an app in a
    subdirectory) wins over one at the repository root."""
    run = connection.execute(
        text("SELECT * FROM code_task_runs WHERE id = :r AND project_id = :p"),
        {"r": code_task_run_id, "p": project_id}).mappings().first()
    if not run:
        return None, "the deployed build is not in this project"
    run = dict(run)
    for key in ("spec", "finalize_ctx", "state"):
        run[key] = _json(run.get(key)) or {}
    from .deploy_service import _resolve_repo_branch
    from .preview_service import lineage_impl_files
    from .stack_deploy import github_source_for

    owner, repo, branch = _resolve_repo_branch(run, settings)
    try:
        files = lineage_impl_files(engine, run) or []
    except Exception:  # noqa: BLE001 — the root manifest is still worth reading
        files = []
    candidates = sorted({f for f in files if f.rsplit("/", 1)[-1] == MANIFEST_NAME},
                        key=lambda f: (-f.count("/"), f))
    candidates.append(MANIFEST_NAME)
    source = github_source_for(connection, settings, project_id, owner, repo)
    for path in dict.fromkeys(candidates):
        try:
            body = source.read_file(owner, repo, path, branch)
        except Exception as exc:  # noqa: BLE001 — said, not raised
            return None, f"could not read {path} from {owner}/{repo}@{branch}: {exc}"[:200]
        if body:
            return body, f"{owner}/{repo}@{branch}:{path}"
    return None, f"no {MANIFEST_NAME} in {owner}/{repo}@{branch}"


def _root_body(url: str) -> tuple[Optional[int], str]:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"),
                                    timeout=15) as resp:
            return resp.status, resp.read(20000).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception:  # noqa: BLE001 — observe() reports unreachable with its diagnosis
        return None, ""


def smoke_deployment(connection: Connection, settings: Any, engine: Any, *,
                     project_id: str, run_id: str,
                     manifest_text: Optional[str] = None,
                     connector: Any = None) -> dict[str, Any]:
    """Probe this run's deployment through its own target. Never raises; always says why."""
    from .deployment_connectors import _app_port, _health_url, connector_for

    dep = run_deployment(connection, run_id)
    if dep is None:
        return {"ok": False, "error": "this run made no deployment, so there is nothing to "
                                      "smoke-test: a smoke test checks the app this run shipped, "
                                      "at the address its deploy put it."}
    target = _target(connection, project_id, str(dep["deployment_target_id"] or ""))
    if target is None:
        return {"ok": False, "error": f"deployment {dep['id']} has no target in this project"}
    if dep.get("stack"):
        return _smoke_stack(connection, dep, target,
                            manifest_text if manifest_text is not None else dep.get("manifest_text"))
    domains = target.get("domains") or []
    if not domains:
        return {"ok": False, "error": f"{target.get('app_name')} has no address to test: its "
                                      "target declares no domain"}

    checks: list[dict[str, Any]] = [dict(c) for c in target.get("health_checks") or []
                                    if isinstance(c, dict)]
    declared_from = ""
    if manifest_text is None:
        packet = _json(dep.get("release_packet")) or {}
        crun = str(packet.get("code_task_run_id") or "")
        if crun:
            try:
                manifest_text, declared_from = _manifest_for(
                    connection, settings, engine, project_id, crun)
            except Exception as exc:  # noqa: BLE001
                declared_from = f"could not read the app's {MANIFEST_NAME}: {exc}"[:200]
        else:
            declared_from = "the deployment names no build"
    declared = declared_health(manifest_text) if manifest_text else []
    port = _app_port(target, default=0) or None
    scheme = next((str(c.get("scheme")) for c in checks if c.get("scheme")), "http")
    seen = {(str(c.get("path") or "/"), int(c.get("expect_status") or c.get("expect") or 200))
            for c in checks}
    for check in declared:
        key = (check["path"], check["expect_status"])
        if key not in seen:
            seen.add(key)
            checks.append({"kind": "http", "scheme": scheme, "port": port, **check})

    conn = connector or connector_for(target)
    if conn is None:
        return {"ok": False, "error": f"no connector can reach {target.get('app_name')}'s target"}
    started = time.time()
    health = conn.observe({**target, "health_checks": checks}, settings)
    ms = round((time.time() - started) * 1000)
    root_url = _health_url(str(domains[0]), {"path": "/", "scheme": scheme}, port)
    base = {"url": root_url, "ms": ms, "checks": health.get("checks") or [],
            "declared_health": [c["path"] for c in declared],
            "declared_from": declared_from or ("given" if manifest_text else ""),
            "deployment_id": dep["id"], "deployment_target_id": target["id"],
            "probed_by": "the deployment target's own health check"}

    failing = [c for c in health.get("checks") or [] if c.get("ok") is False or c.get("error")]
    if health.get("healthy") is not True:
        if failing:
            said = "; ".join(
                f"{c.get('url') or c.get('socket')} "
                + (f"answered {c.get('status')}, expected {c.get('expected')}"
                   if c.get("status") is not None else f"did not answer ({c.get('error')})")
                for c in failing)
        else:
            said = health.get("reason") or "no check could be run"
        return {**base, "ok": False, "status": None,
                "error": f"{target.get('app_name')} is not healthy: {said}"}

    code, body = _root_body(root_url)
    if body and _UNBUILT.search(body):
        return {**base, "ok": False, "status": code,
                "error": f"{root_url} serves the app's unbuilt source (its page loads "
                         f"{_UNBUILT.search(body).group(0)[:80]}…): no browser can run it, so "
                         "the page is blank. The deploy shipped the source tree, not a build."}
    status = next((c.get("status") for c in health.get("checks") or []
                   if isinstance(c.get("status"), int)), code)
    return {**base, "status": status if isinstance(status, int) else 200}


def _smoke_stack(connection: Connection, dep: dict[str, Any], target: dict[str, Any],
                 manifest_text: Optional[str]) -> dict[str, Any]:
    """A stack deployment, smoked through its target: the health the deployed manifest declares
    (the very manifest_text it shipped with), asked at the target's own address — the one
    destination this run put the app at — plus the unbuilt-source check on its page."""
    from .deployment_fleet import get_host
    from .stack_deploy import target_address

    host = get_host(connection, str(dep.get("host_id") or target.get("host_id") or "")) or {}
    if not host.get("address") and not target.get("public_url") and not target.get("domains"):
        return {"ok": False, "error": f"{target.get('app_name')} has no address to test: its "
                                      "target has no host, domain or public URL"}
    base = target_address(target, host).rstrip("/")
    declared = declared_health(manifest_text) if manifest_text else []
    checks = declared or [{"path": "/", "expect_status": 200, "declared_by": "default"}]
    started = time.time()
    ran: list[dict[str, Any]] = []
    for check in checks:
        url = base + (check["path"] if check["path"].startswith("/") else "/" + check["path"])
        code, _body = _root_body(url)
        ran.append({"url": url, "status": code, "expected": check["expect_status"],
                    "ok": code == check["expect_status"]})
    ms = round((time.time() - started) * 1000)
    out = {"url": base + "/", "ms": ms, "checks": ran,
           "declared_health": [c["path"] for c in declared],
           "declared_from": "the deployed boss.deploy.yaml" if declared else "",
           "deployment_id": dep["id"], "stack_deployment_id": dep["id"],
           "deployment_target_id": target["id"],
           "probed_by": "the stack target's declared health"}
    failing = [c for c in ran if not c["ok"]]
    if failing:
        said = "; ".join(f"{c['url']} " + (f"answered {c['status']}, expected {c['expected']}"
                                           if c["status"] is not None else "did not answer")
                         for c in failing)
        return {**out, "ok": False, "status": None,
                "error": f"{target.get('app_name')} is not healthy: {said}"}
    code, body = _root_body(base + "/")
    if body and _UNBUILT.search(body):
        return {**out, "ok": False, "status": code,
                "error": f"{base}/ serves the app's unbuilt source (its page loads "
                         f"{_UNBUILT.search(body).group(0)[:80]}…): no browser can run it."}
    return {**out, "status": next((c["status"] for c in ran if isinstance(c["status"], int)), 200)}
