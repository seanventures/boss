"""BOSS'S OWN SOURCE, for a self-fix: the mirror, the workspace, the diff, the commit, the tests.

BOSSAPP-571 (spec §6.1, §6.3, F3, F8). The install checkout lives on the HOST and is never
mounted into a container (no `.git` in the images, by design), and editing it would break
upgrade.sh's clean-tree rule. So a self-fix builds from the SELF-SOURCE MIRROR: a bare clone of
the checkout (`git clone --mirror`, offline, holding the exact BOSS_SOURCE_SHA and the release
tags), seeded on the host by the self-installer (`boss self-fix seed`) into a volume the API and
the worker mount.

  * The WORKSPACE of one self-fix is a clone of the mirror checked out at its base (the running
    stack head), with its remote REMOVED: the engine that writes the fix in it cannot push
    anywhere. BOSS's pipeline — never the sandbox — writes the self-fix branch to the mirror
    after every gate passed.
  * The DIFF is what git says between the base and the workspace (untracked files included,
    renames detected), as `self_fix_policy.FileChange`s with both contents.
  * The TESTS run with BOSS's own runner, against the workspace's own code (PYTHONPATH of the
    workspace), with JUnit output so a test that already failed on the base is told apart from
    one the fix broke.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .self_fix_integrity import SuiteRun
from .self_fix_policy import FileChange

GIT_TIMEOUT = 300
#: Never a real person: the commit author of a fix Mender wrote.
AUTHOR_NAME = "BOSS Mender"
AUTHOR_EMAIL = "mender@boss.invalid"
REPO_ROOT = Path(__file__).resolve().parents[3]


class SourceError(RuntimeError):
    """A git operation on BOSS's own source failed (fail closed: no self-fix)."""


def _env(extra: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": AUTHOR_NAME,
                "GIT_AUTHOR_EMAIL": AUTHOR_EMAIL, "GIT_COMMITTER_NAME": AUTHOR_NAME,
                "GIT_COMMITTER_EMAIL": AUTHOR_EMAIL, "GIT_CONFIG_NOSYSTEM": "1",
                "HOME": env.get("HOME") or tempfile.gettempdir()})
    env.update(dict(extra or {}))
    return env


def git(args: list[str], cwd: Optional[Path] = None, *, check: bool = True,
        input_text: Optional[str] = None, timeout: int = GIT_TIMEOUT) -> str:
    try:
        done = subprocess.run(["git", "-c", "core.hooksPath=/dev/null",
                               "-c", "commit.gpgsign=false", *args],
                              cwd=str(cwd) if cwd else None, env=_env(), text=True,
                              input=input_text, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SourceError(f"git {' '.join(args[:2])}: {error}") from error
    if check and done.returncode != 0:
        raise SourceError(f"git {' '.join(args[:3])} failed: {(done.stderr or done.stdout)[-600:]}")
    return done.stdout


def mirror_of(settings: Any) -> Path:
    from .self_fix import mirror_path

    return mirror_path(settings)


def has_mirror(settings: Any) -> bool:
    path = mirror_of(settings)
    return (path / "HEAD").exists() or (path / ".git").exists()


def seed(checkout: Path, mirror: Path) -> str:
    """Make (or refresh) the mirror from an install checkout. Returns the mirror's path."""
    mirror = Path(mirror)
    if (mirror / "HEAD").exists():
        git(["fetch", "--prune", "--tags", str(checkout), "+refs/*:refs/*"], cwd=mirror)
    else:
        mirror.parent.mkdir(parents=True, exist_ok=True)
        git(["clone", "--mirror", "--no-hardlinks", str(checkout), str(mirror)])
    return str(mirror)


def file_exists(settings: Any, sha: str, path: str) -> bool:
    """Does BOSS's source at `sha` hold `path`? The mirror answers; without one, the running
    tree (the code this process is)."""
    clean = path.strip().lstrip("/")
    if not clean or ".." in Path(clean).parts:
        return False
    if sha and has_mirror(settings):
        try:
            git(["cat-file", "-e", f"{sha}:{clean}"], cwd=mirror_of(settings))
            return True
        except SourceError:
            return False
    return (REPO_ROOT / clean).is_file()


def workspace(settings: Any, base_sha: str, dest: Path, *, source: Optional[Path] = None
              ) -> Path:
    """A clone of BOSS's source at `base_sha` in `dest`, with no remote (nothing in it can
    push). `source` overrides the mirror (a test, the Replay harness)."""
    origin = Path(source) if source else mirror_of(settings)
    if not ((origin / "HEAD").exists() or (origin / ".git").exists()):
        raise SourceError(f"BOSS's own source is not at {origin}")
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    git(["clone", "--quiet", "--no-checkout", "--shared", str(origin), str(dest)])
    git(["checkout", "--quiet", "--detach", base_sha], cwd=dest)
    git(["remote", "remove", "origin"], cwd=dest, check=False)
    git(["config", "user.name", AUTHOR_NAME], cwd=dest)
    git(["config", "user.email", AUTHOR_EMAIL], cwd=dest)
    return dest


def head(work: Path) -> str:
    return git(["rev-parse", "HEAD"], cwd=work).strip()


def resolve(source: Path, ref: str) -> str:
    return git(["rev-parse", f"{ref}^{{commit}}"], cwd=source).strip()


def _stage_all(work: Path) -> None:
    git(["add", "-A", "."], cwd=work)


def _show(work: Path, sha: str, path: str) -> Optional[str]:
    try:
        return git(["show", f"{sha}:{path}"], cwd=work)
    except SourceError:
        return None


def changes(work: Path, base_sha: str) -> list[FileChange]:
    """Every file the workspace changed against `base_sha` (untracked included, renames
    detected), with both contents."""
    _stage_all(work)
    listing = git(["diff", "--cached", "--name-status", "-M", "--no-color", base_sha], cwd=work)
    out: list[FileChange] = []
    for line in listing.splitlines():
        parts = line.split("\t")
        if not parts or not parts[0]:
            continue
        code = parts[0][0]
        if code == "R" and len(parts) >= 3:
            old, new = parts[1], parts[2]
            out.append(FileChange(path=new, status="R", before=_show(work, base_sha, old),
                                  after=_read(work, new), old_path=old))
        elif code in ("A", "M", "D", "T", "C") and len(parts) >= 2:
            path = parts[-1]
            status = "M" if code in ("T", "C") else code
            out.append(FileChange(path=path, status=status,
                                  before=None if status == "A" else _show(work, base_sha, path),
                                  after=None if status == "D" else _read(work, path)))
    return out


def _read(work: Path, rel: str) -> Optional[str]:
    path = work / rel
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def diff_text(work: Path, base_sha: str, *, limit: int = 400_000) -> str:
    _stage_all(work)
    return git(["diff", "--cached", "--no-color", "-M", base_sha], cwd=work)[:limit]


def diff_stat(work: Path, base_sha: str) -> dict[str, Any]:
    _stage_all(work)
    numstat = git(["diff", "--cached", "--numstat", "-M", base_sha], cwd=work)
    files, added, removed = [], 0, 0
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        a = int(parts[0]) if parts[0].isdigit() else 0
        r = int(parts[1]) if parts[1].isdigit() else 0
        added, removed = added + a, removed + r
        files.append({"path": parts[2], "added": a, "removed": r})
    return {"files": files, "added": added, "removed": removed}


def discard(work: Path, paths: Iterable[str], base_sha: str) -> None:
    """Put `paths` back as the base has them (a write-tests turn that touched code)."""
    for rel in paths:
        before = _show(work, base_sha, rel)
        target = work / rel
        if before is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(before, encoding="utf-8")


def snapshot(work: Path, paths: Iterable[str]) -> dict[str, str]:
    return {p: (_read(work, p) or "") for p in paths}


def commit(work: Path, message: str) -> tuple[str, str]:
    """Commit everything in the workspace. (commit sha, tree sha)."""
    _stage_all(work)
    git(["commit", "--quiet", "--no-verify", "-F", "-"], cwd=work, input_text=message)
    return head(work), git(["rev-parse", "HEAD^{tree}"], cwd=work).strip()


def publish(work: Path, mirror: Path, branch: str) -> None:
    """BOSS's pipeline (never the sandbox) writes the self-fix branch to the mirror."""
    if not re.fullmatch(r"boss/self-fix/[A-Za-z0-9._/-]+", branch):
        raise SourceError(f"refusing to write {branch}: self-fix branches are boss/self-fix/*")
    git(["push", "--quiet", "--force", str(mirror), f"HEAD:refs/heads/{branch}"], cwd=work)


def bundle(work: Path, branch: str, base_sha: str) -> bytes:
    """The fix as a git bundle: the exact commit (so its sha and tree are the ones the gates
    ran on) on its branch, relative to the base. The host self-installer fetches it into the
    checkout; nothing in a container ever writes to the checkout or needs to push."""
    if not re.fullmatch(r"boss/self-fix/[A-Za-z0-9._/-]+", branch):
        raise SourceError(f"refusing to bundle {branch}: self-fix branches are boss/self-fix/*")
    git(["branch", "-f", branch, "HEAD"], cwd=work)
    target = Path(tempfile.mkstemp(prefix="selffix-", suffix=".bundle")[1])
    try:
        git(["bundle", "create", "--quiet", str(target), branch, f"^{base_sha}"], cwd=work)
        return target.read_bytes()
    finally:
        target.unlink(missing_ok=True)


def tree_of(source: Path, commit_sha: str) -> str:
    return git(["rev-parse", f"{commit_sha}^{{tree}}"], cwd=source).strip()


# ── BOSS's tests ─────────────────────────────────────────────────────────────────────────────

def _python() -> str:
    return os.environ.get("BOSS_SELF_FIX_PYTHON") or sys.executable or "python3"


def test_env(work: Path, settings: Any = None) -> dict[str, str]:
    """The environment BOSS's own tests run in: the workspace's code first on the path, the
    self-fix test database when one is configured (never the install's own database)."""
    env = {k: v for k, v in os.environ.items()}
    for key in ("DATABASE_URL", "BOSS_TEST_DATABASE_URL", "BOSS_IDENTITY_TEST_DATABASE_URL"):
        env.pop(key, None)
    url = (os.environ.get("BOSS_SELF_FIX_TEST_DATABASE_URL") or "").strip()
    if url:
        env["BOSS_TEST_DATABASE_URL"] = url
        env["DATABASE_URL"] = url.replace("postgresql+psycopg://", "postgresql://")
    env["PYTHONPATH"] = os.pathsep.join([str(work), str(work / "engine")])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("BOSS_SCRIPTED_SELF_FIX", None)
    return env


def run_pytest(work: Path, tests: Iterable[str], *, settings: Any = None, timeout: int = 900,
               junit: bool = True) -> tuple[SuiteRun, dict[str, str]]:
    """(the run, {test id: passed|failed|error|skipped}) for `tests` in the workspace."""
    selected = [t for t in dict.fromkeys(tests) if t]
    if not selected:
        return SuiteRun(exit_code=5, output="no tests selected", collection_error=True), {}
    report = Path(tempfile.mkstemp(prefix="selffix-junit-", suffix=".xml")[1])
    argv = [_python(), "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(work),
            "-o", "pythonpath=engine", "-x" if not junit else "--maxfail=1000",
            *(["--junitxml", str(report)] if junit else []), *selected]
    try:
        done = subprocess.run(argv, cwd=str(work), env=test_env(work, settings), text=True,
                              capture_output=True, timeout=timeout)
        output = (done.stdout or "") + (done.stderr or "")
        code = done.returncode
    except subprocess.TimeoutExpired as error:
        output = f"the tests did not finish in {timeout}s\n{error.stdout or ''}"
        code = 124
    outcomes = _junit(report) if junit else {}
    report.unlink(missing_ok=True)
    return SuiteRun.from_pytest(code, output), outcomes


def _junit(report: Path) -> dict[str, str]:
    try:
        root = ET.parse(report).getroot()
    except (ET.ParseError, OSError):
        return {}
    out: dict[str, str] = {}
    for case in root.iter("testcase"):
        name = f"{case.get('classname', '')}::{case.get('name', '')}"
        state = "passed"
        for child in case:
            if child.tag in ("failure", "error"):
                state = "failed" if child.tag == "failure" else "error"
            elif child.tag == "skipped":
                state = "skipped"
        out[name] = state
    return out


def affected_tests(work: Path, changed: Iterable[str]) -> tuple[list[str], list[str]]:
    """(the engine tests boss.verify.yaml selects for `changed`, changed code no suite covers),
    read from the BASE's manifest in the workspace."""
    from .verify_manifest import load_verify_manifest, select_tests

    manifest = load_verify_manifest(work)
    changed = [c for c in changed if c]
    if manifest is None:
        return [], changed
    tests: list[str] = []
    covered: set[str] = set()
    for suite in manifest.suites:
        if suite.name != "engine":
            continue
        mine = [c for c in changed if any(_glob(c, p) for p in suite.paths)]
        if not mine:
            continue
        picked, _ = select_tests(work, suite, changed)
        tests.extend(picked)
        covered.update(mine)
    uncovered = [c for c in changed if c not in covered and not c.startswith("engine/tests/")]
    return sorted(set(tests)), uncovered


def _glob(path: str, pattern: str) -> bool:
    from .verify_manifest import glob_match

    return glob_match(path, pattern)


def p0_tests(work: Path) -> list[str]:
    """The P0 engine tier (tests/tiers.yaml) the workspace has."""
    import yaml

    try:
        doc = yaml.safe_load((work / "tests" / "tiers.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    listed = ((doc.get("P0") or {}).get("engine") or []) if isinstance(doc, dict) else []
    return [str(t) for t in listed if (work / str(t)).is_file()]


__all__ = ["AUTHOR_EMAIL", "AUTHOR_NAME", "SourceError", "affected_tests", "bundle", "changes", "commit",
           "diff_stat", "diff_text", "discard", "file_exists", "git", "has_mirror", "head",
           "mirror_of", "p0_tests", "publish", "resolve", "run_pytest", "seed", "snapshot",
           "test_env", "tree_of", "workspace"]
