"""A trial build of an approved template (F2 §2.5 step 4): its own tests, per stack, run against
its reference implementation in a throwaway build sandbox, and the result recorded on the row
(the gallery's "Tested ✓ node, python").

This EXECUTES the template's code, so it runs only for an approved (or built-in) template, only
through the build sandbox (a per-run uid with a fenced network when the install provides it,
exactly as a build's tests run; `BOSS_SANDBOX_ISOLATION=required` refuses to run unisolated), with
the sandbox's command allowlist and timeout, and never with a secret in its environment.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .gate import JUnitCounts, find_reports, parse_junit
from .manifest import TemplateManifest


def _write_tree(dest: Path, files: dict[str, bytes], prefix: str) -> None:
    old = os.umask(0)
    try:
        for path, content in files.items():
            if not path.startswith(prefix):
                continue
            target = (dest / path[len(prefix):]).resolve()
            if dest.resolve() not in target.parents:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    finally:
        os.umask(old)


def run_stack(manifest: TemplateManifest, files: dict[str, bytes], stack: str, *,
              runner: Callable[[str, Path], Any], workdir: Path) -> dict:
    """One stack's trial in `workdir` (already created for the sandbox's uid)."""
    import time

    spec = manifest.tests[stack]  # type: ignore[index]
    _write_tree(workdir, files, f"stacks/{stack}/")
    started = time.time()
    result = runner(spec.command, workdir)
    counts = JUnitCounts()
    for report in find_reports(workdir, since=started):
        c = parse_junit(report.read_bytes())
        if c is not None:
            counts.add(c)
    ok = (getattr(result, "exit_code", 1) == 0 and counts.tests > 0 and not counts.failures
          and not counts.errors and counts.passed >= spec.min_tests)
    out = {"ok": ok, "tests": counts.tests, "passed": counts.passed,
           "failures": counts.failures + counts.errors, "exit_code": getattr(result, "exit_code", None)}
    if not ok:
        tail = str(getattr(result, "stdout", "") or "") + str(getattr(result, "stderr", "") or "")
        from ..identity.redaction import redact_text

        out["output_tail"] = redact_text(tail[-1500:])
    return out


def run(manifest: TemplateManifest, files: dict[str, bytes], *, settings: Any, trial_id: str,
        runner: Optional[Callable[[str, Path], Any]] = None) -> dict:
    """Every declared stack's trial. `runner(command, cwd)` defaults to the build sandbox's."""
    from ..services import owned_process
    from ..services.sandbox import EphemeralSandbox, SandboxError
    from ..services.sandbox_identity import SandboxUnavailable, bind

    results: dict[str, dict] = {}
    with owned_process.owner_scope("template_trial", trial_id):
        try:
            with bind(trial_id) as ident:
                for stack in manifest.stacks:
                    base = ident.workspace(f"trial-{stack}-") if ident is not None else \
                        Path(tempfile.mkdtemp(prefix=f"boss-trial-{stack}-"))
                    try:
                        if runner is not None:
                            use = runner
                        else:
                            sb = EphemeralSandbox(settings, task_id=trial_id)
                            sb.path = base           # a provisioned tree: the template's own files

                            def use(command: str, cwd: Path, _sb=sb) -> Any:
                                return _sb.run(command, cwd=str(cwd),
                                               timeout=int(getattr(settings, "sandbox_validation_timeout_seconds", 300) or 300))
                        try:
                            results[stack] = run_stack(manifest, files, stack, runner=use, workdir=base)
                        except SandboxError as exc:
                            results[stack] = {"ok": False, "error": str(exc)[:300]}
                    finally:
                        shutil.rmtree(base, ignore_errors=True)
        except SandboxUnavailable as exc:
            return {"status": "not_run", "error": str(exc)[:300],
                    "at": datetime.now(timezone.utc).isoformat(), "stacks": {}}
    return {"status": "passed" if results and all(r.get("ok") for r in results.values()) else "failed",
            "at": datetime.now(timezone.utc).isoformat(), "stacks": results}
