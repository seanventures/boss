"""The two gates an applied template adds to a build (F2 §2.3 steps 3-4, §2.9 S2.1).

  template_tests        the manifest's test command for the stack runs in the build sandbox and
                        its JUnit report shows at least `min_tests` passing tests and no failures;
                        AND the template's own test files are in the build's diff (the PR
                        carries them).
  no_committed_secrets  the diff holds no credential-shaped string, no value matching a secret
                        pattern the template declares, and no value an app secret binding holds.

Both are ADDED to the gates a normal build runs; neither replaces one. Mechanical: no model
judges them. `evaluate()` is called by the one hook in governed_code_task_service
(`_template_gates`); everything it needs is passed in, so it is unit-tested without a pipeline.
"""
from __future__ import annotations

import os
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Optional

from .secret_scan import Finding, scan_files

#: Where a report may be looked for: never inside dependency or VCS trees.
_SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "__pycache__", ".pytest_cache"}
#: A JUnit report is XML whose root is <testsuites> or <testsuite>.
_MAX_REPORT_BYTES = 20 * 1024 * 1024


@dataclass
class JUnitCounts:
    tests: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0

    @property
    def passed(self) -> int:
        return max(0, self.tests - self.failures - self.errors - self.skipped)

    def add(self, other: "JUnitCounts") -> None:
        self.tests += other.tests
        self.failures += other.failures
        self.errors += other.errors
        self.skipped += other.skipped


def parse_junit(xml_text: str | bytes) -> Optional[JUnitCounts]:
    """Counts from one JUnit XML report (Surefire, pytest --junitxml, Jest/node junit reporters).
    Counted from the <testcase> elements, not the suite attributes, which some reporters omit or
    total differently; None when the text is not a JUnit report."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    if root.tag not in ("testsuites", "testsuite"):
        return None
    counts = JUnitCounts()
    for case in root.iter("testcase"):
        counts.tests += 1
        tags = {child.tag for child in case}
        if "failure" in tags:
            counts.failures += 1
        elif "error" in tags:
            counts.errors += 1
        elif "skipped" in tags:
            counts.skipped += 1
    return counts


def find_reports(cwd: Path, *, since: float) -> list[Path]:
    """XML files written under `cwd` at or after `since` (the test command's start)."""
    found: list[Path] = []
    cwd = Path(cwd)
    stack = [cwd]
    while stack:
        d = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.is_symlink():
                continue
            if p.is_dir():
                if p.name not in _SKIP_DIRS:
                    stack.append(p)
            elif p.suffix.lower() == ".xml":
                try:
                    st = p.stat()
                except OSError:
                    continue
                if st.st_mtime >= since - 1 and st.st_size <= _MAX_REPORT_BYTES:
                    found.append(p)
    return sorted(found)


def template_test_files(files: Mapping[str, Any], stack: str) -> list[str]:
    """The template's own test files for `stack` (paths under stacks/<stack>/tests/)."""
    prefix = f"stacks/{stack}/tests/"
    return sorted(p[len(prefix):] for p in files if p.startswith(prefix) and not p.endswith("/"))


def install_command(stack: str, cwd: Path) -> Optional[str]:
    """The stack's dependency install for the app in `cwd`, run before a template's tests (the
    gate and the trial): the image holds no app's dependencies (a Python template's authlib, a
    Java template's Maven artifacts). Through the sandbox runner, so through egress's
    allowlisted package route like every build command. None when the app declares nothing."""
    if stack == "python" and (cwd / "requirements.txt").is_file():
        return "python3 -m pip install --user --disable-pip-version-check --no-input -q -r requirements.txt"
    if stack == "node" and (cwd / "package-lock.json").is_file():
        return "npm ci --no-audit --no-fund"
    if stack == "node" and (cwd / "package.json").is_file():
        # BOSS's one install (verification_target.NPM_INSTALL_CMD): the build's verify ran this
        # exact command, so the gate cannot fail an install the build already passed, or pass
        # one it failed.
        from ..services.verification_target import NPM_INSTALL_CMD

        return NPM_INSTALL_CMD
    if stack == "java" and (cwd / "pom.xml").is_file():
        return "mvn -q -B dependency:go-offline"
    return None


def install_dependencies(stack: str, cwd: Path, run_command: Callable[[str, Path], Any]) -> dict:
    """Run `install_command` (if any) with `run_command`; {command, exit_code[, error]}."""
    command = install_command(stack, cwd)
    if command is None:
        return {"command": None}
    result = run_command(command, cwd)
    out: dict[str, Any] = {"command": command, "exit_code": getattr(result, "exit_code", None)}
    if out["exit_code"] not in (0, None):
        from ..identity.redaction import redact_text

        from ..services.verification_target import install_error_summary

        tail = str(getattr(result, "stdout", "") or "") + "\n" + str(getattr(result, "stderr", "") or "")
        out["summary"] = redact_text(install_error_summary(tail))
        out["error"] = f"dependencies did not install: {out['summary']}"
        out["output_tail"] = redact_text(tail[-1500:])
    return out


def install_failure_of(template_tests_record: Any) -> Optional[dict]:
    """The gate's own failed install from its template_tests evidence, shaped like
    verification_target.install_failure, else None."""
    install = (template_tests_record or {}).get("install") if isinstance(template_tests_record, Mapping) else None
    if not isinstance(install, Mapping) or not install.get("error"):
        return None
    return {"command": install.get("command"), "exit_code": install.get("exit_code"),
            "summary": install.get("summary")}


def present_in_app(test_files: list[str], cwd: Optional[Path]) -> set[str]:
    """Names of template test files already in the app's tree (an apply of a template whose
    tests an earlier apply already put on the branch: the PR carries them, unchanged)."""
    if cwd is None or not cwd.is_dir():
        return set()
    wanted = {PurePosixPath(t).name for t in test_files}
    found: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(cwd):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        found |= wanted.intersection(filenames)
    return found


def missing_from_diff(test_files: list[str], changed_paths: list[str]) -> list[str]:
    """Template test files with no file of the same name in the diff. The agent ADAPTS the
    reference into the app, so a test may land in another directory; its file name is what
    must survive (§2.11: the tests are written against HTTP routes, not internal modules)."""
    names = {PurePosixPath(p).name for p in changed_paths}
    return [t for t in test_files if PurePosixPath(t).name not in names]


# TEMPLATE TESTS DO NOT RUN ON THE MICRO-VM SUBSTRATE (APP_TEMPLATES_AND_SECURITY_LOOP.md §2.3).
#
# With BOSS_GOVERNED_SANDBOX_PROVIDER=microvm every working stage runs in a guest of the external
# sandbox-service, and the Python<->Go contract (sandbox_contract, 1.0) brings back only a stage's
# diff and, per test command, its exit code and the tail of its output. This gate counts passing
# tests from the JUnit report the command writes; that file never crosses back, and an exit code
# alone cannot prove `min_tests` passed (a runner that collects nothing exits 0). Reading a count
# out of the output tail would trust text the agent-written code prints. So in micro-VM mode the
# gate does not run and FAILS CLOSED in these words, and an apply is refused up front with the
# same words (apply.plan) rather than starting a build that can only end blocked. Lifting this
# needs an artifact channel in the sandbox contract (the guest returning named report files).
MICROVM_REFUSAL = (
    "Template tests cannot run on the micro-VM build substrate "
    "(BOSS_GOVERNED_SANDBOX_PROVIDER=microvm): the micro-VM guest returns a command's exit code "
    "and output, not the JUnit report this gate counts, so BOSS cannot prove the template's tests "
    "passed. Build template applies on the local sandbox instead: set "
    "BOSS_GOVERNED_SANDBOX_PROVIDER=local (the default) and apply the template again.")


def microvm_refusal(settings: Any) -> Optional[str]:
    """MICROVM_REFUSAL when this install builds on the micro-VM substrate, else None. The same
    test governed_code_task_service._resolve_use_microvm makes for a run with no override."""
    provider = str(getattr(settings, "governed_sandbox_provider", "") or "local").lower()
    return MICROVM_REFUSAL if provider in ("microvm", "firecracker") else None


@dataclass
class GateResult:
    gates: dict[str, bool]
    evidence: dict = field(default_factory=dict)
    #: written by the test command; removed so they never reach the PR
    reports_removed: list[str] = field(default_factory=list)


def probe_paths(manifest: Any) -> list[str]:
    """The connector routes a smoke check can GET as they are: GET routes with no path
    parameter (`GET /auth/providers`, not `GET /auth/login/:provider`)."""
    out: list[str] = []
    for route in list(getattr(manifest, "routes", None) or []):
        parts = str(route).split()
        if len(parts) != 2 or parts[0].upper() != "GET":
            continue
        path = parts[1]
        if path.startswith("/") and not any(ch in path for ch in ":{*"):
            out.append(path)
    return out


def connector_smoke(manifest: Any, serve: Optional[Mapping[str, Any]], *,
                    tests_passed: bool) -> tuple[bool, dict[str, Any]]:
    """A CONNECTOR IS PRE-TESTED (operator, 2026-10-13). Login, Stripe, PayPal are each a fully
    tested path in their own test tier, so a build that adds one neither writes tests for it nor
    runs its suite as a gate. What the build CAN break is the wiring, so the gate is one smoke
    check: the app starts (the app_serves proof) and the connector's GET routes answer on it
    (anything but no answer, 404 or a 5xx). An app BOSS cannot start (not a declared service)
    is left to its own tests, and the record says so."""
    paths = probe_paths(manifest)
    tt: dict[str, Any] = {"kind": "connector", "check": "wiring smoke", "routes": paths,
                          "connector_tests": "run in the connector's own test tier, not in this build"}
    serve = dict(serve or {})
    if not tests_passed:
        tt["error"] = "the build's own tests did not pass, so the connector's wiring was not checked"
        tt["because"] = "tests"  # a consequence: the gate that failed is the build's own checks
        return False, tt
    if serve.get("proven"):
        probes = dict(serve.get("route_probes") or {})
        tt["probes"] = probes
        if not paths:
            return True, tt
        answered = [p for p, status in probes.items()
                    if isinstance(status, int) and status != 404 and status < 500]
        if answered:
            tt["answered"] = answered
            return True, tt
        tt["error"] = ("the app started but none of the connector's routes answered: "
                       + ", ".join(f"GET {p} -> {probes.get(p) or 'no answer'}" for p in paths))
        return False, tt
    if serve.get("ok") is False and not serve.get("harness"):
        tt["error"] = "the app did not start, so the connector's wiring was not checked"
        if serve.get("needs_secret"):
            # Started with the environment the app declares (templates.run_env): what was
            # missing is a secret only a person can set, named.
            tt["error"] = ("the app did not start: it needs " + ", ".join(serve["needs_secret"])
                           + ", so the connector's wiring was not checked")
        tt["because"] = "app_serves"  # a consequence: the gate that failed is app_serves
        return False, tt
    tt["not_run"] = str(serve.get("skipped") or serve.get("not_a_service") or serve.get("source")
                        or "the app was not started")
    return True, tt


def evaluate(*, template: Mapping[str, Any], files: Mapping[str, bytes], changed: Mapping[str, Any],
             cwd: Optional[Path], run_command: Optional[Callable[[str, Path], Any]],
             bound_values: Optional[Mapping[str, str]] = None, tests_passed: bool = True,
             substrate: str = "local", serve: Optional[Mapping[str, Any]] = None,
             as_connector: Optional[bool] = None) -> GateResult:
    """Both gates for one applied template.

    `template` is `{id, version, digest, stack, manifest}` (manifest as a dict), `files` the
    template version's files, `changed` the build's {path: content}, `cwd` the app directory the
    test command runs in, `run_command(command, cwd)` the sandbox's runner (returns an object with
    exit_code/stdout/stderr), `bound_values` the app's bound secret values (scan only),
    `substrate` where the build ran ("microvm": the tests cannot run there, MICROVM_REFUSAL)."""
    from .manifest import validate_manifest
    from .secret_scan import manifest_patterns

    manifest = validate_manifest(dict(template.get("manifest") or {}))
    stack = str(template.get("stack") or "")
    evidence: dict[str, Any] = {"template": {"id": manifest.id, "version": manifest.version,
                                             "digest": template.get("digest"), "stack": stack}}
    removed: list[str] = []

    # ---- no_committed_secrets ------------------------------------------------------------------
    findings: list[Finding] = scan_files(
        {p: (c if isinstance(c, (bytes, str)) else str(c)) for p, c in changed.items()},
        declared=manifest_patterns(manifest), values=bound_values or {})
    evidence["secret_scan"] = {"findings": [f.as_dict() for f in findings[:50]],
                               "count": len(findings)}

    # ---- template_tests ------------------------------------------------------------------------
    # `as_connector=False` runs a connector's own suite: its test tier (the trial), not a build.
    if (manifest.item_kind == "connector") if as_connector is None else as_connector:
        ok, tt = connector_smoke(manifest, serve, tests_passed=tests_passed)
        evidence["template_tests"] = tt
        return GateResult(gates={"template_tests": ok, "no_committed_secrets": not findings},
                          evidence=evidence, reports_removed=removed)
    spec = manifest.tests.get(stack)  # type: ignore[call-overload]
    tt: dict[str, Any] = {"command": spec.command if spec else None,
                          "min_tests": spec.min_tests if spec else None}
    ok = False
    if spec is None:
        tt["error"] = f"the template does not support the {stack!r} stack"
    elif substrate == "microvm":
        tt["not_run"] = "microvm"
        tt["error"] = MICROVM_REFUSAL
    else:
        wanted = template_test_files(files, stack)
        missing = missing_from_diff(wanted, list(changed.keys()))
        already = present_in_app(missing, cwd)
        if already:
            tt["already_in_app"] = sorted(already)
            missing = [t for t in missing if PurePosixPath(t).name not in already]
        tt["test_files"] = wanted
        tt["missing_from_diff"] = missing
        if not wanted:
            tt["error"] = "the template has no test files for this stack"
        elif missing:
            tt["error"] = "the template's tests are not in the change: " + ", ".join(missing[:10])
        elif not tests_passed:
            tt["error"] = "the build's own tests did not pass, so the template's were not run"
            tt["because"] = "tests"
        elif cwd is None or run_command is None:
            tt["error"] = "the template's tests cannot run here: the build has no app directory to run them in"
        elif (install := install_dependencies(stack, cwd, run_command)).get("error"):
            tt["install"] = install
            tt["error"] = install["error"]
            tt["output_tail"] = install.get("output_tail")
        else:
            tt["install"] = install
            started = time.time()
            result = run_command(spec.command, cwd)
            tt["exit_code"] = getattr(result, "exit_code", None)
            reports = find_reports(cwd, since=started)
            counts = JUnitCounts()
            parsed = []
            changed_names = set(changed.keys())
            for report in reports:
                c = parse_junit(report.read_bytes())
                if c is None:
                    continue
                counts.add(c)
                rel = report.relative_to(cwd).as_posix()
                parsed.append(rel)
                # A report the command wrote is evidence, not code: it never reaches the PR.
                if rel not in changed_names:
                    try:
                        report.unlink()
                        removed.append(rel)
                    except OSError:
                        pass
            tt.update({"reports": parsed, "tests": counts.tests, "passed": counts.passed,
                       "failures": counts.failures, "errors": counts.errors,
                       "skipped": counts.skipped})
            if not parsed:
                tt["error"] = "the test command wrote no JUnit report"
            elif counts.failures or counts.errors:
                tt["error"] = f"{counts.failures + counts.errors} template test(s) failed"
            elif counts.passed < spec.min_tests:
                tt["error"] = f"{counts.passed} passing template tests; the template requires {spec.min_tests}"
            elif tt["exit_code"] not in (0, None):
                tt["error"] = f"the test command exited {tt['exit_code']}"
            else:
                ok = True
            if not ok:
                out = (str(getattr(result, "stdout", "") or "") + str(getattr(result, "stderr", "") or ""))
                from ..identity.redaction import redact_text

                tt["output_tail"] = redact_text(out[-1500:])
    evidence["template_tests"] = tt
    return GateResult(gates={"template_tests": ok, "no_committed_secrets": not findings},
                      evidence=evidence, reports_removed=removed)


def applied_template(connection: Any, run: Mapping[str, Any]) -> Optional[dict]:
    """The template a build applies, from its workflow run's input (`input.template`, written by
    POST /v1/templates/{id}/apply), with the pinned row's manifest and files. None for a build
    that applies no template. A run whose recorded digest no longer matches the row is refused
    (the gate then fails closed), never silently re-read."""
    from sqlalchemy import text

    fctx = run.get("finalize_ctx") or {}
    if run.get("id"):
        # The stored row, not the pipeline's copy: the workflow link may be written after load.
        stored = connection.execute(text("SELECT finalize_ctx FROM code_task_runs WHERE id = :i"),
                                    {"i": run["id"]}).scalar()
        if isinstance(stored, dict):
            fctx = {**fctx, **stored}
    wf = str(fctx.get("workflow_run_id") or (fctx.get("origin") or {}).get("workflow_run_id") or "")
    if not wf:
        return None
    raw = connection.execute(text("SELECT input FROM workflow_runs WHERE id = :w"), {"w": wf}).scalar()
    tpl = (raw or {}).get("template") if isinstance(raw, dict) else None
    if not isinstance(tpl, dict) or not tpl.get("id"):
        return None
    row = connection.execute(text("SELECT id, manifest, digest FROM app_templates WHERE id = :t"),
                             {"t": tpl["id"]}).mappings().first()
    if row is None:
        return {**tpl, "error": "the applied template no longer exists"}
    if tpl.get("digest") and row["digest"] != tpl["digest"]:
        return {**tpl, "error": "the template changed since this build started (digest mismatch)"}
    files = {r[0]: bytes(r[1]) for r in connection.execute(
        text("SELECT path, content FROM app_template_files WHERE template_id = :t"), {"t": row["id"]}).all()}
    return {**tpl, "manifest": row["manifest"], "files": files, "workflow_run_id": wf}


# ---- the hold, as an outcome: a build a template gate stopped is BLOCKED -------------------------

#: The two gates an applied template adds. Mechanical and unwaivable: a build that failed one
#: never completes, never opens a PR and is never "Done · no PR".
TEMPLATE_GATES = ("template_tests", "no_committed_secrets")

_WHY = {
    "template_tests": "the template's tests did not pass",
    "no_committed_secrets": "the change holds something that looks like a committed secret",
}
_NEXT = {
    "template_tests": ("Open the build's Checks to read the template test output, then retry the "
                       "build with a note on what to fix, or apply the template again."),
    "no_committed_secrets": ("Open the build's Checks to see which file and line looked like a secret, "
                             "remove it (secrets come from the app's Secrets, never the code), then "
                             "retry the build."),
}


#: What to do when the build's tests never ran because BOSS's dependency install failed.
INSTALL_NEXT = ("Open the build's Checks to read the install output, resolve the dependency "
                "conflict in package.json (versions whose peer dependencies agree; no --force or "
                "--legacy-peer-deps), then retry the build.")


def install_reason(install_failure: Any) -> Optional[str]:
    """"dependencies did not install: ERESOLVE …" for a recorded install failure
    (verification_target.install_failure), else None."""
    if not isinstance(install_failure, Mapping):
        return None
    summary = str(install_failure.get("summary") or "").strip()
    if not summary:
        how = "timed out" if install_failure.get("timed_out") else f"exit {install_failure.get('exit_code')}"
        summary = f"`{install_failure.get('command') or 'the install'}` {how}"
    return f"dependencies did not install: {summary[:500]}"


#: What to do when the app BOSS started for the serve proof did not serve.
APP_SERVES_NEXT = ("Open the build's Checks to read the app's start log, fix what stopped it, "
                   "then retry the build.")


def _derived_from_app_serves(template_tests: Any) -> bool:
    """The template_tests failure is only the consequence of the app not starting (the connector
    smoke check had no app to probe), not a failure of its own."""
    if not isinstance(template_tests, Mapping):
        return False
    return (template_tests.get("because") == "app_serves"
            or str(template_tests.get("error") or "").startswith("the app did not start"))


#: What to do when the build's own checks failed, so the template's gate never ran.
CHECKS_NEXT = ("Open the build's Checks to read the failing check's output, fix it, then retry "
               "the build.")


def _derived_from_tests(template_tests: Any) -> bool:
    """The template_tests failure is only the consequence of the build's own checks failing
    (the template's tests, or a connector's wiring check, were never run), not one of its own."""
    if not isinstance(template_tests, Mapping):
        return False
    return (template_tests.get("because") == "tests"
            or str(template_tests.get("error") or "").startswith("the build's own tests did not pass"))


def secret_scan_where(secret_scan: Any, limit: int = 3) -> str:
    """" (helpdesk-ai/vitest.config.ts:14 assigned_secret, ...)" — WHERE the scan matched: file,
    line and rule, never the matched text. Empty when the scan recorded no finding."""
    findings = (secret_scan or {}).get("findings") if isinstance(secret_scan, Mapping) else None
    if not findings:
        return ""
    shown = [f"{f.get('path')}:{f.get('line')} {f.get('rule')}" for f in findings[:limit]
             if isinstance(f, Mapping)]
    more = int((secret_scan or {}).get("count") or len(findings)) - len(shown)
    return " (" + ", ".join(shown) + (f", and {more} more" if more > 0 else "") + ")"


def hold_outcome(failing: Any, install_failure: Any = None, *, app_serves: Any = None,
                 template_tests: Any = None, secret_scan: Any = None,
                 tests: Any = None) -> Optional[dict]:
    """{gates, reason, next_step, error} for a build whose failing gates include a template gate,
    else None. `error` is the one line a blocked build shows: why, then what to do.

    THE GATE THAT FAILED, NOT ITS CONSEQUENCE. Every reason names the gate that actually failed,
    with its first error line:
    * the build's own install failed (`install_failure`): its tests never ran, so the template's
      could not either — the install, in npm's words (HelpDesk AI, crun_6eaa478dc33a42c6bdb7);
    * the app did not start (`app_serves` failing, its record given): a connector's smoke check
      had nothing to probe — "the app did not start: tsx: Permission denied", never "the
      template's tests did not pass" (HelpDesk AI crun_247435e17c00478483b2, whose template
      tests had passed);
    * the build's own checks failed (`template_tests` says so; `tests` is the run's tests
      record, verification_target.failed_check in it): the check that failed and its first
      line — "the app's checks failed: npm run build — Missing script: \"build\"", never "the
      template's tests did not pass" (crun_decc93dbb0154e768871, whose template never ran)."""
    failing_set = set(failing or ())
    gates = [g for g in TEMPLATE_GATES if g in failing_set]
    if not gates:
        return None
    whys = {g: _WHY[g] for g in gates}
    if "no_committed_secrets" in whys:
        # The blocked line says WHERE, so nobody has to open the Checks to learn which file.
        whys["no_committed_secrets"] += secret_scan_where(secret_scan)
    next_step = _NEXT[gates[0]]
    install = install_reason(install_failure)
    if install and "template_tests" in whys:
        whys["template_tests"] = install
        next_step = INSTALL_NEXT if gates[0] == "template_tests" else next_step
    if not install and "template_tests" in whys and _derived_from_tests(template_tests):
        from ..services.verification_target import checks_headline

        whys["template_tests"] = checks_headline(tests)
        if gates[0] == "template_tests":
            next_step = CHECKS_NEXT
    lead: list[str] = []
    if "app_serves" in failing_set and not install:
        from ..services.preview_service import app_serves_headline

        lead = [app_serves_headline(app_serves if isinstance(app_serves, Mapping) else None)]
        if "template_tests" in whys and _derived_from_app_serves(template_tests):
            whys.pop("template_tests")
        if gates[0] == "template_tests":
            next_step = APP_SERVES_NEXT
        needs = list((app_serves or {}).get("needs_secret") or []) if isinstance(app_serves, Mapping) else []
        if needs and gates[0] == "template_tests":
            from .run_env import needs_secret_action

            next_step = needs_secret_action(needs)
    reason = "; ".join([*lead, *whys.values()])
    return {"gates": gates, "reason": reason[:1].upper() + reason[1:], "next_step": next_step,
            "error": f"Blocked: {reason}. {next_step}"}


def held_run(connection: Any, run_id: str) -> Optional[dict]:
    """`hold_outcome` for a code task run, from its recorded quality gate (and the gates the
    pipeline wrote on the run). None when no template gate failed, or the run applies none."""
    from sqlalchemy import text

    row = connection.execute(text(
        "SELECT q.gates_failed, r.finalize_ctx->'failing_gates' AS fctx_failing, "
        "r.finalize_ctx->'install_failure' AS install_failure, "
        "q.evidence->'app_serves' AS app_serves, q.evidence->'template'->'template_tests' AS template_tests, "
        "q.evidence->'template'->'secret_scan' AS secret_scan, q.evidence->'tests' AS tests "
        "FROM code_task_runs r LEFT JOIN quality_gates q ON q.code_task_run_id = r.id "
        "WHERE r.id = :i ORDER BY q.created_at DESC NULLS LAST LIMIT 1"),
        {"i": run_id}).mappings().first()
    if row is None:
        return None
    failing: set = set()
    for value in (row["gates_failed"], row["fctx_failing"]):
        if isinstance(value, list):
            failing |= {str(g) for g in value}
    return hold_outcome(failing, row["install_failure"], app_serves=row.get("app_serves"),
                        template_tests=row.get("template_tests"), secret_scan=row.get("secret_scan"),
                        tests=row.get("tests"))
