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
NPM_INSTALL_CMD = "npm install --no-audit --no-fund --loglevel=error"
_WEB_STEPS = ["npm run build", "npm run typecheck", "npm run lint", "npm run test:e2e"]
WEB_TEST_COMMANDS = [NPM_INSTALL_CMD, *_WEB_STEPS]
#: Display only — the console shows the sequence as one line. Never execute this.
WEB_TEST_CMD = " && ".join(WEB_TEST_COMMANDS)

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

    for name, version in REQUIRED_WEB_DEV_DEPS.items():
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


def evaluate_web_package_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Contract over a package.json object. Never invents a pass."""
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
    required_runners = ["vite", "@playwright/test"]
    missing_runners = [name for name in required_runners if name not in deps]
    ok = not stubs and not missing_runners
    return {
        "ok": ok,
        "stubs": stubs,
        "missing_runners": missing_runners,
        "missing_recommended": missing,
        "has_verify": bool(str(scripts.get("verify") or "").strip()),
    }


def evaluate_web_package_contract(app_dir: Path | str) -> dict[str, Any]:
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
    return evaluate_web_package_payload(payload)
