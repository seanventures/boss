"""THE BUILD, SPLIT BY BOSS — NOT LEFT TO THE MODEL.

A HelpDesk AI build ran with the CLI's sub-agent tool allowed and a "use parallel sub-agents"
rule in its prompt, and made ZERO sub-agent calls: a soft instruction is a suggestion the model
declines. Its time was output-token bound (write_tests: 63,881 tokens in 686 s, ~93 tok/s;
commands 0.6 min in total), so the only lever on wall-clock is generating in parallel.

So BOSS splits the work itself, from the design's WORK PACKAGES:

1. the **scaffold** package (package manifest, root configs, shared types) runs FIRST, alone;
2. every other package runs as its own CLI session, AT THE SAME TIME, in the same workspace —
   each told its part and the paths it OWNS, and that everything else is forbidden;
3. writes outside the packages' owned paths are REVERTED and reported (`enforce`);
4. ONE integration session wires the parts together and runs the full test command.

Nothing here judges the work: the frozen-test tamper guard, the gates and the review panel run
afterwards exactly as for a single session. When the design has no usable partition, or the
parallel sessions all fail, the caller falls back to today's single session (`run_split`
returns None).
"""
from __future__ import annotations

import concurrent.futures
import contextvars
import fnmatch
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

#: The heading the design session writes its partition under.
PACKAGES_HEAD = "WORK PACKAGES"
SCAFFOLD = "scaffold"
MAX_PACKAGES = 8

#: Generated or installed trees: never owned, never reverted, never hashed.
IGNORED_DIRS = frozenset({
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", ".cache",
    "coverage", "test-results", "playwright-report", "blob-report", ".pytest_cache", ".turbo",
    ".vite", "target", ".mypy_cache", ".ruff_cache", "out", "tmp",
})
#: Files a package manager or a test run writes beside the code (lockfiles are the scaffold's).
_GENERATED_FILES = re.compile(r"(^|/)(\.DS_Store|junit[^/]*\.xml|[^/]*\.tsbuildinfo)$")
#: Snapshots keep content up to this size for a revert; larger files are hashed only.
_MAX_KEPT_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class Package:
    name: str
    owns: tuple[str, ...]
    tests: tuple[str, ...] = ()
    brief: str = ""


@dataclass
class SplitResult:
    cost_usd_micros: int = 0
    summary: str = ""
    session_id: Optional[str] = None
    usage: dict = field(default_factory=dict)
    sessions: list = field(default_factory=list)
    violations: list = field(default_factory=list)

    def as_role_result(self) -> dict:
        return {"cost_usd_micros": self.cost_usd_micros, "summary": self.summary,
                "session_id": self.session_id, "usage": self.usage,
                "split": {"sessions": self.sessions, "violations": self.violations}}


# ── the partition ─────────────────────────────────────────────────────────────────────────


def design_prompt_section() -> str:
    """The design session's fourth section: the partition BOSS runs in parallel."""
    return (
        f"\n\n=== {PACKAGES_HEAD} ===\nHow this change splits into parts that separate agents "
        "can write AT THE SAME TIME. Answer with ONE ```json block: "
        '{"packages": [{"name": "scaffold", "owns": [...], "tests": [...], "brief": "..."}, '
        '{"name": "...", "owns": [...], "tests": [...], "brief": "..."}]}. Rules: the first '
        "package is `scaffold` and owns the SHARED files every part needs (package manifest, "
        "root configs, shared types, the test runner's config); then 2 to 5 packages, each "
        "owning its own directories or files (globs like `app/server/**`). `owns` and `tests` "
        "are disjoint across packages: a path belongs to exactly one. `tests` are where that "
        "part's tests go. A change too small to split (under ~8 files) answers "
        '{"packages": []}.'
    )


def parse_packages(text: Optional[str]) -> list[Package]:
    """The design's packages, or [] when there is no usable partition (never raises)."""
    if not text:
        return []
    blob = None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        blob = m.group(1)
    else:
        m = re.search(r"(\{\s*\"packages\".*\})", text, re.DOTALL)
        blob = m.group(1) if m else None
    if not blob:
        return []
    try:
        data = json.loads(blob)
    except (ValueError, TypeError):
        return []
    raw = data.get("packages") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    out: list[Package] = []
    seen: set[str] = set()
    for item in raw[:MAX_PACKAGES]:
        if not isinstance(item, dict):
            continue
        name = re.sub(r"[^a-z0-9_-]+", "-", str(item.get("name") or "").strip().lower()).strip("-")
        if not name or name in seen:
            continue
        owns = tuple(g for g in (_clean_glob(x) for x in item.get("owns") or []) if g)
        tests = tuple(g for g in (_clean_glob(x) for x in item.get("tests") or []) if g)
        if not owns and not tests:
            continue
        seen.add(name)
        out.append(Package(name=name, owns=owns, tests=tests, brief=str(item.get("brief") or "")[:600]))
    # The scaffold, if any, runs first wherever the design listed it.
    out.sort(key=lambda p: 0 if p.name == SCAFFOLD else 1)
    return out


def _clean_glob(value: Any) -> Optional[str]:
    """A repo-relative glob, or None for anything that could leave the workspace."""
    s = str(value or "").strip().replace("\\", "/")
    if not s or s.startswith("/") or ".." in PurePosixPath(s).parts or s in ("*", "**", "**/*"):
        return None
    return s.lstrip("./") if s.startswith("./") else s


def matches(path: str, glob: str) -> bool:
    """Does repo-relative `path` fall under `glob` (a directory, a `dir/**` tree or an fnmatch)?"""
    path = path.lstrip("./") if path.startswith("./") else path
    g = glob.rstrip("/")
    if g.endswith("/**"):
        base = g[:-3]
        return path == base or path.startswith(base + "/")
    if path == g or path.startswith(g + "/"):
        return True
    return fnmatch.fnmatchcase(path, g) or fnmatch.fnmatchcase(path, g.replace("**/", ""))


def owner_of(path: str, packages: list[Package], *, tests: bool) -> list[str]:
    attr = "tests" if tests else "owns"
    return [p.name for p in packages if any(matches(path, g) for g in getattr(p, attr))]


# ── what the workspace held, and putting it back ──────────────────────────────────────────


def _ignored(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    return any(part in IGNORED_DIRS for part in parts) or bool(_GENERATED_FILES.search(rel))


def snapshot(root: Path) -> dict[str, tuple[str, Optional[bytes]]]:
    """{relpath: (sha1, content-or-None)} for every tracked-worthy file under `root`."""
    out: dict[str, tuple[str, Optional[bytes]]] = {}
    base = Path(root)
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        for name in filenames:
            full = Path(dirpath) / name
            rel = full.relative_to(base).as_posix()
            if _ignored(rel) or full.is_symlink():
                continue
            try:
                data = full.read_bytes()
            except OSError:
                continue
            out[rel] = (hashlib.sha1(data).hexdigest(), data if len(data) <= _MAX_KEPT_BYTES else None)
    return out


def changed_since(root: Path, before: dict) -> list[str]:
    """Paths created, changed or deleted since `before`."""
    now = snapshot(root)
    changed = [p for p, (h, _d) in now.items() if before.get(p, (None, None))[0] != h]
    changed += [p for p in before if p not in now]
    return sorted(set(changed))


def enforce(root: Path, before: dict, allowed: Callable[[str], bool]) -> list[str]:
    """Revert every change since `before` that `allowed` refuses. Returns the reverted paths.

    A new file is removed; a changed or deleted one is restored from the snapshot (a file too
    large to keep is left and reported — it is still a violation)."""
    reverted: list[str] = []
    base = Path(root)
    for rel in changed_since(base, before):
        if allowed(rel):
            continue
        target = base / rel
        prior = before.get(rel)
        try:
            if prior is None:
                if target.is_file():
                    target.unlink()
            elif prior[1] is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(prior[1])
        except OSError:
            log.warning("parallel_build: could not revert %s", rel, exc_info=True)
        reverted.append(rel)
    return reverted


# ── the prompts ────────────────────────────────────────────────────────────────────────────


def _paths(globs: tuple[str, ...]) -> str:
    return ", ".join(f"`{g}`" for g in globs) or "(none)"


def scaffold_prompt(base: str, pkg: Package, *, tests: bool) -> str:
    owned = pkg.tests if tests else pkg.owns
    if tests:
        return (base + f"\n\n=== FIRST: THE TEST SCAFFOLD ===\nSet up ONLY the shared test "
                f"harness the other parts' suites will use: {_paths(owned)} (runner config, "
                "shared fixtures and helpers). Write no feature tests yet; separate agents write "
                "them next, in parallel. Touch nothing outside these paths.")
    return (base + f"\n\n=== FIRST: THE SCAFFOLD ===\nSet up ONLY the shared skeleton every "
            f"other part builds on: {_paths(owned)}. {pkg.brief}\nInstall the dependencies. Do "
            "not implement features: separate agents write them next, in parallel, and must "
            "find this skeleton in place. Touch nothing outside these paths.")


def part_prompt(base: str, pkg: Package, others: list[Package], *, tests: bool) -> str:
    owned = pkg.tests if tests else pkg.owns
    rest = "; ".join(f"{o.name} ({_paths(o.tests if tests else o.owns)})" for o in others) or "none"
    what = ("the TESTS for part" if tests else "part")
    return (
        base + f"\n\n=== YOUR PART: {pkg.name} ===\nBOSS has split this build into parts that "
        f"separate agents are writing AT THE SAME TIME in this same directory. You write ONLY "
        f"{what} `{pkg.name}`: {pkg.brief}\n"
        f"You OWN these paths and may create, edit or delete files ONLY there: {_paths(owned)}.\n"
        "Everything else belongs to another agent or to the scaffold: do NOT create, edit, move or "
        "delete anything outside your paths — BOSS reverts any such write and reports it. Do not "
        "run package installs or change the package manifest, lockfile or root configs (the "
        "scaffold did that). Do not run the whole test suite: the other parts are mid-write. "
        + ("Write the tests from the spec, independent of any implementation. "
           if tests else "Check your own part (type-check it, run its own tests) and stop. ")
        + f"The other parts, for context only: {rest}."
    )


def integration_prompt(base: str, done: list[str], test_cmd: str, *, tests: bool) -> str:
    if tests:
        return (base + f"\n\n=== INTEGRATE THE SUITES ===\nThe test suites for "
                f"{', '.join(done)} were written in parallel by separate agents. Make them run "
                f"together under `{test_cmd}`: fix only harness, config and import problems. Tests "
                "failing because no implementation exists yet is expected: do NOT weaken, skip "
                "or delete any test.")
    return (base + f"\n\n=== INTEGRATE ===\nThe parts {', '.join(done)} were written in "
            "parallel by separate agents. Wire them together and fix the seams (imports, routes, "
            f"shared types, wiring). Run `{test_cmd}` and make it pass. Do not rewrite a part; "
            "change only what integration needs. Do not edit the test files.")


# ── the run ────────────────────────────────────────────────────────────────────────────────


def run_split(*, run_role: Callable[..., dict], root: Path, base_prompt: str,
              packages: list[Package], parallel: int, test_cmd: str, tests: bool,
              on_event: Optional[Callable[[str, dict], None]] = None,
              protect: Optional[Callable[[str], bool]] = None) -> Optional[SplitResult]:
    """Run the build's writing stage split across `packages`, or None to fall back.

    `run_role(prompt)` runs ONE CLI session in the workspace and returns its result dict.
    `protect(path)` marks paths no part may touch even if it owns them (the frozen tests during
    implement). Returns None (nothing written by the parallel phase is kept wrong: the caller's
    single session runs on top) when there is no usable split or every parallel part failed.
    """
    emit = on_event or (lambda kind, data: None)
    attr = "tests" if tests else "owns"
    scaffold = next((p for p in packages if p.name == SCAFFOLD and getattr(p, attr)), None)
    parts = [p for p in packages if p.name != SCAFFOLD and getattr(p, attr)]
    if parallel < 2 or len(parts) < 2:
        return None
    result = SplitResult()

    def _take(r: dict, name: str, phase: str) -> None:
        result.cost_usd_micros += int(r.get("cost_usd_micros") or 0)
        for k, v in (r.get("usage") or {}).items():
            if isinstance(v, (int, float)):
                result.usage[k] = result.usage.get(k, 0) + v
        result.sessions.append({"name": name, "phase": phase, "ok": True})

    if scaffold is not None:
        emit("code_task.split_scaffold", {"package": scaffold.name, "tests": tests})
        before = snapshot(root)
        _take(run_role(scaffold_prompt(base_prompt, scaffold, tests=tests)), scaffold.name, "scaffold")
        allowed_s = (lambda rel: any(matches(rel, g) for g in getattr(scaffold, attr))
                     and not (protect and protect(rel)))
        # The scaffold's globs ARE its limit: a test harness config is not a test file.
        bad = enforce(root, before, allowed_s)
        if bad:
            result.violations += [{"package": scaffold.name, "path": p} for p in bad]

    before = snapshot(root)
    emit("code_task.split_started", {"parts": [p.name for p in parts], "parallel": parallel,
                                     "tests": tests})
    failed: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(parallel, len(parts)),
                                               thread_name_prefix="boss-part") as pool:
        futures = {}
        for pkg in parts:
            others = [o for o in parts if o is not pkg]
            prompt = part_prompt(base_prompt, pkg, others, tests=tests)
            # Each part carries this thread's context (the build's owner scope, its LLM stage
            # label): its processes are this build's, and its cost lands under this stage.
            ctx = contextvars.copy_context()
            futures[pool.submit(ctx.run, run_role, prompt)] = pkg
        for fut in concurrent.futures.as_completed(futures):
            pkg = futures[fut]
            try:
                _take(fut.result(), pkg.name, "part")
            except Exception:  # noqa: BLE001 — one part failing is the integration's to finish
                log.warning("parallel_build: part %s failed", pkg.name, exc_info=True)
                failed.append(pkg.name)
                result.sessions.append({"name": pkg.name, "phase": "part", "ok": False})

    owned = [g for p in parts for g in getattr(p, attr)]
    allowed = (lambda rel: any(matches(rel, g) for g in owned) and not (protect and protect(rel)))
    bad = enforce(root, before, _tests_only(allowed) if tests else allowed)
    if bad:
        result.violations += [{"package": "(parallel parts)", "path": p} for p in bad]
        emit("code_task.split_violations", {"reverted": bad[:50], "count": len(bad)})
    if len(failed) == len(parts):
        emit("code_task.split_fell_back", {"why": "every parallel part failed", "parts": failed})
        return None

    done = [p.name for p in parts if p.name not in failed]
    r = run_role(integration_prompt(base_prompt, done + failed, test_cmd, tests=tests))
    _take(r, "integration", "integration")
    result.summary = str(r.get("summary") or "")
    result.session_id = r.get("session_id")
    emit("code_task.split_finished", {"parts": done, "failed": failed,
                                      "violations": len(result.violations), "tests": tests})
    return result


def _tests_only(allowed: Callable[[str], bool]) -> Callable[[str], bool]:
    """QA's parts may write only TEST files, whatever they own (test independence)."""
    from .governed_code_task_service import _is_test_path

    return lambda rel: allowed(rel) and _is_test_path(rel)
