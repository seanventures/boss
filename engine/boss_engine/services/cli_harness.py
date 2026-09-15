"""Coding agents that are a command line, declared rather than coded.

`ClaudeCodeAgent` is 400 lines of process handling written for one binary. Adding Codex or
Cursor that way means writing it again, twice, and the harness registry (BOSS-409) would be a
registry of hand-written special cases — which is the thing it was built to stop being.

So a CLI harness is a SPEC: what to run, how the prompt is delivered, which flag picks the
model, and what to pass when the step is read-only. Adding a provider is a dict entry, and an
operator whose CLI takes different flags can correct them through `BOSS_CLI_HARNESSES_JSON`
without waiting for a release.

What these agents have in common, and why BOSS does not care what they print
---------------------------------------------------------------------------
They all edit files in place in the working directory. So the change of record is the git
diff, and the authoritative pass/fail is BOSS re-running the tests itself — exactly as for
`claude -p`. Their stdout is kept as the summary and is never trusted for anything else,
which is what makes a new one cheap to add: BOSS only has to be able to START it.

Cost
----
CLI agents bill against a subscription and do not report per-call spend the way an API does.
Recorded as zero and flagged, rather than invented: a made-up number in the ledger would
corrupt the one place BOSS answers "what did this cost".
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Optional

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CliHarnessSpec:
    """Everything BOSS needs to drive one coding CLI."""

    name: str
    #: The executable. Looked up on PATH; a missing binary is a clear error, not a crash.
    binary: str
    #: Argv before the prompt. `{model}` is substituted when a model is supplied.
    args: tuple[str, ...] = ()
    #: Extra argv for a READ-ONLY step (review/security). Empty means the harness has no
    #: read-only mode, and BOSS refuses to use it for one rather than letting a reviewer edit.
    read_only_args: tuple[str, ...] = ()
    #: How the prompt reaches the process. argv is simplest; stdin survives a huge prompt.
    prompt_via: str = "argv"
    #: Flag that selects a model, e.g. "--model". Empty means the CLI has no such flag.
    model_flag: str = ""
    #: Seconds before BOSS gives up on a build step. Review steps get review_timeout.
    timeout: int = 2400
    review_timeout: int = 420
    #: Has a governed code task actually completed on this harness?
    verified: bool = False
    description: str = ""
    #: Env the CLI needs to find its own credentials, on top of the agent env allow-list.
    env_passthrough: tuple[str, ...] = ()

    def supports_read_only(self) -> bool:
        return bool(self.read_only_args)


#: The CLIs BOSS ships a spec for. NONE is marked verified: a spec written from a CLI's
#: documentation is a hypothesis until a governed task completes on it, and presenting an
#: untested adapter as ready is how a registry stops meaning anything.
BUILTIN_CLI_HARNESSES: dict[str, CliHarnessSpec] = {
    "codex": CliHarnessSpec(
        name="codex",
        binary="codex",
        args=("exec", "--skip-git-repo-check"),
        # `codex exec` is the headless mode. There is no documented read-only switch, so a
        # review step must not run on it — see `supports_read_only`.
        read_only_args=(),
        prompt_via="argv",
        model_flag="--model",
        verified=False,
        description="OpenAI Codex CLI (`codex exec`). Authorised by the Codex CLI's own "
                    "login, not an API key — see the openai_codex_chatgpt entitlement.",
        env_passthrough=("CODEX_HOME", "OPENAI_API_KEY"),
    ),
    "cursor": CliHarnessSpec(
        name="cursor",
        binary="cursor-agent",
        args=("-p",),
        read_only_args=(),
        prompt_via="argv",
        model_flag="--model",
        verified=False,
        description="Cursor CLI agent in print mode. Authorised by Cursor's own login.",
        env_passthrough=("CURSOR_API_KEY",),
    ),
    "aider": CliHarnessSpec(
        name="aider",
        binary="aider",
        args=("--yes-always", "--no-auto-commit", "--message"),
        read_only_args=(),
        prompt_via="argv",
        model_flag="--model",
        verified=False,
        description="Aider, non-interactive single message.",
        env_passthrough=("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"),
    ),
}


def load_specs(settings) -> dict[str, CliHarnessSpec]:
    """Built-in specs, with operator overrides from BOSS_CLI_HARNESSES_JSON merged on top.

    The override exists because these flags are a guess until someone runs them. An operator
    who finds `codex exec` wants a different argument should be able to fix it in an env var
    at 2am, not wait for a release.
    """
    specs = dict(BUILTIN_CLI_HARNESSES)
    raw = (getattr(settings, "cli_harnesses_json", "") or "").strip()
    if not raw:
        return specs
    try:
        overrides = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        _log.warning("BOSS_CLI_HARNESSES_JSON is not valid JSON (%s); using built-in specs",
                     exc)
        return specs
    for name, patch in (overrides or {}).items():
        if not isinstance(patch, dict):
            continue
        base = specs.get(name)
        fields = {f: getattr(base, f) for f in CliHarnessSpec.__dataclass_fields__} if base else {"name": name, "binary": name}
        for key, value in patch.items():
            if key not in CliHarnessSpec.__dataclass_fields__:
                continue
            if key in ("args", "read_only_args", "env_passthrough") and isinstance(value, list):
                value = tuple(value)
            fields[key] = value
        fields["name"] = name
        try:
            specs[name] = CliHarnessSpec(**fields)
        except Exception as exc:  # noqa: BLE001
            _log.warning("ignoring bad CLI harness override %r: %s", name, exc)
    return specs


class CliHarnessUnavailable(RuntimeError):
    """The binary is not installed, or the step needs something the CLI cannot do."""


class GenericCliCodeAgent:
    """Drive any coding CLI that edits files in the working directory."""

    def __init__(self, settings, spec: CliHarnessSpec) -> None:
        self.settings = settings
        self.spec = spec
        self.engine = spec.name

    # -- process ------------------------------------------------------
    def _resolve_binary(self) -> str:
        found = shutil.which(self.spec.binary)
        if not found:
            raise CliHarnessUnavailable(
                f"the {self.spec.name!r} harness needs {self.spec.binary!r} on PATH and it is "
                f"not installed. Install the CLI and sign it in, or choose another harness "
                f"(BOSS_CODE_AGENT_ENGINE / BOSS_PROVIDER_LOCK)."
            )
        return found

    def _env(self) -> dict:
        from .agent_env import agent_environment

        env = agent_environment(os.environ)
        # A CLI signed in with its own OAuth needs its own home/config to find the session.
        for name in self.spec.env_passthrough:
            value = os.environ.get(name)
            if value:
                env[name] = value
        env.setdefault("HOME", os.environ.get("HOME", ""))
        env["CI"] = "1"
        return env

    def _argv(self, prompt: str, *, read_only: bool, model: Optional[str]) -> list[str]:
        argv = [self._resolve_binary(), *self.spec.args]
        if read_only:
            argv += list(self.spec.read_only_args)
        if model and self.spec.model_flag:
            argv += [self.spec.model_flag, model]
        if self.spec.prompt_via == "argv":
            argv.append(prompt)
        return argv

    def run_role(self, prompt: str, cwd: Path, *, read_only: bool = False,
                 model: Optional[str] = None, resume_session_id: Optional[str] = None,
                 **_ignored) -> dict:
        from .llm_telemetry import record_llm_call
        from .scheduling import low_priority

        if read_only and not self.spec.supports_read_only():
            # "The reviewer cannot edit" is an invariant of the governed pipeline. A CLI with
            # no read-only mode cannot hold it, so it is refused for the step rather than
            # trusted to behave.
            raise CliHarnessUnavailable(
                f"{self.spec.name!r} has no read-only mode, so it cannot run a review step "
                f"without being able to edit the code it is reviewing. Set "
                f"BOSS_CODE_REVIEW_ENGINE to a harness that can (any API lane), or give this "
                f"one read_only_args via BOSS_CLI_HARNESSES_JSON."
            )
        argv = self._argv(prompt, read_only=read_only, model=model)
        timeout = self.spec.review_timeout if read_only else self.spec.timeout
        started = time.monotonic()
        try:
            proc = subprocess.run(
                low_priority(argv),
                cwd=str(cwd),
                input=(prompt if self.spec.prompt_via == "stdin" else None),
                capture_output=True, text=True, timeout=timeout,
                start_new_session=True, env=self._env(),
            )
        except subprocess.TimeoutExpired:
            record_llm_call(engine_name=self.engine, usage={"model": model or ""},
                            latency_ms=int((time.monotonic() - started) * 1000),
                            ok=False, error=f"timed out after {timeout}s")
            raise CliHarnessUnavailable(
                f"{self.spec.name} did not finish within {timeout}s") from None
        ms = int((time.monotonic() - started) * 1000)
        ok = proc.returncode == 0
        summary = (proc.stdout or "").strip() or (proc.stderr or "").strip()
        record_llm_call(
            engine_name=self.engine,
            # Cost is NOT invented: a subscription CLI does not report per-call spend, and a
            # guessed number would corrupt the ledger BOSS answers cost questions from.
            usage={"model": model or self.spec.name},
            cost_usd_micros=0, latency_ms=ms, turns=1, ok=ok,
            error=None if ok else (proc.stderr or "")[:400],
        )
        if not ok:
            _log.warning("%s exited %s: %s", self.spec.name, proc.returncode,
                         (proc.stderr or "")[:300])
        return {
            "summary": summary[:8000] or f"({self.spec.name} produced no output)",
            "usage": {"model": model or self.spec.name},
            "cost_usd_micros": 0,
            "cost_unreported": True,
            "turns": 1,
            "model": model or self.spec.name,
            "exit_code": proc.returncode,
        }

    def run(self, spec, repo, sandbox):
        """Whole-task entry point. BOSS re-runs the tests; the diff is the change of record."""
        from .code_agent import CodeAgentResult, load_repo_guide

        if sandbox.path is None:
            raise RuntimeError("Sandbox must be provisioned before running the agent.")
        criteria = "\n".join(f"- {c}" for c in spec.acceptance_criteria) or "- (none)"
        tests = "\n".join(f"- {c}" for c in spec.test_commands) or "- (none)"
        guide = load_repo_guide(sandbox.path) or ""
        prompt = (
            "You are a senior software engineer working in a checked-out git repository.\n"
            f"TASK: {spec.title}\n\n{spec.instructions}\n\n"
            f"ACCEPTANCE CRITERIA:\n{criteria}\n\n"
            f"TEST/BUILD COMMANDS (make these pass):\n{tests}\n"
            + (f"\nREPO GUIDE (the repository's own instructions — follow them):\n{guide}\n"
               if guide else "")
            + "\nEdit files in place. Do NOT create a branch, commit, or push — BOSS handles "
              "git, the pull request and the merge gate.\n"
        )
        out = self.run_role(prompt, sandbox.path)
        results = [sandbox.run(cmd) for cmd in spec.test_commands]
        return CodeAgentResult(
            summary=out["summary"], diff=sandbox.diff(), diff_summary=sandbox.diff_summary(),
            tests=results, tests_passed=all(r.ok for r in results) if results else False,
            cost_usd_micros=0, usage=out["usage"], transcript=out["summary"],
            turns=1, engine=self.engine,
        )
