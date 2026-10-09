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
        return "npm install --no-audit --no-fund"
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

        tail = str(getattr(result, "stdout", "") or "") + str(getattr(result, "stderr", "") or "")
        out["error"] = f"installing the app's dependencies failed (exit {out['exit_code']})"
        out["output_tail"] = redact_text(tail[-1500:])
    return out


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


@dataclass
class GateResult:
    gates: dict[str, bool]
    evidence: dict = field(default_factory=dict)
    #: written by the test command; removed so they never reach the PR
    reports_removed: list[str] = field(default_factory=list)


def evaluate(*, template: Mapping[str, Any], files: Mapping[str, bytes], changed: Mapping[str, Any],
             cwd: Optional[Path], run_command: Optional[Callable[[str, Path], Any]],
             bound_values: Optional[Mapping[str, str]] = None, tests_passed: bool = True) -> GateResult:
    """Both gates for one applied template.

    `template` is `{id, version, digest, stack, manifest}` (manifest as a dict), `files` the
    template version's files, `changed` the build's {path: content}, `cwd` the app directory the
    test command runs in, `run_command(command, cwd)` the sandbox's runner (returns an object with
    exit_code/stdout/stderr), `bound_values` the app's bound secret values (scan only)."""
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
    spec = manifest.tests.get(stack)  # type: ignore[call-overload]
    tt: dict[str, Any] = {"command": spec.command if spec else None,
                          "min_tests": spec.min_tests if spec else None}
    ok = False
    if spec is None:
        tt["error"] = f"the template does not support the {stack!r} stack"
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
        elif cwd is None or run_command is None:
            tt["error"] = "the template's tests cannot run in this sandbox mode"
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
