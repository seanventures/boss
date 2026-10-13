"""APP-3 (#93) — web-appropriate verification target.

The governed loop's authoritative check was hardcoded to ``python3 -m unittest discover -v`` (spec 09
§2.4). A React/Next/Vite app's *actual* health signals — does it compile, typecheck, lint, render —
were never run, so Amplify (the repair loop) had no web-native signal to optimize against: it could
make a page "more correct" by unittest while it stayed broken as a web app.

This resolves the verification command set from the **project type**:

  - **web / TS** → ``npm run build`` (compiles) → ``tsc --noEmit`` (typechecks) → ``eslint`` (lints) →
    ``playwright test`` (a minimal e2e that loads the primary route and asserts real content). These are
    exactly the scripts APP-1's scaffold defines, so the target is knowable in advance.
  - **python** → the existing ``unittest`` default, unchanged.

Additive and profile-selected: explicit ``test_commands`` on the task always win; Python tasks are
untouched. Because the repair loop re-issues with whatever the authoritative check printed, switching
the command set automatically gives Amplify **build/typecheck/e2e failure evidence** for web tasks.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .design_prompts import is_ui_bearing

VERIFY_SCRIPT_STEPS = (
    "typecheck",
    "build",
    "lint",
    "test:api",
    "test:e2e",
    "healthcheck",
)

PYTHON_TEST_CMD = "python3 -m unittest discover -v"
#: The Python default when the repo is a pytest project. ``unittest discover`` finds no tests in
#: a suite of bare ``def test_...`` functions and exits 5 ("NO TESTS RAN") on most such repos —
#: or worse, exits 0 having collected only the few TestCase classes.
PYTEST_CMD = "python3 -m pytest -q"

# SEPARATE COMMANDS, NOT A SHELL CHAIN.
#
# This was one string joined with `&&` — "so it fits the pipeline's single-command model" —
# and the sandbox deliberately runs commands WITHOUT a shell, refusing anything containing
# operators. The two rules met for the first time on a real build and the build lost:
#
#   '[ -d node_modules ] || npm install … && npm run build && …' uses shell syntax, and
#   sandbox commands run WITHOUT a shell.
#
# The sandbox is right — an argv[0]-only check would have run `npm install` and silently
# skipped the tests. And the single-command model was never the contract: `test_commands` is a
# LIST and the runner already does `[sandbox.run(cmd) for cmd in spec.test_commands]`, which
# gives the same short-circuit (the first failure is the one the repair sees) with no shell.
#
# The install guard goes too: `[ -d node_modules ] ||` is shell syntax, and `npm install` on a
# populated tree is already close to a no-op. Keeping the install FIRST is what makes the
# target self-sufficient on a fresh clone — without it `npm run build` exits 127 and the
# failure reads as the task's fault when it is the environment's.
# `--include=dev` because BOSS's own image sets NODE_ENV=production, and npm then skips
# devDependencies — where tsc, vite and every other BUILD tool lives. docs-freshness-board's
# preview died on "tsc: not found" (2026-10-03) for exactly that. Every install BOSS runs to
# build or test an app is this one command; preview_service imports it rather than retyping it.
NPM_INSTALL_CMD = "npm install --include=dev --no-audit --no-fund --loglevel=error"
_WEB_STEPS = ["npm run build", "npm run typecheck", "npm run lint", "npm run test:e2e"]
WEB_TEST_COMMANDS = [NPM_INSTALL_CMD, *_WEB_STEPS]
#: Display only — the console shows the sequence as one line. Never execute this.
WEB_TEST_CMD = " && ".join(WEB_TEST_COMMANDS)

#: One step of a multi-service plan: an install that downloads a toolchain's dependencies, a
#: Playwright browser install or a full e2e suite.
STACK_STEP_TIMEOUT_SECONDS = 900


def run_until_failure(sandbox, commands):
    """Run verification commands in order and STOP at the first failure.

    These used to be one `&&` chain, which short-circuits for free. Splitting them into a list
    — necessary, because the sandbox refuses shell operators — dropped that property silently:
    `[sandbox.run(c) for c in commands]` runs all of them however badly the first one goes. A
    web build whose compile failed still paid for a typecheck, a lint and a full Playwright
    suite, and buried the one real error under the cascade of a build that produced nothing.

    Returns the results actually run, so `all(t.ok for t in tests)` reads the same as before.
    """
    results = []
    for command in commands:
        result = sandbox.run(command)
        results.append(result)
        if not getattr(result, "ok", False):
            break
    return results


# A DEPENDENCY INSTALL THAT FAILED IS NOT A FAILING TEST.
#
# HelpDesk AI (crun_6eaa478dc33a42c6bdb7) failed BOSS's `npm install` with ERESOLVE three times;
# the fix round was told "the tests are FROZEN ... fix ONLY the implementation" under a
# `$ npm run verify (exit 1)` header, and the run ended "the template's tests did not pass".
# An install failure is named as one: in the fix round's evidence (with what to do) and in the
# run's error. There is deliberately NO --legacy-peer-deps / --force fallback: it would hide a
# broken tree that the app's own installs (a `pretest` script, a deploy) then trip on.
_INSTALL_COMMAND = re.compile(
    r"^\s*(?:npm\s+(?:install|i|ci)\b|pnpm\s+(?:install|i)\b|yarn(?:\s+install)?\s*$"
    r"|python3?\s+-m\s+pip\s+install\b|pip3?\s+install\b|mvn\b.*dependency:)")
#: The one instruction a fix round gets with an install failure.
INSTALL_REPAIR_INSTRUCTION = (
    "Resolve the dependency conflict in package.json (pick versions whose peer dependencies "
    "agree); do not use --force or --legacy-peer-deps, in scripts or in .npmrc.")


def is_install_command(command: str) -> bool:
    """True for a dependency install (npm install/ci, pnpm, yarn, pip, maven go-offline)."""
    return bool(_INSTALL_COMMAND.match(str(command or "")))


def install_error_summary(output: str) -> str:
    """The install's error in one line: npm's code and the lines that say what conflicted
    ("ERESOLVE unable to resolve dependency tree; Found: eslint@9.39.5; Could not resolve
    dependency: peer eslint@... from eslint-plugin-react-hooks@4.6.2"), else its last line."""
    lines = [ln for ln in _meaningful_lines(output)]
    npm = [re.sub(r"^npm (?:error|ERR!)\s*", "", ln).strip() for ln in lines
           if re.match(r"^npm (?:error|ERR!)", ln)]
    npm = [ln for ln in npm if ln]
    if npm:
        code = next((ln.split(None, 1)[1] for ln in npm if ln.startswith("code ")), "")
        keep = []
        for ln in npm:
            if ln.startswith(("code ", "A complete log", "For a full report", "/", "Fix the upstream",
                              "this command with", "to accept an incorrect", "node_modules/",
                              "While resolving", "Could not resolve dependency:")):
                continue
            if ln.startswith(("Found:", "peer ", "ERESOLVE",
                              "notarget", "404", "gyp ERR! stack Error", "command failed",
                              "path ", "Conflicting peer", "code E")) or not keep:
                keep.append(ln)
        head = (code if code and not code.isdigit() and not any(k.startswith(code) for k in keep)
                else "")
        return "; ".join(([head] if head else []) + keep[:6])[:600]
    return (lines[-1] if lines else "(the install printed nothing)")[:600]


def install_failure(result) -> dict | None:
    """{command, exit_code, summary} when `result` is a FAILED dependency install, else None."""
    if result is None or getattr(result, "ok", True):
        return None
    command = str(getattr(result, "command", "") or "")
    if not is_install_command(command):
        return None
    out = (getattr(result, "stdout", "") or "") + "\n" + (getattr(result, "stderr", "") or "")
    return {"command": command, "exit_code": getattr(result, "exit_code", None),
            "timed_out": bool(getattr(result, "timed_out", False)),
            "summary": install_error_summary(out)}


def install_repair_evidence(failure: dict, output: str = "") -> str:
    """What a fix round is told when BOSS's own install failed: that it was the install, the
    command BOSS runs, npm's error text, and the one instruction."""
    how = "timed out" if failure.get("timed_out") else f"exit {failure.get('exit_code')}"
    tail = "\n".join(_meaningful_lines(output)[-VERIFY_FAILURE_LINES:])
    return (f"BOSS's dependency install failed, so nothing else ran: `{failure.get('command')}` "
            f"({how}) in a clean sandbox.\n{tail}\n\n{INSTALL_REPAIR_INSTRUCTION} Then run "
            f"exactly `{failure.get('command')}` yourself and make sure it passes.")


#: How many times one hand-off may go back to the warm session for BOSS's install alone (the
#: implement budget ends it sooner). The retry of HelpDesk AI (crun_1fdfea1c9c904a8083dd) fixed one
#: conflict, met a second, and fixed that one only after BOSS's last install check had run.
INSTALL_FIX_ROUNDS = 4


def install_fix_prompt(failure: dict, output: str, round_i: int) -> str:
    """The warm session's brief when BOSS's install failed on its hand-off: the evidence, what
    BOSS itself put in package.json (it adds only what is missing, so declaring a compatible
    version keeps it), and that BOSS runs the install again before anything else."""
    added = [a for a in (failure.get("boss_added") or []) if a]
    where = failure.get("cwd") or "."
    return (
        f"--- BOSS'S INSTALL FAILED (round {round_i}) ---\n"
        + install_repair_evidence(failure, output) + "\n"
        + (f"Before installing, BOSS added these missing dev dependencies to package.json: "
           f"{', '.join(added)}. It adds only what is missing: declare a version of each that "
           "agrees with your other packages, or keep them as they are.\n" if added else "")
        + f"Work in `{where}`. BOSS runs the same install again as soon as you stop, before any "
        "test or review; the build cannot finish until it passes. Change only package.json (and "
        "the code a version change needs); end with one line.\n")


def install_rounds(check, ask, *, rounds: int = INSTALL_FIX_ROUNDS, over_budget=lambda: False,
                   emit=lambda kind, data: None) -> bool:
    """The edit -> install -> edit loop, with the pipeline's parts passed in.

    `check()` runs BOSS's exact install and returns None when it passed, else (failure, output);
    `ask(prompt)` sends the prompt to the warm session and returns False when it could not.
    The install is checked again after every reply. Returns True when the install passes."""
    for round_i in range(1, rounds + 2):
        checked = check()
        if checked is None:
            if round_i > 1:
                emit("code_task.install_fixed", {"rounds": round_i - 1})
            return True
        failure, output = checked
        emit("code_task.install_failed", {"round": round_i, "command": failure.get("command"),
                                          "summary": failure.get("summary"),
                                          "boss_added": failure.get("boss_added") or []})
        if round_i > rounds or over_budget():
            return False
        if not ask(install_fix_prompt(failure, output, round_i)):
            return False
    return False


#: How much of a verify run is kept as its evidence (code_task_runs.state.last_verify_output).
VERIFY_LOG_LIMIT = 4000
#: When the tail is all BOSS's own bookkeeping, how many of the failing command's last lines are
#: added back, whatever came before them.
VERIFY_FAILURE_LINES = 40
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _meaningful_lines(text: str) -> list[str]:
    """The lines a person would read: no ANSI colour, no blank lines, no BOSS bookkeeping."""
    out = []
    for line in _ANSI.sub("", text or "").splitlines():
        line = line.rstrip()
        if line.strip() and not line.lstrip().startswith("[boss]"):
            out.append(line)
    return out


def verify_log(header: str, results, *, error: str | None = None,
               limit: int = VERIFY_LOG_LIMIT) -> str:
    """THE ONE RECORD of a verify run: what last_verify_output holds and the repair reads.

    A failed HelpDesk build saved "[boss] \n\n\n" (crun_b4613dcfd3a44e5fba3f): a stack plan that
    refused had an empty description, its failure was written from that description instead of
    its error, and every command's output was empty — so the real reason was lost. Here:

      * the header is the plan's description, or its error when the description says nothing;
      * every command's stdout and stderr follow, and the TAIL is kept (errors print last);
      * when what is kept holds no line of the failing command's own output, its last lines and
        its exit (or timeout) are appended, so the record never ends at "[boss]";
      * credentials are redacted (redaction.redact_secrets, which also removes a run's own
        build-service password), never the code or file paths the repair needs.
    """
    from .redaction import redact_secrets

    results = list(results or [])
    head = (header or "").strip() or (error or "").strip()
    body = "".join(((getattr(r, "stdout", "") or "") + "\n" + (getattr(r, "stderr", "") or "") + "\n")
                   for r in results)
    text = (f"[boss] {head}\n" + body)[-limit:]
    failing = next((r for r in reversed(results) if not getattr(r, "ok", False)), None)
    if failing is not None or error:
        own = _meaningful_lines(((getattr(failing, "stdout", "") or "") + "\n"
                                 + (getattr(failing, "stderr", "") or "")) if failing else "")
        if not own or not any(line in text for line in own[-3:]):
            command = (getattr(failing, "command", "") or "").strip() if failing else ""
            if failing is not None and getattr(failing, "timed_out", False):
                how = "timed out"
            elif failing is not None:
                how = f"exit {getattr(failing, 'exit_code', None)}"
            else:
                how = "refused"
            said = own[-VERIFY_FAILURE_LINES:] or [
                (error or "").strip() or "(the command printed nothing)"]
            tail = (f"[boss] {command or 'the check'} {how}:\n" + "\n".join(said) + "\n")
            text = (text.rstrip("\n") + "\n" + tail)[-limit:]
    return redact_secrets(text)


def run_steps_until_failure(sandbox, steps, *, timeout=None):
    """``run_until_failure`` for steps that each name their directory (a manifest plan)."""
    results = []
    for command, cwd in steps:
        result = sandbox.run(command, cwd=str(cwd), timeout=timeout)
        results.append(result)
        if not getattr(result, "ok", False):
            break
    return results


# Live official BugBox/HookWatch/ApproveIt failed because the model rewrote
# package.json away from the scaffold: echo stubs, ESLint 6, bare `playwright
# test` (image-global CLI has no `test` command), and `vite` not listed so
# `vite: not found`. These deps make `npm install` + `npm run verify` honest.
REQUIRED_WEB_DEV_DEPS = {
    "vite": "^5.3.1",
    "@playwright/test": "^1.45.0",
    "typescript": "^5.4.5",
    "@types/react": "^18.3.3",
    "@types/react-dom": "^18.3.0",
    "eslint": "^8.57.0",
    "eslint-plugin-react": "^7.34.3",
    "eslint-plugin-react-hooks": "^4.6.2",
    "@typescript-eslint/eslint-plugin": "^7.13.1",
    "@typescript-eslint/parser": "^7.13.1",
}

#: The lint plugins above are the eslint-8 set (eslint-plugin-react-hooks 4 declares a peer of
#: eslint <= 8, @typescript-eslint 7 of eslint 8). An app that declares eslint 9 or later owns its
#: lint toolchain (flat config; it lists the plugins it uses), so BOSS adds none of them: adding
#: them is an ERESOLVE. HelpDesk AI (crun_6eaa478dc33a42c6bdb7) declared eslint ^9; BOSS added
#: eslint-plugin-react-hooks ^4.6.2 before every install, the agent removed it in its fix round,
#: BOSS put it back, and all three of BOSS's installs failed in 0.7 s.
ESLINT_COMPANIONS = frozenset({"eslint-plugin-react", "eslint-plugin-react-hooks",
                               "@typescript-eslint/eslint-plugin", "@typescript-eslint/parser"})

# Live official follow-ups used `exit 0`, `node -e 'process.exit(0)'`,
# and `playwright test || true` to make verify green. Those are stubs.
_STUB_SCRIPT = re.compile(
    r"""(?:^|[;&|])\s*(?:echo\s+|process\.exit\(0\)|exit\s+0\b)
        |skipping
        |\|\|\s*(?:true|exit\s+0)\b
        |^\s*true\s*$
        |sleep\s+\d+\s*&&\s*kill""",
    re.I | re.X,
)
_BARE_VITE = re.compile(r"(^|[;&|]\s*)vite\b")
_PLAYWRIGHT_BARE = re.compile(
    r"^(?:npx\s+)?playwright(?:\s+test)?$",
    re.I,
)
_VERIFY_DISPROOF = re.compile(
    r"webServer was not able to start"
    r"|webServer exited early"
    r"|Process from config\.webServer exited early"
    r"|Executable doesn.?t exist"
    r"|placeholder passing test"
    r"|expect\(true\)\.toBeTruthy\(\)"
    r"|sleep\s+\d+\s*&&\s*kill"
    r"|Cannot find module"
    r"|Failed to load PostCSS"
    r"|error: unknown command"
    r"|vite: not found"
    r"|ENOENT"
    r"|command not found"
    r"|process\.exit\(0\)"
    r"|^\s*>\s*exit\s+0\b",
    re.I | re.M,
)
PLAYWRIGHT_INSTALL_CMD = (
    "npx --package=@playwright/test playwright install chromium"
)
_STOP_WORDS = {
    "start", "with", "one", "command", "and", "confirm", "the", "it",
    "appears", "on", "for", "after", "refresh", "from", "that", "this",
    "with", "into", "then", "when", "from", "into", "must", "does",
}

_WEB_KINDS = {"web", "spa", "frontend", "site", "landing", "nextjs", "vite"}


def detect_kind(spec: dict[str, Any], title: str = "") -> str:
    """Classify the project as ``web`` or ``python``. An explicit ``project_kind`` wins; otherwise a
    UI-bearing task (APP-2's detector) implies web. Everything else stays Python."""
    pk = str(spec.get("project_kind", "")).lower()
    if pk in _WEB_KINDS:
        return "web"
    if pk in ("python", "py", "backend", "lib"):
        return "python"
    return "web" if is_ui_bearing(spec, title) else "python"


def default_test_commands(spec: dict[str, Any], title: str = "") -> list[str]:
    """The verification target when the task didn't specify one — web command set for web projects,
    the unittest default otherwise. (Callers use this as the fallback for ``spec['test_commands']``.)"""
    return (list(WEB_TEST_COMMANDS) if detect_kind(spec, title) == "web"
            else [PYTHON_TEST_CMD])


def resolve_authoritative_test_commands(spec: dict[str, Any], title: str = "") -> list[str]:
    """The command list the quality gate must run, in order.

    ``test_commands`` is a list. Taking ``[0]`` was correct only when the web
    target was one ``&&`` string. After it was split so the sandbox could run
    it, ``[0]`` is ``npm install`` and the rest of the suite never ran.
    """
    raw = spec.get("test_commands")
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    if isinstance(raw, (list, tuple)) and raw:
        return [str(item).strip() for item in raw if str(item).strip()]
    return list(default_test_commands(spec, title))


def _mentions_pytest(path: Path) -> bool:
    try:
        return path.is_file() and "pytest" in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def is_pytest_project(root: Path | str) -> bool:
    """A repo whose tests are written for pytest: a conftest, a pytest.ini, or pytest named in
    its pyproject/setup.cfg/tox.ini. A bare pyproject without pytest stays on unittest, which
    is all such a repo can be assumed to have installed."""
    base = Path(root)
    if any((base / rel).is_file() for rel in ("conftest.py", "tests/conftest.py", "pytest.ini")):
        return True
    return any(_mentions_pytest(base / name) for name in ("pyproject.toml", "setup.cfg", "tox.ini"))


@dataclass(frozen=True)
class PinnedVerifyManifest:
    """The repo's ``boss.verify.yaml`` as it stood BEFORE any agent touched the checkout.

    The gate must not take its instructions from the tree it is judging: the implementer edits
    that tree, and a manifest re-read from it can be narrowed (``paths: ["nothing/**"]``), have
    its suite deleted or its ``run`` pointed at a no-op — and the gate then passes on the
    agent's own terms. So the manifest is read once, right after the clone, and every verify of
    the run plans from this copy. ``text`` is None when the repo declares none.
    """

    text: str | None
    manifest: Any = None
    error: str | None = None


def pin_verify_manifest(root: Path | str) -> PinnedVerifyManifest:
    """Read and parse the manifest of a freshly provisioned checkout. Call before any agent."""
    from . import verify_manifest

    path = Path(root) / verify_manifest.MANIFEST_NAME
    if not path.is_file():
        return PinnedVerifyManifest(None)
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        return PinnedVerifyManifest(text, verify_manifest.parse_verify_manifest(text))
    except verify_manifest.VerifyManifestError as exc:
        return PinnedVerifyManifest(text, error=str(exc))


def verify_manifest_tampered(pinned: PinnedVerifyManifest, root: Path | str) -> str | None:
    """Why the run's checkout no longer carries the pinned manifest, or None when it does.

    Like a frozen test, the verify declaration is the bar, not part of the change: creating,
    editing or deleting it in a code task is tampering, whatever the gate would have made of it.
    """
    from . import verify_manifest

    path = Path(root) / verify_manifest.MANIFEST_NAME
    try:
        now = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
    except OSError:
        now = None
    if now == pinned.text:
        return None
    what = ("created" if pinned.text is None else "deleted" if now is None else "modified")
    return (f"This run {what} {verify_manifest.MANIFEST_NAME}. It declares how the change is "
            "verified, so a code task may not change it: the gate keeps verifying with the base "
            f"commit's version. Restore {verify_manifest.MANIFEST_NAME} exactly as it was; a "
            "change to the verify declaration is its own reviewed pull request.")


@dataclass
class GatePlan:
    """What the authoritative verify runs, where, and why. One decision for every caller.

    ``source`` is ``task`` (explicit test_commands), ``manifest`` (boss.verify.yaml),
    ``manifest_error`` (a boss.verify.yaml that does not validate — the gate fails closed with
    ``error``) or ``default`` (the project-type default).
    """

    commands: list[str]
    cwd: Path
    source: str
    description: str
    timeout_seconds: int | None = None
    error: str | None = None
    suites: list[dict[str, Any]] = field(default_factory=list)
    #: Manifest plans only: (command, absolute directory) in order. Suites may run in
    #: different directories, so one ``cwd`` cannot describe them.
    steps: list[tuple[str, Path]] = field(default_factory=list)
    #: Manifest plans only: changed code files no planned test covers. The gate fails on them.
    uncovered: list[str] = field(default_factory=list)
    #: Multi-service plans only (source "stack"): the services.stacks.StackPlan — each
    #: service's own steps, then the backend started and the e2e run against it
    #: (services.stack_verify).
    stack: Any = None

    @property
    def from_manifest(self) -> bool:
        return self.source in ("manifest", "manifest_error")

    @property
    def runs_steps(self) -> bool:
        """Run as `steps` from the repo root, no web scaffold repair: a manifest plan, or a
        stack plan from an opt-in toolchain (services.toolchains), including the one that says
        the toolchain is missing."""
        return self.from_manifest or self.source in ("toolchain", "toolchain_missing", "stack",
                                                     "stack_missing")


_DIR_FLAG = re.compile(r"(?:^|\s)(?:--prefix|--cwd|--dir|-C)(?:=|\s+)\S+")


def names_its_directory(command: str) -> bool:
    """Does this package-manager command already say which directory it runs in?"""
    return named_directory(command) is not None


def named_directory(command: str) -> str | None:
    """The directory a package-manager command names (`npm --prefix X`), or None."""
    head = str(command or "").strip().split(" ", 1)[0]
    if head not in ("npm", "npx", "pnpm", "yarn"):
        return None
    m = _DIR_FLAG.search(command)
    if not m:
        return None
    value = re.split(r"=|\s+", m.group(0).strip(), maxsplit=1)[-1].strip("'\"")
    return value or None


def resolve_gate_plan(
    spec: dict[str, Any],
    title: str,
    root: Path | str,
    *,
    app_id: str | None = None,
    changed: list[str] | None = None,
    pinned: PinnedVerifyManifest | None = None,
    layout: Any = None,
) -> GatePlan:
    """The verify plan for a provisioned checkout.

    Precedence: explicit task ``test_commands`` > the repo's ``boss.verify.yaml`` > the
    project-type default (pytest for a pytest project). The manifest scopes by the changed
    files and runs from the repo root, because its globs are root-relative. Everything else
    runs where ``resolve_verify_cwd`` puts it, which now sees the changed files too: a change
    under ``apps/web`` in a repo without an official app id verifies in ``apps/web``.

    ``pinned`` is the manifest read before any agent ran (``pin_verify_manifest``). The gate
    always passes it; without it the manifest is read from ``root`` now, which is right only
    for a checkout no agent has touched.
    """
    from . import verify_manifest

    base = Path(root)

    def _cwd(commands: list[str]) -> Path:
        # resolve_verify_cwd looks for a package.json ancestor of the changed files, which
        # only means something to npm. A Python command stays at the root it was written for.
        npm = bool(commands) and commands[0].split(" ", 1)[0] in ("npm", "npx", "pnpm", "yarn")
        return resolve_verify_cwd(base, app_id=app_id, changed=changed if npm else None)

    raw = spec.get("test_commands")
    if (isinstance(raw, str) and raw.strip()) or (isinstance(raw, (list, tuple)) and raw):
        commands = resolve_authoritative_test_commands(spec, title)
        # A COMMAND THAT NAMES ITS OWN DIRECTORY RUNS FROM THE ROOT IT WAS WRITTEN FOR.
        # `npm --prefix notes-lite test` was run INSIDE notes-lite/, so npm looked for
        # notes-lite/notes-lite; the agent "fixed" that with a symlink to `.` and the sandbox's
        # diff then refused it, failing a build whose code was fine (crun_a936b22f).
        cwd = base if any(names_its_directory(c) for c in commands) else _cwd(commands)
        return GatePlan(commands, cwd, "task", " && ".join(commands))
    # An official/benchmark app is scaffolded and verified by its own package scripts; a
    # sandbox monorepo's manifest is not its contract.
    if not app_id:
        if pinned is None:
            pinned = pin_verify_manifest(base)
        if pinned.error:
            return GatePlan([], base, "manifest_error", pinned.error, error=pinned.error)
        if pinned.manifest is not None:
            plan = verify_manifest.plan_verify(pinned.manifest, base, changed)
            return GatePlan(plan.commands, base, "manifest", plan.describe(),
                            timeout_seconds=plan.timeout_seconds, suites=plan.suites,
                            steps=[(c, base / d if d != "." else base) for c, d in plan.steps],
                            uncovered=list(plan.uncovered))
    # A MULTI-SERVICE APP (services.stacks): a backend and a web front end — or two backends —
    # each in its own directory with its own manifest. Each is installed, built and unit-tested
    # in its own toolchain, then the backend is started and the e2e runs against it. Treating
    # it as one npm package is how a FastAPI + React app was tested with a Playwright harness
    # at its root and a backend nobody started (crun_78d197f7614f40f38993). A single-service
    # app never matches and keeps everything below.
    from . import stacks

    stack_root = stacks.find_stack_root(base, changed, app_id)
    if stack_root is not None:
        stack = stacks.stack_plan(stack_root)
        return GatePlan([c for c, _ in stack.steps + stack.e2e_steps], stack_root, "stack",
                        stack.describe(base), timeout_seconds=STACK_STEP_TIMEOUT_SECONDS,
                        error=stack.error, steps=list(stack.steps), stack=stack)
    if layout is not None:
        # The brief (or the approved architecture) asked for a backend and a front end in their
        # own directories, and the tree does not hold them: the gate says what is missing
        # rather than testing the app as whatever it happens to look like.
        message = (
            "This app is meant to be a "
            f"{stacks.LANGUAGES[layout.backend_language].label} backend in "
            f"{layout.backend_dir}/ and a Node web front end in {layout.frontend_dir}/, each with "
            "its own manifest (requirements.txt / pom.xml / package.json), and the change does "
            "not have both yet. Put each service in its own directory under the app directory.")
        return GatePlan([], base, "stack_missing", message, error=message)
    # A JAVA BUILD at the root (pom.xml / build.gradle) and no Node app there: Maven or Gradle
    # tests and builds it. Without the toolchain the gate says so in words, not "mvn: not found".
    from . import toolchains

    node_here = (base / "package.json").is_file() \
        or str(spec.get("project_kind", "")).lower() in _WEB_KINDS
    java = None if node_here else toolchains.detect_java(base)
    if java:
        if not toolchains.has("java"):
            message = toolchains.missing_message("java", toolchains.build_file(java))
            return GatePlan([], base, "toolchain_missing", message, error=message)
        commands = toolchains.java_gate_commands(java)
        # A first build downloads its dependencies through egress: give it the manifest's room.
        return GatePlan(commands, base, "toolchain", " && ".join(commands),
                        timeout_seconds=toolchains.GATE_TIMEOUT_SECONDS,
                        steps=[(c, base) for c in commands])
    commands = default_test_commands(spec, title)
    cwd = _cwd(commands)
    if commands == [PYTHON_TEST_CMD] and is_pytest_project(cwd):
        commands = [PYTEST_CMD]
    return GatePlan(commands, cwd, "default", " && ".join(commands))


def manifest_display(root: Path | str, pinned: PinnedVerifyManifest | None = None) -> str | None:
    """How the agent should run the repo's declared checks, for prompts. None without one."""
    from . import verify_manifest

    if pinned is None:
        pinned = pin_verify_manifest(root)
    if pinned.error:
        return f"(the base commit's {verify_manifest.MANIFEST_NAME} is invalid: {pinned.error})"
    manifest = pinned.manifest
    if manifest is None:
        return None
    runs = [
        c.replace(verify_manifest.TESTS_PLACEHOLDER, "<affected test files>")
        for suite in manifest.suites for c in suite.run
    ]
    return " ; ".join(runs)


def verification_profile(spec: dict[str, Any], title: str = "") -> dict[str, Any]:
    """Human-readable profile for the console / run evidence: the kind and the resolved steps."""
    kind = detect_kind(spec, title)
    return {"kind": kind,
            "steps": list(_WEB_STEPS) if kind == "web" else [PYTHON_TEST_CMD],
            "command": (WEB_TEST_CMD if kind == "web" else PYTHON_TEST_CMD)}


def resolve_verify_cwd(
    root: Path | str,
    *,
    app_id: str | None = None,
    changed: list[str] | None = None,
) -> Path:
    """Directory where the authoritative test command must run.

    Official canaries live as subdirectories in a shared sandbox monorepo.
    Running ``npm run verify`` at the clone root either misses the script or
    executes a sibling app. Prefer the named app, then a changed-file
    package.json ancestor, then the repo root.
    """
    base = Path(root)
    aid = str(app_id or "").strip().strip("/")
    if aid and (base / aid / "package.json").is_file():
        return base / aid
    for rel in changed or []:
        parts = Path(str(rel)).parts
        for depth in range(len(parts), 0, -1):
            candidate = base.joinpath(*parts[:depth])
            if candidate.is_dir() and (candidate / "package.json").is_file():
                return candidate
    return base


def ensure_verify_script(app_dir: Path | str) -> bool:
    """Add a ``verify`` script that chains existing required scripts. Never overwrites."""
    pkg_path = Path(app_dir) / "package.json"
    if not pkg_path.is_file():
        return False
    try:
        payload = json.loads(pkg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    scripts = payload.get("scripts")
    if not isinstance(scripts, dict):
        scripts = {}
        payload["scripts"] = scripts
    if str(scripts.get("verify") or "").strip():
        return False
    chain = [name for name in VERIFY_SCRIPT_STEPS if str(scripts.get(name) or "").strip()]
    if not chain:
        return False
    scripts["verify"] = " && ".join(f"npm run {name}" for name in chain)
    pkg_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return True


def official_app_id(spec: dict[str, Any] | None, finalize_ctx: dict[str, Any] | None) -> str | None:
    """Named official/QA app directory, if this run is a pinned benchmark."""
    bench = (finalize_ctx or {}).get("qa_benchmark") if isinstance((finalize_ctx or {}).get("qa_benchmark"), dict) else {}
    for source in (spec or {}, bench):
        value = str(source.get("app_root") or "").strip().strip("/")
        if value:
            return value
    value = str(bench.get("id") or "").strip().strip("/")
    return value or None


def _semver_major(value: str) -> int | None:
    match = re.search(r"(\d+)", str(value or ""))
    return int(match.group(1)) if match else None


def harden_web_package(app_dir: Path | str) -> dict[str, Any]:
    """Make a model-written package.json self-sufficient for ``npm run verify``.

    Does not invent a passing verify. It only restores the env the scaffold
    promised: real runner binaries, no echo stubs, eslint 8+.
    """
    pkg_path = Path(app_dir) / "package.json"
    changed: list[str] = []
    if not pkg_path.is_file():
        return {"changed": changed, "path": str(pkg_path)}
    try:
        payload = json.loads(pkg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"changed": changed, "path": str(pkg_path), "error": "unreadable"}
    if not isinstance(payload, dict):
        return {"changed": changed, "path": str(pkg_path), "error": "not_object"}

    scripts = payload.get("scripts")
    if not isinstance(scripts, dict):
        scripts = {}
        payload["scripts"] = scripts
    deps = payload.get("dependencies")
    if not isinstance(deps, dict):
        deps = {}
        payload["dependencies"] = deps
    dev = payload.get("devDependencies")
    if not isinstance(dev, dict):
        dev = {}
        payload["devDependencies"] = dev

    eslint_major = _semver_major(str(dev.get("eslint") or deps.get("eslint") or ""))
    for name, version in REQUIRED_WEB_DEV_DEPS.items():
        if name in ESLINT_COMPANIONS and eslint_major is not None and eslint_major > 8:
            continue
        if name not in dev and name not in deps:
            dev[name] = version
            changed.append(f"add:{name}")

    for bucket in (dev, deps):
        current = str(bucket.get("eslint") or "")
        major = _semver_major(current)
        if current and major is not None and major < 8:
            bucket["eslint"] = REQUIRED_WEB_DEV_DEPS["eslint"]
            changed.append("upgrade:eslint")

    e2e = str(scripts.get("test:e2e") or "").strip()
    if e2e and _PLAYWRIGHT_BARE.match(e2e):
        scripts["test:e2e"] = "npx --package=@playwright/test playwright test"
        changed.append("pin:playwright")

    for key in ("build", "dev", "preview", "verify"):
        raw = str(scripts.get(key) or "")
        if raw and "npx" not in raw and _BARE_VITE.search(raw):
            scripts[key] = _BARE_VITE.sub(r"\1npx --package=vite vite", raw)
            changed.append(f"pin:vite:{key}")

    stub_replacements = {
        "typecheck": "tsc --noEmit",
        "build": "npx --package=vite vite build",
        "lint": "eslint . --ext .ts,.tsx --max-warnings 0",
        "test:e2e": "npx --package=@playwright/test playwright test",
        "test:api": "node --test",
        "preview": "npx --package=vite vite preview --host 127.0.0.1 --port 4173 --strictPort",
    }
    for key, replacement in stub_replacements.items():
        raw = str(scripts.get(key) or "")
        if raw and _STUB_SCRIPT.search(raw):
            scripts[key] = replacement
            changed.append(f"unstub:{key}")

    if not str(scripts.get("preview") or "").strip():
        scripts["preview"] = stub_replacements["preview"]
        changed.append("add:preview")

    verify_raw = str(scripts.get("verify") or "")
    if verify_raw and _STUB_SCRIPT.search(verify_raw):
        chain = [
            name
            for name in VERIFY_SCRIPT_STEPS
            if str(scripts.get(name) or "").strip()
            and not _STUB_SCRIPT.search(str(scripts.get(name) or ""))
        ]
        if not chain:
            for name in ("typecheck", "build", "test:e2e"):
                if not str(scripts.get(name) or "").strip() or _STUB_SCRIPT.search(
                    str(scripts.get(name) or "")
                ):
                    scripts[name] = stub_replacements[name]
            chain = ["typecheck", "build", "test:e2e"]
        scripts["verify"] = " && ".join(f"npm run {name}" for name in chain)
        changed.append("unstub:verify")

    if changed:
        pkg_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return {"changed": changed, "path": str(pkg_path)}


def verify_output_disproves_pass(output: str | None) -> bool:
    """True when exit 0 still printed a swallowed runner failure."""
    text = str(output or "")
    return bool(text and _VERIFY_DISPROOF.search(text))


_TSC_FILE = re.compile(
    r"(?:^|\n)((?:[\w./-]+/)*[\w.-]+\.(?:ts|tsx|js|jsx))\(\d+,\d+\):",
)


def attach_failing_sources(
    evidence: str,
    root: Path,
    *,
    limit: int = 3,
    max_chars: int = 2000,
) -> str:
    """Append the files tsc named so repair edits the real source, not a guess."""
    text = str(evidence or "")
    paths: list[str] = []
    for match in _TSC_FILE.finditer(text):
        rel = match.group(1).lstrip("./")
        if rel not in paths:
            paths.append(rel)
    chunks: list[str] = []
    for rel in paths[:limit]:
        candidates = [root / rel, *root.glob(f"**/{rel}")]
        found = next((p for p in candidates if p.is_file()), None)
        if found is None:
            continue
        try:
            body = found.read_text(encoding="utf-8")[:max_chars]
        except OSError:
            continue
        shown = str(found.relative_to(root)) if found.is_relative_to(root) else rel
        chunks.append(f"\n--- {shown} ---\n{body}")
    if not chunks:
        return text
    return (
        text
        + "\n\nThe failing files currently contain (fix these; do not invent a new file):\n"
        + "".join(chunks)
    )


def honest_verify_status(
    verify: dict[str, Any] | None,
    output: str | None = None,
) -> str:
    """Exit 0 is not a pass if the log shows a swallowed failure."""
    if not isinstance(verify, dict):
        return "not_recorded"
    if verify.get("timed_out"):
        return "fail"
    if verify.get("exit_code") != 0:
        return "fail"
    if verify_output_disproves_pass(output):
        return "fail"
    return "pass"


def _e2e_is_placeholder(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return "placeholder passing test" in text or "expect(true).toBeTruthy()" in text


def _existing_e2e_specs(app_dir: Path) -> list[Path]:
    found: list[Path] = []
    for folder in (app_dir / "e2e", app_dir / "tests" / "e2e"):
        if folder.is_dir():
            found.extend(sorted(folder.glob("*.ts")))
            found.extend(sorted(folder.glob("*.js")))
    found.extend(sorted(app_dir.glob("**/*.spec.ts")))
    uniq: list[Path] = []
    seen: set[str] = set()
    for path in found:
        key = str(path)
        if key in seen or "node_modules" in path.parts:
            continue
        seen.add(key)
        uniq.append(path)
    return uniq


def _scenario_pattern(scenario: str) -> str:
    words = [
        w.lower()
        for w in re.findall(r"[A-Za-z]{4,}", scenario or "")
        if w.lower() not in _STOP_WORDS
    ]
    return "|".join(words[:6]) or "app|dashboard|home"


def seed_acceptance_e2e(
    app_dir: Path | str,
    scenarios: list[str] | None = None,
) -> dict[str, Any]:
    """Write missing Playwright config + named JTBD specs. Never overwrite."""
    app = Path(app_dir)
    changed: list[str] = []
    names = [str(item).strip() for item in (scenarios or []) if str(item).strip()]
    if not names:
        return {"changed": changed, "seeded_specs": 0}

    config_path = app / "playwright.config.ts"
    if not config_path.is_file() and not (app / "playwright.config.js").is_file():
        config_path.write_text(
            'import { defineConfig } from "@playwright/test";\n'
            "export default defineConfig({\n"
            '  testDir: "./e2e",\n'
            "  timeout: 30_000,\n"
            '  use: { baseURL: "http://127.0.0.1:4173", headless: true },\n'
            "  webServer: {\n"
            '    command: "npm run preview",\n'
            '    url: "http://127.0.0.1:4173",\n'
            "    reuseExistingServer: true,\n"
            "    timeout: 60_000,\n"
            "  },\n"
            "});\n",
            encoding="utf-8",
        )
        changed.append("add:playwright.config.ts")

    existing = _existing_e2e_specs(app)
    live_specs = [path for path in existing if not _e2e_is_placeholder(path)]
    if live_specs:
        return {"changed": changed, "seeded_specs": 0, "reason": "specs_exist"}
    for path in existing:
        if _e2e_is_placeholder(path):
            path.unlink()
            changed.append(f"delete:{path.name}")

    e2e_dir = app / "e2e"
    e2e_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        'import { test, expect } from "@playwright/test";',
        "",
        "// Official acceptance scenarios. Fail closed if the UI cannot show them.",
        "",
    ]
    for scenario in names[:8]:
        pattern = _scenario_pattern(scenario)
        lines.extend(
            [
                f"test({json.dumps(scenario)}, async ({{ page }}) => {{",
                '  await page.goto("/");',
                "  await expect(page.locator(\"body\")).toBeVisible();",
                f"  await expect(page.getByText(/{pattern}/i).first()).toBeVisible();",
                "});",
                "",
            ]
        )
    (e2e_dir / "acceptance.spec.ts").write_text("\n".join(lines), encoding="utf-8")
    changed.append("add:e2e/acceptance.spec.ts")
    return {"changed": changed, "seeded_specs": min(len(names), 8)}


def prepare_web_verify_env(
    app_dir: Path | str,
    *,
    scenarios: list[str] | None = None,
    app_id: str | None = None,
) -> dict[str, Any]:
    """Harden package.json, seed missing e2e, and name install commands."""
    from .official_canary import restore_official_canary

    restored = restore_official_canary(app_dir, app_id)
    hardened = harden_web_package(app_dir)
    seeded = seed_acceptance_e2e(app_dir, scenarios)
    hardened["seeded"] = seeded
    hardened["restored"] = restored
    hardened["changed"] = list(hardened.get("changed") or []) + list(
        seeded.get("changed") or []
    ) + list(restored.get("changed") or [])
    hardened["install_command"] = NPM_INSTALL_CMD
    hardened["browser_install_command"] = PLAYWRIGHT_INSTALL_CMD
    return hardened


def evaluate_web_package_payload(payload: dict[str, Any] | None, *,
                                 runners: tuple[str, ...] = ("vite", "@playwright/test"),
                                 ) -> dict[str, Any]:
    """Contract over a package.json object. Never invents a pass. `runners`: the build and
    test runners it must carry (a Next.js front end builds with next, not vite)."""
    scripts = payload.get("scripts") if isinstance(payload, dict) and isinstance(payload.get("scripts"), dict) else {}
    deps: dict[str, Any] = {}
    if isinstance(payload, dict):
        if isinstance(payload.get("dependencies"), dict):
            deps.update(payload["dependencies"])
        if isinstance(payload.get("devDependencies"), dict):
            deps.update(payload["devDependencies"])
    stubs = [
        name
        for name, cmd in scripts.items()
        if _STUB_SCRIPT.search(str(cmd or ""))
    ]
    missing = [name for name in REQUIRED_WEB_DEV_DEPS if name not in deps]
    # Fail-closed on the runners that killed official canaries, not every helper.
    required_runners = list(runners)
    missing_runners = [name for name in required_runners if name not in deps]
    ok = not stubs and not missing_runners
    return {
        "ok": ok,
        "stubs": stubs,
        "missing_runners": missing_runners,
        "missing_recommended": missing,
        "has_verify": bool(str(scripts.get("verify") or "").strip()),
    }


#: The runners a Next.js front end must carry: it builds and serves with next, not vite.
NEXT_RUNNERS = ("next", "@playwright/test")


def evaluate_web_package_contract(app_dir: Path | str, *,
                                  runners: tuple[str, ...] = ("vite", "@playwright/test"),
                                  ) -> dict[str, Any]:
    pkg_path = Path(app_dir) / "package.json"
    if not pkg_path.is_file():
        return {
            "ok": False,
            "stubs": [],
            "missing_runners": ["vite", "@playwright/test"],
            "has_verify": False,
            "reason": "package.json missing",
        }
    try:
        payload = json.loads(pkg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "ok": False,
            "stubs": [],
            "missing_runners": ["vite", "@playwright/test"],
            "has_verify": False,
            "reason": "package.json unreadable",
        }
    if not isinstance(payload, dict):
        return {
            "ok": False,
            "stubs": [],
            "missing_runners": ["vite", "@playwright/test"],
            "has_verify": False,
            "reason": "package.json not an object",
        }
    return evaluate_web_package_payload(payload, runners=runners)
