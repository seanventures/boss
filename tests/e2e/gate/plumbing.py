"""THE PLUMBING GATE (tier A of the end-to-end release gate). No model is called.

Runs BOSS's real post-agent pipeline on a known-good app (tests/e2e/fixtures/helpdesk-mini), the
way a build runs it, and fails on the class of bugs that broke HelpDesk builds on 2026-10-13:

  1. hardening that breaks the app's own `npm install` (eslint-8 plugins into an eslint-9 app);
  2. a permission pass that strips execute from node_modules/.bin targets (`tsx: Permission denied`);
  3. an app-start proof that does not supply the secrets BOSS generates (SESSION_SECRET);
  4. a GitHub 502 that fails a whole build instead of being retried in place, and retries that
     run under the wrong identity or rebuild what was already built;
  5. an approval that needs step-up and is not re-sent (tests/e2e/gate/approval_step_up.py);
  6. a blocked build that names the wrong gate.

WHERE IT RUNS. Inside a throwaway container of the installed worker image (scripts/e2e-gate.sh
starts it; never the install's own containers), with the BOSS under test mounted at /boss and
imported from there. That image has what a build has: boss-sandbox-exec, the run uid pool, Node and
npm, so every command below runs AS THE RUN'S OWN UID under the helper's umask 007, exactly like a
build; the app's database is a real per-run database made by build_services on a throwaway
Postgres. A check calls BOSS's own functions (the ones the pipeline calls), never a copy of them.

    python3 tests/e2e/gate/plumbing.py --out /out/results.json

Each check is PASS, FAIL (with the reason in words) or SKIP; the results file is read by
scripts/e2e-gate.sh, which prints the table and exits non-zero on any FAIL.
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import inspect
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

HERE = Path(__file__).resolve().parent
FIXTURE = HERE.parent / "fixtures" / "helpdesk-mini"
APP_DIR = "helpdesk"
OWNER, REPO = "gate", "helpdesk-mini"
RESULTS: list[dict[str, Any]] = []


class CheckFailed(Exception):
    """A check's verdict, in words."""


class Skip(Exception):
    pass


def check(name: str, bug: str = ""):
    """Run one check, timed; a raised CheckFailed (or any error) is a FAIL with its words."""
    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        def run(*args: Any, **kwargs: Any) -> Any:
            started = time.monotonic()
            row: dict[str, Any] = {"check": name, "bug": bug}
            try:
                detail = fn(*args, **kwargs)
                row.update(status="PASS", detail=" ".join(str(detail or "").split())[:400])
                return True
            except Skip as exc:
                row.update(status="SKIP", detail=str(exc)[:400])
                return None
            except CheckFailed as exc:
                row.update(status="FAIL", detail=" ".join(str(exc).split())[:600])
                return False
            except Exception as exc:  # noqa: BLE001 — a crash in BOSS's code is a FAIL too
                tb = traceback.extract_tb(exc.__traceback__)
                where = next((f"{Path(f.filename).name}:{f.lineno}" for f in reversed(tb)
                              if "/boss/" in f.filename), "")
                row.update(status="FAIL", detail=" ".join(f"{type(exc).__name__}: {exc}".split())[:500]
                           + (f" (at {where})" if where else ""))
                return False
            finally:
                row["seconds"] = round(time.monotonic() - started, 1)
                RESULTS.append(row)
                print(f"[gate] {row['status']:4} {name} ({row['seconds']}s) {row.get('detail', '')[:200]}",
                      flush=True)
        return run
    return wrap


def _tail(text: str, n: int = 6) -> str:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return " | ".join(lines[-n:])[:500]


def _result_text(result: Any) -> str:
    return f"{getattr(result, 'stdout', '') or ''}\n{getattr(result, 'stderr', '') or ''}"


# ── The world the build runs in ────────────────────────────────────────────────────────────────

class Gate:
    def __init__(self, pg_url: str, work: Path):
        self.pg_url = pg_url              # throwaway Postgres superuser (never printed)
        self.work = work
        self.run_id = f"crun_gate{uuid.uuid4().hex[:12]}"
        self.project_id = "p_e2egate"
        self.settings = None
        self.hub = None
        self.sandbox = None
        self.root: Optional[Path] = None
        self.app: Optional[Path] = None
        self.changed: dict[str, str] = {}
        self.install_ok = False
        self.tests_ok = False
        self.serve: dict[str, Any] = {}
        self.template: Optional[dict] = None
        self.template_record: Optional[Any] = None
        self.retries: list[dict[str, Any]] = []
        self.pr_number: Optional[int] = None
        self.stack = contextlib.ExitStack()

    # -- set-up shared by the checks ----------------------------------------------------------
    def open(self) -> None:
        from boss_engine.config import get_settings
        from boss_engine.services import owned_process, sandbox_identity

        self.settings = get_settings()
        from remote import LocalGitHub  # tests/e2e/gate/remote.py

        self.hub = LocalGitHub(self.work / "github").start()
        files = {}
        for path in sorted(FIXTURE.rglob("*")):
            if path.is_file() and "node_modules" not in path.parts:
                files[path.relative_to(FIXTURE).as_posix()] = path.read_text(encoding="utf-8")
        self.hub.seed(OWNER, REPO, files)
        # The build's own scope, as run_governed_code_task opens it: everything started below is
        # this run's, and runs as this run's uid.
        self.stack.enter_context(owned_process.owner_scope("code_run", self.run_id,
                                                           project_id=self.project_id, fresh=True))
        ident = self.stack.enter_context(sandbox_identity.bind(self.run_id, project_id=self.project_id))
        if ident is None:
            raise RuntimeError("the sandbox identity is off: the gate must run in the worker image "
                               "with BOSS_SANDBOX_ISOLATION=required")
        try:
            from boss_engine.services import transient_retry

            transient_retry.DELAYS = (1.0, 1.0)      # the policy, with the gate's patience
            self.stack.enter_context(transient_retry.reporting(self.retries.append))
        except ImportError:
            pass

    def close(self) -> None:
        from boss_engine.services import build_services, owned_process

        with contextlib.suppress(Exception):
            owned_process.terminate_owner(self.run_id, reason="run_ended", grace=2.0)
        if self.sandbox is not None:
            with contextlib.suppress(Exception):
                self.sandbox.teardown()
        with contextlib.suppress(Exception):
            self.stack.close()
        for rid in (self.run_id, f"{self.run_id}-deploy"):
            with contextlib.suppress(Exception):
                build_services.teardown(rid, server_url=self.pg_url)
        if self.hub is not None:
            self.hub.stop()

    def run(self, command: str, *, cwd: Optional[Path] = None, timeout: int = 600) -> Any:
        return self.sandbox.run(command, cwd=str(cwd or self.app), timeout=timeout)


# ── The checks ─────────────────────────────────────────────────────────────────────────────────

@check("clone through a GitHub 502", bug="4")
def check_clone(g: Gate) -> str:
    """EphemeralSandbox.provision, the build's own clone, from a remote whose first answer is a
    502 (crun_2f76585e). It must be retried in place and the build carry on."""
    from boss_engine.services.sandbox import EphemeralSandbox

    g.hub.blip("git", 1)
    g.sandbox = EphemeralSandbox(g.settings, task_id=g.run_id)
    access = SimpleNamespace(clone_url=g.hub.git_url(OWNER, REPO), git_config=[], own_git=None)
    try:
        g.root = g.sandbox.provision(OWNER, REPO, base_branch="main", egress=access)
    except Exception as exc:
        # Said, and then cloned again without the blip, so every later check still runs.
        g.hub.blip("git", 0)
        g.sandbox = EphemeralSandbox(g.settings, task_id=g.run_id)
        g.root = g.sandbox.provision(OWNER, REPO, base_branch="main", egress=access)
        g.app = g.root / APP_DIR
        raise CheckFailed(f"one GitHub 502 failed the clone, so the whole build: {_tail(str(exc), 3)}")
    g.app = g.root / APP_DIR
    if not (g.app / "package.json").is_file():
        raise CheckFailed("the clone has no helpdesk/package.json")
    said = [r for r in g.retries if "git" in str(r.get("operation", ""))]
    if g.hub.blipped["git"] != 1 or not said:
        raise CheckFailed(f"the 502 was not seen and retried (blipped={g.hub.blipped['git']}, "
                          f"retries reported={len(said)})")
    return f"502 on the clone, retried in place ({said[0].get('operation')}), {g.hub.requests['git']} git requests"


@check("hardening keeps the app's eslint 9", bug="1")
def check_harden(g: Gate) -> str:
    """verification_target.prepare_web_verify_env, the build's package.json hardening."""
    from boss_engine.services import verification_target as vt

    with contextlib.suppress(Exception):
        vt.ensure_verify_script(g.app)
    prep = vt.prepare_web_verify_env(g.app, scenarios=None, app_id=None)
    added = [c.split(":", 1)[1] for c in prep.get("changed") or [] if c.startswith("add:")]
    companions = set(getattr(vt, "ESLINT_COMPANIONS", ()) or (
        "eslint-plugin-react", "eslint-plugin-react-hooks", "@typescript-eslint/eslint-plugin",
        "@typescript-eslint/parser"))
    eslint8 = [a for a in added if a in companions]
    if eslint8:
        raise CheckFailed("BOSS added eslint-8 plugins to an app on eslint 9 (an ERESOLVE): "
                          + ", ".join(eslint8))
    return "added " + (", ".join(added) or "nothing")


@check("BOSS's clean install (as the run's uid, umask 007)", bug="1")
def check_install(g: Gate) -> str:
    """BOSS's one install, NPM_INSTALL_CMD, through the sandbox runner, through install_rounds
    when this BOSS has it (with no session to hand a failure back to)."""
    from boss_engine.services import verification_target as vt

    last: dict[str, Any] = {}

    def _check():
        result = g.run(vt.NPM_INSTALL_CMD, timeout=900)
        last["result"] = result
        failure = vt.install_failure(result) if hasattr(vt, "install_failure") else (
            None if result.ok else {"summary": _tail(_result_text(result), 4)})
        return None if failure is None else (failure, _result_text(result))

    if hasattr(vt, "install_rounds"):
        ok = vt.install_rounds(_check, lambda prompt: False)
    else:
        ok = _check() is None
    if not ok:
        failed = vt.install_failure(last["result"]) if hasattr(vt, "install_failure") else {}
        said = (failed or {}).get("summary") or " | ".join(
            ln.strip() for ln in _result_text(last["result"]).splitlines()
            if any(w in ln for w in ("ERESOLVE", "Found:", "peer ", "Could not resolve")))[:400]
        raise CheckFailed("the app's dependencies did not install: "
                          + (said or _tail(_result_text(last["result"]), 4)))
    g.install_ok = True
    tsx = g.app / "node_modules" / ".bin" / "tsx"
    if not tsx.exists():
        raise CheckFailed("the install left no node_modules/.bin/tsx")
    return f"{vt.NPM_INSTALL_CMD} passed"


@check("verify: build, typecheck, lint, the app's tests")
def check_verify(g: Gate) -> str:
    """The web verification target (pass 1: no browser tests) and the build's own test suite,
    each through the sandbox, with the run's own database (build_services)."""
    if not g.install_ok:
        raise Skip("the install failed")
    from boss_engine.services import build_services
    from boss_engine.services import verification_target as vt

    build_services.provision(g.run_id, server_url=g.pg_url)
    steps = [c for c in vt.resolve_authoritative_test_commands({"project_kind": "web"})
             if c != vt.NPM_INSTALL_CMD and c != getattr(vt, "E2E_STEP", "npm run test:e2e")]
    steps.append("npm test")
    if hasattr(vt, "run_steps_until_failure"):
        results = vt.run_steps_until_failure(g.sandbox, [(c, g.app) for c in steps], timeout=600)
    else:
        results = vt.run_until_failure(_AppSandbox(g), steps)
    for result in results:
        if not result.ok:
            raise CheckFailed(f"`{result.command}` failed on a known-good app: "
                              + _tail(_result_text(result), 5))
    g.tests_ok = True
    return " -> ".join(steps)


class _AppSandbox:
    """sandbox.run in the app directory (for a BOSS without run_steps_until_failure)."""

    def __init__(self, g: Gate):
        self.g = g

    def run(self, command: str, **kw: Any) -> Any:
        return self.g.run(str(command), **kw)


@check("the app's programs stay runnable for the run's uid", bug="2")
def check_exec_bits(g: Gate) -> str:
    """After the run's install (umask 007, the run's uid), BOSS's permission pass (normalize)
    and its copy for the app-start proof (_proof_tree): node_modules/.bin targets must be
    executable by the uid that starts the app, which is "other" to BOSS's copy."""
    if not g.install_ok:
        raise Skip("the install failed")
    from boss_engine.services import preview_service, workspace_perms

    workspace_perms.normalize(g.root)
    tree = preview_service._proof_tree(g.root)
    try:
        bad = []
        for name in ("tsx", "tsc", "eslint", "vitest"):
            link = tree / APP_DIR / "node_modules" / ".bin" / name
            if not link.exists():
                continue
            target = link.resolve()
            st = target.stat()
            # The copy is BOSS's; the app runs as the run's uid, which has no groups: "other".
            if st.st_uid != ident_uid(g) and not st.st_mode & stat.S_IXOTH:
                bad.append(f"{name} -> {target.relative_to(tree)} {oct(stat.S_IMODE(st.st_mode))}")
        if bad:
            raise CheckFailed("the app-start copy is not executable by the run's uid: "
                              + "; ".join(bad))
        return "tsx, tsc, eslint, vitest executable in the proof copy"
    finally:
        shutil.rmtree(tree, ignore_errors=True)


def ident_uid(g: Gate) -> int:
    from boss_engine.services import sandbox_identity

    ident = sandbox_identity.current()
    return int(ident.uid) if ident is not None else os.geteuid()


def _call_site_kwargs() -> set[str]:
    """The keyword arguments the pipeline passes to prove_app_serves (read from its source)."""
    from boss_engine.services import governed_code_task_service as gov

    tree = ast.parse(inspect.getsource(gov))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name == "prove_app_serves":
                found |= {k.arg for k in node.keywords if k.arg}
    return found


#: What the gate passes, as the pipeline does (governed_code_task_service._check_app_serves).
_GATE_SERVE_KWARGS = {"lineage", "known_service", "timeout", "probe_paths"}


@check("the app starts: BOSS's app-start proof", bug="2, 3")
def check_serve(g: Gate) -> str:
    """preview_service.prove_app_serves exactly as _check_app_serves calls it: the permission
    pass first, the declared boss.preview.command, the connector's GET routes probed. The app is
    known-good, so it must start and answer; it needs the SESSION_SECRET BOSS generates."""
    if not g.tests_ok:
        raise Skip("verify did not pass")
    from boss_engine.services import preview_service, workspace_perms
    from boss_engine.templates import gate as template_gate

    extra = _call_site_kwargs() - _GATE_SERVE_KWARGS
    workspace_perms.normalize(g.root)
    manifest = _template_manifest(g)
    probes = template_gate.probe_paths(manifest) if hasattr(template_gate, "probe_paths") else []
    kwargs: dict[str, Any] = {"lineage": [], "known_service": False, "timeout": 90.0}
    if "probe_paths" in inspect.signature(preview_service.prove_app_serves).parameters:
        kwargs["probe_paths"] = probes or None
    g.changed = _changed_files(g)
    g.serve = preview_service.prove_app_serves(g.root, list(g.changed), **kwargs)
    if extra:
        raise CheckFailed("the pipeline now passes " + ", ".join(sorted(extra)) + " to the app-start "
                          "proof and this gate does not: teach tests/e2e/gate/plumbing.py "
                          f"(the proof said: {str(g.serve.get('error') or 'ok')[:200]})")
    if not g.serve.get("ok") or not g.serve.get("proven"):
        headline = getattr(preview_service, "app_serves_headline", None)
        said = headline(g.serve) if headline and g.serve.get("error") else ""
        raise CheckFailed("a known-good app failed BOSS's app-start proof: "
                          + (said or str(g.serve.get("error") or g.serve.get("not_a_service") or g.serve))[:400])
    answered = {p: s for p, s in (g.serve.get("route_probes") or {}).items()}
    return f"`{g.serve.get('command')}` served on :{g.serve.get('port')}; probes {answered}"


def _changed_files(g: Gate) -> dict[str, str]:
    """The build's change, as the pipeline holds it: the fixture's app (what an agent wrote) plus
    whatever BOSS's own preparation changed in the clone."""
    out: dict[str, str] = {}
    for path in sorted((g.root / APP_DIR).rglob("*")):
        rel = path.relative_to(g.root).as_posix()
        if path.is_file() and "node_modules/" not in rel and not path.name.endswith(".log"):
            with contextlib.suppress(UnicodeDecodeError):
                out[rel] = path.read_text(encoding="utf-8")
    deploy = g.root / "boss.deploy.yaml"
    out["boss.deploy.yaml"] = deploy.read_text(encoding="utf-8")
    return out


def _template_manifest(g: Gate) -> Any:
    from boss_engine.templates.manifest import validate_manifest

    tpl = _login_oauth(g)
    return validate_manifest(dict(tpl["manifest"]))


def _login_oauth(g: Gate) -> dict:
    """The bundled login-oauth template, read the way BOSS reads its built-ins."""
    if g.template is None:
        from boss_engine.templates import service

        for _name, report, files in service.builtins():
            m = report.manifest
            if m is not None and m.id == "login-oauth":
                g.template = {"id": m.id, "version": m.version, "digest": report.digest,
                              "stack": "node", "manifest": m.model_dump(mode="json", by_alias=True),
                              "files": files}
                break
        else:
            raise CheckFailed("BOSS's built-in templates have no login-oauth")
    return g.template


@check("connector gate: login-oauth wiring + its own suite", bug="(3)")
def check_connector(g: Gate) -> str:
    """templates.gate.evaluate as _template_gates calls it for the applied connector (the wiring
    smoke on the started app, the secret scan), and the connector's own suite through the same
    gate (its install command and JUnit count), as its test tier runs it."""
    from boss_engine.templates import gate as template_gate

    tpl = _login_oauth(g)
    reference = {p.split("stacks/node/", 1)[1]: c for p, c in tpl["files"].items()
                 if p.startswith("stacks/node/auth/") or p.startswith("stacks/node/tests/")}
    drift = [p for p, c in reference.items()
             if (g.app / p).read_bytes() != (c if isinstance(c, bytes) else str(c).encode())]
    if drift:
        raise CheckFailed("the fixture's connector files no longer match the template (copy them "
                          "again from templates/apps/login-oauth/stacks/node): " + ", ".join(drift))
    if not g.changed:
        g.changed = _changed_files(g)
    params = inspect.signature(template_gate.evaluate).parameters
    kw: dict[str, Any] = {}
    if "serve" in params:
        kw["serve"] = g.serve
    result = template_gate.evaluate(
        template=tpl, files=tpl["files"], changed=g.changed, cwd=g.app,
        run_command=lambda command, where: g.run(command, cwd=Path(where), timeout=600),
        bound_values={}, tests_passed=g.tests_ok, substrate="local", **kw)
    g.template_record = result
    failing = [k for k, v in result.gates.items() if not v]
    said = []
    if failing:
        tt = result.evidence.get("template_tests") or {}
        said.append(f"build gate failed {failing}: {tt.get('error') or result.evidence.get('secret_scan')}")
    if "as_connector" in params:
        suite = template_gate.evaluate(
            template=tpl, files=tpl["files"], changed=g.changed, cwd=g.app,
            run_command=lambda command, where: g.run(command, cwd=Path(where), timeout=600),
            bound_values={}, tests_passed=True, substrate="local", as_connector=False)
        tt = suite.evidence.get("template_tests") or {}
        if not suite.gates.get("template_tests"):
            said.append(f"the connector's own suite failed: {tt.get('error')} {_tail(str(tt.get('output_tail') or ''), 3)}")
        suite_note = f"own suite {tt.get('passed')}/{tt.get('tests')} passed"
    else:
        suite_note = "own suite run as the build gate"
    if said:
        raise CheckFailed("; ".join(said))
    tt = result.evidence.get("template_tests") or {}
    return f"wiring {tt.get('answered') or tt.get('passed') or tt.get('not_run')}; {suite_note}"


@check("a blocked build names the gate that failed", bug="6")
def check_blocked_words(g: Gate) -> str:
    """templates.gate.hold_outcome, the one line a blocked build shows, given (a) this run's own
    gates and (b) the two HelpDesk AI failures it misnamed on 2026-10-13."""
    from boss_engine.templates import gate as template_gate

    def outcome(failing, install=None, serve=None, tt=None):
        # The richest call this BOSS accepts: a BOSS that cannot be told which gate really
        # failed answers from less, and its words are judged the same way.
        for call in (lambda: template_gate.hold_outcome(failing, install, app_serves=serve,
                                                        template_tests=tt),
                     lambda: template_gate.hold_outcome(failing, install),
                     lambda: template_gate.hold_outcome(failing)):
            try:
                return call()
            except TypeError:
                continue
        return None

    wrong = []
    # crun_247435e1: the app did not start; the connector smoke had nothing to probe.
    serve = {"ok": False, "exit_code": 127, "source": "declared", "command": "npm run migrate && …",
             "error": "the app exited (code 127) before serving HTTP: error: sh: 1: tsx: Permission "
                      "denied log tail: > tsx scripts/migrate.ts sh: 1: tsx: Permission denied"}
    tt = {"kind": "connector", "error": "the app did not start, so the connector's wiring was not "
                                        "checked", "because": "app_serves"}
    got = (outcome(["app_serves", "template_tests"], None, serve, tt) or {}).get("error", "")
    if "did not start" not in got or "tsx: Permission denied" not in got \
            or "template's tests did not pass" in got:
        wrong.append(f"app did not start -> {got[:160]!r}")
    # crun_6eaa478d: BOSS's own install failed with ERESOLVE.
    install = {"command": "npm install --include=dev", "exit_code": 1,
               "summary": "ERESOLVE unable to resolve dependency tree; Found: eslint@9.39.5"}
    got = (outcome(["template_tests"], install) or {}).get("error", "")
    if "dependencies did not install" not in got or "ERESOLVE" not in got:
        wrong.append(f"install failed -> {got[:160]!r}")
    # This run: if its proof failed, the words must lead with that, not with the template.
    if g.serve and g.serve.get("ok") is False:
        failing = ["app_serves"] + [k for k, v in (getattr(g.template_record, "gates", {}) or {}).items() if not v]
        tt_rec = (getattr(g.template_record, "evidence", {}) or {}).get("template_tests")
        got = (outcome(failing, None, g.serve, tt_rec) or {}).get("error", "")
        if got and ("template's tests did not pass" in got or "did not" not in got):
            wrong.append(f"this run -> {got[:160]!r}")
    if wrong:
        raise CheckFailed("the blocked message names the wrong gate: " + "; ".join(wrong))
    return "install -> 'dependencies did not install: ERESOLVE …'; app -> 'the app did not start: tsx: …'"


@check("pull request through a GitHub 502", bug="4")
def check_pull_request(g: Gate) -> str:
    """The software factory's GitHub effect (ensure_branch, upsert_file, create_pull_request) with
    GitHubAdapter against the local GitHub, whose first API answer is a 502: retried in place."""
    from boss_engine.services.github_adapter import GitHubAdapter

    if not g.changed:
        g.changed = _changed_files(g)
    adapter = GitHubAdapter(g.settings, owner=OWNER, repo=REPO, token="gate-local-token",
                            api_base_url=g.hub.api_url)
    branch = f"boss/code/{g.run_id}"
    # The blip lands on the call that has no fallback of its own: opening the pull request.
    g.hub.blip("api", 1, match=f"POST /api/repos/{OWNER}/{REPO}/pulls")
    try:
        adapter.ensure_branch(branch)
        for rel in ("boss.deploy.yaml", f"{APP_DIR}/package.json", f"{APP_DIR}/package-lock.json"):
            if rel in g.changed:
                adapter.upsert_file(branch, rel, g.changed[rel], f"BOSS proposal update: {rel}")
        pr = adapter.create_pull_request(branch, "HelpDesk mini: the plumbing gate", "gate")
    except Exception as exc:
        g.hub.blip("api", 0)
        with contextlib.suppress(Exception):     # again without the blip, so the deploy still runs
            adapter.ensure_branch(branch)
            for rel in ("boss.deploy.yaml", f"{APP_DIR}/package.json", f"{APP_DIR}/package-lock.json"):
                if rel in g.changed:
                    adapter.upsert_file(branch, rel, g.changed[rel], f"BOSS proposal update: {rel}")
            g.pr_number = int(adapter.create_pull_request(branch, "HelpDesk mini: the plumbing gate", "gate").number)
        raise CheckFailed(f"one GitHub 502 failed the pull request: {type(exc).__name__}: {str(exc)[:200]}")
    g.pr_number = int(pr.number)
    if g.hub.blipped["api"] != 1:
        raise CheckFailed("the 502 was never answered")
    return f"PR #{pr.number} opened; its POST /pulls met a 502 and was retried in place"


RETRY_SUITES = ["test_a_failed_delivery_retries_only_the_delivery.py",
                "test_an_automatic_retry_runs_under_the_persons_grant_or_not_at_all.py",
                "test_a_retry_of_nothing_is_a_fresh_build_of_its_own_app.py"]


class Background:
    """A check that runs as its own process while the pipeline runs (its wall time is its own)."""

    def __init__(self, argv: list[str], env: dict[str, str], cwd: str, log: Path):
        self.started = time.monotonic()
        self.log = log
        self._fh = open(log, "w")
        self.proc = subprocess.Popen(argv, env=env, cwd=cwd, stdout=self._fh,
                                     stderr=subprocess.STDOUT, text=True)
        self.seconds = 0.0

    def wait(self, timeout: float) -> tuple[int, str]:
        try:
            code = self.proc.wait(timeout=max(1.0, timeout - (time.monotonic() - self.started)))
        except subprocess.TimeoutExpired:
            self.proc.kill()
            code = -9
        self.seconds = time.monotonic() - self.started
        self._fh.close()
        return code, self.log.read_text(errors="replace")


def start_retry_suites(full_db_url: str, work: Path) -> Optional[Background]:
    """The retry policy's own suites, on a full-schema throwaway database (their fixtures need
    one: without BOSS_IDENTITY_FULL_TEST_DATABASE_URL every test tier SKIPS them)."""
    if any(not (Path("/boss/engine/tests") / n).is_file() for n in RETRY_SUITES):
        return None
    env = {**os.environ, "BOSS_IDENTITY_FULL_TEST_DATABASE_URL": full_db_url,
           "BOSS_TEST_DATABASE_URL": full_db_url.replace("postgresql://", "postgresql+psycopg://"),
           "PYTHONPATH": "/boss:/boss/engine"}
    return Background([sys.executable, str(Path(__file__).resolve()), "--retry-suites"],
                      env, "/boss", work / "retry-suites.log")


def run_retry_suites() -> int:
    """`plumbing.py --retry-suites`: the full schema, then the suites (in the background)."""
    _full_schema(os.environ["BOSS_IDENTITY_FULL_TEST_DATABASE_URL"])
    import pytest

    return int(pytest.main(["-q", "-p", "no:cacheprovider", "-W", "ignore", "-rs",
                            *[f"engine/tests/{n}" for n in RETRY_SUITES]]))


@check("retries: delivery only, under the person's grant, a retry of nothing is a fresh build", bug="4")
def check_retry_regressions(bg: Optional[Background]) -> str:
    if bg is None:
        missing = [n for n in RETRY_SUITES if not (Path("/boss/engine/tests") / n).is_file()]
        raise CheckFailed("this BOSS has no retry policy for: " + ", ".join(missing))
    code, out = bg.wait(240)
    last = _tail(out, 1)
    if code != 0:
        failed = [ln for ln in out.splitlines() if ln.startswith(("FAILED", "ERROR"))]
        raise CheckFailed(f"{last}: " + " | ".join(failed[:4]))
    if " skipped" in last:
        raise CheckFailed(f"the retry suites skipped instead of running: {last}")
    return f"{last} (ran alongside, {bg.seconds:.0f}s)"


@check("deploy to a local target: preflight, plan, apply, health", bug="")
def check_deploy(g: Gate) -> str:
    """The stack deploy with the host's transport swapped for this container: the manifest read
    and rendered (app_deploy_manifest), the host preflight, the PR's tree fetched from GitHub
    (GitHubSource), and StackApplier's own steps, health check included. The app's generated
    SESSION_SECRET comes from the deploy's own generators."""
    if g.pr_number is None:
        raise Skip("no pull request to deploy")
    from boss_engine.services import app_deploy_manifest as adm
    from boss_engine.services import build_services, stack_deploy

    manifest = adm.load_manifest(g.root / "boss.deploy.yaml")
    import dataclasses

    deploy_dir = str(g.work / "deploy" / manifest.name)
    manifest = dataclasses.replace(manifest, dir=deploy_dir)
    source = stack_deploy.GitHubSource("gate-local-token")
    source.API = g.hub.api_url
    commit = source.resolve_commit(OWNER, REPO, f"pull/{g.pr_number}")
    ctx = {"commit": commit, "ref": f"pull/{g.pr_number}", "target.host": "127.0.0.1",
           "target.public_url": "http://127.0.0.1:3000", "target.user": "boss"}
    rendered = adm.render(manifest, ctx)

    class LocalHost:
        def stack_exec(self, *, command: str, timeout: int = 120, stdin_text=None, stdin_path=None, **_: Any):
            data = stdin_text.encode() if stdin_text is not None else (
                Path(stdin_path).read_bytes() if stdin_path else None)
            return subprocess.run(["bash", "-c", command], input=data, capture_output=True,
                                  timeout=timeout, cwd=str(g.work))

    access = stack_deploy.HostAccess(host="127.0.0.1", user="boss", port=22, key_material="",
                                     adapter=_TextHost(LocalHost()))
    problems = stack_deploy.preflight_host(access, manifest)
    if problems:
        raise CheckFailed("preflight: " + "; ".join(p["headline"] for p in problems))
    lease = build_services.provision(f"{g.run_id}-deploy", server_url=g.pg_url)
    env_values = {"BOSS_DEPLOY_COMMIT": commit, "BOSS_DEPLOY_REF": ctx["ref"], "BOSS_DEPLOY_DIR": manifest.dir}
    hidden = []
    for item in manifest.env:
        if item.source == "literal":
            env_values[item.key] = rendered["env_literals"][item.key]
        elif item.source == "generate":
            env_values[item.key] = stack_deploy._GENERATE[item.value]()
            hidden.append(env_values[item.key])
        elif item.key == "DATABASE_URL":
            env_values[item.key] = lease.env()["DATABASE_URL"]
            hidden.append(env_values[item.key])
    steps_seen: list[str] = []
    applier = stack_deploy.StackApplier(
        access=access, rendered=rendered, commit=commit, ref=ctx["ref"], env_values=env_values,
        redact_values=hidden, changeset_id="chg_gate",
        fetch_tree=lambda dest: source.download_tarball(OWNER, REPO, commit, dest),
        record=lambda steps, log: steps_seen.__setitem__(slice(None), [f"{s['name']}={s['status']}" for s in steps]),
        now=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ"))
    try:
        out = applier.run()
    except stack_deploy.StackStepFailed as exc:
        raise CheckFailed(f"the deploy failed at {exc.args[0] if exc.args else '?'}: "
                          f"{getattr(exc, 'headline', '')} {_tail(applier.log, 4)}")
    finally:
        _kill_matching("src/server/index.ts")
    leaked = [v for v in hidden if v and v in (out.get("log_tail") or "")]
    if leaked:
        raise CheckFailed("the deploy log carries a generated secret")
    return f"{out['mode']}: " + ", ".join(s["name"] for s in out["steps"] if s["status"] == "succeeded")


def _kill_matching(needle: str) -> None:
    """End the app the deploy started (nohup … & disown), found by its command line."""
    import signal

    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        with contextlib.suppress(OSError):
            if needle in (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace"):
                os.kill(int(proc.name), signal.SIGTERM)


class _TextHost:
    """HostAccess reads `.stdout`/`.stderr` as text."""

    def __init__(self, host: Any):
        self.host = host

    def stack_exec(self, **kw: Any) -> Any:
        done = self.host.stack_exec(**kw)
        return SimpleNamespace(returncode=done.returncode,
                               stdout=(done.stdout or b"").decode(errors="replace"),
                               stderr=(done.stderr or b"").decode(errors="replace"))


def start_approval(db_url: str, work: Path) -> Optional[Background]:
    script = HERE / "approval_step_up.py"
    if not script.is_file():
        return None
    env = {**os.environ, "BOSS_E2E_GATE_DATABASE_URL": db_url,
           "PYTHONPATH": f"/boss:/boss/engine:{HERE}"}
    return Background([sys.executable, str(script)], env, "/tmp", work / "approval.log")


@check("approval that needs step-up is re-sent and the step resumes", bug="5")
def check_approval(bg: Optional[Background]) -> str:
    if bg is None:
        raise CheckFailed("tests/e2e/gate/approval_step_up.py is missing")
    code, out = bg.wait(240)
    line = next((ln for ln in reversed(out.splitlines()) if ln.startswith("RESULT ")), "")
    if code != 0 or not line.startswith("RESULT PASS"):
        raise CheckFailed((line[len("RESULT FAIL "):] if line else _tail(out, 4)) or f"exit {code}")
    return f"{line[len('RESULT PASS '):]} (ran alongside, {bg.seconds:.0f}s)"


# ── Databases on the throwaway Postgres ─────────────────────────────────────────────────────────

def _database(pg_url: str, name: str) -> str:
    import psycopg
    from psycopg import sql

    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    base, _, _ = pg_url.rpartition("/")
    return f"{base}/{name}"


def _full_schema(url: str) -> None:
    from sqlalchemy import create_engine

    from boss_engine.db import ensure_schema

    engine = create_engine(url.replace("postgresql://", "postgresql+psycopg://"), future=True)
    try:
        ensure_schema(engine)
    finally:
        engine.dispose()


def main() -> int:
    if sys.argv[1:] == ["--retry-suites"]:
        return run_retry_suites()
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    pg_url = os.environ["BOSS_E2E_GATE_PG_URL"]
    os.environ.setdefault("BOSS_BUILD_SERVICES_PREFIX", "boss_e2egate_")
    work = Path(tempfile.mkdtemp(prefix="boss-e2egate-"))
    started = time.monotonic()
    g = Gate(pg_url, work)
    approval = retries = None
    try:
        # The two checks that need only a database run alongside the pipeline.
        approval = start_approval(_database(pg_url, "boss_test_e2egate").replace(
            "postgresql://", "postgresql+psycopg://"), work)
        retries = start_retry_suites(_database(pg_url, "boss_test_e2egate_full"), work)
        g.open()
        check_clone(g)
        if g.root is not None:
            check_harden(g)
            check_install(g)
            check_verify(g)
            check_exec_bits(g)
            check_serve(g)
            check_connector(g)
        check_blocked_words(g)
        if g.root is not None:
            check_pull_request(g)
            check_deploy(g)
        check_retry_regressions(retries)
        check_approval(approval)
    except Exception as exc:  # noqa: BLE001 — the gate's own set-up failing is a FAIL
        RESULTS.append({"check": "gate set-up", "bug": "", "status": "FAIL", "seconds": 0,
                        "detail": f"{type(exc).__name__}: {exc}"[:500]})
        traceback.print_exc()
    finally:
        for bg in (approval, retries):
            if bg is not None and bg.proc.poll() is None:
                bg.proc.kill()
        g.close()
        logs = Path(args.out).parent
        for name in ("approval.log", "retry-suites.log"):
            with contextlib.suppress(OSError):
                shutil.copy(work / name, logs / name)
        shutil.rmtree(work, ignore_errors=True)
    Path(args.out).write_text(json.dumps({"results": RESULTS,
                                          "seconds": round(time.monotonic() - started, 1)}, indent=1))
    # A release gate passes only on PASS: a check that could not run proved nothing.
    return 1 if any(r["status"] != "PASS" for r in RESULTS) else 0


if __name__ == "__main__":
    sys.exit(main())
