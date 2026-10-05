"""IaC verification lane (spec 05 §3.2 / T3, BOSS-67).

The governed pipeline's Verify axis is Python-`unittest`-only; this module closes that
gap for the IaC domain. When a code task's produced diff touches Terraform/HCL files,
`run_iac_verify_lane` runs — in order, with NO cloud credentials in the subprocess
environment —

    1. `<tf> fmt -check -recursive`                       (style gate)
    2. `<tf> init -backend=false` + `<tf> validate -json` (syntax/provider-schema gate)
    3. `tflint --format=json`                             (lint gate, fails closed)
    4. `checkov` or `tfsec`                               (security gate, fails closed)
    5. `<tf> plan -input=false -refresh=false`            (plan-dry: graph-resolution gate)
    6. plan-live                                          (recorded skipped placeholder —
       requires an approval Decision + scoped read-only creds; out of T3 scope)

`<tf>` is resolved per spec 05 Open Decision #2: BOSS_IAC_TF_BINARY defaults to "auto",
which prefers OpenTofu (`tofu`, MPL-licensed, matches the OSS posture) and falls back to
HashiCorp `terraform`; enterprises that standardize on one can pin it explicitly.

Fail-closed intent: on an IaC task a missing verification tool is a FAILED step
(reason "tool_missing"), never a silent skip — an unverifiable Terraform change must
not pass the gate. plan-dry runs credential-free, so a plan failure caused ONLY by
missing provider credentials still passes the graph-resolution gate (recorded with a
note); any other plan error fails. Everything returned is JSON-serializable so it can
ride quality-gate evidence / the review packet unchanged.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional, Union
from . import owned_process

_IAC_SUFFIXES = (".tf", ".tf.json", ".tfvars", ".hcl")

# Per-step captured output cap (~8KB). Keep head AND tail: JSON reports lead, error
# text trails.
_OUTPUT_CAP = 8192
_STEP_TIMEOUT_SECONDS = 600

# Cloud credentials must never reach the lane's subprocesses (spec 05 §3.2: "in a
# sandbox with no cloud credentials"). Scrubbed by prefix + a few well-known names.
_CRED_ENV_PREFIXES = ("AWS_", "BOSS_AWS_", "ARM_", "TF_TOKEN_")
_CRED_ENV_EXACT = (
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CREDENTIALS",
    "GOOGLE_OAUTH_ACCESS_TOKEN",
)

# plan-dry failure triage (conservative): a graph marker anywhere in the output fails
# the step even if a credential marker is also present; ONLY a clearly credential/authn
# shaped error passes with a note.
_GRAPH_ERROR_MARKERS = (
    "reference to undeclared",
    "unsupported argument",
    "unsupported block type",
    "invalid reference",
    "missing required argument",
    "invalid function argument",
    "invalid expression",
    "duplicate resource",
    "cycle:",
)
_CRED_ERROR_MARKERS = (
    "no valid credential sources",
    "failed to retrieve credentials",
    "unable to locate credentials",
    "credentials could not be found",
    "error validating provider credentials",
    "invalid provider credentials",
    "retrieving aws account details",
    "sts:getcalleridentity",
    "invalidclienttokenid",
    "expiredtoken",
    "401 unauthorized",
    "403 forbidden",
)


@dataclass
class IacStepResult:
    """One lane step. `skipped` steps (the plan-live placeholder) do not count toward
    the lane verdict; a failed required step carries `reason` (e.g. "tool_missing")."""

    step: str
    ok: bool
    output: str = ""
    duration_ms: int = 0
    skipped: bool = False
    reason: Optional[str] = None
    note: Optional[str] = None

    def to_dict(self) -> dict:
        d: dict = {"step": self.step, "ok": self.ok, "output": self.output,
                   "duration_ms": self.duration_ms, "skipped": self.skipped}
        if self.reason:
            d["reason"] = self.reason
        if self.note:
            d["note"] = self.note
        return d


@dataclass
class IacVerifyResult:
    """The whole lane. ok == every non-skipped step passed (fail-closed rollup)."""

    ok: bool
    tf_binary: Optional[str]
    scanner: Optional[str]
    files: list[str]
    steps: list[IacStepResult]

    def to_dict(self) -> dict:
        return {"ok": self.ok, "tf_binary": self.tf_binary, "scanner": self.scanner,
                "files": self.files, "steps": [s.to_dict() for s in self.steps]}


def detect_iac_files(files: Union[Mapping[str, str], Iterable[str]]) -> list[str]:
    """Paths that make a change an IaC change: .tf / .tf.json / .tfvars / .hcl."""
    paths = files.keys() if isinstance(files, Mapping) else files
    return sorted(p for p in paths if str(p).lower().endswith(_IAC_SUFFIXES))


# Task-level IaC detection (spec 05 §3.2 / T4). Deliberately conservative — only an
# explicit task_type or an unambiguous word-boundary mention of the tooling / file
# suffixes routes a task to the governed lane. `\.tf\b` matches "main.tf" / ".tf
# modules" but NOT "platform" (no dot) or ".tfx" (no word boundary after "tf").
_IAC_TASK_RE = re.compile(
    r"\bterraform\b"
    r"|\bopentofu\b"
    r"|\binfrastructure[-\s]+as[-\s]+code\b"
    r"|\.tf\b"
    r"|\.tfvars\b"
    r"|\.hcl\b",
    re.IGNORECASE,
)


def is_iac_task(spec: Optional[Mapping], title: Optional[str]) -> bool:
    """True when a code task is an IaC-authoring task and must run on the governed
    lane (the native inline lane truncates repo context and has no IaC verify lane).

    Positive when spec["task_type"] == "iac", or when the title / instructions /
    acceptance criteria mention Terraform, OpenTofu, "infrastructure as code", or
    .tf/.tfvars/.hcl files (word-boundary regex; no false positives on e.g.
    "platform").
    """
    spec = spec or {}
    if str(spec.get("task_type") or "").strip().lower() == "iac":
        return True
    parts = [str(title or ""), str(spec.get("instructions") or "")]
    parts.extend(str(c) for c in (spec.get("acceptance_criteria") or []))
    return bool(_IAC_TASK_RE.search("\n".join(parts)))


def _scrubbed_env() -> dict[str, str]:
    """The parent env minus anything that smells like a cloud credential."""
    return {
        k: v for k, v in os.environ.items()
        if not (k.startswith(_CRED_ENV_PREFIXES) or k in _CRED_ENV_EXACT)
    }


def _resolve_tf_binary(settings) -> Optional[str]:
    """BOSS_IAC_TF_BINARY: "auto" prefers OpenTofu over terraform (Open Decision #2);
    an explicit name is honored only if present on PATH (missing => fail closed)."""
    pref = (getattr(settings, "iac_tf_binary", "auto") or "auto").strip().lower()
    if pref == "auto":
        for candidate in ("tofu", "terraform"):
            if shutil.which(candidate):
                return candidate
        return None
    return pref if shutil.which(pref) else None


def _resolve_scanner(settings) -> tuple[Optional[str], Optional[str]]:
    """-> (scanner, note). Preferred scanner (default checkov), falling back to the
    other one if the preferred binary is absent; (None, None) when neither exists."""
    pref = (getattr(settings, "iac_security_scanner", "checkov") or "checkov").strip().lower()
    if pref not in ("checkov", "tfsec"):
        pref = "checkov"
    if shutil.which(pref):
        return pref, None
    fallback = "tfsec" if pref == "checkov" else "checkov"
    if shutil.which(fallback):
        return fallback, f"{pref} not found on PATH; fell back to {fallback}"
    return None, None


def _truncate(text: str) -> str:
    if len(text) <= _OUTPUT_CAP:
        return text
    half = _OUTPUT_CAP // 2
    return f"{text[:half]}\n... [output truncated] ...\n{text[-half:]}"


def _exec(argv: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, str, int]:
    """Run one tool invocation; -> (exit_code, combined_output, duration_ms).
    Never raises: a timeout or spawn failure is a failed step, not a crashed lane."""
    started = time.monotonic()
    try:
        proc = owned_process.run(  # noqa: S603 — fixed argv, no shell
            argv, cwd=str(cwd), env=env, capture_output=True, text=True,
            timeout=_STEP_TIMEOUT_SECONDS,
        )
        code, out = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        code, out = -1, f"$ {' '.join(argv)} timed out after {_STEP_TIMEOUT_SECONDS}s"
    except OSError as exc:
        code, out = -1, f"$ {' '.join(argv)} failed to start: {exc}"
    return code, out, int((time.monotonic() - started) * 1000)


def _module_dir(workdir: Path, iac_files: list[str]) -> Path:
    """The deepest directory containing every detected IaC file — where init/validate/
    plan run (fmt is -recursive, so it covers nested modules either way)."""
    dirs = [Path(p).parent.as_posix() for p in iac_files] or ["."]
    common = dirs[0]
    for d in dirs[1:]:
        while common != "." and not (d == common or d.startswith(common + "/")):
            common = Path(common).parent.as_posix()
    candidate = workdir if common in (".", "") else workdir / common
    return candidate if candidate.is_dir() else workdir


def run_iac_verify_lane(workdir, settings, *, files) -> IacVerifyResult:
    """Run the full lane against `workdir` (a checkout containing the IaC files in
    `files`). Cloud credentials are scrubbed from every subprocess; init/plan artifacts
    are kept OUT of the checkout (TF_DATA_DIR + plan output in a scratch dir) so the
    lane never pollutes the PR diff."""
    workdir = Path(workdir)
    iac_files = detect_iac_files(files)
    module_dir = _module_dir(workdir, iac_files)
    env = _scrubbed_env()
    tf = _resolve_tf_binary(settings)
    scanner, scanner_note = _resolve_scanner(settings)
    steps: list[IacStepResult] = []

    pref = getattr(settings, "iac_tf_binary", "auto") or "auto"
    tf_missing_output = (
        f"no terraform/OpenTofu binary found on PATH (BOSS_IAC_TF_BINARY={pref}); "
        "an IaC change cannot be verified — failing closed"
    )

    with tempfile.TemporaryDirectory(prefix="boss-iac-scratch-") as scratch:
        env["TF_DATA_DIR"] = os.path.join(scratch, "tfdata")
        env.setdefault("TF_IN_AUTOMATION", "1")
        plan_out = os.path.join(scratch, "plan.tfplan")
        lock_file = module_dir / ".terraform.lock.hcl"
        lock_existed = lock_file.exists()

        # 1. fmt — style gate.
        if tf is None:
            steps.append(IacStepResult("fmt", False, tf_missing_output, reason="tool_missing"))
        else:
            code, out, ms = _exec([tf, "fmt", "-check", "-recursive"], module_dir, env)
            steps.append(IacStepResult("fmt", code == 0, _truncate(out), ms))

        # 2. init (backendless, offline-friendly) then validate — one gate.
        if tf is None:
            steps.append(IacStepResult("validate", False, tf_missing_output, reason="tool_missing"))
        else:
            code, out, ms = _exec([tf, "init", "-backend=false", "-input=false"], module_dir, env)
            if code != 0:
                steps.append(IacStepResult(
                    "validate", False,
                    _truncate(f"$ {tf} init -backend=false -input=false (exit {code})\n{out}"),
                    ms, reason="init_failed"))
            else:
                vcode, vout, vms = _exec([tf, "validate", "-json"], module_dir, env)
                steps.append(IacStepResult("validate", vcode == 0, _truncate(vout), ms + vms))

        # 3. tflint — lint gate; a missing binary on an IaC task FAILS (never silent-skip).
        if shutil.which("tflint") is None:
            steps.append(IacStepResult(
                "lint", False,
                "tflint binary not found on PATH; the IaC lint gate fails closed",
                reason="tool_missing"))
        else:
            code, out, ms = _exec(["tflint", "--format=json"], module_dir, env)
            steps.append(IacStepResult("lint", code == 0, _truncate(out), ms))

        # 4. security scan — feeds the security review axis; fails closed like tflint.
        if scanner is None:
            steps.append(IacStepResult(
                "security", False,
                "neither checkov nor tfsec found on PATH; the IaC security gate fails closed",
                reason="tool_missing"))
        else:
            argv = (["checkov", "-d", ".", "--output", "json"] if scanner == "checkov"
                    else ["tfsec", ".", "--format", "json"])
            code, out, ms = _exec(argv, module_dir, env)
            steps.append(IacStepResult("security", code == 0, _truncate(out), ms,
                                       note=scanner_note))

        # 5. plan-dry — the graph-resolution gate, credential-free by design. A failure
        #    caused ONLY by missing provider credentials is the expected outcome of the
        #    scrubbed env and still passes; any graph/config error fails.
        if tf is None:
            steps.append(IacStepResult("plan_dry", False, tf_missing_output, reason="tool_missing"))
        else:
            code, out, ms = _exec(
                [tf, "plan", "-input=false", "-refresh=false", f"-out={plan_out}"],
                module_dir, env)
            if code == 0:
                steps.append(IacStepResult("plan_dry", True, _truncate(out), ms))
            else:
                low = out.lower()
                graph_hit = next((m for m in _GRAPH_ERROR_MARKERS if m in low), None)
                cred_hit = next((m for m in _CRED_ERROR_MARKERS if m in low), None)
                if graph_hit is None and cred_hit is not None:
                    steps.append(IacStepResult(
                        "plan_dry", True, _truncate(out), ms,
                        note=(f"plan failed only on missing cloud credentials ({cred_hit!r}) — "
                              "expected in the credential-free plan-dry lane; the graph-"
                              "resolution gate passes")))
                else:
                    steps.append(IacStepResult("plan_dry", False, _truncate(out), ms))

        # 6. plan-live — OUT of T3 scope beyond the config flag: always a recorded
        #    skipped placeholder. Even when enabled it must be gated behind an approval
        #    Decision before scoped read-only creds touch a real provider (spec 05
        #    §3.2 step 5 / Open Decision #3).
        live_enabled = bool(getattr(settings, "iac_plan_live_enabled", False))
        steps.append(IacStepResult(
            "plan_live", True, skipped=True,
            reason=("plan-live enabled but not implemented in this lane yet; requires an "
                    "approval Decision before scoped read-only credentials are used"
                    if live_enabled else
                    "plan-live disabled (BOSS_IAC_PLAN_LIVE_ENABLED=false); requires an "
                    "approval Decision")))

        # init writes .terraform.lock.hcl into the module dir even with TF_DATA_DIR
        # redirected — drop it unless the repo already tracked one (PR-diff hygiene).
        if not lock_existed and lock_file.exists():
            try:
                lock_file.unlink()
            except OSError:
                pass

    overall_ok = all(s.ok for s in steps if not s.skipped)
    return IacVerifyResult(ok=overall_ok, tf_binary=tf, scanner=scanner,
                           files=iac_files, steps=steps)
